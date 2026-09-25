# M4 实施与验收计划

**状态：原 M4 工作包保留，活动阶段改为 H2 与 X1（D049）；尚无真机或空地联合验收。** 当前 M2 和 M3-SITL 已通过各自仿真门禁，JIL 待 H1。最新排期与任务见[路线图](roadmap.md)和[运营实施任务](operations-implementation.md)。

A 线 11 个工作包整体由 H2 承接；B 线 10 个工作包由 X1 承接，其中能力发现、所有权和预约的通用部分提前在 P1/P3 实现，X1 复用并补空地语义。两线证据分别留存，互不证明。

启动条件：H2 的台架 / 设备执行依赖 H1 和对应控制路径的 SITL 准入，硬件方案可提前准备；X1 依赖 P3 及其需要的 M3-SITL 能力，**不依赖 arm64 或 JIL**。旧的“整个 M3 关闭后才能做 M4-B”停止适用。P5 不以 X1 为前置。

## 退出标准拆解

| 线 | 门槛 | 可检查形式 | 证据 |
|---|---|---|---|
| A | RC 接管、飞控失效保护、伴飞计算机断电、串口拔出全部真机验证 | 四类共因故障各在台架（不装桨）→ 系留 / 低空悬停 → 受限任务三级递进中执行；每级 ULog 证明飞控原生路径不依赖机载软件 | `docs/verification/m4a-*.json`、ULog、现场记录 |
| A | 真机纪律满足（`CLAUDE.md` 工程红线） | RC 在手、围栏与返航点已验证、首飞降速、UOM 报备、无关人员不在场，逐项有记录 | 飞行前检查单 |
| B | 交接成功率、重复执行数、任务丢失数、空间标注误差有基线 | 固定场景集 × 多种子；四项指标 + 复核完成率入 `eval/BASELINES.md` | 裁判输出 |
| B | 复核证据闭环通过 | UAV 异常候选 → 协调器生成复核任务 → rover 自判可达性 → 近距复核 → 合并报告三列；`unknown` 不出现在「已完成」 | 场景回执与报告核对 |

## 实施顺序

| 批次 | 指示周期 | 内容 | 验收 |
|---|---|---|---|
| A0（H2 准备） | 可与 P 线 / H1 并行 | 硬件选型条目、采购、实名登记 | 决策与登记记录 |
| A1 | 约 1–2 周 | 组装、台架测试、真机平台配置与能力表、真机适配器差异 | 桨叶拆除下四类共因故障台架通过 |
| A2 | 约 2 周 | 围栏与返航点验证、UOM 报备接入、飞行前检查单、真机裁判 | 系留 / 低空悬停下共因故障通过 |
| A3 | 约 2 周 | 首飞降速、受限场景任务、readiness | 受限任务多次通过，错误成功 0 |
| B1 | 约 2 周 | rover SITL 同世界、rover 运行时（executive / guardian / 技能 / 策略） | rover 单机场景通过 |
| B2 | 约 2–3 周 | 协调器四项能力、交接协议、`SpatialAlignment` | 交接状态机与对齐单测 + 双机 SITL |
| B3 | 约 2 周 | 复核证据闭环、多机器人裁判、故障注入、基线、readiness | 指标基线入账 |

上表周期保留为原工作量估算，不再合并成一个 M4 日历窗口。H2 取决于设备 / 现场，X1 排在 P3 后；H2 每级不通过即回退一级。

