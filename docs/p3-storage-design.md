# P3 存储设计：调度扩展表（WP-P3-03 前置）

[P3 方案](p3-implementation.md) · [决策 D059 / D060](decisions.md) · [P2 存储设计](p2-storage-design.md) · [P1 存储设计](p1-storage-design.md)

**状态：2026-09-26 已批准（用户批准，含常驻任务台账本的带备份迁移），实施中。** 本文列出现有 schema、P3 新增表、事务与唯一约束、与 P1 / P2 表的连接、迁移 / 备份 / 回退与演练。常驻任务台仍须先完成第 6 节的副本演练再迁移。

## 1. 现有 schema（schema v2 + 工作流扩展 v1）

| 部分 | 表 | P3 是否改动 |
|---|---|---|
| v1（M2） | `meta`、`requests`、`missions`、`versions`、`operations`、`deliveries`、`events`、`evidence`、`verifications`、`facts`、`issues`、`robots`、`reports` | 否。分配产生的任务仍是普通请求 / 任务行：请求者沿用任务的请求者，幂等键为 `<任务键>#a<代次>`，已有 `UNIQUE(requested_by, idempotency_key)` 即任务服务一侧的去重 |
| v2（P1） | 12 张 `op_` 表 | 否（不改表、列与索引）。空域单元作为新的资源 ID 命名空间 `air.<frame>.<i>.<j>` 写入 `op_holds`，由既有 `op_exclusive_hold` 保证独占；撤回与任务取消写 `op_cancellations`；审计沿用 `op_events`（主题 `task:`、`airspace:`） |
| 工作流 v1（P2） | 9 张 `wf_` 表 | 否。分配模式节点的输出只多一个任务 ID（JSON 结果，列不变） |

`meta.schema_version` 保持 `2`，`meta.workflow_schema` 保持 `1`；调度扩展另记 `meta.scheduling_schema = 1`（理由同 D058：旧版本可以直接部署在迁移后的库上回退）。

## 2. 新增表（调度扩展 v1，纯增量）

```sql
CREATE TABLE IF NOT EXISTS sc_catalogs (
    catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, body TEXT NOT NULL, loaded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sc_tasks (
    task_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, idempotency_key TEXT NOT NULL, project_id TEXT NOT NULL,
    asset_id TEXT NOT NULL, volume_id TEXT NOT NULL, candidates TEXT NOT NULL, priority INTEGER NOT NULL,
    not_after TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL,
    state_version INTEGER NOT NULL, epoch INTEGER NOT NULL, robot_id TEXT, mission_id TEXT, excluded TEXT NOT NULL,
    reason TEXT, detail TEXT, cancel TEXT, outcome TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (requested_by, idempotency_key));
CREATE INDEX IF NOT EXISTS sc_tasks_state ON sc_tasks (state, priority, created_at);
CREATE INDEX IF NOT EXISTS sc_tasks_project ON sc_tasks (project_id, created_at);
CREATE TABLE IF NOT EXISTS sc_assignments (
    task_id TEXT NOT NULL, epoch INTEGER NOT NULL, robot_id TEXT NOT NULL, mission_id TEXT, decision_id TEXT NOT NULL,
    state TEXT NOT NULL, reason TEXT, blocked_since TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, epoch));
CREATE UNIQUE INDEX IF NOT EXISTS sc_live_assignment ON sc_assignments (task_id) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS sc_assignment_mission ON sc_assignments (mission_id) WHERE mission_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS sc_assignments_robot ON sc_assignments (robot_id, created_at);
CREATE TABLE IF NOT EXISTS sc_decisions (
    decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, verdict TEXT NOT NULL, robot_id TEXT, epoch INTEGER NOT NULL,
    snapshot_sha256 TEXT NOT NULL, policy_version TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sc_decisions_task ON sc_decisions (task_id, created_at);
CREATE TABLE IF NOT EXISTS sc_footprints (
    activity_key TEXT PRIMARY KEY, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, robot_id TEXT NOT NULL,
    frame TEXT NOT NULL, cells TEXT NOT NULL, box TEXT NOT NULL, volume_box TEXT NOT NULL, created_at TEXT NOT NULL);
```

