# M3 实现与验收记录

**状态：M3-SITL 通过（2026-09-25，候选 `75382dc`）；M3-JIL 待硬件，M3 尚未关闭（D038）。** 候选 `75382dc` 通过 M3 发布门禁的全部 SITL 判据：云端检查、对抗语料、M3 场景集、M1 与 M2 回归、M2 的 Zenoh 子集、D040 测量与 v2 恢复边绑定，门禁的 `m3_sitl` 为 `passed`。`jil` 判据按设计为 missing，门禁总体为 `not_passed`，路线图不勾选 M3。

M3 的验收按 D038 拆成两条线：**M3-SITL**（共享云主机 amd64：第二控制路径、自主层、CPU 推理、四类场景、监督周期与意图延迟测量）与 **M3-JIL**（Jetson-in-the-loop：同一 Dockerfile 的 arm64 镜像在 Jetson 上运行机载侧进程）。两条线都通过才关闭 M3。本页记录 M3-SITL 的候选证据；JIL 线需要的硬件、网络与 arm64 构建方式见文末「未完成：M3-JIL」。

## 候选与入口

| 项目 | 候选信息 |
|---|---|
| 运行源码 | `75382dca07509726f4274078ce11e7e8cee064e6` |
| 云端部署 | `20260924T164104Z-9fcf8617`；控制面哈希 `4cbadb08c989aebfec297340f0e302753b22e2ddd348a46712975a8b3d65996a`；PX4 v1.17.0（`d6f12ad1`）未解锁冒烟通过 |
| 云端测试 | 983 项，0 failed / error / skipped（Linux 检查镜像） |
| 镜像 | `drone-agent-m3-aircraft` `sha256:7e44c4fe…`、`drone-agent-m3-sim` `sha256:77f2c512…`、`drone-agent-m1-ground` `sha256:6d2565b7…`、`drone-agent-m2-sim` `sha256:bbb3f268…`；每份回执记录完整摘要 |
| M3 场景集 | 8 批（每批 2 个场景 × 7 / 19 / 41）合计 **48/48 通过**：12 完成、36 合理安全中止；作废 0、错误成功报告 0、在线 / 回放一致 48；每批其他容器身份前后一致。另有 2 批因其他租户重建容器、隔离判据不成立而整批重跑，原回执功能 12/12 通过，单独保留、不计入；部署后的单例验证 `ext_inspect-7` 同样通过，作为门禁输入 |
| D040 测量 | 4 批合计 15/15 通过（事件检测关闭；连续推理；共享核心下的空载与感知满载；共享核心下的连续推理），另有 1 批隔离失效的回执不计入；独立配额的空载档取自场景集的 `route_fallback` |
| M1 回归 | 11 批（每批 2 个场景 × 7 / 19 / 41）合计 **66/66 通过**：18 完成、48 合理安全中止，错误成功报告 0，在线 / 回放一致；另有 3 份不计入：2 份隔离失效，1 份 `expired_intent-41` 起飞后仿真冻结（见下文） |
| M2 回归（gRPC） | 3 批 18/18：12 完成、6 合理不飞，错误成功报告 0，在线 / 回放一致 18 |
| M2 子集（Zenoh） | 1 批 6/6：`nl_inspect_red`、`service_outage` × 3 种子全部完成，错误成功报告 0，在线 / 回放一致 6 |
| 恢复策略 v2 | 21 条边全部绑定到本版本：9 条新边来自 M3 场景集，12 条继承边（与 v1 定义逐字节相同）来自 M1 回归；[绑定报告](verification/m3-2026-09-25/recovery-edges-bind.json) |
| 出口节点 | `da_egress_ext` 0.1.0（C++），`px4_ros2_cpp` release/1.17 @ `4a3370f0`、`px4_msgs` @ `86d8239e`，均按提交与归档哈希固定 |
| 事件检测 | CLIP ViT-B/32 视觉编码器量化 ONNX（Xenova @ `d15189d7`，SHA-256 `583fd111…`），ONNX Runtime 1.30.0 CPU；提示集 `event_prompts_v1` |
| 任务来源 | M3 场景由确定性夹具 `eval/m3_fixture.py` 生成任务包；M2 回归沿用脚本规划回答，不计模型行为 |
| 发布门禁 | [门禁结果](verification/m3-2026-09-25/release.json)：`m3_sitl: passed`，checks、adversarial、m3_suite、m1_regression、m2_regression、m2_zenoh、measurement、recovery_edges 八项全部通过（29 个输入的 SHA-256 与仓库文件一致）；`jil: missing`，总体 `not_passed`。门禁在只增加记录与文档的后代提交 `cc148a8` 上运行 |

机器可读证据在 [`verification/m3-2026-09-25/`](verification/m3-2026-09-25/)：部署回执、分批回执（含不计入的回执）、门禁批次日志、恢复边绑定报告与门禁结果。运行方法见 [云端开发指南](cloud-development.md) 的「M3-SITL 验证」。门禁通过后，按 D039 把 `drone.autonomy.v1` 写入 [wire 锁](../proto/v1-wire-lock.json)（只追加 14 个定义，原有条目不变），此后该协议的字段号与类型不得再改。

## 退出标准核对

| 门槛 | 本次证据 | 状态 |
|---|---|---|
| 观测过期、任务卡住、网络中断、计算过载四类场景行为可验证 | SITL：观测过期 3 个场景、任务卡住 2 个、网络中断 2 个（另有 M2 的 `service_outage` 在 gRPC 与 Zenoh 下）、计算过载 1 个，各 3 个种子，恢复与 v2 对应边一致；M0 矩阵中 `stage: M3` 的三条草案边（外部模式观测过期、轨迹过期、GNSS 失效但视觉可用）都有注入记录并已绑定。JIL：没有 | SITL 通过；JIL 待硬件 |
| 安全监督周期 p99 达标 | 48 例逐例 p99 ≤ 106.6 ms（预算 120 ms）；最大值除 `egress_watchdog` 外 ≤ 132.2 ms（预算 200 ms）。`egress_watchdog` 的最大值 3.29–3.49 s 就是注入的 guardian 冻结本身，按 D045 只查 p99。D040 三档（guardian 独立配额）p99 ≤ 102.5 ms、最大 ≤ 105.3 ms | SITL 通过；JIL 待硬件 |
| guardian 是否重写为 C++/Rust 有测量结论 | D040 补记：M3-SITL 维持 Python，前提是 guardian 独占 CPU 配额；与推理共用一个核心时最大周期 193.3 ms，余量只有 7 ms，不是可接受的部署配置。JIL 数据到位后再补记 | SITL 结论已记；JIL 待硬件 |
| Jetson-in-the-loop 延迟预算（`08-evaluation.md` §5） | 控制路径不含模型推理（契约测试）；SITL 上事件检测单帧推理 p99 ≤ 295.9 ms（预算 1 s）、意图延迟 p99 ≤ 175.3 ms（预算 250 ms）；Jetson 上未测 | SITL 部分满足；JIL 待硬件 |
| 第二控制路径下单一出口不变 | 契约测试钉住出口节点只转发 guardian 授权，不能解锁、起飞、降落、选择其他模式或延期失效保护，车队协议不能携带授权设定值；裁判逐例核对 MAVLink 飞行写入从不落在外部模式激活区间内、出口节点只在有效授权窗口内发布、撤销之后不再发布旧授权（D047）、非看门狗场景没有出口节点发出的 Hold，并用 ULog 核对模式迁移；48 例全部通过 | SITL 通过 |

