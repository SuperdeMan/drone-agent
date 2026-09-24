// da_egress_ext: the physical extension of the guardian's control egress into ROS 2 (D039).
//
// It registers exactly one PX4 external mode ("DroneAgent Local"). It has no mission semantics: while the mode is
// active it forwards the newest guardian authorization as a multicopter goto setpoint, and only while that
// authorization is within its time-to-live. When the authorization lapses during an active mode it stops
// publishing in the same cycle and sends the only command it may send, "switch to PX4 Hold"; if PX4 is still in the
// mode a second later it reports the mode as unable to run so PX4's own failsafe takes over. When the guardian
// revokes the authorization itself (D047) it is commanding PX4 over MAVLink, and this node's view of the mode may lag
// that command: the node then stops publishing and switches nothing, and only reports the mode unable to run if it
// still sees it active a second later. A newer authorization ends the revocation. It never re-sends a stale setpoint,
// never arms, takes off, lands or selects another mode, never registers a mode executor and never requests failsafe
// deferral. Authorizations and revocations arrive over a private Unix socket from the guardian as length-prefixed
// drone.autonomy.v1 frames; lower epochs, non-increasing sequences, expired or malformed frames are refused.
//
// da_egress_ext：guardian 控制出口在 ROS 2 一侧的物理延伸（D039）。
//
// 它只注册一个 PX4 外部模式（"DroneAgent Local"），没有任务语义：模式激活期间，把 guardian 最新的授权作为多旋翼 goto
// 设定值转发，且仅在该授权的存活时间内。模式激活期间授权失效时，当周期停止发布，并发出它唯一允许的命令「切到 PX4
// Hold」；1 秒后 PX4 仍在该模式，就报告该模式不可运行，交给 PX4 自身的失效保护。guardian 自己撤销授权时（D047），
// 它正经 MAVLink 指挥 PX4，而本节点看到的模式可能滞后于该命令：节点只停止发布、不切换任何模式，1 秒后仍见激活才报告
// 该模式不可运行。更新的授权结束撤销状态。它从不重发过期设定值，从不解锁、起飞、降落或选择其他模式，从不注册模式
// 执行器，也从不请求失效保护延期。授权与撤销经私有 Unix 套接字以带长度前缀的 drone.autonomy.v1 帧从 guardian 到达；
// 低代次、序号不递增、过期或格式错误的帧一律拒收。

#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <fstream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include <Eigen/Core>
#include <px4_msgs/msg/vehicle_command.hpp>
#include <px4_msgs/msg/vehicle_status.hpp>
#include <px4_ros2/components/mode.hpp>
#include <px4_ros2/control/setpoint_types/multicopter/goto.hpp>
#include <px4_ros2/third_party/nlohmann/json.hpp>
#include <px4_ros2/utils/message_version.hpp>
#include <rclcpp/rclcpp.hpp>

#include "drone/autonomy/v1/autonomy.pb.h"

namespace da
{
using SteadyClock = std::chrono::steady_clock;
using WallClock = std::chrono::system_clock;
using Json = nlohmann::json;
namespace pb = drone::autonomy::v1;

constexpr const char * kModeName = "DroneAgent Local";
constexpr const char * kSchemaVersion = "0.1.0";
constexpr uint32_t kMaxFrameBytes = 64 * 1024;
constexpr float kMaxHorizontalSpeed = 5.f;
constexpr float kMaxVerticalSpeed = 3.f;
constexpr uint16_t kCommandSetMode = 176;  // MAV_CMD_DO_SET_MODE
constexpr uint8_t kCompanionComponent = 191;  // MAV_COMP_ID_ONBOARD_COMPUTER, for attribution in the ULog

double wall_seconds() {return std::chrono::duration<double>(WallClock::now().time_since_epoch()).count();}

// Non-durable JSON-lines evidence for the judge and the viewer. / 供裁判与证据浏览器使用的非持久 JSON 行记录。
class EvidenceLog
{
public:
  explicit EvidenceLog(const std::string & path)
  : stream_(path, std::ios::app) {}

  void write(Json row)
  {
    row["wall_time"] = wall_seconds();
    std::lock_guard<std::mutex> lock(mutex_);
    if (stream_) {
      stream_ << row.dump() << '\n';
      if (++pending_ >= 20) {
        stream_.flush();
        pending_ = 0;
      }
    }
  }

