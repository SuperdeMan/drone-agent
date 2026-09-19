# 前沿无人机 Agent 架构调研（截至 2026-09）

> 目的：回答「当前前沿的无人机 agent 架构长什么样、哪些设计值得吸收、哪些不能照搬」。本文是 `docs/architecture/` 各项决策的依据，不是文献综述。每条都给出「吸收什么 / 不吸收什么」。

## 0. 一句话结论

2026 年的主流共识已经收敛为：**「大模型表达目标与判断证据，确定性运行时持续执行，独立安全机制约束全程」**。没有任何一个可部署系统让 LLM/VLA 直接、无约束地驱动飞控。因此本项目的主线不是「让大模型飞」，而是「让大模型编译任务、让本地运行时执行任务、让证据确认结果」。

## 1. 分层共识：System 2 / System 1 + 结构化契约

| 来源 | 分层方式 | 我们吸收的 | 我们不照搬的 |
|---|---|---|---|
| Gemini Robotics ER 2 / Gemini Robotics 2（DeepMind，2026-07） | ER 模型做「具身推理编排」（任务进度跟踪、成功检测、多机器人任务委派），把动作执行交给下层 VLA | 编排器 / 执行器分离；把「任务进度跟踪」和「视频成功检测」作为独立的验证角色；多机器人任务委派由编排层完成 | 让编排器直接输出运动指令；把安全判断交给模型 |
| P–C–A 视角综述（*Drones* 2026, 10(9):669） | 感知—认知—行动三层，强调 LLM 输出必须经过「结构化输出契约 + 确定性验证 + 安全过滤 + 回退」 | 四件套原样采用：结构化契约、确定性校验、安全过滤、回退 | — |
| UAVs Meet Agentic AI 综述（arXiv 2506.08045） | 感知 / 认知 / 控制 / 通信四层 + 记忆与反思闭环；边缘推理延迟阈值（~100 ms 级） | 通信层是一等公民；机载推理的延迟预算要显式定义 | 综述中的 L4–L5 自主等级表述不作为工程目标 |
| AerialClaw（arXiv 2606.12142，2026-06） | brain–skill–runtime：LLM 选技能 → 运行时执行并做参数校验、地理围栏、看门狗、失效保护 | 技能注册表 + 运行时强制安全约束的模式；故障注入评测 | 单个 LLM 直接协调多机（token 与延迟不可扩展）；把 LLM 放在反应式控制回路内 |
| General-Purpose Aerial Agents（arXiv 2503.08302） | 慢速深思规划（机载 14B LLM）+ 快速反应控制（状态估计 / 建图 / 避障 / 运动规划）的双向架构 | 「慢 / 快」双时标是基本形态；机载运行 LLM 可行但功耗高（220 W 峰值） | 把 LLM 放在机载作为必要依赖 |
| Hierarchical Agentic Framework for Inspection（arXiv 2510.00259） | Head Agent 规划 + 评估，Worker Agent 控单机；ReActEval（plan–reason–act–evaluate） | 显式 evaluate 阶段：每步执行后由独立环节判定效果 | 全自然语言的 agent 间通信（不可验证、不可回放） |

## 2. 异构多机器人协同

| 来源 | 关键设计 | 吸收 | 不吸收 |
|---|---|---|---|
| CoMuRoS（*Front. Robot. AI* 2026） | 中心化任务管理 LLM + 各机器人本地 LLM；机器人能力用配置文件（形态、能力、约束、初始状态）描述；VLM 事件检测 → 相关 / 无关分类 → 触发重规划；任务分为 independent / sequential / coordinated / infeasible | 能力描述配置化（→ `CapabilityDescriptor`）；事件驱动重规划；任务四分类；混合层级优于全去中心化的结论 | 机器人本地 LLM 生成可执行 Python（不可审计、不可准入） |
| RobotFleet（arXiv 2510.10379） | 目标 → DAG；LLM 或 MILP 分配；机器人以容器化服务 + host/port 注册；失败重试 3 次后重规划 | 任务 DAG + 可替换的分配器（LLM 提议、优化器 / 规则裁决）；机器人作为可注册的服务 | 假设任务时长固定、执行必然成功；能力仅用自然语言描述 |
| Hierarchical LMs for Aerial-Ground System（arXiv 2506.05020） | 空中机器人构建全局语义地图并做层级任务分解，地面机器人做局部导航与操作 | 「空中给全局语义，地面做局部执行」的分工；共享的是语义地图，不是控制 | 特定任务的模型组合 |
| 多机器人 3D 场景图（MR-COGraphs 2412.18381；2506.07454；FOUND-IT 2605.25371） | 开放词汇 3D 场景图作为紧凑、可带宽受限共享的世界表示；LLM 在场景图上规划 | `BeliefWorld` 采用「几何层 + 语义场景图」双表示，节点携带坐标系、时间、不确定性 | 把场景图当作实时避障地图 |
| Open-RMF / free_fleet + Zenoh | 异构车队通过 fleet adapter 接入；机器人之间用 Zenoh 桥接隔离的 ROS 2 域 | 每台机器人独立 ROS 2 域，跨机通过 Zenoh；车队协议与机器人内部中间件解耦 | RMF 的室内交通调度模型直接用于三维空域 |
| Auterion Nemyx（2025-09 起） | 跨厂商无人机集群编排 | 「厂商无关的车队协议」是趋势 | 军用打击链场景 |

