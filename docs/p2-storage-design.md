# P2 存储设计：持久工作流扩展表（WP-P2-02 前置）

[P2 方案](p2-implementation.md) · [决策 D057 / D058](decisions.md) · [P1 存储设计](p1-storage-design.md) · [运营架构 §4](architecture/09-operations.md)

**状态：2026-09-26 已批准（用户批准，含常驻任务台账本的带备份迁移），实施中。** 常驻任务台账本须先完成第 6 节的副本演练再迁移。本文列出现有 schema、P2 新增表、事务与唯一约束、与 P1 表的连接、迁移 / 备份 / 回退和演练。

## 1. 现有 schema（schema v2，P1 已批准）

| 部分 | 表 | P2 是否改动 |
|---|---|---|
| v1（M2） | `meta`、`requests`、`missions`、`versions`、`operations`、`deliveries`、`events`、`evidence`、`verifications`、`facts`、`issues`、`robots`、`reports` | 否。工作流提交的任务仍是普通请求 / 任务行：`requests.requested_by = workflow:<run_id>`，`idempotency_key` 为活动键；已有 `UNIQUE(requested_by, idempotency_key)` 就是任务服务一侧的去重 inbox |
| v2（P1） | 12 张 `op_` 表 | 否。工作流取消在同一事务中为子任务写入 `op_cancellations`（首个有效，沿用 P1 的取消屏障）；审计沿用 `op_events`（主题 `workflow:` / `schedule:` / `order:`） |

`meta.schema_version` 保持 `2`。工作流扩展用独立的 `meta.workflow_schema = 1` 标记（见 §5 的回退理由）。

## 2. 新增表（工作流扩展 v1，纯增量）

```sql
CREATE TABLE IF NOT EXISTS wf_catalogs (
    catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, body TEXT NOT NULL, fixtures TEXT NOT NULL,
    loaded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_schedules (
    project_id TEXT NOT NULL, workflow_id TEXT NOT NULL, trigger_id TEXT NOT NULL, version INTEGER NOT NULL,
    catalog_sha256 TEXT NOT NULL, state TEXT NOT NULL, cursor_at TEXT, changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY (project_id, workflow_id, trigger_id));
CREATE TABLE IF NOT EXISTS wf_triggers (
    project_id TEXT NOT NULL, workflow_id TEXT NOT NULL, version INTEGER NOT NULL, source TEXT NOT NULL,
    event_id TEXT NOT NULL, disposition TEXT NOT NULL, run_id TEXT, actor TEXT NOT NULL, body TEXT NOT NULL,
    received_at TEXT NOT NULL, PRIMARY KEY (project_id, workflow_id, version, source, event_id));
CREATE TABLE IF NOT EXISTS wf_runs (
    run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workflow_id TEXT NOT NULL, version INTEGER NOT NULL,
    spec_sha256 TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, trigger_source TEXT NOT NULL,
    trigger_event TEXT NOT NULL, started_by TEXT NOT NULL, inputs TEXT NOT NULL, state TEXT NOT NULL,
    state_version INTEGER NOT NULL, cancel_epoch INTEGER NOT NULL, cancel TEXT, owner TEXT,
    owner_epoch INTEGER NOT NULL, lease_until TEXT, outcome TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS wf_runs_project ON wf_runs (project_id, created_at);
CREATE INDEX IF NOT EXISTS wf_runs_state ON wf_runs (state);
CREATE TABLE IF NOT EXISTS wf_nodes (
    run_id TEXT NOT NULL, node_id TEXT NOT NULL, occurrence INTEGER NOT NULL, activity TEXT NOT NULL,
    state TEXT NOT NULL, state_version INTEGER NOT NULL, reason TEXT, detail TEXT, result TEXT, deadline_at TEXT,
    started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (run_id, node_id, occurrence));
CREATE TABLE IF NOT EXISTS wf_outbox (
    outbox_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
    occurrence INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
    cancel_epoch INTEGER NOT NULL, claimed_by TEXT, claim_epoch INTEGER, attempts INTEGER NOT NULL,
    result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS wf_outbox_state ON wf_outbox (state, created_at);
CREATE TABLE IF NOT EXISTS wf_analyses (
    analysis_id TEXT PRIMARY KEY, activity_key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
    mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, evidence_id TEXT NOT NULL, asset_id TEXT NOT NULL,
    analyzer TEXT NOT NULL, source TEXT NOT NULL, verdict TEXT NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_reviews (
    review_id TEXT PRIMARY KEY, activity_key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
    analysis_id TEXT NOT NULL, reviewer TEXT NOT NULL, request_id TEXT NOT NULL, decision TEXT NOT NULL,
    note TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_work_orders (
    order_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
    asset_id TEXT NOT NULL, review_id TEXT NOT NULL, analysis_id TEXT NOT NULL, state TEXT NOT NULL,
    body TEXT NOT NULL, feedback TEXT, reinspection_run TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS wf_work_orders_project ON wf_work_orders (project_id, created_at);
```