  void flush()
  {
    std::lock_guard<std::mutex> lock(mutex_);
    stream_.flush();
  }

private:
  std::mutex mutex_;
  std::ofstream stream_;
  int pending_{0};
};

struct Authorization
{
  Eigen::Vector3f position_ned;
  float max_horizontal_speed;
  float max_vertical_speed;
  int64_t epoch;
  int64_t seq;
  SteadyClock::time_point expires;
};

// State shared by the ROS thread and the socket thread. / ROS 线程与套接字线程共享的状态。
struct Shared
{
  std::mutex mutex;
  std::optional<Authorization> authorization;
  int64_t highest_epoch{-1};
  int64_t last_seq{-1};
  int64_t last_forwarded_seq{-1};
  int64_t forwarded{0};
  int64_t rejected_stale{0};
  int64_t rejected_epoch{0};
  int64_t rejected_seq{0};
  int64_t rejected_other{0};
  int64_t watchdog_exits{0};
  int64_t hold_commands{0};
  int64_t cant_run_reports{0};
  int64_t revocations{0};
  bool revoked{false};
  SteadyClock::time_point revoked_at{};
  std::string last_watchdog_reason;
  bool registered{false};
  bool compatibility_ok{false};
  bool mode_active{false};
  int nav_state{0};
  bool armed{false};
  uint64_t px4_timestamp_us{0};
  SteadyClock::time_point last_px4_status{};
  bool cant_run{false};
};

class EgressMode : public px4_ros2::ModeBase
{
public:
  EgressMode(rclcpp::Node & node, Shared & shared, EvidenceLog & log)
  : ModeBase(node, Settings{kModeName}.preventArming(true)), node_(node), shared_(shared), log_(log)
  {
    goto_ = std::make_shared<px4_ros2::MulticopterGotoSetpointType>(*this);
    setSetpointUpdateRate(20.f);
    command_pub_ = node.create_publisher<px4_msgs::msg::VehicleCommand>(
      "fmu/in/vehicle_command" + px4_ros2::getMessageNameVersion<px4_msgs::msg::VehicleCommand>(), 1);
  }

  void onActivate() override
  {
    lapse_handled_ = false;
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      shared_.mode_active = true;
    }
    log_.write({{"event", "activated"}});
  }

  void onDeactivate() override
  {
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      shared_.mode_active = false;
    }
    log_.write({{"event", "deactivated"}});
  }

  void checkArmingAndRunConditions(px4_ros2::HealthAndArmingCheckReporter & reporter) override
  {
    bool cant_run = false;
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      cant_run = shared_.cant_run;
    }
    if (cant_run) {
      reporter.armingCheckFailureExt(
        px4_ros2::events::ID("drone_agent_authorization_lapsed"), px4_ros2::events::Log::Error,
        "DroneAgent Local: guardian authorization lapsed");
    }
  }

  void updateSetpoint(float /*dt_s*/) override
  {
    const auto now = SteadyClock::now();
    std::optional<Authorization> authorization;
    uint64_t px4_time = 0;
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      if (shared_.authorization && now < shared_.authorization->expires) {
        authorization = shared_.authorization;
      }
      px4_time = shared_.px4_timestamp_us;
    }
    if (!authorization) {
      lapse(now);
      return;
    }
    goto_->update(
      authorization->position_ned, std::nullopt, authorization->max_horizontal_speed,
      authorization->max_vertical_speed);
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      ++shared_.forwarded;
      shared_.last_forwarded_seq = authorization->seq;
    }
    log_.write({{"event", "published"}, {"epoch", authorization->epoch}, {"seq", authorization->seq},
        {"px4_timestamp_us", px4_time},
        {"position_ned", {authorization->position_ned.x(), authorization->position_ned.y(),
            authorization->position_ned.z()}}});
  }