## 3. 局部自主：VLA / VLN / 世界模型

| 来源 | 关键点 | 对本项目的定位 |
|---|---|---|
| AutoFly（ICLR 2026, arXiv 2602.09657） | RGB-only 端到端 VLA 野外导航；实机部署时模型在远端服务器，局域网回传速度指令 | 可作为 `LocalPolicy` 插件；**不能假设模型已机载离线可靠** |
| WorldFly（arXiv 2606.06147） | 世界模型 + 动作联合建模 | `PredictedWorld` 接口的目标用户；预测画面不是当前事实 |
| DreamFly（arXiv 2608.12308，2026-08） | 因果记忆 + 滚动时域扩散规划；预测一段动作只执行第一步；终止判断与动作生成分离 | 滚动执行 + 显式终止判定，写入 `LocalPolicy` 契约 |
| ARIES-Mission2（arXiv 2608.12763，2026-08） | 视觉语义接地与航路优化（TSP）分离 | 「语义任务 → 确定性任务编译」的直接佐证 |
| UAV-VLA（2501.05014）、Exp2VLA（2607.03146）、UAV-Track VLA（2604.02241）、VLN 综述（2604.07705） | 任务生成、专家示教蒸馏、跟踪 | 研究支线候选；统一进影子运行评测 |
| Unified Autonomy Stack（arXiv 2605.12735） | 多模态感知（因子图融合）+ 多行为规划 + 多层安全导航（地图规划 / 学习策略 / CBF 最后防线），空中与足式机器人共用 | 「学习策略与 CBF 安全过滤并存」正是我们局部自主层 + 安全监督的形态；跨本体共用一套导航栈是可行的 |
| Cosmos 3（NVIDIA，2026-06）、Isaac Sim 5.1 + Pegasus 5.1 | 全模态世界模型、合成数据；Isaac 上的 PX4 多机仿真 | 数据线与研究线的工具，不是 M1 前置条件 |

**判断：** 学习型局部技能只以「插件 + 影子运行 + 有限接管」的方式进入系统；它们向下提交与确定性规划器相同类型的候选目标 / 轨迹，经过同一套约束检查。换模型不改变权限、超时、空间约束和证据要求。

## 4. 安全：运行时保障（RTA）是既有学科，不必发明

| 来源 | 关键点 | 采用方式 |
|---|---|---|
| Run Time Assurance 导论（arXiv 2110.03506）、Simplex 架构 | 高性能控制器 + 已验证基线控制器 + 决策模块切换 | 安全监督器 = 决策模块；基线 = 平台恢复策略图（悬停 / 盘旋 / 返航 / 就地降落 / 飞控原生失效保护） |
| CBF 地理围栏与避撞（arXiv 2403.02508）、Safety on the Fly（2410.11157） | 控制屏障函数作为最后一道过滤 | 控制出口前的 `ConstraintFilter`（M3 起引入 CBF；M1 用包络与围栏硬检查） |
| 分布式 / 黑盒 Simplex（多智能体，Springer 2024；ScienceDirect） | 多智能体下的 Simplex 与安全保证 | 空地协同阶段的参考 |
| Mission-level RTA（arXiv 2606.06996）、Progress-certified Reversible Simplex（2601.19499） | 任务级保障、可逆切换与活性 | 「切回高性能控制器」必须有准入条件，恢复不是单向的 |
| PX4 v1.17 ROS 2 Control Interface | 外部模式、模式执行器、解锁检查、失效保护集成（部分实验性；可 `deferFailsafes`） | 预留 PX4-ROS2 适配路径；**不使用失效保护延期** |

## 5. 协议与工具接口