## 已实现的范围

| 批次 / 工作包 | 实现 | 主要验证 | 状态 |
|---|---|---|---|
| A 测量与地基（WP-M3-01…05） | 拓扑与算力决策（D038：SITL 留在共享云主机，CPU 推理，不开 GPU）；`sim/m3.Dockerfile` 的 ROS 2 Jazzy 机载镜像（固定基础镜像内的 Jazzy、`px4_msgs` 与 `px4_ros2_cpp` 按提交与哈希固定、uXRCE-DDS agent、事件检测运行时），按 `TARGETARCH` 选择 ONNX Runtime 轮子（arm64 分支已写，未构建）；D040 周期预算与三档负载 × 两种隔离的测量方法；Lyrical 评估（D043，维持 Jazzy） | 每个版本在云端从固定输入构建 M3 镜像；外部模式注册为 nav_state 23、消息兼容性检查通过；D040 测量见下文 | SITL 完成；JIL 拓扑（WP-M3-03）、arm64 构建与 Jetson 上的 M1 回归待硬件 |
| B 自主层节点（WP-M3-06…09） | `autonomy/` 插件接口（`LocalPlanner` / `LocalPolicy` / `PerceptionProvider` / `WorldPredictor`）与 `drone.autonomy.v1` 本地协议（私有 Unix 套接字、按角色分套接字、`SO_PEERCRED`）；`da_localization`（PX4 估计器输出 → `LocalizationReport`，D045 / D048 的新鲜度规则）、`da_local_nav`（深度 → 体素地图 → 邻近障碍集合；A* 短时域候选片段）、`da_perception`（下视 RGB 颜色特征检测 → 带协方差的信念事实） | 契约测试：`predicted` 不满足前置、真值来源在非裁判进程被拒、缺协方差拒收、节点不导入 `drone_agent`、不触及 Gazebo 真值；节点纯逻辑在 `tests/ros2_ws/` 测试 | SITL 完成 |
| C 第二控制路径与安全过滤（WP-M3-10…14） | `da_egress_ext`（只注册 `DroneAgent Local`，只转发 TTL 内的 guardian 授权；授权过期请求 PX4 Hold；guardian 显式撤销后只停发、不切模式，D039 / D047）；guardian 的外部模式控制（悬停授权下进入、首个片段先于模式、每周期 CBF 过滤后授权；外部模式任务在出口节点与自主层交付后才开放控制，D048）；`guardian/constraint_filter.py`（CBF，边界容差 0.25 m）；`skill.flight.goto_local` 与 `skill.inspect.asset_local`，外部模式不可用时编译回退到航线版本；`guardian/energy.py` 能源可达性；`multirotor_m3@v2`（21 条边，其中 9 条新边） | 契约测试钉住出口节点的禁止项；两链路互斥与模式迁移由裁判读 ULog 核对；v2 在 `mission_upload` 上下文下与 v1 选边相同；恢复边绑定见下文 | SITL 完成 |
| D 机载推理与通信（WP-M3-15…17） | `da_edge_inference`：CLIP 视觉编码器零样本场景分类（最多 1 Hz、丢帧不排队），输出模型来源的候选事实与重规划触发，任务服务把触发转成一律需人工批准的 `candidate_event` 重规划（D044）；`fleet/transport_zenoh.py`（同一报文、TLS + mTLS、按证书通用名的默认拒绝 ACL，D046）；计算过载（规划节点自身过载）与网络中断（XRCE agent 停止、出口节点停止、任务服务停机）注入 | `edge_killed`：杀掉事件检测后任务照常完成；推理延迟见下文；Zenoh 下的 M2 子集 6/6 | SITL 完成；生成式小 VLM 与 TensorRT 属 JIL |
| E 准出（WP-M3-18…20） | `autonomy/shadow.py` 影子运行框架与裁判侧真值认可率（`eval/shadow_m3.py`，报告单独归档）；`admission/airspace.py` 的 `AirspaceConstraintProvider` 真实接口与录制的 UOM 后端（真实模式没有报备即拒绝）；`configs/scenarios/m3_suite.yaml`（16 个场景）、`eval/judge_m3.py`（真值裁判 + 回放）、`scripts/verify_m3_release.py`、`scripts/bind_recovery_edges.py`；证据浏览器的 M3 视图 | 见下文 | SITL 完成；门禁的 `jil` 判据待硬件 |

## 场景集

`configs/scenarios/m3_suite.yaml`（世界 `m3_campus_v3`：登记墙体与立柱、未登记箱体、北侧资产 `asset_green`、备用降落点 `site_b`），每个场景 × 种子 7 / 19 / 41。注入只在声明的边界施加（自主层节点输入、容器 / 进程、仿真 GNSS 接收机、guardian 运行时输入），从不改动飞控失效保护；注入类场景只承认注入之后出现的期望恢复（D045）。逐场景期望见 `m3_expectations.yaml`。

| 类别 | 场景 | 注入（边界） | 期望 | 验证的 v2 边 |
|---|---|---|---|---|
| 标称 | `ext_inspect` | — | 外部模式绕墙接近 `asset_green`、拍摄、返航，完成 | — |
| 标称 | `ext_goto` | — | 两段 `goto_local`（到 `asset_green` 附近再回起降点上方）后降落，完成 | — |
| 标称 | `route_fallback` | 不启动出口节点（外部模式真实不可用），任务包为航线版本 | 按航线完成巡检；全程没有出口发布 | — |
| 观测过期 | `ext_observation_stale` | 局部地图停止发布障碍集合（自主层节点输入） | `observation_stale / local_map_stale` → hold → rtl | `fi.m3.observation_stale.external` |
| 观测过期 | `ext_trajectory_stale` | 规划节点停止发布片段（自主层节点输入） | `trajectory_stale / no_valid_segment` → hold → rtl | `fi.m3.trajectory_stale.external` |
| 观测过期 | `egress_watchdog` | guardian 进程冻结 3 s | 出口节点因授权过期自行请求 Hold，PX4 切到 Hold | — |
| 任务卡住 | `ext_progress_stalled` | 规划节点只发新鲜但原地不动的片段（自主层节点输入） | `progress_stalled` → hold → rtl | `fi.m3.progress_stalled` |
| 任务卡住 | `cbf_blocks_unsafe_planner` | 规划节点忽略障碍（自主层节点输入） | CBF 修改目标、真值不越界，`progress_stalled` → hold → rtl | — |
| 网络中断 | `ext_dds_link_lost` | 停止 XRCE agent（DDS 链路） | `autonomy_unavailable` → hold → rtl | `fi.m3.autonomy_unavailable` |
| 网络中断 | `ext_egress_lost` | 停止出口节点进程 | `autonomy_unavailable` → hold → rtl | `fi.m3.autonomy_unavailable` |
| 计算过载 | `ext_compute_overload` | 规划节点发布后补足周期到 300 ms（自主层容器配额内） | `compute_overloaded` → hold → rtl；guardian 周期不受影响 | `fi.m3.compute_overloaded` |
| 事件检测 | `edge_killed` | 杀掉事件检测进程 | 任务照常完成 | — |
| 定位 | `gnss_lost_visual_ok` | `SIM_GPS_USED=0`（仿真 GNSS 接收机） | `localization_degraded / gnss_lost` → hold → 原地降落 | `fi.m3.localization.gnss_lost_visual_ok` |
| 能源 | `energy_rtl` | 接近目标 4 m 内电量 0.32（运行时输入） | 返航可达 → rtl | `fi.m3.energy.rtl_reachable` |
| 能源 | `energy_land_at` | 同上，电量 0.29 | 返航不可达、`site_b` 可达 → `land_at(site_b)`，真值落在降落点内 | `fi.m3.energy.land_at` |
| 能源 | `energy_land_here` | 同上，电量 0.25 | 两者都不可达 → 原地降落 | `fi.m3.energy.land_here` |