## A 线工作包

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M4A-01 硬件选型 | `decisions.md` 条目：Pixhawk 6 级飞控 + Jetson Orin NX 伴飞计算机，或 ModalAI VOXL 2 一体方案；机架、RC（带手动模式切换）、GCS、遥测链路、GNSS、安全开关；伴飞计算机供电与飞控隔离，串口 / 以太网连接方式 | 决策待办 ① | 条目 + 物料清单 + 供电与串口拓扑图 |
| WP-M4A-02 组装与台架 | 组装、校准、参数基线（围栏、RTL 高度、失效保护阈值只加严不放松）；台架（不装桨）跑 M1 任务链的地面部分（解锁检查、航线上传、模式观测） | 01 | 台架记录 + ULog |
| WP-M4A-03 真机平台配置与适配器差异 | `configs/platforms/px4_pixhawk6_multirotor.yaml`：真实能力表（相机、限速、续航）、串口 MAVSDK 连接；适配器差异只在 `adapters/px4_mavsdk.py` 参数化，不改控制语义；guardian 在真机模式拒绝 `--simulation` 类入口 | 决策待办 ②；02 | 平台锁与能力表通过 `test_m0_assets` 类检查；仿真专用注入入口在真机模式不可用 |
| WP-M4A-04 共因故障清单（台架级） | 四类：伴飞计算机断电、串口拔出、RC 接管、飞控失效保护触发（RC 丢失 / 低电阈值）；每类记录飞控行为、机载软件状态、恢复过程 | 03 | 每类 ULog 证明飞控原生路径独立于机载软件；guardian 账本显示不重夺控制 |
| WP-M4A-05 围栏与返航点验证 | 围栏上传到飞控并回读；home 与返航高度验证；登记表体积与飞控围栏一致性检查进入准入 | 03 | 回读一致；不一致 fail closed |
| WP-M4A-06 UOM 报备接入 | `admission/airspace.py` 真实后端（接口来自 WP-M3-19）：实名登记、飞行活动申请、起飞确认；准入要求 `filed` 状态；报备记录进入任务证据 | M3 WP-19 | 无报备任务被准入拒绝；报备记录与任务绑定 |
| WP-M4A-07 飞行前检查单与现场规程 | `docs/m4a-field-procedures.md`：RC 在手、围栏 / 返航点、首飞降速、报备、无关人员、气象上限、go / no-go；每次飞行填一份记录 | 05, 06 | 无记录不飞 |
| WP-M4A-08 真机裁判 | `src/drone_agent/eval/judge_field.py`：无 TruthWorld，依据 ULog、GNSS / RTK、现场观察分类；不可核实项显式为 `unverifiable`，不计入完成 | 03 | 三分类 + `unverifiable` 列；与仿真裁判规则对照 |
| WP-M4A-09 系留 / 低空共因故障 | 在系留或 ≤ 3 m 悬停下重复 WP-04 四类 | 04, 05, 07 | 全部通过；任何一次不通过回到台架 |
| WP-M4A-10 首飞降速与受限任务 | 平台配置限速 / 限高 / 限范围；受限任务链 `takeoff → fly_route（短）→ capture_image → return_home → land` 多次执行 | 09 | 多次通过；错误成功 0；每次有检查单、ULog、影像、账本 |
| WP-M4A-11 readiness | `docs/m4a-readiness.md`、`docs/verification/m4a-*.json`；边界：单机、受限场景、无自主层接管 | 10 | 门禁通过后更新阶段状态 |

