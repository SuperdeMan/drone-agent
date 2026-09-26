# P3 详细方案：多站多机任务调度

[总任务表](operations-implementation.md) · [运营架构 §5](architecture/09-operations.md) · [存储设计](p3-storage-design.md) · [路线图](roadmap.md)

**状态：2026-09-26 设计生效（D059–D061），存储扩展 D060 已获批准，实施中。** P2 已在 `3bdbd50` 关闭，项目身份、预约与领取闸门（P1）、持久工作流与取消代次（P2）直接复用。P3 交付：候选硬约束过滤与确定性排序、唯一任务所有者与分配代次、按空域网格的时空预约、失联包络、领取前改派与任务级接力；验证为 S0 逻辑节点阶梯（10 / 30 / 100）与两台同世界 PX4 SITL × 3 种子。

## 1. 验收场景与范围

- **分配**：一个任务 = 一个项目内的单资产巡检需求（资产、体积、候选机器人、优先级、时间窗）。调度器对候选做硬约束过滤，按确定性键排序，选一台机器人；同一事务写入分配（带分配代次）、为该机器人编译 / 准入的任务与全部预约。每个任务仍逐个人工审批（D032），调度器没有审批身份。
- **时空预约**：资源 = P1 的 `<robot>.motion`、`<dock>.pad`、`<dock>.charger`，加上航迹覆盖的空域网格单元。单元与资源同样独占，软持有有到期时间，领取后的物理占用直到对账释放；失联的在飞机器人另有随时间增长的保守包络。
- **故障与接力**：领取前机器人持续不可用时撤回分配并改派（新任务、新审批）；已领取的任务不改派，失联先对账；飞行明确失败并对账后可改派给其他候选；已完成的子任务不重做。
- **边界**：第一版只做分区、分时与预验证航线；不做近距离编队、空中换代次、跨机场异地着陆、提前预订未来时段或地面机器人（X1）。一次飞行仍是 M2 单资产任务；每台机器人只在自己站点地图登记的资产上作业。

S0 在逻辑世界（真实机载 uplink / guardian / executive + 逻辑飞行 + 逻辑机场）中验证逻辑与规模；S1 在同一 Gazebo 世界中运行两个 PX4 SITL 实例。二者分开计数，S0 的节点数从不写成物理飞行架数。

## 2. 对现有代码的改动边界

| 当前入口 | P3 改造 |
|---|---|
| `fleet/coordinator.py` | 保留 `PassthroughCoordinator`（M2 / 固定绑定）；新增纯函数 `decide(task, snapshot)`：逐候选原因、排序键、判定 `assign / wait / reject`、快照摘要 |
| `fleet/dispatch.py`、`operations_store.py` | 加载调度目录时，活动的资源 = 机器人资源 ∪ 航迹空域单元；预约在事务内拒绝落入他人失联包络的单元；领取阶段另查单元持有与分配代次。无调度目录时行为不变 |
| `fleet/service.py` | 抽出确定性提交（P2 的 `submit_workflow` 与分配共用）；`tasks.*` 视图与入口；后台加 `tick_scheduler()` |
| `fleet/workflow*.py` | `submit_mission` 可不带 `robot_id` 而带 `candidates / priority`（分配模式），节点输出任务 ID；`await_mission` 跟随任务的改派直到了结；取消在同一事务级联到本运行的任务 |
| `fleet/api.py`、`runtime/permission.py` | `tasks.submit / list / get / cancel`，沿用项目内 `mission.*` scope；第三方、后端与匿名调用方不可用 |
| `console/mission.*` | 调度面板：任务队列、所有者与代次、等待 / 排除原因、候选排序、空域持有与包络；提交与取消任务；没有审批捷径 |
| `fleet/main.py` | `--scheduling <目录>`（需同时带 `--catalog`）；启动时按 D060 迁移 |
| `eval/dock_simulator.py`、`sim/collect.py` | 机位坐标可配置；相机话题可配置（默认值不变） |

