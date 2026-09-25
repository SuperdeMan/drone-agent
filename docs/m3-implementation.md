# M3 实施与验收计划

**状态：M3-SITL 已通过（2026-09-25，候选 `75382dc`，见 [M3 验收记录](m3-readiness.md)）；M3-JIL 待硬件，M3 未关闭。** 以下工作包的 SITL 部分已实现并通过门禁；JIL 部分（WP-M3-02 的 arm64 构建、WP-M3-03，以及 WP-M3-04 / 15 / 17 / 20 在 Jetson 上的验收）待硬件。决策待办由 D038–D042 补齐，实飞修正与门禁批次中的发现见 D045–D048。验收拆成 **M3-SITL**（共享云主机，amd64）与 **M3-JIL**（Jetson-in-the-loop，待硬件）两条线，两条都通过才关闭 M3（D038）。

D049 将本计划中的 JIL 工作迁入 **H1**，M3-SITL 作为独立能力门禁保留；原 M3 总门禁仍需两线都通过。P1/P2 和预置航线的 P3 不等待 H1；具体局部自主场景继续要求相应版本的 M3-SITL 证据。

本页保留 20 个原工作包 ID 并按实际落位修订。范围是 ROS 2 Jazzy、自主层、px4_ros2 外部模式、CBF、能源可达性 / 恢复 v2、Zenoh、CLIP 候选事件、空域录制接口和影子框架；H1 承接 arm64、Jetson 与生成式机载 VLM。SITL 的实际软件路径、参数及门禁结果以 D038–D048 和 [readiness](m3-readiness.md) 为准。

前置 M2 已关闭。SITL 已按 D038 在共享云主机 CPU 配额内完成；原计划中“必须新增 GPU 才能推进”的假设不再成立。H1 待目标设备与实际拓扑，H2 硬件方案可提前设计，但采购和系统变更仍按项目红线。

## 退出标准拆解

| 门槛 | 可检查形式 | 证据 |
|---|---|---|
| 观测过期、任务卡住、网络中断、计算过载四类场景行为可验证 | 每类在 SITL 与 Jetson-in-the-loop 各 ≥ 1 个注入场景、3 种子，行为与恢复策略 v2 的对应边一致；覆盖 M0 矩阵中 `stage: M3` 的三条草案边 | `configs/scenarios/m3_suite.yaml`、恢复边绑定记录 |
| 安全监督周期 p99 达标 | 批次 A 已在 D040 固定预算（D040 已冻结：10 Hz，p99 ≤ 120 ms，最大 ≤ 200 ms，在感知 + 局部规划 + VLM 满载下；M1 实测 p99 102.9 ms 是空载参照）；每个场景报告 p99 / max | 场景回执的周期审计 |
| guardian 是否重写为 C++/Rust 有测量结论 | 在 Jetson-in-the-loop 满载下测 Python guardian 周期分布；结论与理由写入 `decisions.md`；若重写，`drone.control.v1` 接口不变，先冻结接口测试再迁移 | D 条目 + 测量数据 |
| Jetson-in-the-loop 延迟预算达标（`08-evaluation.md` §5） | 控制路径无模型推理；事件检测 VLM 延迟 ≤ 1 s、非阻塞；感知 / 局部规划到 guardian 的意图延迟有上限 | 延迟分布进入回执 |
| 第二控制路径下单一出口不变 | 外部模式节点只接受 guardian 授权的设定值（带代次 / 序号 / 新鲜度），无授权即停止发布；MAVSDK 与 uXRCE-DDS 两条链路不同时写 | 契约测试 + SITL 场景 + ULog 模式核对 |

## 原批次与剩余工作

