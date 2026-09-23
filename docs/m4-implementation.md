# M4 实施与验收计划

**状态：后续阶段的实施计划，尚无 M4 阶段验收。** 当前只完成 M2 仿真；本页的真机与联合仿真能力均待实施，见[路线图](roadmap.md)。

本轮拆解路线图的 M4「两条验证线并行」。A 线是单机受限真机；B 线是一架 UAV + 一台 rover 的联合仿真。两条线共享 M3 的 arm64 镜像、Zenoh 传输与 `AirspaceConstraintProvider` 接口，但证据分别留存：A 线的通过不证明协同能力，B 线的通过不证明真机安全。

启动条件：M3 通过验收并关闭；当前尚未满足。A 线同时受采购、场地与报备周期约束，硬件选型与实名登记应在 M3 期间提前启动（批次 A0）。

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
| A0（M3 期间启动） | 与 M3 并行 | 硬件选型条目、采购、实名登记 | 决策与登记记录 |
| A1 | 约 1–2 周 | 组装、台架测试、真机平台配置与能力表、真机适配器差异 | 桨叶拆除下四类共因故障台架通过 |
| A2 | 约 2 周 | 围栏与返航点验证、UOM 报备接入、飞行前检查单、真机裁判 | 系留 / 低空悬停下共因故障通过 |
| A3 | 约 2 周 | 首飞降速、受限场景任务、readiness | 受限任务多次通过，错误成功 0 |
| B1 | 约 2 周 | rover SITL 同世界、rover 运行时（executive / guardian / 技能 / 策略） | rover 单机场景通过 |
| B2 | 约 2–3 周 | 协调器四项能力、交接协议、`SpatialAlignment` | 交接状态机与对齐单测 + 双机 SITL |
| B3 | 约 2 周 | 复核证据闭环、多机器人裁判、故障注入、基线、readiness | 指标基线入账 |

A 线与 B 线并行，各约 6–7 周，合计落在路线图 8–10 周窗口内；A 线每级不通过即回退一级，周期只会拉长不会压缩。

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
| WP-M4B-05 任务所有权与交接协议 | `fleet/handoff.py` + `proto/drone/fleet/v1` 兼容增 `HandoffAck` / `HandoffComplete`：`offered → accepted / rejected → ack（所有权转移，接收方新 lease_epoch）→ completed / timeout`；任一时刻单一所有者；超时回协调器重分配，重分配前考虑失联机器人最后状态 | 决策待办 ④；03, 04 | wire 检查通过（只增）；状态机单测：重复 offer、迟到 accept、超时后 accept 全部拒绝 |
| WP-M4B-06 时空资源预约 | `fleet/reservation.py`：降落区、通道、观察位置的 `{resource_id, holder, time_window, spatial_bound}`；失联机器人的预约保留保守包络直到对账 | 03 | 冲突预约被拒；失联包络随时间增长 |
| WP-M4B-07 复核证据闭环 | 场景：UAV 巡检 → 异常候选（M2 Verifier 或 M3 机载 VLM）→ 协调器生成复核任务 → offer → rover 自判可达性（自己的地图）→ accept → `skill.ground.recheck` → 证据合并 → 报告三列（每对象带两端证据引用） | 02, 05 | 报告「已完成」只含 `succeeded ∧ verified`；rover 拒绝不可达任务；空中坐标不直接成为 rover 目标 |
| WP-M4B-08 多机器人裁判与故障注入 | 裁判读两机真值；指标：交接成功率、重复执行数、任务丢失数、空间标注误差（UAV 标注 vs 真值、rover 重定位 vs 真值）、复核完成率；注入：交接超时、接收方中途断链、重复 offer、旧所有者继续执行（租约过期 ≠ 物理停止）、预约冲突 | 07 | 每类注入行为可验证；不安全 / 错误行为 0 |
| WP-M4B-09 基线与 readiness | `configs/scenarios/m4b_suite.yaml`、`scripts/verify_m4b_release.py`、`docs/m4b-readiness.md`；`eval/BASELINES.md` 追加四项指标与复核完成率 | 08 | 同一 SHA、多种子；指标入账 |
| WP-M4B-10 Nav2 适配器（仿真，M5 前置） | `adapters/nav2.py`：`nav_to_pose` / `follow_path` 能力协商；ROS 2 节点在 `ros2_ws/`；二维导航不等同三维避障 | 02；M3 WP-02 | 同一 `skill.ground.nav_to` 在 PX4 rover 与 Nav2 两种适配器下可编译；可延至 M5 首批，但必须在真实地面平台前完成 |

## 设计边界

- A 线上真机的代码必须先过 SITL 故障注入与 guardian 覆盖（M1–M3 证据）；共因故障从台架到系留再到受限任务逐级放开，任何一级不通过回退一级。
- A 线不启用 M3 的外部模式与自主层接管；受限任务只用 mission_upload 技能链。自主层真机接管属于 M5 之后。
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

真实地面平台与 Nav2 真机（M5）；真实场景巡检报告（M5）；第二平台、多机调度、数据飞轮（M6）。