裁判 `eval/judge_m3.py` 对每个用例：用 Gazebo 真值核对与所有障碍（含未登记箱体）的净距、围栏与外部模式步骤的逐技能效果；核对 MAVLink 飞行写入从不落在外部模式激活区间内、出口节点只在有效授权窗口内发布、撤销之后不再发布旧授权（D047）、非看门狗场景没有出口节点发出的 Hold；核对期望的 v2 恢复边与后续动作、D040 的监督周期与意图延迟预算、事件检测单帧推理 p99；再从 MCAP 回放重判一次，在线与回放必须一致。外部模式用例另生成单独归档的影子报告。继承自 v1 的 12 条边由同一版本上的 M1 完整回归绑定。

## 逐例记录

计入门禁的 8 批回执（`suite.json` 汇总与逐例 `judge/result.json`）如下。「期望恢复」一栏只对安全中止的用例列出实际首个恢复；完成用例在落地上锁后 executive 结束任务、心跳消失，回执里会出现一条 `executive_heartbeat_lost`，这是 M1 以来的既有行为，不是飞行中的恢复。CBF 一栏是「修改 / 拒绝」的目标数。

| 用例 | 裁判 | 期望恢复（实际首个） | 监督周期 p99 / 最大 (ms) | 意图 p99 (ms) | 推理 p99 (ms) | CBF 修改 / 拒绝 | guardian CPU 均值 | 运行 |
|---|---|---|---|---|---|---|---|---|
| `ext_inspect-7` | 通过 · completed · 回放一致 | — | 106.6 / 132.2 | 104.7 | 265.2 | 0 / 0 | 0.108 | `m3-20260925T062721Z-24b0185c` |
| `ext_inspect-19` | 通过 · completed · 回放一致 | — | 102.3 / 128.1 | 14.5 | 210.9 | 0 / 0 | 0.101 | `m3-20260925T062721Z-24b0185c` |
| `ext_inspect-41` | 通过 · completed · 回放一致 | — | 102.5 / 123.3 | 17.2 | 208.2 | 0 / 0 | 0.095 | `m3-20260925T062721Z-24b0185c` |
| `ext_goto-7` | 通过 · completed · 回放一致 | — | 101.8 / 103.8 | 79.0 | 250.2 | 0 / 0 | 0.107 | `m3-20260925T065717Z-3d9e73a0` |
| `ext_goto-19` | 通过 · completed · 回放一致 | — | 102.4 / 105.8 | 102.5 | 231.9 | 0 / 0 | 0.108 | `m3-20260925T065717Z-3d9e73a0` |
| `ext_goto-41` | 通过 · completed · 回放一致 | — | 102.2 / 109.2 | 33.2 | 286.6 | 0 / 0 | 0.103 | `m3-20260925T065717Z-3d9e73a0` |
| `route_fallback-7` | 通过 · completed · 回放一致 | — | 101.7 / 102.5 | — | — | — | 0.075 | `m3-20260925T062721Z-24b0185c` |
| `route_fallback-19` | 通过 · completed · 回放一致 | — | 101.9 / 103.5 | — | — | — | 0.091 | `m3-20260925T062721Z-24b0185c` |
| `route_fallback-41` | 通过 · completed · 回放一致 | — | 101.7 / 102.2 | — | — | — | 0.087 | `m3-20260925T062721Z-24b0185c` |
| `ext_observation_stale-7` | 通过 · safe_abort · 回放一致 | observation_stale | 102.1 / 105.9 | 101.3 | 276.6 | 0 / 0 | 0.082 | `m3-20260925T070940Z-b1991524` |
| `ext_observation_stale-19` | 通过 · safe_abort · 回放一致 | observation_stale | 102.0 / 102.4 | 130.6 | 241.5 | 0 / 0 | 0.085 | `m3-20260925T070940Z-b1991524` |
| `ext_observation_stale-41` | 通过 · safe_abort · 回放一致 | observation_stale | 102.5 / 108.2 | 74.4 | 259.6 | 0 / 0 | 0.087 | `m3-20260925T070940Z-b1991524` |
| `ext_trajectory_stale-7` | 通过 · safe_abort · 回放一致 | trajectory_stale | 102.2 / 102.8 | 155.5 | 239.5 | 0 / 0 | 0.083 | `m3-20260925T070940Z-b1991524` |
| `ext_trajectory_stale-19` | 通过 · safe_abort · 回放一致 | trajectory_stale | 102.2 / 102.9 | 69.6 | 213.5 | 0 / 0 | 0.077 | `m3-20260925T070940Z-b1991524` |
| `ext_trajectory_stale-41` | 通过 · safe_abort · 回放一致 | trajectory_stale | 102.0 / 102.4 | 124.3 | 229.5 | 0 / 0 | 0.084 | `m3-20260925T070940Z-b1991524` |
| `egress_watchdog-7` | 通过 · safe_abort · 回放一致 | energy_low | 102.8 / 3290.6 | 129.7 | 234.9 | 0 / 0 | 0.079 | `m3-20260925T072051Z-94a40bac` |
| `egress_watchdog-19` | 通过 · safe_abort · 回放一致 | energy_low | 102.0 / 3488.2 | 66.7 | 251.1 | 0 / 0 | 0.083 | `m3-20260925T072051Z-94a40bac` |
| `egress_watchdog-41` | 通过 · safe_abort · 回放一致 | energy_low | 102.2 / 3337.9 | 113.0 | 213.8 | 0 / 0 | 0.077 | `m3-20260925T072051Z-94a40bac` |
| `ext_progress_stalled-7` | 通过 · safe_abort · 回放一致 | progress_stalled | 101.9 / 104.4 | 91.2 | 254.2 | 0 / 0 | 0.079 | `m3-20260925T072051Z-94a40bac` |
| `ext_progress_stalled-19` | 通过 · safe_abort · 回放一致 | progress_stalled | 102.4 / 103.6 | 98.9 | 226.6 | 0 / 0 | 0.078 | `m3-20260925T072051Z-94a40bac` |
| `ext_progress_stalled-41` | 通过 · safe_abort · 回放一致 | progress_stalled | 101.8 / 105.8 | 131.7 | 227.1 | 0 / 0 | 0.083 | `m3-20260925T072051Z-94a40bac` |
| `cbf_blocks_unsafe_planner-7` | 通过 · safe_abort · 回放一致 | progress_stalled | 101.7 / 103.6 | 70.2 | 227.8 | 109 / 0 | 0.097 | `m3-20260925T073224Z-9990c5b2` |
| `cbf_blocks_unsafe_planner-19` | 通过 · safe_abort · 回放一致 | progress_stalled | 101.8 / 111.0 | 51.2 | 242.2 | 110 / 0 | 0.088 | `m3-20260925T073224Z-9990c5b2` |
| `cbf_blocks_unsafe_planner-41` | 通过 · safe_abort · 回放一致 | progress_stalled | 102.6 / 111.6 | 66.8 | 230.6 | 109 / 0 | 0.086 | `m3-20260925T073224Z-9990c5b2` |
| `ext_dds_link_lost-7` | 通过 · safe_abort · 回放一致 | autonomy_unavailable | 101.9 / 102.3 | 131.1 | 252.9 | 0 / 0 | 0.092 | `m3-20260925T073224Z-9990c5b2` |
| `ext_dds_link_lost-19` | 通过 · safe_abort · 回放一致 | autonomy_unavailable | 101.7 / 102.1 | 77.2 | 199.7 | 0 / 0 | 0.077 | `m3-20260925T073224Z-9990c5b2` |
| `ext_dds_link_lost-41` | 通过 · safe_abort · 回放一致 | autonomy_unavailable | 102.0 / 102.4 | 90.4 | 207.9 | 0 / 0 | 0.073 | `m3-20260925T073224Z-9990c5b2` |
| `ext_egress_lost-7` | 通过 · safe_abort · 回放一致 | autonomy_unavailable | 101.8 / 101.8 | 156.5 | 203.1 | 0 / 0 | 0.084 | `m3-20260925T080050Z-b711585b` |
| `ext_egress_lost-19` | 通过 · safe_abort · 回放一致 | autonomy_unavailable | 101.9 / 105.5 | 175.3 | 227.7 | 0 / 0 | 0.097 | `m3-20260925T080050Z-b711585b` |
| `ext_egress_lost-41` | 通过 · safe_abort · 回放一致 | autonomy_unavailable | 102.2 / 103.4 | 62.4 | 253.9 | 0 / 0 | 0.071 | `m3-20260925T080050Z-b711585b` |
| `ext_compute_overload-7` | 通过 · safe_abort · 回放一致 | compute_overloaded | 102.0 / 103.0 | 151.7 | 263.9 | 0 / 0 | 0.074 | `m3-20260925T065717Z-3d9e73a0` |
| `ext_compute_overload-19` | 通过 · safe_abort · 回放一致 | compute_overloaded | 101.8 / 103.0 | 72.0 | 243.8 | 0 / 0 | 0.096 | `m3-20260925T065717Z-3d9e73a0` |
| `ext_compute_overload-41` | 通过 · safe_abort · 回放一致 | compute_overloaded | 103.0 / 111.9 | 111.9 | 244.6 | 0 / 0 | 0.082 | `m3-20260925T065717Z-3d9e73a0` |
| `edge_killed-7` | 通过 · completed · 回放一致 | — | 102.8 / 104.4 | 102.9 | 231.1 | 0 / 0 | 0.091 | `m3-20260925T080050Z-b711585b` |
| `edge_killed-19` | 通过 · completed · 回放一致 | — | 102.0 / 120.6 | 24.2 | 198.7 | 0 / 0 | 0.088 | `m3-20260925T080050Z-b711585b` |
| `edge_killed-41` | 通过 · completed · 回放一致 | — | 102.1 / 110.0 | 32.2 | 232.2 | 0 / 0 | 0.107 | `m3-20260925T080050Z-b711585b` |
| `gnss_lost_visual_ok-7` | 通过 · safe_abort · 回放一致 | localization_degraded | 102.3 / 112.5 | 65.9 | 239.7 | 0 / 0 | 0.083 | `m3-20260925T081446Z-4b3cfb20` |
| `gnss_lost_visual_ok-19` | 通过 · safe_abort · 回放一致 | localization_degraded | 102.0 / 102.6 | 69.1 | 215.0 | 0 / 0 | 0.077 | `m3-20260925T081446Z-4b3cfb20` |
| `gnss_lost_visual_ok-41` | 通过 · safe_abort · 回放一致 | localization_degraded | 101.7 / 106.3 | 69.3 | 231.6 | 0 / 0 | 0.091 | `m3-20260925T081446Z-4b3cfb20` |
| `energy_rtl-7` | 通过 · safe_abort · 回放一致 | energy_low | 101.7 / 102.4 | 51.1 | 210.7 | 0 / 0 | 0.080 | `m3-20260925T081446Z-4b3cfb20` |
| `energy_rtl-19` | 通过 · safe_abort · 回放一致 | energy_low | 101.9 / 103.5 | 44.0 | 229.4 | 0 / 0 | 0.104 | `m3-20260925T081446Z-4b3cfb20` |
| `energy_rtl-41` | 通过 · safe_abort · 回放一致 | energy_low | 102.1 / 104.0 | 23.4 | 235.4 | 0 / 0 | 0.084 | `m3-20260925T081446Z-4b3cfb20` |
| `energy_land_at-7` | 通过 · safe_abort · 回放一致 | energy_low | 102.5 / 103.6 | 43.5 | 242.8 | 0 / 0 | 0.096 | `m3-20260925T082814Z-16b3eb17` |
| `energy_land_at-19` | 通过 · safe_abort · 回放一致 | energy_low | 102.4 / 104.5 | 40.7 | 248.6 | 0 / 0 | 0.092 | `m3-20260925T082814Z-16b3eb17` |
| `energy_land_at-41` | 通过 · safe_abort · 回放一致 | energy_low | 102.2 / 103.3 | 89.4 | 217.3 | 0 / 0 | 0.114 | `m3-20260925T082814Z-16b3eb17` |
| `energy_land_here-7` | 通过 · safe_abort · 回放一致 | energy_low | 101.9 / 106.0 | 101.3 | 217.3 | 0 / 0 | 0.093 | `m3-20260925T082814Z-16b3eb17` |
| `energy_land_here-19` | 通过 · safe_abort · 回放一致 | energy_low | 101.7 / 103.8 | 80.2 | 229.2 | 0 / 0 | 0.089 | `m3-20260925T082814Z-16b3eb17` |
| `energy_land_here-41` | 通过 · safe_abort · 回放一致 | energy_low | 102.5 / 103.6 | 103.5 | 240.6 | 0 / 0 | 0.095 | `m3-20260925T082814Z-16b3eb17` |