private:
  // Report the mode unable to run once; PX4 then decides from its own current mode. / 报告该模式不可运行（仅一次）；
  // 随后由 PX4 按自身当前模式决定。
  void report_cant_run(const char * reason)
  {
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      if (shared_.cant_run) {
        return;
      }
      shared_.cant_run = true;
      ++shared_.cant_run_reports;
    }
    log_.write({{"event", "cant_run_reported"}, {"reason", reason}});
  }

  // No authorization while active. Revoked: the guardian is commanding PX4 itself, so switch nothing and escalate
  // only a second later (D047). Lapsed: stop in this cycle, ask PX4 for Hold once, escalate after a second.
  // 激活期间没有授权。已撤销：guardian 正亲自指挥 PX4，因此不切换任何模式，一秒后才升级（D047）。已过期：本周期
  // 停止，向 PX4 请求一次 Hold，一秒后升级。
  void lapse(SteadyClock::time_point now)
  {
    bool revoked = false;
    SteadyClock::time_point revoked_at{};
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      revoked = shared_.revoked;
      revoked_at = shared_.revoked_at;
    }
    if (revoked) {
      if (now - revoked_at > std::chrono::seconds(1)) {
        report_cant_run("revoked_mode_still_active");
      }
      return;
    }
    if (lapse_handled_) {
      if (now - lapse_time_ > std::chrono::seconds(1)) {
        report_cant_run("authorization_lapsed");
      }
      return;
    }
    lapse_handled_ = true;
    lapse_time_ = now;
    px4_msgs::msg::VehicleCommand command{};
    command.timestamp = 0;  // PX4 stamps it. / 由 PX4 打时间戳。
    command.command = kCommandSetMode;
    command.param1 = 1.f;  // MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    command.param2 = 4.f;  // PX4_CUSTOM_MAIN_MODE_AUTO
    command.param3 = 3.f;  // PX4_CUSTOM_SUB_MODE_AUTO_LOITER (Hold)
    command.target_system = 1;
    command.target_component = 1;
    command.source_system = 1;
    command.source_component = kCompanionComponent;
    command.from_external = true;
    command_pub_->publish(command);
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      ++shared_.watchdog_exits;
      ++shared_.hold_commands;
      shared_.last_watchdog_reason = "authorization_lapsed";
    }
    log_.write({{"event", "watchdog_exit"}, {"reason", "authorization_lapsed"}});
  }

  rclcpp::Node & node_;
  Shared & shared_;
  EvidenceLog & log_;
  std::shared_ptr<px4_ros2::MulticopterGotoSetpointType> goto_;
  rclcpp::Publisher<px4_msgs::msg::VehicleCommand>::SharedPtr command_pub_;
  bool lapse_handled_{false};
  SteadyClock::time_point lapse_time_{};
};

// Client side of the guardian's egress socket. / guardian 出口套接字的客户端。
class GuardianLink
{
public:
  GuardianLink(std::string path, std::string robot_id, int max_ttl_ms, Shared & shared, EvidenceLog & log)
  : path_(std::move(path)), robot_id_(std::move(robot_id)), max_ttl_ms_(max_ttl_ms), shared_(shared), log_(log) {}

  ~GuardianLink()
  {
    stop_ = true;
    close_socket();
    if (thread_.joinable()) {
      thread_.join();
    }
  }

  void start() {thread_ = std::thread([this] {run();});}

  bool send(const pb::AutonomyFrame & frame)
  {
    std::string body;
    if (!frame.SerializeToString(&body) || body.empty() || body.size() > kMaxFrameBytes) {
      return false;
    }
    const uint32_t size = htonl(static_cast<uint32_t>(body.size()));
    std::lock_guard<std::mutex> lock(write_mutex_);
    const int fd = fd_.load();
    if (fd < 0) {
      return false;
    }
    return write_all(fd, reinterpret_cast<const char *>(&size), 4) && write_all(fd, body.data(), body.size());
  }

private:
  static bool write_all(int fd, const char * data, size_t size)
  {
    while (size > 0) {
      const ssize_t written = ::send(fd, data, size, MSG_NOSIGNAL);
      if (written <= 0) {
        return false;
      }
      data += written;
      size -= static_cast<size_t>(written);
    }
    return true;
  }

  static bool read_all(int fd, char * data, size_t size)
  {
    while (size > 0) {
      const ssize_t received = ::recv(fd, data, size, 0);
      if (received <= 0) {
        return false;
      }
      data += received;
      size -= static_cast<size_t>(received);
    }
    return true;
  }

