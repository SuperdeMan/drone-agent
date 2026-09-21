# drone-agent 路线图

> 推进原则：按「通过什么验证才能进入下一阶段」推进，不按「演示界面能动」推进。周期按 1–3 人团队估算，是指示性排期而非承诺；退出标准是硬门槛。研究支线（VLA / 世界模型 / 合成数据）与工程主线并行，不是 M1–M4 的前置条件。

## 阶段总览

| 阶段 | 名称 | 指示周期 | 核心交付 | 退出标准（硬门槛） |
|---|---|---|---|---|
| **M0（已完成）** | 领域边界与基础契约 | 2026-09-19 完成（原估约 3 周） | 独立仓库、规范、架构文档、六类契约、proto 草案、技能草案、注入矩阵、复用清单、未解锁环境冒烟 | 258 项测试通过；红线测试、通用/飞行边界与实际环境证据齐备 |
| **M1（已完成）** | 无大模型的单机安全闭环 | 2026-09-20 完成（原估 6–8 周） | PX4 SITL + Gazebo；executive / guardian 双进程；五技能；恢复策略 v1；MCAP/ULog/事件与回放；独立裁判；v1 wire 冻结 | `eefe76e`：400 项测试、22 场景 × 3 种子 66/66 通过；错误成功报告 0；14 恢复边覆盖；[验收范围](m1-readiness.md) |
| **M2** | 接入受约束 Agent | 2026-12 → 2027-01（约 6–8 周） | Provider 移植；Planner（结构化输出 `MissionSpec`）；Compiler / Admission / ApprovalRecord；有界重规划；Evidence Verifier（确定性 + VLM 业务判断）；任务控制台 v0；A2A 任务入口 | 对抗性规划测试（错误坐标 / 能力 / 参数 / 顺序、提示注入、扩大范围）全部被准入拦截；`unknown` 不进入依赖步骤；`MissionSpec` 一次通过准入率有基线 |
| **M3** | 局部自主与降级 | 2027-02 → 2027-04（约 8–12 周） | ROS 2 Jazzy 集成（uXRCE-DDS、px4_ros2 外部模式路径）；感知 / 定位健康 / 局部 ESDF / 短时域规划；CBF 约束过滤；能源可达性与恢复策略 v2；机载容器（arm64）与 Jetson-in-the-loop；Zenoh；机载小 VLM 事件检测；`AirspaceConstraintProvider` 桩 → 真实接口 | 观测过期、任务卡住、网络中断、计算过载四类场景行为可验证；安全监督周期 p99 达标；guardian 是否需重写为 C++/Rust 有测量结论 |
| **M4** | 两条验证线并行 | 2027-04 → 2027-06（约 8–10 周） | **A**：单机受限真机（Pixhawk 6 级 + Jetson Orin NX 或 VOXL 2），UOM 报备，共因故障清单；**B**：一架 UAV + 一台 rover 的联合仿真（同一 Gazebo 世界，PX4 rover SITL），Coordinator 四项协同能力，交接协议，空间对齐，复核证据闭环 | A：RC 接管、飞控失效保护、伴飞计算机断电 / 串口拔出全部真机验证；B：交接成功率、重复执行数、任务丢失数、空间标注误差有基线，复核证据闭环通过 |
| **M5** | 真实空地协同 | 2027-07 → 2027-09（约 8–12 周） | 明确授权与受控范围内的「巡检—发现—复核—报告」；Nav2 地面平台接入；报告三列（已完成 / 未完成 / 不确定） | 部分完成不被报告为全部完成；设备失联、退出、交接失败时正确收尾；三分类结果与仿真基线可比 |
| **M6** | 模型与平台扩展 | 2027-Q4 起，持续 | VLA / 世界模型插件（影子 → 有限接管）；第二平台（DJI Cloud API）；多机调度（LLM 提议 + 优化器裁决）；数据飞轮；抽出 `agent-kernel`（触发器见 D001） | 相同任务与裁判下新增能力有可量化收益且无不可接受的安全 / 可靠性退化 |