几点说明：

- **没有作废用例**：本版本全部 61 个 M3 用例（含不计入的回执与部署后的单例验证）共记录 122 次仿真停顿，全部发生在 guardian 启动至少 11 s 之前的仿真启动期；飞行期间停顿 0 次。着色器缓存预热后保持 42–43 个条目（D045 第 11 条）。每个用例都在安静窗口内开始，最长等待 60 s。
- **`egress_watchdog`**：guardian 被冻结 3 s 期间，出口节点因授权过期自行请求 Hold，PX4 切到 Hold，节点在撤销到达前已经退出激活（证据顺序 `watchdog_exit → deactivated → revoked`）。guardian 解冻后的第一个周期里所有输入都已过期，电量未知按低电保守处理，返航与备用降落点都不能判为可达，于是选择 `energy_low → land_here`。场景期望只钉住出口节点与 PX4 的行为（`m3_expectations.yaml` 的注释）；真机上 guardian 自身冻结后是否应先等一两个周期拿到新鲜输入再选边，留待 M4-A 评估。
- **`cbf_blocks_unsafe_planner`**：规划节点被注入为直接穿墙瞄向目标，CBF 修改了 109–110 个目标、拒绝 0 个，真值净距始终满足，guardian 以 `progress_stalled` 中止。
- **`ext_compute_overload`**：规划节点发布片段后把周期补足到 300 ms，guardian 连续两个越限片段后以 `compute_overloaded` 中止；同一用例的 guardian 监督周期 p99 ≤ 103.0 ms，过载没有传到 guardian。
- **`gnss_lost_visual_ok`**：`SIM_GPS_USED=0` 后 GNSS 报告失锁、估计器仍融合外部视觉，guardian 走 `localization_degraded → hold → land_here`。
- **能源三例**：接近目标 4 m 内注入电量，按能源可达性分别选 `rtl`、`land_at(site_b)`（真值落在降落点半径内）与 `land_here`。

