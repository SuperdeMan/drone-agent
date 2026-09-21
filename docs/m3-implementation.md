# M3 实施与验收计划

本轮拆解路线图的 M3「局部自主与降级」。范围：在 M2 的任务服务与 M1 的安全闭环之上，引入 ROS 2 Jazzy 自主层、px4_ros2 外部模式作为第二控制路径、CBF 约束过滤、能源可达性、恢复策略 v2、机载容器（arm64）与 Jetson-in-the-loop、Zenoh、机载小 VLM 事件检测、`AirspaceConstraintProvider` 真实接口定义与 `LocalPolicy` 影子运行框架。不接真机（M4-A），不做多机器人（M4-B）。

前置：M2 已关闭。M3 是三段中风险最高的一段，原因有三：它第一次把控制写入路径扩展到 ROS 2；它需要当前云端 4 vCPU / 8 GiB 之外的算力（GPU 与 Jetson）；它决定 guardian 是否改写为 C++/Rust（D003 触发器）。因此批次 A 先做测量与决策，再做功能。M4-A 的硬件选型与实名登记应在本阶段期间启动（见 [M4 计划](m4-implementation.md) A0）。

## 退出标准拆解

| 门槛 | 可检查形式 | 证据 |
|---|---|---|
| 观测过期、任务卡住、网络中断、计算过载四类场景行为可验证 | 每类在 SITL 与 Jetson-in-the-loop 各 ≥ 1 个注入场景、3 种子，行为与恢复策略 v2 的对应边一致；覆盖 M0 矩阵中 `stage: M3` 的三条草案边 | `configs/scenarios/m3_suite.yaml`、恢复边绑定记录 |
| 安全监督周期 p99 达标 | 批次 A 在 `decisions.md` 固定预算（草案：10 Hz，p99 ≤ 120 ms，最大 ≤ 200 ms，在感知 + 局部规划 + VLM 满载下；M1 实测 p99 102.9 ms 是空载参照）；每个场景报告 p99 / max | 场景回执的周期审计 |
| guardian 是否重写为 C++/Rust 有测量结论 | 在 Jetson-in-the-loop 满载下测 Python guardian 周期分布；结论与理由写入 `decisions.md`；若重写，`drone.control.v1` 接口不变，先冻结接口测试再迁移 | D 条目 + 测量数据 |
| Jetson-in-the-loop 延迟预算达标（`08-evaluation.md` §5） | 控制路径无模型推理；事件检测 VLM 延迟 ≤ 1 s、非阻塞；感知 / 局部规划到 guardian 的意图延迟有上限 | 延迟分布进入回执 |
| 第二控制路径下单一出口不变 | 外部模式节点只接受 guardian 授权的设定值（带代次 / 序号 / 新鲜度），无授权即停止发布；MAVSDK 与 uXRCE-DDS 两条链路不同时写 | 契约测试 + SITL 场景 + ULog 模式核对 |

## 实施顺序

| 批次 | 指示周期 | 实现 | 验收 |
|---|---|---|---|
| A：测量与地基 | 约 2–3 周 | 算力与拓扑决策、aircraft 镜像加 ROS 2 Jazzy / `px4_msgs` / uXRCE-DDS agent、arm64 多架构构建、Jetson-in-the-loop 拓扑、周期预算与 guardian 测量、Lyrical 评估纪要 | 预算与语言结论入 `decisions.md`；arm64 镜像在 Jetson 上跑通 M1 场景 |
| B：自主层节点 | 约 2–3 周 | `ros2_ws/`：感知、定位健康、局部地图（占据 / ESDF）、短时域局部规划；`autonomy/` 插件接口与桥接 | 输出带协方差 / 置信度 / `valid_until`；真值不泄漏 |
| C：第二控制路径与安全过滤 | 约 2–3 周 | 外部模式出口节点、guardian 的 CBF 约束过滤、Offboard 类技能、能源可达性模型、恢复策略 v2 | M0 矩阵 M3 草案边全部有注入记录；两链路互斥 |
| D：机载推理与通信 | 约 1–2 周 | edge-inference（小 VLM 事件检测）、Zenoh 传输、计算过载与网络中断场景 | 延迟预算达标；控制路径无推理；过载不降低监督周期 |
| E：准出 | 约 1 周 | 四类场景集、`LocalPolicy` 影子框架、`AirspaceConstraintProvider` 真实接口、门禁与 readiness | 多种子通过；错误成功 0；影子输出只记录不执行 |