| 批次 | 指示周期 | 实现 | 验收 |
|---|---|---|---|
| A：测量与地基 | SITL 已完成；JIL 归 H1 | 算力与拓扑决策、aircraft 镜像加 ROS 2 Jazzy / `px4_msgs` / uXRCE-DDS agent、arm64 多架构构建、Jetson-in-the-loop 拓扑、周期预算与 guardian 测量、Lyrical 评估纪要 | 预算与语言结论入 `decisions.md`；arm64 镜像在 Jetson 上跑通 M1 场景 |
| B：自主层节点 | SITL 已完成 | `ros2_ws/`：感知、定位健康、局部地图（深度体素与邻近障碍点）、短时域局部规划；`autonomy/` 插件接口与桥接 | 输出带协方差 / 置信度 / `valid_until`；真值不泄漏 |
| C：第二控制路径与安全过滤 | SITL 已完成 | 外部模式出口节点、guardian 的 CBF 约束过滤、Offboard 类技能、能源可达性模型、恢复策略 v2 | M0 矩阵 M3 草案边全部有注入记录；两链路互斥 |
| D：机载推理与通信 | SITL 已完成；Jetson 部分归 H1 | edge-inference（小 VLM 事件检测）、Zenoh 传输、计算过载与网络中断场景 | 延迟预算达标；控制路径无推理；过载不降低监督周期 |
| E：准出 | SITL 已完成；H1 另验 | 四类场景集、`LocalPolicy` 影子框架、`AirspaceConstraintProvider` 真实接口、门禁与 readiness | 多种子通过；错误成功 0；影子输出只记录不执行 |

## 工作包

### 批次 A

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-01 算力与拓扑决策 | D038：SITL 在现有云主机按配额运行，CPU CLIP；JIL 独立接入 Jetson，具体拓扑待硬件 | — | SITL 已决策并测量；硬件采购与本机真栈 / 系统变更另按红线批准 |
| WP-M3-02 aircraft 镜像 ROS 2 化 | `sim/m3.Dockerfile`：Ubuntu 24.04 + ROS 2 Jazzy + `px4_msgs`（与 PX4 1.17.0 同版本）+ Micro XRCE-DDS agent + `px4_ros2_cpp`；amd64 + arm64（JetPack 6 / L4T 36）双架构；ROS 2 节点用系统 Python 3.12 或 C++，本包仍不 import `rclpy` | 01 | 同一 Dockerfile 两架构构建；SITL 中 uXRCE-DDS 桥接通、话题版本匹配；M1 场景在新镜像上回归通过 |
| WP-M3-03 Jetson-in-the-loop 拓扑 | `sim/compose.jetson.yaml`：SITL + Gazebo 在工作站，executive / guardian / autonomy / edge-inference 在 Jetson，走 `AIR_SUBNET`；时钟对齐与延迟测量探针 | 01, 02 | 先跑 M1 nominal 验证链路，arm64 准出完成完整 M1 回归；链路延迟与时钟偏差进入回执 |
| WP-M3-04 周期预算与 guardian 测量 | 定义安全监督周期与意图延迟预算（数值见 D040）；在 SITL 与 Jetson 上分别测 Python guardian 在空载 / 感知满载 / VLM 满载三档下的周期分布；CPU 隔离（cgroup / 亲和）作为对照 | 02（SITL）；03（JIL） | 预算与测量结论写入 `decisions.md`（D003 触发器）；若重写 guardian，先冻结接口测试再迁移 |
| WP-M3-05 ROS 2 Lyrical 评估 | 按 D008 条件核查 `px4_msgs`、Nav2、BehaviorTree.ROS2、`rmw_zenoh` 在 Lyrical 的稳定发布状态，纪要存 `docs/research/`；默认维持 Jazzy | — | 纪要 + `decisions.md` 重估记录；切换需另立条目 |