新增：`fleet/scheduling_models.py`（调度目录、任务 / 分配 / 判定契约、原因码）、`fleet/reservation.py`（共享坐标、空域网格、航迹覆盖、失联包络）、`fleet/scheduling_store.py`（D060 迁移与读写）、`fleet/scheduler.py`（后台调度：结算、撤回、分配；任务入口）、`configs/scheduling/`、`configs/sites/p3_*`、`configs/scenarios/p3_*`、`configs/workflows/p3_*`、`eval/p3_world.py`、`eval/judge_p3.py`、`eval/p3_prepare.py`、`sim/p3.Dockerfile`、`sim/p3_setup.py`、`sim/p3_sitl.sh`、`sim/compose.p3.yaml`、`scripts/remote_p3.py`、`scripts/verify_p3_release.py`。机载 executive / guardian / 适配器 / uplink、wire、技能、平台与恢复策略不改（D061 用部署配置而不是代码区分两台机器人）。

## 3. 语义要点（D059）

**调度目录**：`drone.scheduling-catalog/v1`（`configs/scheduling/`），绑定运营目录 ID，与运营目录分开以免改变已固定的目录摘要。声明共享坐标系、每个站点地图原点在其中的平移、网格边长与航迹缓冲、失联判定与包络余量、排序规则版本与改派策略版本。加载时核对：每个站点都有原点；同一项目中被多个站点登记的资产在共享坐标中位置一致（容差 0.5 m）；每台机器人的航迹覆盖可计算。

**任务**：由 `tasks.submit`（operator）或分配模式的工作流节点创建，去重键 `(requested_by, idempotency_key)`。状态 `queued → assigned → completed / failed / outcome_unknown / rejected`，取消为 `cancel_requested → cancelling → cancelled`。改派时任务回到 `queued`，分配代次保留，下一次分配加一。

**判定（WP-P3-02）**：`decide` 是快照的纯函数，相同快照得到相同判定、原因与排序。
- 候选 = 任务显式列出的机器人，未列出时为项目内全部机器人；只评估候选。
- 硬约束（原因码分阻断 / 未知，另标永久）：P1 `evaluate` 的全部原因（机场六维、机体阶段、能力、能源含返程、环境、维护、机器人资源冲突、时间窗），加 `asset.unregistered`（资产或体积未在该机器人站点地图登记）、`volume.unapproved`、`task.robot_excluded`（本任务此前因该机失败或准入拒绝而排除）、`airspace.cell_held`（航迹单元被其他活动持有）、`airspace.envelope`（落入他人失联包络）。
- 排序：任务按（优先级降序、创建时间、任务 ID）逐个处理；候选按（预计到场时间、近一小时分配次数、robot ID）升序。预计到场 = 共享坐标中机位到资产的水平距离 / 巡航速度 + 固定起降开销，取 0.1 s。
- 有可派遣候选即 `assign` 排序第一者；全部候选都带永久原因即 `reject`；否则 `wait` 并记录每个候选的原因——无候选时从不乐观派遣。判定只在结论或原因变化时追加记录，带快照摘要、规则与策略版本。

**分配与预约（WP-P3-03）**：一个 `BEGIN IMMEDIATE` 事务内：核对任务仍 `queued` 且状态版本未变、未取消 → 在事务内重建所选候选的快照并重判（必须仍可派遣）→ 插入分配（每个任务至多一个 `active` 分配，唯一索引）→ 为该机器人确定性生成单资产草案、编译、准入，写入请求 / 任务 / 绑定 / 版本 → 预约机器人资源与航迹单元（独占索引）→ 更新任务。任一步冲突即整体回滚。任务请求键为 `<任务键>#a<代次>`，请求者沿用任务的请求者（工作流任务为 `workflow:<run_id>`，因此 P2 的取消级联与子任务查询不变）。准入拒绝时该次分配以 `admission_rejected` 结束、该机器人对本任务排除，任务回队列。

**空域网格**：共享坐标按边长 `cell_m` 划分，单元资源 ID `air.<frame>.<i>.<j>`，与 P1 资源共用独占索引。任务版本的航迹覆盖 = 其任务包引用的 home、降落点与全部航线航点经站点原点平移后的外包矩形，外扩 `buffer_m`，与之相交的全部单元。固定机器人的任务（P2 路径）在加载调度目录时同样预约航迹单元，否则分配来的飞行与之无从避让。