## D040 测量与 guardian 语言结论

三档负载（空载 = 无自主层的航线版任务；感知满载 = 深度、局部地图与规划、定位健康；推理满载 = 再加事件检测连续推理，把事件检测的 0.5 核配额持续占满）× 两种隔离（guardian 独立 0.6 核配额；guardian 与自主层、事件检测用 cpuset 绑到同一个核心，各自配额不变），每格种子 7 / 19 / 41。门禁只对独立配额三档设判据；同一核心的三档只报告，用于 D003 的语言结论。

| 负载 | 隔离 | 种子 | 监督周期 p99 / 最大 (ms) | 意图 p99 (ms) | 单帧推理 p99 (ms) | guardian CPU 均值 / 节流 | 自主层 CPU 均值 | 事件检测 CPU 均值 | 运行 |
|---|---|---|---|---|---|---|---|---|---|
| 空载 | 独立配额 | 7 | 101.7 / 102.5 | — | — | 0.075 / 0.000 | — | — | `m3-20260925T062721Z-24b0185c` |
| 空载 | 独立配额 | 19 | 101.9 / 103.5 | — | — | 0.091 / 0.000 | — | — | `m3-20260925T062721Z-24b0185c` |
| 空载 | 独立配额 | 41 | 101.7 / 102.2 | — | — | 0.087 / 0.000 | — | — | `m3-20260925T062721Z-24b0185c` |
| 空载 | 同一核心 | 7 | 101.8 / 103.7 | — | — | 0.089 / 0.000 | — | — | `m3-20260925T090624Z-e0da031e` |
| 空载 | 同一核心 | 19 | 101.6 / 108.0 | — | — | 0.068 / 0.000 | — | — | `m3-20260925T090624Z-e0da031e` |
| 空载 | 同一核心 | 41 | 101.8 / 103.7 | — | — | 0.090 / 0.000 | — | — | `m3-20260925T090624Z-e0da031e` |
| 感知满载 | 独立配额 | 7 | 102.0 / 104.0 | 79.0 | — | 0.105 / 0.000 | 0.277 | — | `m3-20260925T085039Z-cc8d4ec6` |
| 感知满载 | 独立配额 | 19 | 102.1 / 104.8 | 98.3 | — | 0.099 / 0.000 | 0.281 | — | `m3-20260925T085039Z-cc8d4ec6` |
| 感知满载 | 独立配额 | 41 | 101.7 / 102.0 | 45.2 | — | 0.102 / 0.000 | 0.260 | — | `m3-20260925T085039Z-cc8d4ec6` |
| 感知满载 | 同一核心 | 7 | 102.0 / 102.8 | 86.0 | — | 0.088 / 0.000 | 0.242 | — | `m3-20260925T090624Z-e0da031e` |
| 感知满载 | 同一核心 | 19 | 102.0 / 103.1 | 86.2 | — | 0.086 / 0.000 | 0.259 | — | `m3-20260925T090624Z-e0da031e` |
| 感知满载 | 同一核心 | 41 | 102.0 / 103.4 | 89.0 | — | 0.086 / 0.000 | 0.240 | — | `m3-20260925T090624Z-e0da031e` |
| 推理满载 | 独立配额 | 7 | 102.2 / 104.3 | 79.9 | 270.6 | 0.095 / 0.000 | 0.277 | 0.500 | `m3-20260925T085810Z-4b78614c` |
| 推理满载 | 独立配额 | 19 | 102.2 / 105.3 | 75.8 | 270.2 | 0.096 / 0.000 | 0.279 | 0.500 | `m3-20260925T085810Z-4b78614c` |
| 推理满载 | 独立配额 | 41 | 102.5 / 103.4 | 67.9 | 278.1 | 0.102 / 0.000 | 0.272 | 0.500 | `m3-20260925T085810Z-4b78614c` |
| 推理满载 | 同一核心 | 7 | 112.9 / 173.2 | 105.4 | 295.9 | 0.105 / 0.000 | 0.266 | 0.499 | `m3-20260925T092045Z-9d32bd83` |
| 推理满载 | 同一核心 | 19 | 102.9 / 193.3 | 70.3 | 291.5 | 0.103 / 0.000 | 0.268 | 0.499 | `m3-20260925T092045Z-9d32bd83` |
| 推理满载 | 同一核心 | 41 | 102.7 / 120.8 | 73.7 | 282.3 | 0.094 / 0.000 | 0.254 | 0.499 | `m3-20260925T092045Z-9d32bd83` |

结论（已补记于 D040）：独立配额下三档都几乎贴着 100 ms 的调度周期（p99 ≤ 102.5 ms、最大 ≤ 105.3 ms），guardian 平均只用约 0.1 核、从未被节流，因此 M3-SITL 维持 Python guardian，不按 D003 重写。与推理共用一个核心时，p99 升到 112.9 ms、最大周期到 193.3 ms，距 200 ms 的预算只剩 7 ms，因此「guardian 独占 CPU 配额 / 核心」是部署要求，写入 M4-A 的机载部署；JIL 数据到位后在 Jetson 上用同一方法重测并再补记。