| 表 | 作用 | 关键不变量 |
|---|---|---|
| `wf_catalogs` | 每个加载过的工作流目录原文、摘要与分析夹具摘要 | 运行固定的目录摘要必须能在此找到原文；同一（项目、流程、版本）在不同目录中内容不同即拒绝启动 |
| `wf_schedules` | 排班的启用 / 停用状态、固定版本与游标 | 启用与停用写入操作者、时间与原因；停用不影响已开始的运行；游标只前进 |
| `wf_triggers` | 触发 inbox：每个去重键一行，记录启动 / 错过 / 拒绝 | 主键即 `(project, workflow, version, source, event_id)`（09-operations §4.2）；同一事件 100 次只有一行、一个运行 |
| `wf_runs` | 运行实例：固定模板与目录摘要、触发身份、状态与版本号、取消代次、执行租约 | 状态变更用 CAS（`state_version`）；`owner` / `owner_epoch` / `lease_until` 是 worker fencing，接管时代次加一 |
| `wf_nodes` | 节点状态、原因、类型化输出与持久截止时刻 | 主键即逻辑活动键 `(run_id, node_id, occurrence)`；CAS 推进；`deadline_at` 是持久定时器，不依赖进程内 sleep |
| `wf_outbox` | 需要对外投递的效果：提交任务、创建工单、启动复检 | `idempotency_key` 唯一（= 活动键），通信重试不产生第二条；写入时记录运行的取消代次 |
| `wf_analyses` | 分析作业结果与来源 | 每个活动键一行；只引用服务复核通过的证据；结果不写回飞行三元状态 |
| `wf_reviews` | 人工复核记录 | 每个复核节点只有一个决定，首个有效；同一 `request_id` 重复提交返回原记录 |
| `wf_work_orders` | 模拟工单、维修反馈与复检链接 | `idempotency_key` 唯一，重试不重复建单；维修反馈只转 `repair_reported`，复检请求只转 `reinspection_requested`，P2 不关单 |

新表都在服务端；机载 wire、任务包哈希与签名边界不变。工作流对象的契约见 `fleet/workflow_models.py`（WP-P2-01，不涉及存储）。

## 3. 事务与串行化边界

所有写入仍经 `BusinessLedger` 的单连接、可重入锁与 `BEGIN IMMEDIATE`；多进程（S0 的双 worker 用例）由 SQLite 写锁串行化。

| 操作 | 同一事务内 | 失败时 / 保证 |
|---|---|---|
| 启动（人工 / 定时 / 事件 / 内部） | 插入 `wf_triggers`（主键去重）→ 新键才插入 `wf_runs` 与全部 `wf_nodes`；定时触发同时推进游标；写审计 | 重复键返回原运行；不会出现有运行无节点或有触发无运行 |
| 租约 | `UPDATE wf_runs SET owner, owner_epoch+1, lease_until WHERE 空闲或已过期`；续租只允许当前 owner 与代次 | 同一时刻一个 worker；旧 worker 的代次失效 |
| 节点推进 | 核对 owner 与代次（fencing）→ `UPDATE wf_nodes … WHERE state_version=?`；开始有外部效果的节点时同事务插入 `wf_outbox` | 过期 worker 写入 0 行并放弃；状态与 outbox 不会半提交 |
| outbox 领取 | fencing → `pending → claimed`，要求行的取消代次等于运行当前代次且运行未请求取消 | 取消与领取按提交先后串行，可复算 |
| 投递 `submit_mission` | 任务服务的确定性提交：请求、任务、绑定、版本、软预约在一个事务内；事务开头再核对该 outbox 行仍由本 worker 领取且代次未变 | 取消已提交则不建任务；已存在的幂等键返回原任务 |
| 记录投递结果 | fencing → outbox `delivered` + 节点 `completed`（输出）+ 审计 | 若在此前崩溃，重投命中任务服务幂等键，返回同一任务 |
| 取消 | 运行 → `cancel_requested`、取消代次加一；`pending` outbox 作废；由已领取 / 已投递的活动键查得的子任务写入 `op_cancellations` | 取消先于后续派遣持久化；已领取的包进入对账，由 P1 屏障作废未领取投递、转发在飞取消 |
| 复核 / 维修反馈 | 核对节点等待中或工单状态 → 插入 `wf_reviews` / 更新工单 | 首个决定有效；反馈只一次 |
| 排班启停 | 更新 `wf_schedules` + 审计 | 权限、作用域与生效时间入账 |