| 表 | 作用 | 关键不变量 |
|---|---|---|
| `sc_catalogs` | 每个加载过的调度目录原文与摘要 | 任务固定的目录摘要必须能在此找到原文 |
| `sc_tasks` | 任务：资产、体积、候选、优先级、时间窗、状态与状态版本、当前分配代次 / 机器人 / 任务、排除列表、取消与结果 | `(requested_by, idempotency_key)` 唯一：同一请求重试返回原任务；状态变更按 `state_version` CAS；取消状态不被普通推进覆盖 |
| `sc_assignments` | 每次分配（任务、代次、机器人、任务 ID、判定、状态、持续阻断起点） | `sc_live_assignment`：每个任务同时至多一个 `active` 分配（双 worker 同抢只一个成功）；`sc_assignment_mission`：一个任务只属于一次分配 |
| `sc_decisions` | 判定记录：结论、所选机器人、逐候选原因与排序键、完整快照、规则与策略版本 | 只追加；只在结论或原因变化时写；快照可按原样重放 |
| `sc_footprints` | 每个任务版本的航迹覆盖：共享坐标系、单元、外包矩形与批准体积矩形 | 与该活动首次预约同一事务写入；失联包络与裁判都据此重算 |

机器人资源与空域单元的独占持有全部落在既有 `op_holds`，因此同一事务、同一唯一索引裁决机器人、机位、充电位与空域单元的冲突。新表都在服务端；机载 wire、任务包哈希与签名边界不变。

## 3. 事务与串行化边界

所有写入仍经 `BusinessLedger` 的单连接、可重入锁与 `BEGIN IMMEDIATE`；多进程（S0 的双调度器用例）由 SQLite 写锁串行化。

| 操作 | 同一事务内 | 失败时 / 保证 |
|---|---|---|
| 提交任务 | 插入 `sc_tasks`（唯一键去重）+ 审计 | 重复键返回原任务；项目 / 候选 / 资产不合法在事务外即拒绝 |
| 分配 | 核对任务 `queued` 与状态版本、未取消 → 事务内重建所选候选快照并重判 → 插入 `sc_assignments` → 写请求 / 任务 / 绑定 / 版本（编译与准入）→ 预约机器人资源与航迹单元（`op_reservations` / `op_holds`）+ `sc_footprints` → 更新任务 + 判定 + 审计 | 任一冲突（唯一索引、单元持有、包络、状态版本）整体回滚，任务保持 `queued`；准入拒绝则提交「分配以准入拒绝结束、该机排除」 |
| 撤回 | 核对分配 `active`、该任务版本没有 `claimed` 领取 → 写 `op_cancellations`（原因 `assignment_withdrawn`）→ 分配 `withdrawn` → 释放未领取持有 → 任务回 `queued` + 审计 | 领取先提交则撤回放弃；撤回先提交则领取闸门按取消意图作废投递 |
| 结算 | 任务终态且预约已释放 → 分配结束 + 任务状态 / 结果（或回队列、排除该机） | 预约未释放时不结算；未知结果不改派 |
| 取消任务 | 任务 `cancel_requested` + 该任务未了结的任务写 `op_cancellations` + 审计；工作流取消在 P2 的取消事务内对本运行的任务做同样写入 | 取消先于后续分配持久化；已领取的飞行照 P1 对账收尾 |
| 预约（任何有调度目录的活动） | P1 预约事务内另算他人失联包络，拒绝落入包络的单元；航迹覆盖首次写入 `sc_footprints` | 包络随时间增长，因此在写事务内计算，不能预先缓存为持有行 |

## 4. 与 P1 / P2 数据的连接

- 分配任务的任务：`requests.requested_by` = 任务的请求者（工作流为 `workflow:<run_id>`，API 为操作者身份），`idempotency_key = <任务键>#a<代次>`；`op_bindings` 为所分配机器人的项目 / 站点 / 机场 / 机器人；来源与 P2 相同（`deterministic`，`provider_id=scheduler`，`model_id=<模板或 task>`）。
- 领取闸门：P1 的取消意图检查之外，另核对投递所属分配仍为 `active` 且代次等于任务当前代次；领取阶段要求本活动持有全部航迹单元。
- 工作流：分配模式的 `submit_mission` 节点写入任务（去重键为活动键），`await_mission` 按任务跟随其当前任务；P2 的运行取消事务同时取消本运行的任务。