## 事件检测与影子运行

**事件检测（D044）**，场景集默认档（最多 1 Hz）的 48 例合计：单帧推理 p99 最高 286.6 ms（预算 1 s）；帧到事实延迟 p99 最高 522.0 ms（含帧在队列中的等待，只作报告）。裁判用真值足迹给无歧义的帧打标签，943 帧的一致率为 0.78（只记录，不设门槛）。在 SITL 世界里不存在的触发类出现 9 次（`person` 7、`smoke_or_fire` 2），其中 1 次概率超过 0.6、成为重规划触发（`cbf_blocks_unsafe_planner-41`，贴墙时）。SITL 中它只进入信念账本；走任务服务时，这类触发只能生成需要人工批准的 `candidate_event` 版本。`edge_killed` 三例在杀掉事件检测后都照常完成任务，飞行不受影响。

**影子运行（WP-M3-18）**：外部模式的 15 个场景 × 3 种子各生成一份单独归档的影子报告 `judge/shadow.json`，影子策略是学习型实现阶段固定为 `shadow` 的直线基线 `goal_direct_baseline` 0.1.0。45 份报告共 3340 个周期，执行次数 0；按真值净距与围栏计算的认可率为 94.0%，包络违反率（CBF 会修改或拒绝的提议）为 11.6%，去掉故意注入不安全规划的 `cbf_blocks_unsafe_planner` 后为 7.4%。这些数字只记录，不参与任何判定。

## M2 回归与 Zenoh

同一版本上，gRPC 下的完整 M2 端到端 6 个场景 × 3 种子 18/18 通过（12 完成、6 合理不飞；`replan_degraded_image` 各 1 次重规划），错误成功报告 0、在线 / 回放一致 18。Zenoh 传输（D046）下的标称巡检与服务停机 × 3 种子 6/6 通过，裁判与 gRPC 相同：同一报文、TLS + mTLS、按证书通用名的默认拒绝访问控制；服务停机期间机载按已授权任务包继续，服务恢复后账本追平。

## 恢复策略 v2 绑定

`scripts/bind_recovery_edges.py` 只在某条边所指场景在被测版本上有至少三个不同种子通过（回放一致、无问题、无虚报、裁判把该边记为已验证、回执隔离成立）时写入验证记录；记录绑定边内容哈希、场景、软件版本与证据回执，边内容哈希不含记录本身。

| 来源 | 边（注入场景） | 证据 |
|---|---|---|
| M3 场景集（9 条新边） | `fi.m3.observation_stale.external`、`fi.m3.trajectory_stale.external`、`fi.m3.progress_stalled`、`fi.m3.compute_overloaded`、`fi.m3.autonomy_unavailable`、`fi.m3.localization.gnss_lost_visual_ok`、`fi.m3.energy.rtl_reachable`、`fi.m3.energy.land_at`、`fi.m3.energy.land_here` | `m3-suite/` 下对应批次，各 3 个种子 |
| M1 回归（12 条继承边，定义与 `multirotor_m1@v1` 逐字节相同） | 心跳丢失（起飞、巡航）、观测过期、租约过期、围栏预测越界、定位丢失、上行丢失、飞控失效保护、暂停超时、取消（起飞、巡航、降落） | `m1/` 下对应批次，各 3 个种子 |

v1 的两条能源边（`fi.energy.low`、`fi.energy.critical`）在 v2 中由能源可达性的三条边取代（D042）；`mission_upload` 上下文下 v2 与 v1 的选边结果相同，由单元测试钉住。

## 试跑中发现并修正的问题

M3-SITL 从首次云端构建到候选经过多轮修正。每一轮都是在云端真实飞行中暴露的问题，按根因修正，没有放宽 guardian 的安全阈值；首次判定与诊断都保留。下表列出每一轮的运行、现象、根因与修正提交；同样的内容以完整 SHA 追加在 [仿真评测基线](../eval/BASELINES.md)。