| 协议 | 2026 状态 | 在本项目中的位置 |
|---|---|---|
| MCP（Model Context Protocol） | 企业 1 万+ 服务器；ROS 2 MCP 服务器已有多个实现（wise-vision/ros2_mcp、LCAS/ros2_mcp 等），部分直接发布 `cmd_vel` | **只用于任务规划层的只读 / 查询类工具**（地图、资产库、天气、空域）。禁止任何 MCP 工具触达控制出口——这是与社区 ROS 2 MCP 服务器最大的不同 |
| A2A（Agent2Agent） | 150+ 组织生产使用 | 跨应用 agent 互操作：`cockpit-agent`（座舱）作为授权任务入口调用 `drone-agent` 的协调器；只传任务请求与进度，不传控制 |
| MAVLink 2 / MAVSDK | PX4 v1.17 默认 MAVLink 2；MAVSDK-Python 现行 3.17.x（gRPC 封装 + `mavsdk_server`，PyPI 2026-09-17 最新 3.17.4）；上游 main 文档已描述 v4 原生绑定（PyPI `mavsdk` 将改为原生，旧 gRPC 包已拆为 `mavsdk-grpc` 3.17.4），但 v4 尚未发布 | M1 飞控适配器传输层用 3.17.x，v4 发布后只迁适配器；Offboard 插件 20 Hz 自动重发是活性检测要单独覆盖的点 |
| uXRCE-DDS / px4_ros2 | PX4 ↔ ROS 2 标准桥；in-tree Zenoh 在 v1.17 与 rmw_zenoh 兼容成熟 | M3 局部自主层 |
| ROS 2 | Lyrical Luth（2026-05，LTS 至 2031-05）、Jazzy（LTS 至 2029-05）；默认 RMW 仍是 Fast DDS；rmw_zenoh 可选 | 基线 Jazzy + Ubuntu 24.04 + Gazebo Harmonic；M3 评估切 Lyrical |
| DJI Cloud API（MQTT/HTTPS/WebSocket） | Dock 3、航线、直播、远程解锁；不开放连续 Offboard | 「能力协商」的典型案例：DJI 适配器只声明 `wayline_mission / camera / gimbal / live / dock`，不伪装成 `offboard` |
| Remote ID / UTM / U-space；中国 UOM | 中国：《无人驾驶航空器飞行管理暂行条例》2024-01-01 施行，UOM（uom.caac.gov.cn）为唯一报备入口，2026-05-01 起未实名登记锁飞 | 准入层的 `AirspaceConstraintProvider` 插件点；M1 不实现，M4 真机前必须接入 |

## 6. 机载算力与部署形态

| 平台 | 事实 | 定位 |
|---|---|---|
| NVIDIA Jetson Orin Nano Super / Orin NX | ≤8B 参数 LLM/VLM 可跑；VILA-2.7B 级 VLM 可做基础监控 | 首选伴飞计算机；机载小 VLM 做事件检测，不做任务规划 |
| ModalAI VOXL 2 | PX4 + 15 TOPS 一体，16 g，Blue UAS | 小型机备选 |
| Jetson Thor | Blackwell，面向人形 / 重载 | 地面机器人或大型平台 |
| aerial-autonomy-stack（arXiv 2602.07264） | 三容器（sim / ground / aircraft），aircraft 镜像 amd64+arm64 多架构，Jetson-in-the-loop、comms-in-the-loop，faster-than-real-time 仿真 | 部署与 CI 形态直接借鉴 |
| Aerostack2（arXiv 2303.18237） | ROS 2 行为（action）+ 行为树任务 + 平台插件 + 多机 | 技能 = 带生命周期的 action 的思路一致；不整体依赖其框架 |

## 7. 评测与数据

- 三类结果分开统计：**任务完成 / 合理拒绝或安全中止 / 不安全或错误行为**。只看成功率会奖励虚报成功；只看事故率会奖励拒绝一切。
- 独立裁判只读 `TruthWorld`（仿真真值），被测 agent 只读 `BeliefWorld`。
- 原始数据保留多频率传感与飞控日志（MCAP + ULog + 任务事件流），训练格式（LeRobot 等）只是派生视图。
- 学习模型升级流程：回放 → 仿真 → 影子运行 → 受限接管 → 正式。不在飞行中在线训练或热换策略。

## 8. 本项目相对于上述工作的差异点

1. **单一控制出口 + 控制权租约**：所有控制写入经过一个仲裁点，携带任务版本与控制权代次；社区 LLM-drone 项目普遍缺少这一层。
2. **三元状态**（执行状态 / 效果判定 / 安全判定）：`UNKNOWN` 不能包装成成功，依赖步骤只在效果被证实后放行。
3. **契约优先于模块**：先定六类契约与契约测试，再按里程碑创建模块。
4. **厂商能力协商**：不用统一假接口掩盖平台差异（DJI 无 Offboard、固定翼不能悬停）。
5. **法规接入点内建**：空域 / 报备作为准入约束提供者，不是事后补丁。

## 来源