## 工作包

### 批次 A

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-01 算力与拓扑决策 | 现有共享云主机（4 vCPU / 8 GiB，SITL 限 1.5 CPU，M1 已需把相机压到 160×120 / 5 Hz）不能同时跑 SITL + ROS 2 自主层 + VLM；按 D023 重估触发器决定 GPU 云实例、本地 Linux 工作站（WSL2 / Docker，ASCII 路径）、Jetson 开发套件 + 本地 SITL 三者的组合；采购 Jetson Orin NX（或 Orin Nano Super）开发套件 | — | `decisions.md` 条目；`cloud-development.md` 增补 M3 拓扑；安装全局依赖与系统配置仍按红线先问 |
| WP-M3-02 aircraft 镜像 ROS 2 化 | `sim/aircraft.Dockerfile`：Ubuntu 24.04 + ROS 2 Jazzy + `px4_msgs`（与 PX4 1.17.0 同版本）+ Micro XRCE-DDS agent + `px4_ros2_cpp`；amd64 + arm64（JetPack 6 / L4T 36）双架构；ROS 2 节点用系统 Python 3.12 或 C++，本包仍不 import `rclpy` | 01 | 同一 Dockerfile 两架构构建；SITL 中 uXRCE-DDS 桥接通、话题版本匹配；M1 场景在新镜像上回归通过 |
| WP-M3-03 Jetson-in-the-loop 拓扑 | `sim/compose.jetson.yaml`：SITL + Gazebo 在工作站，executive / guardian / autonomy / edge-inference 在 Jetson，走 `AIR_SUBNET`；时钟对齐与延迟测量探针 | 01, 02 | Jetson 上跑通 M1 nominal 场景；链路延迟与时钟偏差进入回执 |
| WP-M3-04 周期预算与 guardian 测量 | 定义安全监督周期与意图延迟预算（草案见退出标准）；在 SITL 与 Jetson 上分别测 Python guardian 在空载 / 感知满载 / VLM 满载三档下的周期分布；CPU 隔离（cgroup / 亲和）作为对照 | 03 | 预算与测量结论写入 `decisions.md`（D003 触发器）；若重写 guardian，先冻结接口测试再迁移 |
| WP-M3-05 ROS 2 Lyrical 评估 | 按 D008 条件核查 `px4_msgs`、Nav2、BehaviorTree.ROS2、`rmw_zenoh` 在 Lyrical 的稳定发布状态，纪要存 `docs/research/`；默认维持 Jazzy | — | 纪要 + `decisions.md` 重估记录；切换需另立条目 |

### 批次 B

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-06 `autonomy/` 插件接口与桥接 | `src/drone_agent/autonomy/`：`LocalPlanner` / `LocalPolicy` / `PerceptionProvider` / `WorldPredictor` 接口（参考 `embodied providers/{policy,perception}.py`，输出改为带协方差与置信度的 `WorldFact` 与带 `valid_until` 的轨迹片段）；与 `ros2_ws/` 节点经 proto / IPC 交互（新增 `drone.autonomy.v1`，草案 → 冻结） | 决策待办 ①；02 | 契约测试：`predicted` 事实不满足前置；`sim_truth` 来源在非裁判进程被拒；字段清单与 wire 检查通过 |
| WP-M3-07 感知节点 | `ros2_ws/src/da_perception`：YOLO 级检测器（ONNX Runtime；amd64 CUDA / arm64 TensorRT）→ 带协方差与置信度的检测；Gazebo 增加深度或双目传感器（x500_depth 级模型） | 决策待办 ④；02, 06 | 检测输出经桥接进入 BeliefWorld；协方差缺失即拒绝入图；真值不进节点 |
| WP-M3-08 定位健康节点 | `ros2_ws/src/da_localization`：EKF 状态、GNSS fix、视觉定位健康（有则用）→ `LocalizationHealth`；guardian 用它替换 M1 的单源判断 | 02, 06 | GNSS 失效 + 视觉可用的注入场景（M0 `fi.localization.gnss_lost_visual_ok`）行为可验证 |
| WP-M3-09 局部地图与短时域规划 | `ros2_ws/src/da_local_map`（占据栅格 / ESDF，机载不共享）、`da_local_planner`（确定性短时域规划，输出轨迹片段 + `valid_until` 200 ms 级）；executive 给「做什么、范围、时限」，节点决定「此时此地怎么做」 | 07 | 规划输出在围栏与 ESDF 距离约束内；无路可走时报 `progress_stalled`，不静默重试 |