### 批次 B

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-06 `autonomy/` 插件接口与桥接 | `src/drone_agent/autonomy/`：`LocalPlanner` / `LocalPolicy` / `PerceptionProvider` / `WorldPredictor` 接口（参考 `embodied providers/{policy,perception}.py`，输出改为带协方差与置信度的 `WorldFact` 与带 `valid_until` 的轨迹片段）；与 `ros2_ws/` 节点经 proto / IPC 交互（新增 `drone.autonomy.v1`，M3-SITL 通过后已冻结） | 决策待办 ①；02 | 契约测试：`predicted` 事实不满足前置；`sim_truth` 来源在非裁判进程被拒；字段清单与 wire 检查通过 |
| WP-M3-07 感知节点 | `ros2_ws/src/da_perception`：颜色特征与深度测距，输出协方差 / 置信度；深度与视觉机架按 D041/D045 | 02、06 | SITL 已验真值隔离与信念输入；真实素材上的行业检测归 P4，设备传感器归 H 线 |
| WP-M3-08 定位健康节点 | `ros2_ws/src/da_localization`：EKF 状态、GNSS fix、视觉定位健康（有则用）→ `LocalizationHealth`；guardian 用它替换 M1 的单源判断 | 02, 06 | GNSS 失效 + 视觉可用的注入场景（M0 `fi.localization.gnss_lost_visual_ok`）行为可验证 |
| WP-M3-09 局部地图与短时域规划 | `ros2_ws/src/da_local_nav` 合并地图与规划：深度体素、邻近障碍集合、A* 短时域片段；TTL 按 D040 | 07 | SITL 已验候选轨迹、围栏 / 障碍约束、卡住与恢复；不等同完整语义场景图 |

### 批次 C

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-10 外部模式出口节点 | `ros2_ws/src/da_egress_ext`：只转发 guardian 新鲜授权；授权失效时停发并请求 Hold，未退出交飞控；显式撤销后只停不切模式（D039/D047） | 02、06 | SITL 已验互斥、TTL、看门狗、撤销与 ULog；不使用失效保护延期 |
| WP-M3-11 CBF 约束过滤 | `guardian/constraint_filter.py`：围栏、登记障碍与新鲜邻近障碍点的控制屏障函数过滤，作用于每条设定值 / 轨迹片段；不可行即拒绝并进入恢复；M1 的包络硬检查保留为下界 | 09, 10 | 单测：越界设定值被过滤或拒绝；SITL：接近障碍与围栏时真值轨迹不越界；过滤耗时计入周期预算 |
| WP-M3-12 Offboard 类技能 | `configs/skills/goto_local.yaml`（`skill.flight.goto_local`）、`skill.inspect.asset` 增加外部模式路径（绕障接近）；能力表加入 `external_mode`；M1 的 mission_upload 技能保持可用作降级 | 10, 11 | 技能清单完整；同一任务在两种控制模式下可编译；外部模式不可用时编译回退到航线版本或拒绝 |
| WP-M3-13 能源可达性模型 | `guardian/energy.py`：按实测消耗率、距离、风与余量估计 `rtl_reachable` / `nearest_site_reachable`，带不确定性；缺输入为 unknown → 保守边；仅 v2 使用，M1/M2 的 v1 计算保持不变 | 07 | 注入低电场景在可达 / 不可达两种上下文下选边正确；unknown 不选乐观边 |
| WP-M3-14 恢复策略 v2 | `configs/recovery_policies/multirotor_m3_v2.yaml`：并入 M0 草案的 `observation_stale.offboard`、`trajectory_stale.offboard`、`localization.gnss_lost_visual_ok`；新增 `progress_stalled`、`compute_overloaded`、`autonomy_unavailable`（降级到 mission_upload 或 hold）；每条边一个注入场景；切回高性能路径的准入条件按 `03-safety.md` §3 | 10–13 | `unverified_edges()` 为空才进生产；每条边 3 种子绑定记录 |