- Gemini Robotics ER 2：<https://deepmind.google/models/gemini-robotics/embodied-reasoning/>；Gemini Robotics 2（2026-07-30）：<https://deepmind.google/blog/gemini-robotics-2-brings-whole-body-intelligence-to-robots/>
- P–C–A 综述：<https://doi.org/10.3390/drones10090669>
- UAVs Meet Agentic AI：<https://arxiv.org/abs/2506.08045>
- AerialClaw：<https://arxiv.org/abs/2606.12142>
- General-Purpose Aerial Intelligent Agents：<https://arxiv.org/abs/2503.08302>
- Hierarchical Agentic Framework for Inspection：<https://arxiv.org/abs/2510.00259>
- Taking Flight with Dialogue（PX4 NL agent）：<https://arxiv.org/abs/2506.07509>
- CoMuRoS：<https://www.frontiersin.org/journals/robotics-and-ai/articles/10.3389/frobt.2026.1843313/full>
- RobotFleet：<https://arxiv.org/abs/2510.10379>；MultiUAV-Plat：<https://arxiv.org/abs/2606.31073>
- Hierarchical LMs Aerial-Ground：<https://arxiv.org/abs/2506.05020>
- MR-COGraphs：<https://arxiv.org/abs/2412.18381>；多机器人场景图规划：<https://arxiv.org/abs/2506.07454>；FOUND-IT：<https://arxiv.org/abs/2605.25371>
- Unified Autonomy Stack：<https://arxiv.org/abs/2605.12735>
- AutoFly：<https://arxiv.org/abs/2602.09657>；WorldFly：<https://arxiv.org/abs/2606.06147>；DreamFly：<https://arxiv.org/abs/2608.12308>；ARIES-Mission2：<https://arxiv.org/abs/2608.12763>；UAV-VLA：<https://arxiv.org/abs/2501.05014>；Exp2VLA：<https://arxiv.org/abs/2607.03146>；UAV-Track VLA：<https://arxiv.org/abs/2604.02241>；Aerial VLN 综述：<https://arxiv.org/abs/2604.07705>
- RTA 导论：<https://arxiv.org/abs/2110.03506>；CBF 围栏：<https://arxiv.org/abs/2403.02508>；Mission-level RTA：<https://arxiv.org/abs/2606.06996>；Reversible Simplex：<https://arxiv.org/abs/2601.19499>；Black-box Simplex（多智能体）：<https://link.springer.com/article/10.1007/s11334-024-00553-6>
- Aerostack2：<https://arxiv.org/abs/2303.18237>；aerial-autonomy-stack：<https://arxiv.org/abs/2602.07264>
- PX4 v1.17 Release Notes：<https://docs.px4.io/main/en/releases/1.17>；PX4 ROS 2 Control Interface：<https://docs.px4.io/v1.17/en/ros2/px4_ros2_control_interface>；uXRCE-DDS：<https://docs.px4.io/main/en/middleware/uxrce_dds>
- MAVSDK-Python v4 迁移指南（上游 main 文档，2026-09-17 尚未发布到 PyPI）：<https://mavsdk.mavlink.io/main/en/python/migration.html>
- ROS 2 Lyrical Luth：<https://docs.ros.org/en/kilted/Releases/Release-Lyrical-Luth.html>；Fast DDS 仍为默认：<https://discourse.openrobotics.org/t/ros-2-lyrical-luth-and-11-years-of-fast-dds-as-ros-2-default-middleware/55062>；rmw_zenoh：<https://github.com/ros2/rmw_zenoh>
- ArduPilot AP_DDS：<https://ardupilot.org/dev/docs/ros2.html>
- Open-RMF free_fleet：<https://github.com/open-rmf/free_fleet>
- ROS 2 MCP 服务器：<https://github.com/wise-vision/ros2_mcp>、<https://github.com/LCAS/ros2_mcp>；A2A：<https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/>
- DJI Cloud API：<https://developer.dji.com/cloud-api/>
- UOM 与条例：<https://www.caac.gov.cn/XXGK/XXGK/TZTG/202312/t20231231_222550.html>、<https://www.caac.gov.cn/XXGK/XXGK/ZCFBJD/202604/t20260421_230620.html>
- Cosmos 3：<https://research.nvidia.com/labs/cosmos-lab/cosmos3/technical-report.pdf>；Pegasus Simulator：<https://pegasussimulator.github.io/PegasusSimulator/>
- Jetson 边缘 VLM：<https://developer.nvidia.com/blog/getting-started-with-edge-ai-on-nvidia-jetson-llms-vlms-and-foundation-models-for-robotics/>；VOXL 2：<https://www.modalai.com/products/voxl-2>
- Auterion Nemyx：<https://www.tectonicdefense.com/auterion-launches-new-drone-swarming-technology/>
- BehaviorTree.ROS2：<https://github.com/BehaviorTree/BehaviorTree.ROS2>；SkiROS2：<https://arxiv.org/abs/2306.17030>