M1 提前于估算关闭；M2–M4 的窗口保持指示性，实际起点以前一阶段的 readiness 为准。三段的可执行拆解（批次、工作包、验收判据、决策待办）见 [M2 实施计划](m2-implementation.md)、[M3 实施计划](m3-implementation.md)、[M4 实施计划](m4-implementation.md)（D026）；每段动手前先补对应的 `decisions.md` 条目。

## M0 · 领域边界与基础契约（已完成，2026-09-19）

已完成（2026-09-19）：

- [x] 仓库与规范：`CLAUDE.md`、`AGENTS.md`、`docs/decisions.md`（D001–D022）
- [x] 架构文档 `docs/architecture/00–08`
- [x] 前沿调研与 GPT-6 Pro 评估摘要 `docs/research/`
- [x] 六类契约 Pydantic 模型 `src/drone_agent/contracts/` 与契约测试 `tests/contracts/`
- [x] 复用清单 `docs/reuse-from-embodied-agent.md`
- [x] 示例配置：`configs/planner_tools.yaml`、`configs/recovery_policies/multirotor_campus_v1.yaml`、`configs/platforms/px4_sitl_multirotor.yaml`

本次补齐：

- [x] proto 骨架：`proto/drone/control/v1`（executive ↔ guardian）、`proto/drone/fleet/v1`（车队协议）、共享契约与自动字段清单；48 个模型 / 316 个字段；编译和 wire 边界测试通过
- [x] Linux 开发环境：Docker Linux 后端、ASCII 暂存、固定版本镜像构建与 compose 冒烟通过；PX4 v1.17.0 / Gazebo Harmonic 8.15.0，连续未解锁遥测与世界时钟验证通过，见 [核对记录](m0-readiness.md)
- [x] 在 embodied-agent 的 `decisions.md` 追加 D006 重估记录：D016，维持复制改造，公共内核等待双场景契约验证
- [x] M1 技能清单：五个 [SkillManifest 草案](../configs/skills/) 与 [行为说明](m1-skill-catalog.md)；恢复策略 15 条边的 [注入矩阵](../configs/scenarios/m0_fault_matrix.yaml) 和 8 类运行时故障，静态覆盖通过
- [x] 补齐契约回归：撤销后旧代次、续期身份、嵌套参数、审批哈希/有效期/范围、任务空间/时间/能源边界与 UTC 哈希、编译 DAG；恢复场景引用与实际验证记录分离（D021）

M0 已关闭：`ruff`、默认 importlib 模式下的 258 项测试、proto 编译与未解锁环境冒烟均通过。M0 的 15 条恢复边保留为草案；M1 闭环使用后续独立策略。历史证据见 [核对记录](m0-readiness.md)。

## M1 · 无大模型的单机安全闭环（已完成，2026-09-20）

六项交付已完成并通过 [M1 版本限定验收](m1-readiness.md)。实现顺序见 [实施计划](m1-implementation.md)；历史 M0 云端证据保持独立。当前验收使用 1× 请求时钟，加速入口独立保留；Offboard 物理控制与视觉定位降级仍在 M3。

任务：

- [x] `sim/`：sim / ground / aircraft 三镜像 Compose，固定 PX4/Gazebo；显式时钟倍率与实测记录。
- [x] `guardian`：新鲜度、活性、租约、围栏、能源、模式仲裁；恢复图、持久化出口与 PX4 mission_upload 适配器。
- [x] `executive`：DAG、生命周期、本地权威事件、MCAP；取消与安全收尾分离。
- [x] 五个基础技能及完整清单，真实 RGB 影像与逐航点/返航等待/着陆证据。
- [x] 场景 DSL、故障注入、独立 Gazebo 真值裁判与基线账本。
- [x] MCAP 重建事件、完整离线重判、v1 字段与 RPC 签名冻结。

退出标准见总览；额外要求：guardian 与 executive 的 proto 契约冻结为 `v1`。