**失联包络**：占用中（已领取）或不确定的预约，若机器人最近状态早于 `contact_timeout_s` 且其后没有落地证据，则把航迹外包矩形按 `最大速度 × 失联时长 + 余量` 外扩、并截在该任务批准体积之内，覆盖的单元对新预约一律冲突（`airspace.envelope`）。包络在预约事务内计算；软件所有权失效与物理占用解除是两件事，只有 P1 的对账释放才解除占用。

**改派与接力（WP-P3-04）**：
- 领取前：分配的任务在准备 / 预览判定中持续阻断或未知达 `reassign_after_s`，且忽略本分配自身持有后另有候选可派遣，则在一个事务内确认该任务从未被领取、写入 P1 取消意图（原因 `assignment_withdrawn`）、分配转 `withdrawn`、释放未领取持有、任务回队列；下一轮以代次 + 1 分配给其他机器人，新任务、新编译、新审批。撤回与领取由 SQLite 写锁串行：领取先提交则撤回放弃，撤回先提交则领取闸门作废投递。
- 领取后：不改派。失联只增长包络；终态、落地与机场在位证据齐备并释放后才结算。
- 结算：完成且服务复核通过 → 任务 `completed`；报告不确定 → `outcome_unknown`（不改派，避免重复飞行）；飞行后明确未完成 → 该机排除、代次未满 `max_assignments` 时回队列（接力），否则 `failed`；人工驳回 → `failed`（不改派）；操作者直接取消任务 → `failed`；投递过期或机器人拒收（无物理动作）→ 回队列。
- 工作流：分配模式的节点逐个成为任务，已完成的任务不重做；后续节点照常以前驱为条件。

**两类代次**：`assignment_epoch` 是云端任务的分配代次，所有调度写入按任务状态版本 CAS，领取闸门另查投递所属分配仍为 `active` 且代次等于任务当前代次（否则作废，原因 `assignment_superseded`）；`lease_epoch` 仍由机载按持久化水位产生。二者从不互相复制。

**取消**：`tasks.cancel` 在一个事务中写入取消意图、为该任务未了结的任务写 P1 取消意图；之后不再分配。子任务全部了结且预约释放后才 `cancelled`。工作流取消在同一事务级联到本运行的任务。迟到的成功只记录。

## 4. 两机同世界 SITL 与容量（D061，WP-P3-01）

- **世界**：独立镜像 `sim/p3.Dockerfile`（不改 M1 / M2 镜像与世界）：x500 的下视相机不写绝对话题，Gazebo 按模型生成各自话题；世界放三个标记——`asset_west`（红，(-4,4)）、`asset_mid`（绿，(5,4)）、`asset_east`（蓝，(16,4)）。
- **实例**：一个 sitl 容器内一个 gz 服务、PX4 实例 0（机位 (0,0)，`uav_01`）与实例 1（(12,0)，`uav_02`，standalone 连接同一 gz）。镜像内的 PX4 启动脚本按环境变量把各实例的 API 链路发往各自 guardian 的固定地址，端口保持默认 14540，因此 guardian 与 MAVSDK 代码不变。
- **机载栈**：每台一套 uplink / guardian / executive、各自的证书、信任、inbox / mailbox / 控制权状态与飞行目录；机器人身份由各自的平台描述文件提供（部署时为第二台挂载其身份文件），不改代码。每台一个真值 / 相机转接与一个逻辑机场后端（机位坐标按共享坐标给出）。
- **站点地图**：`p3_s1_a_v1`（原点 (0,0)，west / mid）与 `p3_s1_b_v1`（原点 (12,0)，mid / east），航线与 M2 同构、单程不超过 7 m。网格 4 m、缓冲 2 m：A 查 mid 与 B 查 east 的单元不相交（并行），A 与 B 同查 mid 相交（分时）。
- **容量**：先跑探针（两实例空闲 60 s + 首个两机并行用例）并记录各容器 CPU / 内存 / 节流、主机负载与 CPU 压力、按真值时间戳算的实时因子、两个 guardian 的监督周期 p99。据此冻结预算写回 D061；达不到（实时因子 < 0.5 或 guardian p99 > 120 ms）则停止两机矩阵并写出具体扩容拓扑，不以两个串行单机用例代替。