| 运行（部署 / 版本） | 现象 | 根因 | 修正 |
|---|---|---|---|
| `m3-20260924T084056Z-a8016c0d`（`c6513de`） | aircraft3 镜像构建失败：`No module named 'catkin_pkg'` | venv 的 python3 先于系统 python3 进入 PATH，ament 的 CMake 用错解释器 | `022f0fe`：ROS 构建先于 venv 进入 PATH |
| `m3-20260924T091050Z-b0e74182`（`03bf105`） | 起飞瞬间 `localization_degraded` 交还飞控；裁判以 `inf` 序列化崩溃 | 定位节点按 0.5 s 判估计器标志新鲜，而 EKF2 在无变化时 1 Hz 重发，一半时间被读成「未融合」；真值采集器按 M1 实体名 `x500_0` 过滤，M3 是 `x500_vision_0`，真值为空 | `8b97f74`：标志 1.5 s 新鲜；guardian 只采信 PX4 输入新鲜的报告。`f68d671`：采集实体名可配置，缺真值不给间距值 |
| `m3-20260924T092819Z-dab04075`（`f68d671`） | 外部模式接近中 `autonomy_unavailable`（`egress_unavailable`） | 出口节点按 500 ms 判 PX4 链路，而 `vehicle_status` 无变化时每 500 ms 发布，抖动即误判 | `4ca1923`：连续 1 s 收不到才算丢失 |
| `m3-20260924T093853Z-79b637eb`（`430b1b7`） | 飞到资产上方后拍摄被拒：`unsupported skill`；`ext_goto`、`route_fallback` 通过 | 适配器的执行分派只认 M1 的巡检技能 | `2e995db`：本地巡检的拍摄相位走同一拍摄路径，接近相位仍只由 guardian 飞 |
| `m3-20260924T095109Z-1418f1e8`（`2e995db`） | `ext_inspect` 返航途中 `observation_stale`；`ext_trajectory_stale` 意图延迟超标；`egress_watchdog` 监督周期最大值超标；`cbf_blocks_unsafe_planner` 以 `trajectory_stale` 收尾 | 仿真冻结 0.55 s；有效期内复用旧片段被计入意图延迟；注入的 3 s 冻结本身计入最大周期；CBF 把收敛到边界的 0.2 mm 越界当作「已在不安全集合内」 | `ab552bd`：意图延迟只计首次授权、冻结注入只查 p99、CBF 0.25 m 边界容差（向外推回）、仿真停顿作废规则与资源采样、SITL 上限 2.0 CPU |
| `m3-20260924T101938Z-5b199b76`（`ab552bd`） | aircraft3 构建失败：Dockerfile 解析错误 | 经 Bash heredoc 生成的续行与 `\n` 被吃掉 | `23f4cc8`：恢复续行 |
| `m3-20260924T102424Z-f493d717`（`23f4cc8`） | 三项口径修正全部生效（轨迹过期、出口看门狗、CBF 均通过）；`ext_inspect` 与 `edge_killed` 返航时停顿但未被判作废 | 作废规则只看停顿结束后的窗口，而 guardian 在冻结期间就已判定过期；注入之后的恢复一律不作废，挡住了本不期望任何恢复的杀进程场景 | `40640a2`：窗口覆盖停顿期间；只保护注入之后以场景期望原因出现的恢复 |
| `m3-20260924T105649Z-cf65881c`（`bc98b1a`） | `ext_dds_link_lost` 以 `localization_degraded` 交还飞控；`ext_compute_overload` 以心跳丢失收尾；`gnss_lost_visual_ok` 注入命令失败；三个能源场景未注入；`energy_land_here` 事件检测延迟 1001 ms | GPS 过 1 s 先判过期而 2 Hz 的状态仍「新鲜」，断链被读成 GNSS 失效；CPU 竞争进程与三个节点平分配额，无法只让规划越限；PX4 v1.17 的 gz 桥不处理 `failure gps`；拍摄相位短于注入延迟；那一帧推理 155 ms，延迟来自冻结期间取到的旧帧 | `52d2c1b`：报告按任一话题最新消息计年龄、guardian 0.3 s；规划自身过载。`4d61190`：`SIM_GPS_USED=0` 仿真接收机失锁；能源场景接近目标 4 m 内注入；事件检测按单帧推理耗时判预算 |
| `m3-20260924T123438Z-84edb27e`（`2fc9464`） | `ext_dds_link_lost` 与三个能源场景通过；`ext_compute_overload` 以 `trajectory_stale` 收尾；`gnss_lost_visual_ok` hold 中途被交还飞控 | 忙算放在发布片段之前，第一个过载周期片段间隔 500 ms 超过有效期；任务中止后 executive 退出，定位不健康时心跳丢失无匹配边而直接交还飞控 | `deb0e85`：结束任务的恢复归 guardian 独有（暂停除外）。`2834bfa`：先发布再补足周期 |
| 同上，线程级采样 | 返航交接后 Gazebo 稳定冻结约 0.5 s | 冻结期间 Gazebo 进程中单一线程满载、内存 +9 MB、llvmpipe 光栅化线程空闲；Mesa 25.2 在容器内写着色器磁盘缓存——相机首次看到新视野时 llvmpipe 在渲染线程内 JIT 编译着色器，gz-sim 传感器系统等待渲染完成；每个用例都是空缓存的新容器 | `deb0e85`：M3 SITL 挂载持久化 Mesa 着色器缓存 |
| `m3-20260924T130233Z-90ae18ee`（`deb0e85`） | 冷缓存的 `ext_inspect-7` 在外部模式前被停顿打断；热缓存的 `ext_inspect-19` 完成；GNSS ×2、杀进程 ×2 通过；规划过载 ×2 仍为 `trajectory_stale` | 同上一行的过载顺序问题；「外部模式从未激活」不在作废规则的「未执行」集合 | `2834bfa`：候选 |
| `m2-20260924T104935Z-65787caa`（`c782412`） | Zenoh 下 M2 `nl_inspect_red`、`service_outage`（种子 7）通过 | — | 作为 Zenoh 首次端到端证据，门禁计数取候选上的三种子运行 |

门禁批次中的发现：候选 `2834bfa` 的首个门禁批次 `m3-20260924T134948Z-e333d092` 里，`ext_goto-7` 被裁判判为不安全或不正确（`unexpected_egress_hold_command`）。第二个步骤刚进入外部模式 52 ms，guardian 就以「无有效片段」悬停：规划节点以 5 Hz 运行，新任务的首个片段还没到。随后出口节点又发出一条多余的 Hold：guardian 已先停止授权、再经 MAVLink 下发 Hold，但节点经 DDS 得知模式退出晚了约 0.2 s。若 guardian 当时下发的是降落或返航，这条 Hold 会把 PX4 切回 Hold。修正为 D047（`af9a16f`）：新任务拿到首个有效片段后才请求外部模式；结束外部控制时先向出口节点显式撤销，撤销后的节点只停发、不切模式。评审中另用测试复现并修正了进入阶段的竞态：确认循环会把恢复发送撤销期间才看到的模式切换记为激活。候选因此改为 `af9a16f`，门禁证据全部在新候选上重跑。旧候选的回执（含同批通过的 5 例、以及旧候选上已完成的感知满载测量档）只作诊断，不计入门禁。

`af9a16f` 的门禁批次又发现一处：批次 `m3-20260924T154904Z-708287a6` 中，`ext_dds_link_lost-7` 在起飞 0.64 s 时被交还飞控，外部模式与注入都没有发生。PX4 估计器正常融合、没有失效保护；共享主机当时 CPU 压力约 58%（同批隔离判据失效），XRCE 链路直到起飞才把 PX4 数据送到定位节点，第一份报告里估计器标志尚未到达，节点把「未知」写成「未融合」，guardian 读成 GNSS 与视觉都失效。修正为 D048（`8c58d01`）：报告携带估计器标志年龄，标志过期或从未到达、输入话题未到齐的报告不作结论；含外部模式步骤的任务在出口节点与自主层交付之前，guardian 不打开控制套接字（有上限等待）。同一版本的该批重跑 6/6 通过，但候选仍改为 `8c58d01`，证据全部重跑；`af9a16f` 上已跑的用例只作诊断。

`8c58d01` 本身又引入一处启动顺序错误：为等待自主层，把出口节点等待挪到了 guardian 构建之后，而构建时要按实时能力校验外部模式任务包，于是首批 `m3-20260924T162300Z-599d2318` 的三例 `ext_inspect` 都以 `guardian did not become ready` 失败（guardian 因 `unsupported capability` 退出），航线版的 `route_fallback` 不受影响。`75382dc` 改为先等出口节点连通再构建 guardian，其余等待时间留给自主层；新增的启动器测试在 `8c58d01` 上复现该崩溃。本地测试原先没有覆盖这段启动顺序。

## 边界与诚实说明

