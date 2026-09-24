# 部署形态：仿真、容器、硬件与网络

[返回架构总览](00-overview.md) · [云端开发](../cloud-development.md) · [任务台使用](../tailnet-desk.md)

**当前是云端 amd64 仿真 / 验证部署。** M2 任务服务、签名上行与任务台已实现；arm64、ROS 2 自主层、Jetson-in-the-loop 和真机均为后续阶段。

| 当前入口 | 用途 | 配置 / 指南 |
|---|---|---|
| M0 空闲 SITL 与检查镜像 | 环境冒烟、源码与契约检查 | [compose.cloud.yaml](../../sim/compose.cloud.yaml) |
| M1 场景批次 | 固定任务、故障注入、裁判与回放 | [compose.m1.yaml](../../sim/compose.m1.yaml) |
| M2 场景批次 | 规划到报告的端到端验证，脚本 / 实调规划分开记录 | [compose.m2.yaml](../../sim/compose.m2.yaml) |
| M1 常驻控制台 | 固定仿真任务；Tailnet HTTPS `8447` → 回环 `8768` | [控制台指南](../tailnet-console.md) |
| 统一云端飞行台（D037） | `8448` → 回环 `8769`；`/` 为 M2 自然语言任务，`/fixed/` 为 M1 固定巡检；网页复用两条受限通道 | [compose.desk.yaml](../../sim/compose.desk.yaml) · [任务台指南](../tailnet-desk.md) |
| 本机 M2 任务台 | 规划、准入与签名，没有机器人连接 | [快速体验](../../README.zh-CN.md#本机体验) |

下文保留容器、硬件和数据平台的完整路线；涉及后续阶段的组件不是当前部署清单。准确运行版本通过 `scripts/dev_stack.py status` 与对应入口状态查询；已归档结果见[M2 验收](../m2-readiness.md)和[任务台验收](../tailnet-desk-readiness.md)。

## 1. 仿真两条线

| 线 | 目的 | 工具 | 阶段 |
|---|---|---|---|
| A. 飞控与系统验证线 | 飞控逻辑、模式变化、遥测、任务状态、通信异常、故障注入；后续扩展多设备 | PX4 v1.17 SITL + Gazebo Harmonic；升级按版本决策另验 | M1/M2 已作为云端验证主线；不表示已有 GitHub Actions 流水线 |
| B. 感知与学习数据线 | 高保真视觉、合成数据、学习型策略评测 | Pegasus Simulator 5.1（Isaac Sim 5.1）+ PX4；Cosmos 3 用于数据增广 | M3 起按需；不是 M1–M2 前置 |

MuJoCo 继续承担 `embodied-agent` 的机械臂基线，不用于飞控集成验证。

空地联合仿真（M4-B）：同一 Gazebo 世界内 PX4 SITL 多旋翼 + PX4 rover SITL（v1.17 rover 模式 / Ackermann SIH），避免早期跨仿真器时空同步。

仿真时钟：当前场景倍率与墙钟 / 仿真时钟边界见 [M1 验收](../m1-readiness.md)及具体场景配置。M3 接入 ROS 2 后再统一节点的 `/clock` 或等价时钟机制；不能把参考项目的加速上限当成本项目已验证倍率。

## 2. 容器与镜像

**目标形态**借鉴 aerial-autonomy-stack 的三镜像分工；表内的 ArduPilot、ROS 2 自主层、arm64 和推理服务按后续阶段接入：

| 镜像 | 架构 | 内容 |
|---|---|---|
| `sim-image` | amd64 | Gazebo、PX4/ArduPilot SITL、世界与机体模型、rover 模型 |
| `ground-image` | amd64 | mission-service、console、QGroundControl（调试）、MAVLink 路由、评测裁判、数据平台 |
| `aircraft-image` | **amd64 + arm64（Jetson JetPack 6 / L4T 36）** | executive、guardian、autonomy（ROS 2）、edge-inference（ONNX Runtime：amd64 CUDA / arm64 TensorRT FP16） |

M3 目标是让同一份 `aircraft-image` Dockerfile 覆盖工作站仿真与机载部署，再引入 Jetson-in-the-loop 拓扑和对应自动化验证。当前实际构建入口为 [m1.Dockerfile](../../sim/m1.Dockerfile) 与上表的 Compose 文件。

以上为完整路线：M1 已实现 amd64 的 sim/aircraft/ground 分工与云端 Compose；arm64、Jetson-in-the-loop 和 ROS 2 自主层仍按 M3 引入，不将当前 amd64 仿真镜像宣称为机载镜像。

目标网络分为仿真侧与机器人上行侧；当前实际网络、挂载与权限以各 Compose 文件为准，跨机器人无线 / mesh 的 comms-in-the-loop 尚未实现。

## 3. 机载硬件路线

| 阶段 | 计算 | 飞控 | 说明 |
|---|---|---|---|
| M1–M2 | 工作站容器 | PX4 SITL | 无硬件 |
| M3 | Jetson Orin Nano Super / Orin NX（Jetson-in-the-loop） | PX4 SITL | 验证算力、延迟预算、TensorRT 推理 |
| M4-A | Jetson Orin NX 或 ModalAI VOXL 2（PX4 一体） | PX4 v1.17 真机（Pixhawk 6 级） | 受限真机；RC 接管；共因故障测试 |
| M5+ | 同上；地面机器人 Jetson Orin / Thor | PX4 + Nav2 平台 | 空地协同真机 |

机载推理预算（M3 定义并测量）：控制路径不含任何模型推理；事件检测 VLM 延迟 ≤ 1 s、非阻塞；安全监督周期 ≥ 10 Hz，尾延迟（p99）有上限并进入评测。

## 4. 软件版本基线（D008、D022、D025、D029）

| 组件 | 基线 | 升级评估点 |
|---|---|---|
| OS | Ubuntu 24.04 | — |
| Python | 3.12 | — |
| ROS 2（M3 计划） | Jazzy Jalisco；当前尚无自主层 ROS 2 节点 | M3 按 D008 评估新版本，须验证 px4_msgs、Nav2、BehaviorTree.ROS2 与 rmw_zenoh 的兼容组合 |
| Gazebo | Harmonic（Jazzy 配对） | 随 ROS 2 升级到 Jetty |
| PX4 | v1.17.x（锁定小版本） | 不跟随 main |
| MAVSDK-Python | 精确锁定 `3.17.4`（gRPC 封装 + `mavsdk_server`，D025） | 原生绑定或 `mavsdk-grpc` 迁移单独验证 |
| RMW（后续计划） | Fast DDS（机器人内）；Zenoh（跨机器人） | 随 M3 中间件验证 |
| Nav2（M4-B 后续 / M5 首批） | 计划采用 ROS 2 对应版本，当前未接入 | 随 ROS 2 与地面平台验证 |
| LLM Provider | MiniMax-M3，OpenAI 兼容 HTTP（`httpx`），配置沿用 car-agent（D029） | 模型或工具调用率变化时按 D029 重估 |

版本组合锁定在 `configs/platforms/*.yaml` 与容器基础镜像标签中；任何变更走 ADR。

### M0 开发冒烟交付

以下是 M0 交付时的历史范围：提前创建 `proto/`、`scripts/`、`sim/`，保存五个基础技能草案与故障注入矩阵。当前已有 M1/M2 运行时、六个技能和对应场景集；不能把历史草案说明当作现有能力清单。

`sim/compose.yaml` 先提供独立的 PX4 SITL + Gazebo 服务；不预建没有实现的 ground/aircraft 服务。2026-09-19 实查官方预编译仓库缺少 v1.17.0 标签，因此用固定 digest 的官方 Jazzy 开发镜像构建 v1.17.0 源码（D022）。启动使用 headless `gz_x500`；冒烟只验证容器平台、PX4 版本、Gazebo 世界/时钟与遥测，不解锁、不起飞、不改变飞控参数。后续 M1 扩展成上述三镜像拓扑。

Windows 开发通过已安装的 Docker Desktop Linux 后端运行；构建、运行与挂载入口使用 ASCII 路径，挂载目录与源仓库分离，仅复制冒烟所需文件，不复制 `.env`、`.git` 或其他项目文件。复现方法与实际结果分别见[仿真目录指南](../../sim/README.md)与 [M0 核对记录](../m0-readiness.md)。主机安装 WSL 发行版、全局依赖或调整系统设置仍按用户红线先批准。

### 云端开发与联调（D023）

按用户 2026-09-19 的明确要求，后续需要 Linux 真栈、仿真或服务联调时默认使用现有云服务器；本机保留编辑与快速确定性检查。统一入口为 `scripts/dev_stack.py`，只面向 cloud，不在连接失败时回落到本地 Compose。

| 内容 | 云端职责 | 阶段 |
|---|---|---|
| 契约验证、PX4/Gazebo 仿真、测试产物 | 云端工作区与独立容器 | 当前可部署 |
| Planner、Compiler/Admission、Catalog、业务账本、控制台、证据归档 | 任务服务与入口容器 | M2 已实现，常驻任务台按 D035/D036 部署 |
| executive、guardian、uplink、飞控适配器 | 当前与模拟飞控在同一云主机；后续真机留在设备侧 | M1/M2 已实现；autonomy 按 M3 引入，真机按 M4-A 验证 |

服务器上使用 SSH 用户家目录下的 `drone-agent/`，Compose project 固定 `drone-agent-cloud`，独立网络和产物目录。SITL 上限为 1.5 CPU / 2 GiB，验证容器上限为 1 CPU / 1 GiB；不发布宿主端口。部署不改变现有 car-agent 容器、数据、配置与服务入口。

D028 对网页入口增加明确例外：`sim/compose.console.yaml` 的网页容器只发布 `127.0.0.1:8768`，由现有 Tailscale Serve 的独立 HTTPS `8447` 端口代理，不向公网监听、不启用 Funnel。网页使用受限 ASGI 容器；同机专用 systemd 任务代理经私有 UDS 接收固定请求。原 SITL、executive、guardian 与验证容器的网络和资源限制不变。入口安装及回退只能调整本项目服务与新 Serve 映射，保持 car-agent 的 443/8443–8446 映射原样。

M2（D030、D031、D033）在同一工作区增加：ground 镜像中的 mission-service（Planner、Compiler / Admission、审批与签名、业务账本、Evidence Verifier、报告），只在内部上行网络上以 mTLS 接受机器人连接，不发布宿主端口；aircraft 侧新增 uplink 进程，是机载唯一接入该网络的进程，主动拨出、无监听端口，与 guardian（仿真网络）和 executive（无网络）经机载私有卷交接。签发私钥、模型 key、CA 与各证书私钥只存在本项目 `secrets/`（0600）并只读挂载到需要它的单个容器；机器人只挂载公钥 `trust.json` 与自己的客户端证书。M2 控制台使用 Serve 注入的 tailnet 登录身份，但常驻入口按 D035 独立部署在 `8448`；不替换 D028 的 M1 固定入口。

M2 用例的落位（`sim/compose.m2.yaml`、`scripts/remote_m2.py`）：`secrets/m2/` 由 `fleet/provision.py` 在 ground 镜像中以工作区用户身份生成（签名密钥、`trust/`、`ca/`、`service-tls/`、`robot-tls/`），重复运行保留签名密钥，回执只记录签名密钥 ID 与证书指纹；`secrets/m2-model/` 只在实调时放入模型 key 文件。每个用例目录：`inbox/`（uplink 写入的已验签任务包及 `history/`）、`mailbox/`（uplink 写、executive 只读的操作者信箱）、`aircraft/<mission_id>/v<n>/`（每个任务版本一次飞行，guardian 与 executive 的产物；uplink 只读挂载整个 `aircraft/`）、`robot/`（guardian 的代次水位与已接受版本）、`uplink/`（转发进度）、`service/`（业务账本、媒体、`ready.json`、实调录制）、`service-export/`（结束时经 API 导出的视图）。新版本的 guardian 与 executive 各自启动、使用新代次；任务服务只经 mTLS 与 uplink 通信，API 套接字只在服务容器内经 `docker compose exec` 调用；实调规划只能经 `model-proxy`（内部 `model` 网络 + 可出站 `egress` 网络，允许列表只有模型端点，D036）出站。M2 仿真镜像 `sim2` 在 M1 世界上加入蓝色资产，M1 镜像与已记录运行不变。

M2 任务台常驻入口（D035，`sim/compose.desk.yaml`、`scripts/remote_desk.py`、`scripts/desk_supervisor.py`）：Tailscale Serve 独立 HTTPS `8448` → 宿主 `127.0.0.1:8769`，D028 的 `8447` 不变。常驻容器 `desk`（网页，只读根、属主 UID、无 capabilities、独立 `desk_ingress` 网络，只读挂载 API 套接字与监管者公开状态）、`desk-service`（任务服务，内部 `desk_uplink` 与 `desk_model` 网络，状态在 `~/drone-agent/desk/service/`；只能经 `desk-model-proxy` 出站到允许列表中的模型端点，D036）、`desk-model-proxy`（CONNECT 允许列表代理，唯一接入可出站 `desk_egress` 网络的常驻容器）、`desk-uplink`（机载 uplink，机器人目录 `~/drone-agent/desk/robot/`）；信任根在 `secrets/desk/`，与端到端用例分开。飞行容器 `desk-sitl`、`desk-collector`、`desk-guardian`、`desk-executive`、`desk-judge` 属于 profile `flight`，只由专用 systemd unit `drone-agent-desk-supervisor.service` 在持有项目锁时启停：每个版本全新启动 SITL 并预热，飞行期间暂停 M0 空闲 SITL，结束后恢复；任务终态后在独立用例目录运行 `judge_m2`（在线与回放），结果进入公开状态目录。激活只安装本项目 unit、`desk*` 容器与 `8448` 映射，前后核对其他容器与 Serve 映射。

应用源码只从指定 Git commit 导出；控制脚本、Compose 和锁定依赖分别记录哈希，不能把控制面草案伪称为应用 release。首次复用 M0 已验证的镜像，经 SSH 上传并校验归档哈希、文件系统层与运行配置；后续验证镜像在服务器构建。秘密不进入快照，SSH 连接参数从进程环境读取，不复制 car-agent `.env`。操作、目录与结果边界见[云端开发指南](../cloud-development.md)。

## 5. 数据记录

- 当前原始层：MCAP（自定义消息）、ULog（PX4）、任务事件流（JSONL）及服务侧 SQLite 业务账本；按任务 / 版本与时间核对关联。ROS 2 传感器记录随 M3 接入。
- 每次任务关联：任务与审批版本、模型 / 策略 / 软件 / 飞控配置版本、坐标变换与地图版本、动作与控制模式、安全干预、任务结果、原始影像与遥测证据。
- 后续派生层：LeRobot 等训练格式按需增加导出器；当前未提供此导出能力，不为统一模型输入强制重采样原始数据。
- 回放：任何任务可离线重放事件流并重新运行裁判。

## 6. 可观测性

- 当前通过健康状态、任务 / 控制账本、部署回执、裁判与证据浏览器定位问题；记录任务、步骤、命令和软件版本关联。
- 当前评测从运行记录复算监督周期、尾延迟、恢复与拒绝事件；各指标是否有结果以对应运行产物为准。
- OpenTelemetry 跨进程 trace、集中指标平台及独立的媒体保留策略配置尚未实现，不能把它们作为现有运维接口。

## 7. 安全（信息安全）

- M1 仿真从本地可信文件加载批准任务包，gRPC local credentials + 私有 UDS 验证本地连接，包哈希绑定范围；不把哈希宣称为数字签名。
- M2 的远程任务入口增加签名、机载验签与机器人 ↔ mission-service 双向 TLS（D030）；Zenoh 接入时启用认证。
- 密钥不进代码与镜像；通过运行时挂载。
- 认知链路攻击（提示注入进入 Planner 工具返回）：工具返回视为数据，不作为指令；任何来自工具的「扩大范围」建议都必须经准入。