## 5. API 与入口

| 方法 | scope（角色） | 参数 |
|---|---|---|
| `tasks.submit` | `mission.submit`（operator） | `project_id`, `asset_id`, `volume_id`, `candidates`, `priority`, `idempotency_key` |
| `tasks.list` | `mission.read`（viewer 起） | `project_id` → 队列、各机器人当前分配与预览判定、空域持有与包络 |
| `tasks.get` | `mission.read` | `project_id`, `task_id` → 分配历史、判定（逐候选原因与排序键）、任务 |
| `tasks.cancel` | `mission.operate`（operator） | `project_id`, `task_id`, `request_id`, `reason` |

不可读的项目与任务一律 `service.not_found`；候选必须属于本项目。任务台 hri.v0 增加 `tasks` / `task` 下行帧与 `tasks_watch`、`task_watch`、`task_submit`、`task_cancel` 上行帧；审批仍在原任务视图按任务包哈希逐个进行。

## 6. 工作包落位

| 工作包 | 落位 | 完成判据 |
|---|---|---|
| WP-P3-01 容量与多机拓扑 | `sim/p3*`、`sim/compose.p3.yaml`、`scripts/remote_p3.py`、D061 | 两实例各自 executive / guardian / uplink、真值、证书与目录；探针记录与冻结预算；不足时的扩容方案 |
| WP-P3-02 确定性分配 | `fleet/coordinator.py`、`fleet/scheduling_models.py` | 同快照同结果；最近候选不可用时给出排除原因；无候选等待 / 拒绝 |
| WP-P3-03 分配与时空预约 | `fleet/reservation.py`、`fleet/scheduling_store.py`、`fleet/dispatch.py` | 双 worker 同抢只一个所有者；起降 / 通道单元重叠拒绝；租约过期不释放物理占用 |
| WP-P3-04 故障对账与接力 | `fleet/scheduler.py`、`fleet/workflow.py` | 旧所有者未停止不交出冲突区域；换机重编译 / 审批；已完成子任务不重做 |
| WP-P3-05 规模与双机裁判 | `eval/p3_world.py`、`eval/judge_p3.py`、`configs/scenarios/p3_suite.yaml` | 下列矩阵与阶梯；S1 两机 × 3 种子；五项计数全 0，延迟与等待分布分层记录 |
| WP-P3-06 调度入口与准出 | `console/`、`scripts/verify_p3_release.py`、`docs/p3-readiness.md` | 队列与排除原因可见；同候选受影响回归；两机准出证据齐备 |

## 7. 故障与验收矩阵

S0（`eval/p3_world.py`，目录 `p3_campus_v1`：两组相邻站点 a/b、c/d 相距 100 m，外加隔离项目 h；每项 × 种子 7 / 19 / 41，独立裁判在线与按录制各判一次）：

| ID | 注入 / 操作 | 期望 |
|---|---|---|
| P3-F01 | mid 与 east 同时提交 | mid → 最近的 a、east → b，两机并行；判定按录制快照重放一致 |
| P3-F02 | a 维护 / 补能中 / 离线（按种子）时提交 mid | 选 b，a 的排除原因入判定 |
| P3-F03 | 资产不在任一候选登记；唯一候选被阻断 | 前者拒绝；后者等待且不建任务，解除后分配并完成 |
| P3-F04 | 两个调度器同时处理同一队列；过期快照的调度器事后提交 | 每个任务一个所有者、每台机器人一个活动；迟到提交被拒 |
| P3-F05 | 两个 mid 任务，c/d 组同时有任务 | 第二个 mid 因单元持有等待，a 释放后由 b 执行；c/d 组并行 |
| P3-F06 | a 飞 mid 途中链路中断 | 包络覆盖 b 的 east 单元时 b 等待（`airspace.envelope`），c/d 组照常；链路恢复对账后 b 执行 |
| P3-F07 | a 领取回执丢失、落地无结果、服务重启 | 预约不确定期间任务仍归 a、不改派、单元保持；证据到齐后释放 |
| P3-F08 | a 分配后、领取前机场维护 | 超过改派时限撤回（a 的任务取消、从未领取），b 以代次 2 执行；对旧任务的审批被拒 |
| P3-F09 | 撤回与领取竞态（两种先后） | 每种先后恰好一次飞行，无双重所有者 |
| P3-F10 | 旧代次的审批、领取与调度提交 | 全部拒绝或作废，无效果 |
| P3-F11 | 分配模式的巡回：mid 由 a 完成后 a 故障 | 第二个任务接力给 b，mid 不重做，运行完成 |
| P3-F12 | a 飞行明确失败 | 对账后排除 a、代次 2 由 b 完成；两台都失败时到上限即失败 |
| P3-F13 | 排队中 / 待审批 / 飞行中取消任务；工作流取消级联 | 取消后不新建、不领取；飞行中取消对账后才 `cancelled` |
| P3-F14 | 跨项目提交 / 读取 / 取消、项目外候选、viewer 提交、A2A 提交 | 全部拒绝且不泄漏 |
| P3-F15 | 审批人驳回 | 任务失败，不改派 |