## 5. 迁移、备份与失败恢复

1. 只在服务带 `--scheduling`（且带 `--catalog`）启动时迁移；带 `--workflows` 时先完成 D058。不带 `--scheduling` 的服务保持 P2 行为，不创建 `sc_` 表。
2. 前置：`schema_version` 必须为 `2` 且 12 张 `op_` 表齐全，否则拒绝启动。`scheduling_schema` 为 `1` → 核对 5 张 `sc_` 表后继续；为其他值 → 拒绝启动。
3. 文件库且已有任务 / 请求行时，先用 `sqlite3.Connection.backup` 写 `<state>/backups/ledger-v2-sc-<UTC>.sqlite3`，对备份跑 `PRAGMA integrity_check` 并逐表核对行数，计算 SHA-256；任一不符即拒绝迁移，原库不动。
4. 在一个 `BEGIN IMMEDIATE` 事务内执行全部 DDL，写入 `scheduling_schema=1`、`scheduling_migrated_at`、`scheduling_backup`（路径、摘要、行数）；任何异常 `ROLLBACK`，服务拒绝启动并报告原因。
5. 回退路径：
   - **部署上一版本（P2 候选）**：P2 代码只核对 `schema_version=2`、`op_` 与 `wf_` 表，忽略 `sc_` 表，可以直接启动。已有 `air.*` 持有属于各自预约，P1 的对账释放按预约停用其全部持有，因此会随任务了结释放。限制：未分配或正在改派的任务不再推进（它们只存在于 `sc_tasks`）；回退前应停止提交任务、等待或取消进行中的任务，并记录。
   - **停服恢复备份**：用 `ledger-v2-sc-*.sqlite3` 替换，丢失迁移后的全部数据，须显式确认后执行。

## 6. 演练计划

本地 / CI（批准前即可运行，只作用于临时目录）：

| 演练 | 通过判据 |
|---|---|
| 真实数据迁移 | 用 P2 S0 用例写出的账本（含 `wf_` 表）迁移；备份存在、完整性 ok、逐表行数一致；非 `sc_` 对象的 `iterdump`（去掉 `scheduling_` meta 键）迁移前后逐字相同 |
| 重复启动 | 第二次迁移为 no-op，不产生第二份备份 |
| 失败注入 | 让一条 DDL 失败：`scheduling_schema` 不存在、无 `sc_` 表、原数据不变，服务拒绝启动 |
| 旧代码读新库 | P2 的 `WorkflowStore` / `OperationsStore` 在迁移后的库上报告 `current` 并能提交、领取、对账（P1 / P2 S0 用例）；M2 模式的 v1 `BusinessLedger` 能读取历史任务 |
| 备份恢复 | 用备份替换后回到无 `sc_` 表的库，数据与迁移前一致 |
| 并发 | 两个调度器同抢同一任务 / 同一机器人只产生一个 `active` 分配与一组持有；过期状态版本的写入被拒 |

云端常驻任务台（批准后、激活 P3 版本前）：在隔离容器（无网络）中复制当前账本，依次运行 D056、D058、D060 演练，记录备份摘要、行数与完整性回执；通过后再激活，服务启动时对真实库再做一次带备份的迁移。回执只含摘要与计数，不含任务内容。

## 7. 批准范围

需要批准的是：在本项目任务服务账本中新增第 2 节的 5 张表、6 个索引与 `scheduling_*` meta 键（`schema_version` 保持 2，`workflow_schema` 保持 1），`op_holds` 中新增 `air.*` 资源 ID 命名空间，以及按第 5 节对常驻任务台现有账本执行带备份的迁移。批准不包括删除或改写任何 v1 / v2 / 工作流表与行，也不包括其他应用的数据库。
