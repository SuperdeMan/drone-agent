"""Red lines of the M3 autonomy layer as executable source checks (D014, D039, D041).

- ROS 2 nodes never import the drone_agent package; they talk to it only through drone.autonomy.v1 frames.
- No autonomy node can reach simulator truth: no Gazebo transport, no world/pose topics, no truth files.
- The egress node forwards guardian authorizations only: it never defers failsafes, registers a mode executor, arms,
  takes off, lands or selects any mode other than the fixed Hold of its watchdog.
- Shadow-stage outputs are never forwarded: the guardian refuses segments whose `may_execute` is false.
- The fleet protocol can never carry an authorized setpoint.

M3 自主层红线的可执行源码检查（D014、D039、D041）：ROS 2 节点从不导入 drone_agent 包，只经 drone.autonomy.v1 帧交互；
自主层节点无法触及仿真真值；出口节点只转发 guardian 授权，从不延期失效保护、注册模式执行器、解锁、起飞、降落或选择
看门狗固定 Hold 之外的模式；影子阶段的输出从不被转发；车队协议永远无法携带授权设定值。
"""

import re
import runpy
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "ros2_ws" / "src"
FORBIDDEN_TERMS = re.compile(r"qpos|qvel|gripper|ee_pose|joint_targets|cockpit|cabin|座舱", re.IGNORECASE)


def sources(*suffixes):
    return [path for path in sorted(WS.rglob("*")) if path.suffix in suffixes and path.is_file()]


def test_ros_nodes_never_import_the_drone_agent_package():
    offenders = [str(p) for p in sources(".py") if re.search(r"^\s*(from|import)\s+drone_agent\b", p.read_text(
        encoding="utf-8"), re.MULTILINE)]
    assert sources(".py") and not offenders


def test_autonomy_nodes_cannot_reach_simulator_truth():
    pattern = re.compile(r"gz\.transport|gz\.msgs|ignition|/world/|pose/info|m3_setup|truth\.jsonl|m3-truth|/truth\b")
    offenders = [f"{p}:{n}" for p in sources(".py", ".cpp", ".hpp", ".sh")
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert not offenders, offenders
    # The only Gazebo reader on the aircraft side is the fixed camera relay, which lives with the simulator.
    # 机载一侧唯一的 Gazebo 读取者是固定的相机转接，它属于仿真器。
    relay = (ROOT / "sim/sensor_relay.py").read_text(encoding="utf-8")
    assert set(re.findall(r'"(/drone/[a-z0-9_/]+)"', relay)) == {"/drone/cam_0/image", "/drone/depth_0/depth_image"}


def test_the_egress_node_only_forwards_and_can_only_ask_for_hold():
    source = (WS / "da_egress_ext/src/egress_node.cpp").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
    for banned in ("deferFailsafes", "defer_failsafes", "ModeExecutor", "ARM_DISARM", "NAV_TAKEOFF", "NAV_LAND",
                   "SET_NAV_STATE", "replaceInternalMode", "setSkipMessageCompatibilityCheck", "TrajectorySetpoint",
                   "AttitudeSetpoint", "RatesSetpoint", "ActuatorMotors", "Offboard"):
        assert banned not in code, banned
    assert code.count("command_pub_->publish(") == 1
    assert "kCommandSetMode = 176" in code and "command.param2 = 4.f" in code and "command.param3 = 3.f" in code
    assert "preventArming(true)" in code
    assert not FORBIDDEN_TERMS.search(source)


def test_ros_sources_keep_the_terminology_discipline():
    offenders = [f"{p}:{n}" for p in sources(".py", ".cpp", ".hpp", ".sh", ".txt", ".xml")
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if FORBIDDEN_TERMS.search(line)]
    assert not offenders, offenders


def test_the_guardian_refuses_shadow_segments():
    source = (ROOT / "src/drone_agent/guardian/external.py").read_text(encoding="utf-8")
    assert "or not found.may_execute" in source


def test_the_fleet_protocol_cannot_carry_an_authorized_setpoint(tmp_path):
    descriptor = runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"](tmp_path)
    pool = descriptor_pool.DescriptorPool()
    for file in descriptor_pb2.FileDescriptorSet.FromString(descriptor.read_bytes()).file:
        pool.Add(file)
    service = pool.FindServiceByName("drone.fleet.v1.FleetTransport")
    seen, pending = set(), [method.input_type for method in service.methods]
    pending += [method.output_type for method in service.methods]
    while pending:
        message = pending.pop()
        if message.full_name in seen:
            continue
        seen.add(message.full_name)
        pending.extend(field.message_type for field in message.fields if field.message_type)
    assert not {name for name in seen if name.startswith("drone.autonomy.")}
