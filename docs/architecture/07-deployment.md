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
| MAVSDK-Python | 3.17.x（PyPI `mavsdk`，gRPC 封装 + `mavsdk_server`）；锁 `>=3.17,<5` | v4 原生绑定发布后迁移适配器（上游已拆出 `mavsdk-grpc`） |
| RMW | Fast DDS（机器人内）；Zenoh（跨机器人） | — |
| Nav2 | Jazzy 对应版本 | 随 ROS 2 |
| Anthropic SDK | 最新稳定；模型 `claude-opus-5` | 按模型迁移指南 |

版本组合锁定在 `configs/platform/*.yaml` 与容器基础镜像标签中；任何变更走 ADR。

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

- 上行任务包签名（审批记录 + 包哈希）；机载验签后才接受。
- 机器人 ↔ mission-service 双向 TLS；Zenoh 路由启用认证。
- 密钥不进代码与镜像；通过运行时挂载。
- 认知链路攻击（提示注入进入 Planner 工具返回）：工具返回视为数据，不作为指令；任何来自工具的「扩大范围」建议都必须经准入。
