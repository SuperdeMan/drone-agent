# 部署形态：仿真、容器、硬件与网络

## 1. 仿真两条线

| 线 | 目的 | 工具 | 阶段 |
|---|---|---|---|
| A. 飞控与系统验证线 | 真实飞控逻辑、模式变化、遥测、任务状态、通信异常、故障注入、多设备集成 | PX4 v1.17 SITL + Gazebo（Harmonic；随 ROS 2 版本升级到 Jetty） | M1 起，始终是 CI 主线 |
| B. 感知与学习数据线 | 高保真视觉、合成数据、学习型策略评测 | Pegasus Simulator 5.1（Isaac Sim 5.1）+ PX4；Cosmos 3 用于数据增广 | M3 起按需；不是 M1–M2 前置 |

MuJoCo 继续承担 `embodied-agent` 的机械臂基线，不用于飞控集成验证。

空地联合仿真（M4-B）：同一 Gazebo 世界内 PX4 SITL 多旋翼 + PX4 rover SITL（v1.17 rover 模式 / Ackermann SIH），避免早期跨仿真器时空同步。

仿真时钟：所有节点订阅仿真 `/clock`（或等价机制），支持 faster-than-real-time（参考 aerial-autonomy-stack 的 4 ms 物理步长与 ≤ 15× 实时因子上限，避免 SITL 抖动）。

## 2. 容器与镜像

借鉴 aerial-autonomy-stack 的三镜像形态：

| 镜像 | 架构 | 内容 |
|---|---|---|
| `sim-image` | amd64 | Gazebo、PX4/ArduPilot SITL、世界与机体模型、rover 模型 |
| `ground-image` | amd64 | mission-service、console、QGroundControl（调试）、MAVLink 路由、评测裁判、数据平台 |
| `aircraft-image` | **amd64 + arm64（Jetson JetPack 6 / L4T 36）** | executive、guardian、autonomy（ROS 2）、edge-inference（ONNX Runtime：amd64 CUDA / arm64 TensorRT FP16） |

同一份 `aircraft-image` Dockerfile 同时构建工作站仿真与机载部署，形成垂直集成 CI；`docker compose` 提供 `sim`、`sil`（software-in-the-loop）、`jetson-in-the-loop` 三种拓扑。

以上为完整路线：M1 已实现 amd64 的 sim/aircraft/ground 分工与云端 Compose；arm64、Jetson-in-the-loop 和 ROS 2 自主层仍按 M3 引入，不将当前 amd64 仿真镜像宣称为机载镜像。

网络：`SIM_SUBNET`（仿真内部高速 UDP）与 `AIR_SUBNET`（机器人间链路，可替换为真实无线 / mesh 做 comms-in-the-loop）。

## 3. 机载硬件路线

| 阶段 | 计算 | 飞控 | 说明 |
|---|---|---|---|
| M1–M2 | 工作站容器 | PX4 SITL | 无硬件 |
| M3 | Jetson Orin Nano Super / Orin NX（Jetson-in-the-loop） | PX4 SITL | 验证算力、延迟预算、TensorRT 推理 |
| M4-A | Jetson Orin NX 或 ModalAI VOXL 2（PX4 一体） | PX4 v1.17 真机（Pixhawk 6 级） | 受限真机；RC 接管；共因故障测试 |
| M5+ | 同上；地面机器人 Jetson Orin / Thor | PX4 + Nav2 平台 | 空地协同真机 |

机载推理预算（M3 定义并测量）：控制路径不含任何模型推理；事件检测 VLM 延迟 ≤ 1 s、非阻塞；安全监督周期 ≥ 10 Hz，尾延迟（p99）有上限并进入评测。

## 4. 软件版本基线（ADR-0004）

| 组件 | 基线 | 升级评估点 |
|---|---|---|
| OS | Ubuntu 24.04 | — |
| Python | 3.12 | — |
| ROS 2 | Jazzy Jalisco（LTS → 2029-05） | M3 评估 Lyrical Luth（LTS → 2031-05）：条件是 px4_msgs、Nav2、BehaviorTree.ROS2、rmw_zenoh 均有稳定发布 |
| Gazebo | Harmonic（Jazzy 配对） | 随 ROS 2 升级到 Jetty |
| PX4 | v1.17.x（锁定小版本） | 不跟随 main |
| MAVSDK-Python | 精确锁定 `3.17.4`（gRPC 封装 + `mavsdk_server`，D025） | 原生绑定或 `mavsdk-grpc` 迁移单独验证 |
| RMW | Fast DDS（机器人内）；Zenoh（跨机器人） | — |
| Nav2 | Jazzy 对应版本 | 随 ROS 2 |
| LLM Provider | MiniMax-M3，OpenAI 兼容 HTTP（`httpx`），配置沿用 car-agent（D029） | 模型或工具调用率变化时按 D029 重估 |

版本组合锁定在 `configs/platforms/*.yaml` 与容器基础镜像标签中；任何变更走 ADR。

### M0 开发冒烟交付

M0 提前创建 `proto/`、`scripts/`、`sim/`；M1 运行时子模块仍按实际实现创建。`configs/skills/` 保存五个基础技能草案，`configs/scenarios/` 保存故障注入矩阵。