## B 线工作包

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M4B-01 rover SITL 同世界 | `sim/`：同一 Gazebo 世界加 PX4 rover（差速 `r1_rover` 或 Ackermann）第二 SITL 实例，独立 MAVLink 端口与 `ROS_DOMAIN_ID`；真值采集覆盖两机 | 决策待办 ③ | 两机同时起动、遥测独立、真值分别可读；资源在 M3 决定的拓扑内 |
| WP-M4B-02 rover 运行时 | `configs/platforms/px4_sitl_rover.yaml`（`ground_wheeled`，`mission_upload`，无悬停语义）；技能 `skill.ground.nav_to`、`skill.ground.recheck`（接近 + 拍摄 + 质量检查）、`skill.ground.hold`；恢复策略 `rover_campus_v1`（stop / hold / return_to_dock）；executive / guardian 复用同一代码，本体差异在配置与适配器 | 01 | rover 单机场景多种子通过；每条恢复边有注入记录 |
| WP-M4B-03 能力发现 | `fleet/catalog.py` 扩展：静态能力 + 1 Hz 状态；分配条件同时看技能、控制模式、限制与当前能源 / 定位 / 通信 | M2 WP-12 | 契约测试：缺能力不分配、不做假接口 |
| WP-M4B-04 `SpatialAlignment` | `fleet/spatial.py`：跨机器人坐标转换与协方差合成（Jacobian 传播或保守膨胀）；对齐质量低于阈值时交付「搜索区域 + 语义描述」而非精确点；坐标约定按 `05-world-model.md` §3 | — | 单测：协方差不缩小；低质量输出为区域；地图版本不一致拒绝 |
| WP-M4B-05 任务所有权与交接协议 | `fleet/handoff.py` + `proto/drone/fleet/v1` 兼容增 `HandoffAck` / `HandoffComplete`：`offered → accepted / rejected → ack（确认业务所有权；接收方按本机水位新建控制 lease_epoch）→ completed / timeout`；任一时刻单一所有者；超时回协调器先对账；未证明原执行者停止时保留保守占用，不直接重分配冲突任务 | 决策待办 ④；03, 04 | wire 检查通过（只增）；状态机单测：重复 offer、迟到 accept、超时后 accept 全部拒绝 |
| WP-M4B-06 时空资源预约 | `fleet/reservation.py`：降落区、通道、观察位置的 `{resource_id, holder, time_window, spatial_bound}`；失联机器人的预约保留保守包络直到对账 | 03 | 冲突预约被拒；失联包络随时间增长 |
| WP-M4B-07 复核证据闭环 | 场景：UAV 巡检 → 异常候选（M2 Verifier 或 M3 机载 VLM）→ 协调器生成复核任务 → offer → rover 自判可达性（自己的地图）→ accept → `skill.ground.recheck` → 证据合并 → 报告三列（每对象带两端证据引用） | 02, 05 | 报告「已完成」只含 `succeeded ∧ verified`；rover 拒绝不可达任务；空中坐标不直接成为 rover 目标 |
| WP-M4B-08 多机器人裁判与故障注入 | 裁判读两机真值；指标：交接成功率、重复执行数、任务丢失数、空间标注误差（UAV 标注 vs 真值、rover 重定位 vs 真值）、复核完成率；注入：交接超时、接收方中途断链、重复 offer、旧所有者继续执行（租约过期 ≠ 物理停止）、预约冲突 | 07 | 每类注入行为可验证；不安全 / 错误行为 0 |
| WP-M4B-09 基线与 readiness | `configs/scenarios/m4b_suite.yaml`、`scripts/verify_m4b_release.py`、`docs/m4b-readiness.md`；`eval/BASELINES.md` 追加四项指标与复核完成率 | 08 | 同一 SHA、多种子；指标入账 |
| WP-M4B-10 Nav2 适配器（仿真，X1 后续 / H3 前置） | `adapters/nav2.py`：`nav_to_pose` / `follow_path` 能力协商；ROS 2 节点在 `ros2_ws/`；二维导航不等同三维避障 | 02；M3 WP-02 | 同一 `skill.ground.nav_to` 在 PX4 rover 与 Nav2 两种适配器下可编译；可与 H3 地面平台准备衔接，但必须在真实地面平台前完成 |

## 设计边界

- A 线上真机的代码必须先过 SITL 故障注入与 guardian 覆盖（M1–M3 证据）；共因故障从台架到系留再到受限任务逐级放开，任何一级不通过回退一级。
- A 线不启用 M3 的外部模式与自主层接管；受限任务只用 mission_upload 技能链。自主层真机接管须在 H2 之后另立场景和准入门禁。
- 真机没有 TruthWorld；裁判只核实可核实的部分，其余显式 `unverifiable`，不能靠仿真裁判规则推断。
- B 线共享的是任务、事实与证据，不共享运动控制；rover 用自己的地图判断可通行性；控制权强一致（租约 + 代次），地图事实最终一致。
- 交接的 `accepted` 不是所有权转移；只有原所有者 ack 且接收方获得新代次租约后才转移。
- 两线证据分别留存，不能互相追认。

## 决策待办（动手前补 `decisions.md`）