### 批次 C

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-10 外部模式出口节点 | `ros2_ws/src/da_egress_ext`：基于 `px4_ros2_cpp` 的外部模式节点，是 guardian 控制出口的物理延伸：只接受来自 guardian 本地 IPC 的设定值（带 `lease_epoch` / `command_seq` / `valid_until`），无新鲜授权即停止发布（让 PX4 自行退出外部模式）；不注册失效保护延期；guardian 观察 `CURRENT_MODE` 保证 MAVSDK mission_upload 与外部模式不同时写 | 决策待办 ①；02, 06 | 契约测试：节点无 guardian 授权不能发布；SITL 场景：授权中断 → 停止发布 → 飞控退出外部模式；ULog 核对模式迁移 |
| WP-M3-11 CBF 约束过滤 | `guardian/constraint_filter.py`：围栏与 ESDF 距离的控制屏障函数过滤，作用于每条设定值 / 轨迹片段；不可行即拒绝并进入恢复；M1 的包络硬检查保留为下界 | 09, 10 | 单测：越界设定值被过滤或拒绝；SITL：接近障碍与围栏时真值轨迹不越界；过滤耗时计入周期预算 |
| WP-M3-12 Offboard 类技能 | `configs/skills/goto_local.yaml`（`skill.flight.goto_local`）、`skill.inspect.asset` 增加外部模式路径（绕障接近）；能力表加入 `external_mode`；M1 的 mission_upload 技能保持可用作降级 | 10, 11 | 技能清单完整；同一任务在两种控制模式下可编译；外部模式不可用时编译回退到航线版本或拒绝 |
| WP-M3-13 能源可达性模型 | `guardian/energy.py`：按实测消耗率、距离、风与余量估计 `rtl_reachable` / `nearest_site_reachable`，带不确定性；缺输入为 unknown → 保守边；替换 M2 的静态能耗估计 | 07 | 注入低电场景在可达 / 不可达两种上下文下选边正确；unknown 不选乐观边 |
| WP-M3-14 恢复策略 v2 | `configs/recovery_policies/multirotor_m3_v2.yaml`：并入 M0 草案的 `observation_stale.offboard`、`trajectory_stale.offboard`、`localization.gnss_lost_visual_ok`；新增 `progress_stalled`、`compute_overloaded`、`autonomy_unavailable`（降级到 mission_upload 或 hold）；每条边一个注入场景；切回高性能路径的准入条件按 `03-safety.md` §3 | 10–13 | `unverified_edges()` 为空才进生产；每条边 3 种子绑定记录 |

### 批次 D

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-15 edge-inference | `autonomy/edge_inference/`：≤ 8B 级 VLM 事件相关性分类（TensorRT FP16 / ONNX Runtime CUDA），输出 `WorldFact(candidate)` 与重规划触发；独立进程、非阻塞、控制路径不依赖它；延迟 ≤ 1 s 预算 | 03, 06 | 延迟分布达标；杀掉该进程任务照常完成；输出只能触发 M2 的有界重规划，不能产生控制 |
| WP-M3-16 Zenoh 传输 | `fleet/transport_zenoh.py`：`FleetTransport` 的 Zenoh 实现，启用认证；M3 在机器人 ↔ mission-service 链路上验证，M4-B 复用于机器人 ↔ 机器人；机器人内 DDS 不跨无线链路；M2 的 gRPC 实现保留 | 02 | 消息 schema 不变；断链场景（`uplink_lost`）在 Zenoh 下行为与 M1 一致 |
| WP-M3-17 计算过载与网络中断场景 | 注入：VLM 与感知满负荷、CPU 竞争进程、Zenoh 链路断开 / 抖动、DDS 话题延迟；guardian 检测自身周期超限即视为健康退化并按策略处理 | 14, 15, 16 | 四类场景（观测过期、任务卡住、网络中断、计算过载）SITL 与 Jetson 各通过；监督周期 p99 不因过载越预算 |