  void close_socket()
  {
    std::lock_guard<std::mutex> lock(write_mutex_);
    const int fd = fd_.exchange(-1);
    if (fd >= 0) {
      ::shutdown(fd, SHUT_RDWR);
      ::close(fd);
    }
  }

  void run()
  {
    while (!stop_) {
      const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
      sockaddr_un address{};
      address.sun_family = AF_UNIX;
      std::snprintf(address.sun_path, sizeof(address.sun_path), "%s", path_.c_str());
      if (fd < 0 || ::connect(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) {
        if (fd >= 0) {
          ::close(fd);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        continue;
      }
      fd_ = fd;
      log_.write({{"event", "guardian_connected"}});
      while (!stop_) {
        uint32_t size_be = 0;
        if (!read_all(fd, reinterpret_cast<char *>(&size_be), 4)) {
          break;
        }
        const uint32_t size = ntohl(size_be);
        if (size == 0 || size > kMaxFrameBytes) {
          log_.write({{"event", "protocol_error"}, {"reason", "frame_size"}});
          break;
        }
        std::string body(size, '\0');
        if (!read_all(fd, body.data(), size)) {
          break;
        }
        pb::AutonomyFrame frame;
        if (!frame.ParseFromString(body)) {
          log_.write({{"event", "protocol_error"}, {"reason", "unparseable_frame"}});
          break;
        }
        if (frame.has_authorized_setpoint()) {
          accept(frame.authorized_setpoint());
        } else if (frame.has_authorization_revoked()) {
          revoke(frame.authorization_revoked());
        } else {
          // The guardian only ever sends authorizations and revocations here. / guardian 在这里只会发送授权与撤销。
          log_.write({{"event", "protocol_error"}, {"reason", "unexpected_frame"}});
          break;
        }
      }
      close_socket();
      log_.write({{"event", "guardian_disconnected"}});
    }
  }

  void reject(int64_t Shared::* counter, const std::string & reason, int64_t epoch, int64_t seq)
  {
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      ++(shared_.*counter);
    }
    log_.write({{"event", "rejected"}, {"reason", reason}, {"epoch", epoch}, {"seq", seq}});
  }

  void accept(const pb::AuthorizedSetpoint & message)
  {
    const auto receipt = SteadyClock::now();
    const int64_t epoch = message.has_lease_epoch() ? message.lease_epoch() : -1;
    const int64_t seq = message.has_command_seq() ? message.command_seq() : -1;
    const bool complete = message.has_schema_version() && message.schema_version() == kSchemaVersion &&
      message.has_robot_id() && message.robot_id() == robot_id_ && message.has_lease_epoch() &&
      message.has_command_seq() && message.has_ttl_ms() && message.has_issued_at() &&
      message.has_position_ned() && message.position_ned().has_x() && message.position_ned().has_y() &&
      message.position_ned().has_z() && message.has_max_horizontal_speed_mps() &&
      message.has_max_vertical_speed_mps();
    if (!complete) {
      reject(&Shared::rejected_other, "incomplete_or_foreign", epoch, seq);
      return;
    }
    const auto & p = message.position_ned();
    const float max_h = static_cast<float>(message.max_horizontal_speed_mps());
    const float max_v = static_cast<float>(message.max_vertical_speed_mps());
    if (!std::isfinite(p.x()) || !std::isfinite(p.y()) || !std::isfinite(p.z()) || !(max_h > 0.f) ||
      max_h > kMaxHorizontalSpeed || !(max_v > 0.f) || max_v > kMaxVerticalSpeed)
    {
      reject(&Shared::rejected_other, "value_out_of_bounds", epoch, seq);
      return;
    }
    const int64_t ttl_ms = message.ttl_ms();
    const double issued = message.issued_at().seconds() + message.issued_at().nanos() * 1e-9;
    const double age_ms = (wall_seconds() - issued) * 1000.0;
    if (ttl_ms < 1 || ttl_ms > max_ttl_ms_ || age_ms > ttl_ms || age_ms < -50.0) {
      reject(&Shared::rejected_stale, "expired_or_future", epoch, seq);
      return;
    }
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      if (epoch < shared_.highest_epoch) {
        ++shared_.rejected_epoch;
      } else if (epoch == shared_.highest_epoch && seq <= shared_.last_seq) {
        ++shared_.rejected_seq;
      } else {
        if (epoch > shared_.highest_epoch) {
          shared_.highest_epoch = epoch;
        }
        shared_.last_seq = seq;
        const auto remaining = std::chrono::microseconds(
          static_cast<int64_t>((static_cast<double>(ttl_ms) - std::max(age_ms, 0.0)) * 1000.0));
        shared_.authorization = Authorization{
          Eigen::Vector3f(static_cast<float>(p.x()), static_cast<float>(p.y()), static_cast<float>(p.z())),
          max_h, max_v, epoch, seq, receipt + remaining};
        // A newer authorization ends a revocation. / 更新的授权结束撤销状态。
        shared_.revoked = false;
        if (!shared_.mode_active) {
          // A fresh authorization while inactive clears an earlier escalation. / 未激活时收到新鲜授权即清除先前的升级。
          shared_.cant_run = false;
        }
        return;
      }
    }
    log_.write({{"event", "rejected"}, {"reason", "stale_epoch_or_sequence"}, {"epoch", epoch}, {"seq", seq}});
  }