`sim/compose.yaml` 先提供独立的 PX4 SITL + Gazebo 服务；不预建没有实现的 ground/aircraft 服务。2026-09-19 实查官方预编译仓库缺少 v1.17.0 标签，因此用固定 digest 的官方 Jazzy 开发镜像构建 v1.17.0 源码（D022）。启动使用 headless `gz_x500`；冒烟只验证容器平台、PX4 版本、Gazebo 世界/时钟与遥测，不解锁、不起飞、不改变飞控参数。后续 M1 扩展成上述三镜像拓扑。

Windows 开发通过已安装的 Docker Desktop Linux 后端运行；构建、运行与挂载入口使用 ASCII 路径，挂载目录与源仓库分离，仅复制冒烟所需文件，不复制 `.env`、`.git` 或其他项目文件。复现方法与实际结果分别见 `sim/README.md` 与 `docs/m0-readiness.md`。主机安装 WSL 发行版、全局依赖或调整系统设置仍按用户红线先批准。

### 云端开发与联调（D023）

按用户 2026-09-19 的明确要求，后续需要 Linux 真栈、仿真或服务联调时默认使用现有云服务器；本机保留编辑与快速确定性检查。统一入口为 `scripts/dev_stack.py`，只面向 cloud，不在连接失败时回落到本地 Compose。

| 内容 | 云端职责 | 阶段 |
|---|---|---|
| 契约验证、PX4/Gazebo 仿真、测试产物 | 云端工作区与独立容器 | 当前可部署 |
| Planner、Compiler/Admission、Catalog、业务账本、控制台、证据归档 | 任务服务与数据服务 | M2 起按实现部署 |
| executive、guardian、autonomy、飞控适配器 | 仿真时与模拟飞控在同一云主机；真机时留在设备侧 | M1 起实现，真机不依赖公网连续控制 |

服务器上使用 SSH 用户家目录下的 `drone-agent/`，Compose project 固定 `drone-agent-cloud`，独立网络和产物目录。SITL 上限为 1.5 CPU / 2 GiB，验证容器上限为 1 CPU / 1 GiB；不发布宿主端口。部署不改变现有 car-agent 容器、数据、配置与服务入口。

D028 对网页入口增加明确例外：`sim/compose.console.yaml` 的网页容器只发布 `127.0.0.1:8768`，由现有 Tailscale Serve 的独立 HTTPS `8447` 端口代理，不向公网监听、不启用 Funnel。网页使用受限 ASGI 容器；同机专用 systemd 任务代理经私有 UDS 接收固定请求。原 SITL、executive、guardian 与验证容器的网络和资源限制不变。入口安装及回退只能调整本项目服务与新 Serve 映射，保持 car-agent 的 443/8443–8446 映射原样。

M2（D030、D031、D033）在同一工作区增加：ground 镜像中的 mission-service（Planner、Compiler / Admission、审批与签名、业务账本、Evidence Verifier、报告），只在内部上行网络上以 mTLS 接受机器人连接，不发布宿主端口；aircraft 侧新增 uplink 进程，是机载唯一接入该网络的进程，主动拨出、无监听端口，与 guardian（仿真网络）和 executive（无网络）经机载私有卷交接。签发私钥、模型 key、CA 与各证书私钥只存在本项目 `secrets/`（0600）并只读挂载到需要它的单个容器；机器人只挂载公钥 `trust.json` 与自己的客户端证书。控制台沿用 D028 入口，身份取 Serve 注入的 tailnet 登录名。

应用源码只从指定 Git commit 导出；控制脚本、Compose 和锁定依赖分别记录哈希，不能把控制面草案伪称为应用 release。首次复用 M0 已验证的镜像，经 SSH 上传并校验归档哈希、文件系统层与运行配置；后续验证镜像在服务器构建。秘密不进入快照，SSH 连接参数从进程环境读取，不复制 car-agent `.env`。操作、目录与结果边界见 `../cloud-development.md`。

## 5. 数据记录

- 原始层：MCAP（ROS 2 / 自定义 schema）、ULog（PX4）、任务事件流（JSONL / SQLite）。三者用 `mission_id + 单调时间` 关联。
- 每次任务关联：任务与审批版本、模型 / 策略 / 软件 / 飞控配置版本、坐标变换与地图版本、动作与控制模式、安全干预、任务结果、原始影像与遥测证据。
- 派生层：LeRobot 等训练格式按需导出；不为统一模型输入把原始数据强制重采样。
- 回放：任何任务可离线重放事件流并重新运行裁判。

## 6. 可观测性

- OpenTelemetry：跨 mission-service / executive / guardian 的 trace（以 `mission_id`、`step_id`、`command_id` 为关联键）。
- 指标：安全监督周期与尾延迟、意图拒绝率、恢复触发次数、租约续期失败、观测新鲜度分布。
- 日志脱敏：影像与位置属于敏感数据；保留期与访问范围由 `configs/policy/` 定义。

## 7. 安全（信息安全）

- M1 仿真从本地可信文件加载批准任务包，gRPC local credentials + 私有 UDS 验证本地连接，包哈希绑定范围；不把哈希宣称为数字签名。
- M2 的远程任务入口增加签名、机载验签与机器人 ↔ mission-service 双向 TLS（D030）；Zenoh 接入时启用认证。
- 密钥不进代码与镜像；通过运行时挂载。
- 认知链路攻击（提示注入进入 Planner 工具返回）：工具返回视为数据，不作为指令；任何来自工具的「扩大范围」建议都必须经准入。