### 批次 D

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-15 edge-inference | `ros2_ws/src/da_edge_inference`：SITL 为 CLIP ONNX CPU，最多 1 Hz，候选事实与需人工审批的触发（D044）；生成式 VLM/TensorRT 归 H1 | 02、06；Jetson 部分依赖 03 | SITL 已验推理 p99 ≤1 s、杀进程不影响控制；生成式模型与 Jetson 预算仍待硬件 |
| WP-M3-16 Zenoh 传输 | `fleet/transport_zenoh.py`：`FleetTransport` 的 Zenoh 实现，启用认证；M3 在机器人 ↔ mission-service 链路上验证，P3/X1 复用车队协议；机器人内 DDS 不跨无线链路；M2 的 gRPC 实现保留 | 02 | 消息 schema 不变；断链场景（`uplink_lost`）在 Zenoh 下行为与 M1 一致 |
| WP-M3-17 计算过载与网络中断场景 | 注入：VLM 与感知满负荷、CPU 竞争进程、Zenoh 链路断开 / 抖动、DDS 话题延迟；guardian 检测自身周期超限即视为健康退化并按策略处理 | 14, 15, 16 | 四类场景（观测过期、任务卡住、网络中断、计算过载）SITL 与 Jetson 各通过；监督周期 p99 不因过载越预算 |

### 批次 E

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M3-18 `LocalPolicy` 影子运行框架 | `autonomy/shadow.py`：并行运行学习型策略，只记录其候选轨迹与确定性规划器的差异、包络违反率、裁判认可率；`implementation.learned_stage=shadow` 的技能永不执行 | 06, 09 | 契约测试：影子阶段 `may_execute` 为假；影子报告格式进入 `08-evaluation.md` §6 |
| WP-M3-19 `AirspaceConstraintProvider` 真实接口 | `admission/airspace.py`：定义 UOM 查询 / 报备状态 / Remote ID 的接口与录制后端（实际流程由 H2 执行）；准入在真实模式要求 `filed` 状态 | M2 WP-06 | 录制响应驱动的准入测试；真实模式无报备 fail closed |
| WP-M3-20 门禁与 readiness | `configs/scenarios/m3_suite.yaml`、`scripts/verify_m3_release.py`、`docs/m3-readiness.md`；`eval/BASELINES.md` 追加周期 / 延迟 / 四类场景结果 | 14, 17 | 同一 SHA；多种子；错误成功 0；回放一致；Jetson 与 SITL 分别留证 |

## 设计边界

- 单一控制出口不变：外部模式节点是 guardian 出口的延伸，不是第二个仲裁者。它没有任务语义，只做「有授权就转发、无授权就停」。guardian 仍是唯一决定写与不写的进程，两条链路（MAVLink 经 MAVSDK、uXRCE-DDS 经节点）互斥。
- 自主层节点只提交候选目标 / 轨迹，与学习型插件走同一条约束过滤路径；换实现不改变权限、超时、空间约束与证据要求。
- 控制路径不含任何模型推理；VLM 只产生 `candidate` 事实与重规划触发。
- 恢复策略 v2 是新文件；M1 的 `multirotor_m1_v1` 保留为 mission_upload 模式的生产策略，M3 在自主层不可用时降级到它。
- Jetson 上的验证与云端 SITL 的验证分别留证；arm64 镜像跑通不等于真机就绪（H2）。
- 不使用 PX4 失效保护延期；外部模式注册时不请求 `deferFailsafes`。

## 决策待办（动手前补 `decisions.md`）

| 编号 | 议题 | 阻塞 | 结论 |
|---|---|---|---|
| ① | 外部模式出口节点作为 guardian 出口延伸的进程边界、IPC 与看门狗语义；`drone.autonomy.v1` 包的范围 | WP-M3-06、10 | D039 |
| ② | M3 算力与拓扑（云 GPU / 本地工作站 / Jetson）与预算；D023 云端默认是否调整 | WP-M3-01 | D038：SITL 线留在共享云主机，JIL 线待硬件 |
| ③ | 安全监督周期与意图延迟预算数值；guardian 语言结论（D003 触发器） | WP-M3-04 | D040（数值与方法；M3-SITL 语言结论已补记：维持 Python，guardian 须独占 CPU 配额；JIL 结论待硬件） |
| ④ | Gazebo 传感器配置（深度 / 双目）与感知模型选型 | WP-M3-07 | D041 |