## 4. 与 P1 / M2 数据的连接

- 子任务：`requests.requested_by = workflow:<run_id>`，`idempotency_key = wf:<run_id>:<node_id>:<occurrence>`。对账用这两个值查询原任务，不按时间或文本猜测。
- 任务入口：新增请求入口值 `workflow`（`admission/models.py` 的服务侧枚举，不属于冻结契约与 wire）；规划来源记为 `deterministic`（`provider_id=workflow`，`model_id=<流程>@v<版本>`）。
- 取消：工作流取消写入的 `op_cancellations` 与操作者取消完全相同，任务级取消意图继续阻止领取与 D032 自动重飞。
- 分析来源：除 `wf_analyses` 外，每次分析另以 `analysis:<id>` 追加到任务版本的不可变来源记录（D054 机制），报告与证据页照常显示。

## 5. 迁移、备份与失败恢复

1. 只在服务同时带 `--catalog` 与 `--workflows` 启动时迁移。不带 `--workflows` 的服务保持 P1 行为（schema v2，无 `wf_` 表）。
2. 前置：`schema_version` 必须为 `2` 且 12 张 `op_` 表齐全，否则拒绝启动。`workflow_schema` 为 `1` → 核对 9 张 `wf_` 表后继续；为其他值 → 拒绝启动。
3. 文件库且已有任务 / 请求行时，先用 `sqlite3.Connection.backup` 写 `<state>/backups/ledger-v2-<UTC>.sqlite3`，对备份跑 `PRAGMA integrity_check` 并逐表核对行数，计算 SHA-256；任一不符即拒绝迁移，原库不动。
4. 在一个 `BEGIN IMMEDIATE` 事务内执行全部 DDL，写入 `workflow_schema=1`、`workflow_migrated_at`、`workflow_backup`（路径、摘要、行数）；任何异常 `ROLLBACK`，服务拒绝启动并报告原因。
5. 回退路径：
   - **部署上一版本（P1 候选）**：P1 代码只检查 `schema_version=2` 与 `op_` 表，忽略 `wf_` 表，因此可以直接启动，已有数据不丢。限制：工作流创建的任务的请求入口为 `workflow`，P1 代码对这些任务的自动重规划会因无法识别入口而报 `service.degraded`；回退前应停用排班、取消或等待进行中的工作流任务结束，并记录。
   - **停服恢复备份**：用 `ledger-v2-*.sqlite3` 替换，丢失迁移后的全部数据，须显式确认后执行。
   - 这是不把 `schema_version` 升为 `3` 的原因：P1 代码遇到未知版本会拒绝启动，升版本会让回退只剩恢复备份一条路。

## 6. 演练计划

本地 / CI（批准前即可运行，只作用于临时目录）：

| 演练 | 通过判据 |
|---|---|
| 真实 v2 数据迁移 | 用 P1 S0 用例写出的账本迁移；备份存在、完整性 ok、逐表行数一致；非 `wf_` 对象的 `iterdump`（去掉 `workflow_` meta 键）迁移前后逐字相同 |
| 重复启动 | 第二次迁移为 no-op，不产生第二份备份 |
| 失败注入 | 让一条 DDL 失败：`workflow_schema` 不存在、无 `wf_` 表、原数据不变，服务拒绝启动 |
| 旧代码读新库 | P1 的 `OperationsStore` / `migrate` 在迁移后的库上报告 `current` 并能提交、领取、对账（P1 S0 用例）；M2 模式的 v1 `BusinessLedger` 能读取历史任务 |
| 备份恢复 | 用备份替换后回到无 `wf_` 表的 v2，数据与迁移前一致 |
| 并发与 fencing | 两个 worker 抢同一运行只有一个 owner；过期代次的写入被拒；同一活动键并发投递只产生一个任务 |

云端常驻任务台（批准后、激活 P2 版本前）：在隔离容器（无网络）中复制当前账本执行同一迁移（先 P1 演练、再 P2 演练），记录备份摘要、行数与完整性回执；通过后再激活，服务启动时对真实库再做一次带备份的迁移。回执只含摘要与计数，不含任务内容。

## 7. 批准范围

需要批准的是：在本项目任务服务账本中新增第 2 节的 9 张表、5 个索引与 `workflow_*` meta 键（`schema_version` 保持 2），以及按第 5 节对常驻任务台现有账本执行带备份的迁移。批准不包括删除或改写任何 v1 / v2 表与行，也不包括其他应用的数据库。