补遗（2026-09-21）：人工核对入口。`scripts/dev_stack.py runs` / `fetch` 只读列出并拉取云端运行、核对摘要；`drone_agent.eval.viewer` 生成离线证据浏览器页面。它不改变 M1 结论，只让人能看；原则与扩展点见 [评测体系](architecture/08-evaluation.md) §7，操作见 [云端开发指南](cloud-development.md)。

## M2 · 接入受约束 Agent

M1 人工体验补遗 D027 已增加固定任务的 [实时仿真入口](live-simulation.md)，部署与受影响场景验证见 [验收记录](live-console-readiness.md)。这不关闭 M2 的自然语言、审批、签名上行或完整控制台工作包。

拆解见 [M2 实施计划](m2-implementation.md)：四个批次、20 个工作包；批次 A / B 的确定性部分在本机完成，服务联调与飞行在云端。原任务清单（Provider 移植；Planner `claude-opus-5` 结构化输出 `MissionSpec` 与 `refusal` 回退；MCP 只读工具；Compiler / Admission；ApprovalRecord 与控制台审批流；Evidence Verifier；有界重规划；A2A 入口；对抗性规划测试集）全部映射到下列工作包。

任务：

- [ ] 批次 A 规划层地基：Provider 移植与录制回放（WP-M2-01）；结构化 Issue 与 scope（WP-M2-02）；MCP 只读工具（WP-M2-03）；Compiler（WP-M2-04）；Admission（WP-M2-05）；`AirspaceConstraintProvider` 桩（WP-M2-06）；SITL 能耗估计（WP-M2-07）
- [ ] 批次 B 模型接入：Planner 引擎（WP-M2-08）；自然语言对抗集（WP-M2-09）；有界重规划与审批策略（WP-M2-10）
- [ ] 批次 C 服务与入口：签名与机器人身份（WP-M2-11）；mission-service 骨架（WP-M2-12）；Evidence Verifier 与三列报告（WP-M2-13）；`skill.inspect.asset`（WP-M2-14）；任务控制台 v0（WP-M2-15）；A2A 入口（WP-M2-16）；机载 uplink 进程（WP-M2-17）
- [ ] 批次 D 准出：云端部署扩展（WP-M2-18）；E2E 场景集与裁判扩展（WP-M2-19）；准入率基线与发布门禁（WP-M2-20）
- [ ] 决策待办：签名与机器人身份；受限上行网络与 executive 无网络边界；审批策略

退出标准见总览；额外要求：远程任务包有签名与机载验签；`drone.*.v1` 只增字段。

## M3 · 局部自主与降级

拆解见 [M3 实施计划](m3-implementation.md)：五个批次、20 个工作包；测量与算力决策先于功能。当前共享云主机不足以同时承载 SITL + ROS 2 自主层 + VLM，批次 A 必须先决定算力拓扑（D023 重估触发器）。原任务清单（`ros2_ws/` 节点；px4_ros2 外部模式第二控制路径，不使用失效保护延期；CBF 约束过滤；能源可达性模型；机载容器多架构与 Jetson-in-the-loop；Zenoh；机载 VLM 事件检测；ROS 2 Lyrical 评估；`LocalPolicy` 影子运行框架）全部映射到下列工作包。

任务：

- [ ] 批次 A 测量与地基：算力与拓扑决策（WP-M3-01）；aircraft 镜像 ROS 2 Jazzy 化与 arm64（WP-M3-02）；Jetson-in-the-loop 拓扑（WP-M3-03）；周期预算与 guardian 语言测量（WP-M3-04，D003 触发器）；ROS 2 Lyrical 评估（WP-M3-05，D008）
- [ ] 批次 B 自主层节点：`autonomy/` 插件接口与桥接（WP-M3-06）；感知（WP-M3-07）；定位健康（WP-M3-08）；局部地图与短时域规划（WP-M3-09）
- [ ] 批次 C 第二控制路径与安全过滤：外部模式出口节点（WP-M3-10）；CBF 约束过滤（WP-M3-11）；Offboard 类技能（WP-M3-12）；能源可达性模型（WP-M3-13）；恢复策略 v2（WP-M3-14）
- [ ] 批次 D 机载推理与通信：edge-inference 小 VLM 事件检测（WP-M3-15）；Zenoh 传输（WP-M3-16）；计算过载与网络中断场景（WP-M3-17）
- [ ] 批次 E 准出：`LocalPolicy` 影子运行框架（WP-M3-18）；`AirspaceConstraintProvider` 真实接口（WP-M3-19）；四类场景门禁与 readiness（WP-M3-20）
- [ ] 决策待办：外部模式出口节点边界与 `drone.autonomy.v1`；算力拓扑；周期预算与 guardian 语言；传感器与感知模型