- **M3-JIL 缺席**：项目内没有 Jetson，arm64 镜像只有构建定义（`TARGETARCH` 分支与 ONNX Runtime arm64 轮子的固定哈希），未构建、未运行；`sim/compose.jetson.yaml`、Jetson 上的 M1 nominal、Jetson 满载下的 D040 重测都没有做。门禁的 `jil` 判据为 missing，M3 未关闭，路线图不勾选 M3。M4-A 真机仍以 M3-JIL 通过为前提（D038）。
- **事件检测不是生成式 VLM**：M3-SITL 用 CLIP ViT-B/32 视觉编码器做零样本场景分类（D041 / D044），验证的是真实推理负载、1 s 预算与「只产生候选」通路；≤ 8B 生成式 VLM 的机载评估属于 JIL（TensorRT）。与真值的一致率只记录不设门槛（见上文），类别只有三色标记与空地，`person` / `smoke_or_fire` 在 SITL 世界中不存在，出现即误报并照实计数。
- **感知是确定性的**：资产检测用颜色特征，局部几何用深度 → 体素 → 邻近障碍点（D041）；YOLO 级学习检测器在 M4-A 有真实相机数据后选型，SITL 里不用假信号代替。
- **任务来源**：M3 场景的任务包由确定性夹具生成（`eval/m3_fixture.py`），不经规划器；规划器与编译器到外部模式技能的路径（按实时能力展开为 `goto_local + capture_image`，外部模式缺席时回退航线版本或拒绝）由编译与准入测试覆盖。M2 回归与 Zenoh 子集沿用 M2 的脚本规划回答（`drone.planner.scripted/v1`），不计模型行为。
- **新镜像上的 M1 语义**：M1 完整回归在同一版本的 M1 编排与镜像上运行；新的 ROS 2 机载镜像上，`mission_upload` 路径由 `route_fallback`（外部模式不可用、航线版本飞完巡检）的 3 个种子覆盖，没有在新镜像上重跑完整 M1 矩阵。
- **共享云主机**：同机常驻约 30 个其他项目容器，其中 car-agent 频繁发布。每个用例起飞前等主机安静窗口（1 分钟负载 < 3.5 且 CPU 压力 < 15%，最多 10 分钟；到时仍不安静也照常开始，回执记为 `quiet: false`）；每批核对其他容器身份前后一致，不一致则整批作废重跑，回执全部保留。仿真停顿按 D045 作废并同版本同种子重跑，作废数量与明细见上文；越界、净距、双链路写入、虚报成功等安全或正确性问题从不作废。
- **M1 回归中的一次仿真冻结**：M1 `stale_epoch+expired_intent` 首轮中，`expired_intent-41` 起飞后 4.66 s 仿真整体冻结 0.51 s（仿真时间只前进 0.044 s），guardian 按原阈值以观测过期悬停，10 s 后返航；过期意图探针所在的航线步骤没有开始，M1 裁判判不通过。M1 裁判没有作废规则，按 M2 门禁的先例同版本整批重跑通过，原回执保留在 `not-counted/`。M1 编排没有挂载 D045 的着色器缓存，这类停顿在 M1 栈上仍可能出现。
- **仿真停顿的来源**：每个用例在 guardian 启动前至少 11 s（仿真启动期）有两次停顿，与任务无关；任务期间的停顿来自 llvmpipe 首次编译着色器（D045 第 11 条，持久化缓存后消失）或主机 CPU 争用。
- **空域**：`AirspaceConstraintProvider` 只有录制的 UOM 后端；真实报备与 Remote ID 在 M4-A 执行，真实模式没有报备一律拒绝。
- **常驻入口**：`8448` 任务台仍是 M2 自然语言任务与 M1 固定巡检，M3 外部模式没有接入常驻入口。门禁通过后按 D037 先后重新激活 `console-cloud` 与 `desk-cloud`，两者都运行 `75382dc`（部署 `20260924T164104Z-9fcf8617`），健康检查 ready、监管者空闲、两条链路版本一致；回执见 [`resident-console-activation.json`](verification/m3-2026-09-25/resident-console-activation.json) 与 [`resident-desk-activation.json`](verification/m3-2026-09-25/resident-desk-activation.json)（入口地址已脱敏）。
- **不在 M3 范围**：真机与 UOM 实际报备（M4-A）、多机器人交接（M4-B）、Nav2 地面平台（M5）、学习型策略有限接管（M6）。

## 未完成：M3-JIL

关闭 M3 还需要 JIL 线（D038），这部分依赖硬件与用户决定：

1. **硬件**：Jetson Orin NX 16 GB 开发套件（备选 Orin Nano Super 8 GB），JetPack 6.x（L4T 36.x）；SITL + Gazebo 放在与 Jetson 同一局域网的工作站，飞控链路走独立网段。采购、接入网络与工作站上的真栈都需要用户操作；在工作站启动真栈会在 JIL 线上偏离 D023，届时单独确认。
2. **arm64 镜像**：同一个 `sim/m3.Dockerfile` 已按 `TARGETARCH` 固定 arm64 的 ONNX Runtime 轮子，但 ROS 2 基础镜像与 `px4_ros2_cpp` 的 arm64 构建没有运行过。可选在 Jetson 上原生构建，或经用户批准在云主机注册 qemu binfmt 后交叉构建（系统配置变更）。
3. **验证**：`sim/compose.jetson.yaml`（WP-M3-03）；Jetson 上的 M1 nominal 与四类场景 × 3 种子；在 Jetson 满载下按 D040 同一方法重测 guardian 周期并补记语言结论；生成式小 VLM（TensorRT）的事件检测延迟评估。
4. **门禁**：`verify_m3_release.py` 目前对 `--jil` 回执一律判为未评估，JIL 线开始前先扩展门禁与回执格式。

## 复现与审计

```powershell
uv run python scripts/dev_stack.py deploy --sha 75382dca07509726f4274078ce11e7e8cee064e6 --apply
# M3 场景集：每批两个场景，与 car-agent 的发布错开安静窗口，隔离判据不成立的整批重跑、原回执保留
uv run python scripts/dev_stack.py m3 --scenario ext_inspect,route_fallback --seeds 7,19,41 --keep-going
# D040 测量档位
uv run python scripts/dev_stack.py m3 --scenario ext_inspect --seeds 7,19,41 --edge off --keep-going
uv run python scripts/dev_stack.py m3 --scenario ext_inspect --seeds 7,19,41 --edge-period 0 --keep-going
uv run python scripts/dev_stack.py m3 --scenario route_fallback,ext_inspect --seeds 7,19,41 --edge off --isolation shared --keep-going
uv run python scripts/dev_stack.py m3 --scenario ext_inspect --seeds 7,19,41 --edge-period 0 --isolation shared --keep-going
# M2（gRPC 与 Zenoh）与 M1 回归
uv run python scripts/dev_stack.py m2 --scenario nl_inspect_red,service_outage --seeds 7,19,41 --transport zenoh
uv run python scripts/dev_stack.py m2 --scenario nl_inspect_red,nl_inspect_blue --seeds 7,19,41
uv run python scripts/dev_stack.py m1 --scenario nominal,duplicate --seeds 7,19,41
# 恢复边绑定与门禁（在只增加记录与文档的后代提交上运行）
uv run python scripts/bind_recovery_edges.py --sha 75382dca07509726f4274078ce11e7e8cee064e6 --m3 <计入的 M3 场景集回执> --m1 <M1 回执> --apply
uv run python scripts/verify_m3_release.py --sha 75382dca07509726f4274078ce11e7e8cee064e6 --deployment <部署回执> --m3 <场景集与测量回执> --m1 <M1 回执> --m2 <gRPC 回执> --m2-zenoh <Zenoh 回执> --output <门禁结果>
```

门禁的输入哈希按字节计算，回执以 LF 存储。隔离判据不成立的回执，以及 M1 中因仿真冻结判不通过的那一批，不作为门禁输入，单独保存在证据目录的 `not-counted/` 下；先前候选的回执在 `diagnostics/` 下，只作诊断。