### 批次 E

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-18 `LocalPolicy` 影子运行框架 | `autonomy/shadow.py`：并行运行学习型策略，只记录其候选轨迹与确定性规划器的差异、包络违反率、裁判认可率；`implementation.learned_stage=shadow` 的技能永不执行 | 06, 09 | 契约测试：影子阶段 `may_execute` 为假；影子报告格式进入 `08-evaluation.md` §6 |
| WP-M3-19 `AirspaceConstraintProvider` 真实接口 | `admission/airspace.py`：定义 UOM 查询 / 报备状态 / Remote ID 的接口与录制后端（真实报备 M4-A 执行）；准入在真实模式要求 `filed` 状态 | M2 WP-06 | 录制响应驱动的准入测试；真实模式无报备 fail closed |
| WP-M3-20 门禁与 readiness | `configs/scenarios/m3_suite.yaml`、`scripts/verify_m3_release.py`、`docs/m3-readiness.md`；`eval/BASELINES.md` 追加周期 / 延迟 / 四类场景结果 | 14, 17 | 同一 SHA；多种子；错误成功 0；回放一致；Jetson 与 SITL 分别留证 |

## 设计边界

- 单一控制出口不变：外部模式节点是 guardian 出口的延伸，不是第二个仲裁者。它没有任务语义，只做「有授权就转发、无授权就停」。guardian 仍是唯一决定写与不写的进程，两条链路（MAVLink 经 MAVSDK、uXRCE-DDS 经节点）互斥。
- 自主层节点只提交候选目标 / 轨迹，与学习型插件走同一条约束过滤路径；换实现不改变权限、超时、空间约束与证据要求。
- 控制路径不含任何模型推理；VLM 只产生 `candidate` 事实与重规划触发。
- 恢复策略 v2 是新文件；M1 的 `multirotor_m1_v1` 保留为 mission_upload 模式的生产策略，M3 在自主层不可用时降级到它。
- Jetson 上的验证与云端 SITL 的验证分别留证；arm64 镜像跑通不等于真机就绪（M4-A）。
- 不使用 PX4 失效保护延期；外部模式注册时不请求 `deferFailsafes`。

## 决策待办（动手前补 `decisions.md`）

| 编号 | 议题 | 阻塞 |
|---|---|---|
| ① | 外部模式出口节点作为 guardian 出口延伸的进程边界、IPC 与看门狗语义；`drone.autonomy.v1` 包的范围 | WP-M3-06、10 |
| ② | M3 算力与拓扑（云 GPU / 本地工作站 / Jetson）与预算；D023 云端默认是否调整 | WP-M3-01 |
| ③ | 安全监督周期与意图延迟预算数值；guardian 语言结论（D003 触发器） | WP-M3-04 |
| ④ | Gazebo 传感器配置（深度 / 双目）与感知模型选型 | WP-M3-07 |

## 验收记录要求

在 M1 / M2 要求之上，每个场景另记：运行拓扑（SITL 本地 / 云 / Jetson-in-the-loop）、控制模式（mission_upload / external_mode）、自主层节点版本、模型版本（检测器、VLM）、监督周期分布、意图延迟分布、VLM 延迟分布、CPU / 内存占用、两条飞控链路的写入计数。影子运行输出单独归档，不与执行结果混记。

## 不在 M3 范围

真机与 UOM 实际报备（M4-A）；多机器人协同、交接、空间对齐（M4-B）；Nav2 地面平台（M5）；学习型策略有限接管（M6）。