退出标准见总览；额外要求：第二控制路径下单一控制出口不变，两条飞控链路互斥。

## M4 · 两条验证线并行

拆解见 [M4 实施计划](m4-implementation.md)：A 线 11 个工作包、B 线 10 个工作包，并行推进，证据分别留存。A 线的硬件选型与实名登记在 M3 期间启动（A0）。

A（真机）：硬件选型与组装、UOM 报备、地理围栏与返航点验证、共因故障清单、首飞降速、受限场景任务。

- [ ] A0：硬件选型条目、采购、实名登记（WP-M4A-01）
- [ ] A1：组装与台架（WP-M4A-02）；真机平台配置与适配器差异（WP-M4A-03）；共因故障清单台架级（WP-M4A-04）
- [ ] A2：围栏与返航点验证（WP-M4A-05）；UOM 报备接入（WP-M4A-06）；飞行前检查单与现场规程（WP-M4A-07）；真机裁判（WP-M4A-08）；系留 / 低空共因故障（WP-M4A-09）
- [ ] A3：首飞降速与受限任务（WP-M4A-10）；readiness（WP-M4A-11）

B（联合仿真）：PX4 rover SITL 同世界、Coordinator 四项协同能力、交接协议、`SpatialAlignment` 服务、复核证据闭环、协同指标基线。

- [ ] B1：rover SITL 同世界（WP-M4B-01）；rover 运行时（WP-M4B-02）
- [ ] B2：能力发现（WP-M4B-03）；`SpatialAlignment`（WP-M4B-04）；任务所有权与交接协议（WP-M4B-05）；时空资源预约（WP-M4B-06）
- [ ] B3：复核证据闭环（WP-M4B-07）；多机器人裁判与故障注入（WP-M4B-08）；基线与 readiness（WP-M4B-09）；Nav2 适配器仿真版（WP-M4B-10，可延至 M5 首批）
- [ ] 决策待办：真机硬件与供电 / 串口拓扑；真机模式入口隔离；rover 形态与恢复行为；交接 wire 增字段与 Zenoh 可靠性

退出标准见总览；共因故障从台架到系留再到受限任务逐级放开，任何一级不通过回退一级。

## M5 · 真实空地协同

Nav2 地面平台接入（Unitree Go2 或轮式 rover）；真实场景巡检—发现—复核—报告；报告三列；覆盖矩阵扩展（环境、天气、光照、故障）。

## M6 · 模型与平台扩展

VLA / 世界模型插件按影子 → 有限接管 → 正式推进；DJI Cloud API 适配器（能力协商）；多机调度；数据飞轮（MCAP → 派生数据集 → 策略回归）；`agent-kernel` 抽出（D001 触发器）。

## 研究支线（与 M2 起并行）

- Pegasus / Isaac Sim 5.1 感知线搭建；Cosmos 3 合成数据评估。
- UAV VLN / VLA（AutoFly、DreamFly 类）在评测集上的影子运行与差异分析。
- 世界模型（WorldFly 类）作为 `WorldPredictor` 的候选评估价值。
- 多机器人场景图（MR-COGraphs 类）在空地对齐中的带宽 / 精度权衡。

## 节奏与机制

- 每阶段结束：更新 `CLAUDE.md` 当前阶段、`roadmap.md` 勾选、`eval/BASELINES.md` 追加。
- 季度技术雷达（1/4/7/10 月）：复查 `docs/research/frontier-2026.md` 与 `decisions.md` 重估触发器。
- 任何阶段的「跳过」必须在 `decisions.md` 记录理由。