  // The guardian ends the authorization itself (D047): drop it at once; the mode is the guardian's to change.
  // guardian 自己结束授权（D047）：立即丢弃授权；模式由 guardian 切换。
  void revoke(const pb::AuthorizationRevoked & message)
  {
    const int64_t epoch = message.has_lease_epoch() ? message.lease_epoch() : -1;
    const int64_t seq = message.has_command_seq() ? message.command_seq() : -1;
    const bool complete = message.has_schema_version() && message.schema_version() == kSchemaVersion &&
      message.has_robot_id() && message.robot_id() == robot_id_ && message.has_lease_epoch() &&
      message.has_command_seq() && message.has_issued_at() && message.has_reason() && !message.reason().empty();
    if (!complete) {
      reject(&Shared::rejected_other, "incomplete_or_foreign_revocation", epoch, seq);
      return;
    }
    const double issued = message.issued_at().seconds() + message.issued_at().nanos() * 1e-9;
    const double age_ms = (wall_seconds() - issued) * 1000.0;
    if (age_ms > max_ttl_ms_ || age_ms < -50.0) {
      reject(&Shared::rejected_stale, "expired_or_future_revocation", epoch, seq);
      return;
    }
    {
      std::lock_guard<std::mutex> lock(shared_.mutex);
      if (epoch < shared_.highest_epoch) {
        ++shared_.rejected_epoch;
      } else if (epoch == shared_.highest_epoch && seq <= shared_.last_seq) {
        ++shared_.rejected_seq;
      } else {
        shared_.highest_epoch = std::max(shared_.highest_epoch, epoch);
        shared_.last_seq = seq;
        shared_.authorization.reset();
        shared_.revoked = true;
        shared_.revoked_at = SteadyClock::now();
        ++shared_.revocations;
        log_.write({{"event", "revoked"}, {"reason", message.reason()}, {"epoch", epoch}, {"seq", seq}});
        return;
      }
    }
    log_.write({{"event", "rejected"}, {"reason", "stale_revocation"}, {"epoch", epoch}, {"seq", seq}});
  }

  std::string path_;
  std::string robot_id_;
  int max_ttl_ms_;
  Shared & shared_;
  EvidenceLog & log_;
  std::atomic<int> fd_{-1};
  std::atomic<bool> stop_{false};
  std::mutex write_mutex_;
  std::thread thread_;
};

}  // namespace da