规模阶梯（`p3_l10`、`p3_l30`、`p3_l100`，种子 7）：N 个逻辑节点（每节点一台逻辑 UAV + 一个逻辑机场），两两成组；目录与站点地图由确定性生成器写入 `outputs/` 并记录摘要。闭环任务流保持不超过 S0 并发预算的在途任务，共 2N 个任务（共享、独占与不可行混合，另注入维护造成等待）。记录分配延迟与判定耗时 p50 / p95 / p99、每轮耗时、等待原因与拒绝分布、在途任务曲线与主机规格；三档分开报告，不混算。

S1（`scripts/remote_p3.py`，两 PX4 SITL + 两逻辑机场，同一正式服务）：`p3_s1_parallel` × 7 / 19 / 41（mid → uav_01 与 east → uav_02 同时飞行），`p3_s1_contention` × 7（两个 mid：第二个等单元释放后由 uav_02 执行），`p3_s1_relay` × 7（uav_01 分配后机场维护，改派 uav_02）。每个任务另跑 M2 飞行裁判（真值换算到该机站点坐标），再做 P1 / P3 检查与两机真值净距。

裁判计数：`double_ownership`（同一任务的有效分配区间重叠，或同一机器人资源被两个活动同时持有）、`duplicate_execution`（同一任务完成多于一次、同一分配代次多于一次飞行，或被撤回 / 取消的分配仍有领取或起飞）、`lost_task`（受理的任务结束时既不终结也没有记录原因的等待）、`conflicting_reservation`（同一单元或资源的有效持有区间重叠、落入他人包络的新预约，S1 另查两机同时空中时水平净距 < 3 m）、`false_success`（任务完成但任务未完成或巡检未经证实）、`stale_epoch_effect`（旧代次产生的审批、领取或飞行）。P1 的派遣 / 释放与 P2 的工作流计数在同一用例上一并核对。

## 8. 门禁与接手

`scripts/verify_p3_release.py` 判据：scope（相对 `3bdbd50` 的改动在 P3 路径内；机载代码与飞行配置未改则不需 M1 回归，否则必须补齐）、checks（云端全量）、adversarial（M2 与工作流草案语料）、s0_matrix（当场 P3-F01–F15 × 3）、s0_ladder（当场 10 / 30 / 100 并报告分布）、p1_regression 与 p2_regression（当场 P1 / P2 S0 矩阵）、capacity（探针与冻结预算）、s1（两机 5 例）、p2_s1_regression（服务与工作流改动后的 P2 S1 5 例）、m2_regression（18 例）、desk（带调度目录激活，三次演练与迁移，队列与排除原因可见，拒绝注入与控制帧）、desk_session（经任务台提交任务并飞完）、historical（M3 / P0 / P1 / P2 记录不变）。缺证据为 `missing`。

P4 / P5 / X1 接手物：任务、分配代次与判定记录；空域网格与失联包络；撤回 / 接力语义；两机同世界拓扑。P3 不证明近距离协同、真实空域报备、机场硬件或地面机器人交接。