| 编号 | 议题 | 阻塞 |
|---|---|---|
| ① | 真机硬件选型与供电 / 串口拓扑 | WP-M4A-01 |
| ② | 真机模式下的运行入口与仿真专用注入的隔离方式 | WP-M4A-03 |
| ③ | rover 平台形态（差速 / Ackermann）与 rover 恢复行为集合 | WP-M4B-01、02 |
| ④ | 交接协议 wire 增字段与 Zenoh 下的可靠性语义 | WP-M4B-05 |

## 验收记录要求

A 线每次飞行记：检查单、报备记录、飞控参数哈希、平台配置哈希、软件 SHA、ULog、机载账本、影像、现场观察、气象、参与人员角色；共因故障每类记注入方式（物理断电 / 拔线 / RC 切换 / 飞控参数触发）、飞控行为与时间线。B 线沿用 M1 要求，另记两机真值、交接事件时间线、对齐前后协方差、预约表。证据浏览器（`08-evaluation.md` §7）在 A 线增加真机裁判的 `unverifiable` 列与检查单记录，在 B 线增加多机器人序列、交接事件带与对齐前后协方差。

## 不在 M4 范围

真实地面平台与 Nav2 真机归 X1 后续 / H3；多 UAV 调度与业务数据闭环已前移 P3/P4，不由本页的空地工作包重复实现；厂商真实接入归 X2，研究与训练导出归 X3。

## 新路线映射（D049）

| 原工作包 | 新工作入口 / 调整 |
|---|---|
| WP-M4A-01 硬件选型 | H2 准备；采购与系统动作仍须授权 |
| WP-M4A-02 组装台架 | WP-H2-01 |
| WP-M4A-03 平台配置 / 适配 | WP-H2-01；仿真入口与真实入口隔离 |
| WP-M4A-04 台架共因故障 | WP-H2-01；H1 完成不替代物理断电 / 拔线证据 |
| WP-M4A-05 围栏 / 返航点 | WP-H2-02 |
| WP-M4A-06 实际空域流程 | WP-H2-02；按目标场地和当时接口核对，录制 UOM 不替代 |
| WP-M4A-07 现场规程 | WP-H2-02 |
| WP-M4A-08 真机裁判 | WP-H2-02；不可核实项保持 unverifiable |
| WP-M4A-09 系留 / 低空故障 | WP-H2-02；逐级准入 |
| WP-M4A-10 受限任务 | WP-H2-02；首版仍 mission_upload，不启用自主层真机接管 |
| WP-M4A-11 readiness | H2 证据入口；保留旧 ID 的映射，不能宣称原 M4-B 同时通过 |
| WP-M4B-01 rover 同世界 | WP-X1-01；复用 P3 的多机隔离和裁判基础 |
| WP-M4B-02 rover 运行时 | WP-X1-01；本体技能与恢复独立验证 |
| WP-M4B-03 能力发现 | WP-P1-01/03/05、WP-P3-02 建通用部分；X1 增地面能力 |
| WP-M4B-04 SpatialAlignment | WP-X1-01；协方差与地面可达性，不并入初版 UAV 调度 |
| WP-M4B-05 所有权 / 交接 | WP-P3-03/04 先做唯一分配与对账；X1 增 offer/accept/ack 空地语义 |
| WP-M4B-06 时空预约 | WP-P1-06 最小独占，WP-P3-03 完整预约；X1 复用 |
| WP-M4B-07 复核证据 | WP-X1-01；复用 P4 业务证据模型，增加两端采集与可达性 |
| WP-M4B-08 多机裁判 | P3 提供多 UAV 基础；X1 另验交接和空间误差，不能复用通过数字 |
| WP-M4B-09 基线与 readiness | X1 独立门禁与基线 |
| WP-M4B-10 Nav2 | WP-X1-01 后续；真实地面前必须完成，对应 WP-X1-02 |

本页表内旧拟建文件名是原任务定位，实际新增时按 H2/X1 命名并保留旧 ID 映射。历史决策中的 M4/M5 标签按本映射解读；不得为了重编号复制两套目录、交接器或资源服务。