int main(int argc, char * argv[])
{
  GOOGLE_PROTOBUF_VERIFY_VERSION;
  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("da_egress_ext");
  const auto socket_path = node->declare_parameter<std::string>("socket", "/run/egress/egress.sock");
  const auto robot_id = node->declare_parameter<std::string>("robot_id", "uav_01");
  const auto max_ttl_ms = static_cast<int>(node->declare_parameter<int64_t>("max_ttl_ms", 500));
  const auto log_path = node->declare_parameter<std::string>("evidence_log", "/artifacts/egress.jsonl");
  const auto node_version = node->declare_parameter<std::string>("node_version", "0.1.0");
  if (max_ttl_ms < 1 || max_ttl_ms > 500) {
    throw std::runtime_error("max_ttl_ms must be within 1..500");
  }
  da::EvidenceLog log(log_path);
  da::Shared shared;
  std::unique_ptr<da::EgressMode> mode;
  while (rclcpp::ok()) {
    mode = std::make_unique<da::EgressMode>(*node, shared, log);
    if (mode->doRegister()) {
      break;
    }
    mode.reset();
    log.write({{"event", "registration_failed"}});
    std::this_thread::sleep_for(std::chrono::seconds(2));
  }
  if (!mode) {
    return 1;
  }
  {
    std::lock_guard<std::mutex> lock(shared.mutex);
    shared.registered = true;
    // doRegister() includes px4_ros2's message compatibility check. / doRegister() 已包含 px4_ros2 的消息兼容性检查。
    shared.compatibility_ok = true;
    shared.nav_state = mode->id();
  }
  log.write({{"event", "registered"}, {"nav_state", mode->id()}, {"mode", da::kModeName}});

  auto status_sub = node->create_subscription<px4_msgs::msg::VehicleStatus>(
    "fmu/out/vehicle_status" + px4_ros2::getMessageNameVersion<px4_msgs::msg::VehicleStatus>(),
    rclcpp::QoS(1).best_effort(),
    [&shared](px4_msgs::msg::VehicleStatus::UniquePtr message) {
      std::lock_guard<std::mutex> lock(shared.mutex);
      shared.last_px4_status = da::SteadyClock::now();
      shared.px4_timestamp_us = message->timestamp;
      shared.armed = message->arming_state == px4_msgs::msg::VehicleStatus::ARMING_STATE_ARMED;
    });

  da::GuardianLink link(socket_path, robot_id, max_ttl_ms, shared, log);
  link.start();
  auto status_timer = node->create_wall_timer(
    std::chrono::milliseconds(200), [&]() {
      da::pb::AutonomyFrame frame;
      auto * status = frame.mutable_egress_status();
      const double now = da::wall_seconds();
      std::unique_lock<std::mutex> lock(shared.mutex);
      status->set_schema_version(da::kSchemaVersion);
      status->set_robot_id(robot_id);
      status->mutable_stamp()->set_seconds(static_cast<int64_t>(now));
      status->mutable_stamp()->set_nanos(static_cast<int32_t>((now - static_cast<int64_t>(now)) * 1e9));
      status->set_node_version(node_version);
      status->set_registered(shared.registered);
      status->set_mode_nav_state(shared.nav_state);
      status->set_mode_active(shared.mode_active);
      // PX4 publishes vehicle_status on change and otherwise every 500 ms, so the link counts as lost only after
      // two missed periods. / PX4 在变化时、否则每 500 ms 发布 vehicle_status，因此连续错过两个周期才算链路丢失。
      status->set_fmu_link_ok(
        da::SteadyClock::now() - shared.last_px4_status < std::chrono::milliseconds(1000));
      status->set_compatibility_ok(shared.compatibility_ok);
      status->set_highest_epoch(shared.highest_epoch);
      status->set_last_forwarded_seq(shared.last_forwarded_seq);
      status->set_forwarded_setpoints(shared.forwarded);
      // Invalid and expired authorizations share one counter. / 无效与过期的授权共用一个计数。
      status->set_rejected_stale(shared.rejected_stale + shared.rejected_other);
      status->set_rejected_epoch(shared.rejected_epoch);
      status->set_rejected_seq(shared.rejected_seq);
      status->set_watchdog_exits(shared.watchdog_exits);
      status->set_last_watchdog_reason(shared.last_watchdog_reason);
      status->set_armed(shared.armed);
      status->set_hold_commands(shared.hold_commands);
      status->set_cant_run_reports(shared.cant_run_reports);
      status->set_revocations(shared.revocations);
      lock.unlock();  // Never write to the socket while holding the shared state. / 持有共享状态时从不写套接字。
      link.send(frame);
    });
  rclcpp::spin(node);
  log.flush();
  rclcpp::shutdown();
  return 0;
}