另补 D042：恢复策略 v2、CBF、能源可达性、任务卡住与计算过载的安全语义。

## 验收记录要求

在 M1 / M2 要求之上，每个场景另记：运行拓扑（SITL 本地 / 云 / Jetson-in-the-loop）、控制模式（mission_upload / external_mode）、自主层节点版本、模型版本（检测器、VLM）、监督周期分布、意图延迟分布、VLM 延迟分布、CPU / 内存占用、两条飞控链路的写入计数。影子运行输出单独归档，不与执行结果混记。证据浏览器（`08-evaluation.md` §7）增加自主层与 edge-inference 延迟序列、两条飞控链路的写入计数与监督周期分布；影子输出作为独立序列展示，不与执行轨迹混色。

## 不在 M3 范围

真机与实际空域流程（H2）；资源与工作流（P1/P2）；多 UAV 调度（P3）；空间对齐、rover 和 Nav2（X1）；学习策略有限接管（X3）。

## 新路线映射（D049）

旧 ID 保留，既有 SITL 证据不迁移或改名；H1 只是剩余工作的活动入口。

| 原工作包 | 当前状态与承接 |
|---|---|
| WP-M3-01 | SITL 拓扑决策已完成；Jetson 设备 / 拓扑进入 WP-H1-01 |
| WP-M3-02 | amd64 已验证；arm64 构建定义尚未运行 → WP-H1-01 |
| WP-M3-03 | JIL 拓扑与设备回归未完成 → WP-H1-01 |
| WP-M3-04 | SITL D040 结论已归档；Jetson 满载与热 / CPU 隔离重测 → WP-H1-02 |
| WP-M3-05 | Lyrical 评估完成，维持 Jazzy（D043），继续按重估触发器跟踪 |
| WP-M3-06 | 自主层接口与 wire 已实现 / 冻结，后续保持兼容 |
| WP-M3-07 | 确定性色标感知已在 SITL 验证；行业分析由 P4 扩展，硬件相机另验 |
| WP-M3-08 | 定位健康已在 SITL 验证；设备输入特性在 H1/H2 复验 |
| WP-M3-09 | da_local_nav 地图 / 规划已在 SITL 验证 |
| WP-M3-10 | 外部模式出口已在 SITL 验证；目标硬件链路 H1/H2 分别验证 |
| WP-M3-11 | CBF 已在 SITL 验证；新平台不能直接继承结论 |
| WP-M3-12 | 局部技能与航线降级已在 SITL 验证；P 线按场景选择 |
| WP-M3-13 | v2 能源可达性已在仿真验证；实际电池与风模型由 H 线重测 |
| WP-M3-14 | 21 条 v2 边已绑定 `75382dc`；内容改变后重验 |
| WP-M3-15 | CLIP/CPU 候选事件通路已验；生成式机载 VLM/TensorRT → WP-H1-02 |
| WP-M3-16 | Zenoh 单机子集已验；P3/X1 多机可靠性另验 |
| WP-M3-17 | SITL 四类故障已验；Jetson 同矩阵 → WP-H1-02 |
| WP-M3-18 | 影子框架已实现，仅记录；学习策略收益和有限接管归 X3 |
| WP-M3-19 | 空域接口与录制后端已实现；live UOM / 实际流程 → H2 |
| WP-M3-20 | SITL 门禁通过；具体 JIL 回执校验与 H1 readiness → WP-H1-03 |

`verify_m3_release.py` 目前对 JIL 是占位评估，输入一个 `--jil` 文件不能关闭 M3。H1 实现具体验证后，原总门禁才可满足；P 系列新增独立门禁，不修改历史总结果。
