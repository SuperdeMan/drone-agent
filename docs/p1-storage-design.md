# P1 存储与授权设计（WP-P1-02）

[P1 方案](p1-implementation.md) · [决策 D055 / D056](decisions.md) · [运营架构](architecture/09-operations.md)

**状态：2026-09-26 已批准并实施（用户批准，含常驻任务台账本的带备份迁移）。** 常驻任务台账本已在 `b49701b` 激活时迁移：先在副本上演练通过，再带经校验的备份迁移，见 [P1 记录](p1-readiness.md)。 本文是 D056 的设计依据：列出现有账本 schema、P1 新增表、事务与唯一约束、legacy 项目映射、备份 / 失败恢复与演练。常驻任务台仍须先完成第 6 节的副本演练再激活。

## 1. 现有 schema v1（`fleet/ledger.py`）

| 表 | 主键 / 唯一约束 | P1 是否改动 |
|---|---|---|
| `meta` | `key` | 只把 `schema_version` 从 `1` 改为 `2`，另追加迁移记录键 |
| `requests` | `request_id`；`UNIQUE(requested_by, idempotency_key)` | 否；新任务的绑定行与请求 / 任务行在同一事务写入 |
| `missions` | `mission_id` | 否（状态值增加 `cancelled`、`dispatch_expired`，列不变） |
| `versions` | `(mission_id, version)` | 否（状态值增加 `withdrawn`） |
| `operations` | `operation_id`；活动操作部分唯一索引 | 否 |
| `deliveries` | `cursor`；`delivery_id` 唯一 | 否；领取 / 作废另记在新表 |
| `events` / `evidence` / `verifications` / `facts` / `issues` / `robots` / `reports` | 见代码 | 否 |

v1 代码不读取 `schema_version`，也不访问任何 `op_` 前缀表；这是回退路径成立的前提，由演练测试钉住。

## 2. 新增表（schema v2，纯增量）

```sql
CREATE TABLE IF NOT EXISTS op_catalogs (
    catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, body TEXT NOT NULL, loaded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS op_bindings (
    mission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, site_id TEXT NOT NULL, dock_id TEXT NOT NULL,
    robot_id TEXT NOT NULL, execution_backend TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS op_bindings_project ON op_bindings (project_id, created_at);
CREATE TABLE IF NOT EXISTS op_dock_state (
    dock_id TEXT PRIMARY KEY, boot_id TEXT NOT NULL, seq INTEGER NOT NULL, session TEXT NOT NULL,
    complete_reports INTEGER NOT NULL, source TEXT NOT NULL, report TEXT NOT NULL, observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS op_dock_sessions (
    dock_id TEXT NOT NULL, boot_id TEXT NOT NULL, first_seen TEXT NOT NULL, PRIMARY KEY (dock_id, boot_id));
CREATE TABLE IF NOT EXISTS op_dock_locks (
    dock_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, set_by TEXT NOT NULL, set_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS op_dock_actions (
    action_id TEXT PRIMARY KEY, dock_id TEXT NOT NULL, kind TEXT NOT NULL, activity_key TEXT NOT NULL,
    state TEXT NOT NULL, requested_at TEXT NOT NULL, acked_at TEXT, ack_accepted INTEGER, ack_reason TEXT,
    result_at TEXT, UNIQUE (activity_key, kind));
CREATE TABLE IF NOT EXISTS op_reservations (
    reservation_id TEXT PRIMARY KEY, activity_key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL,
    mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, robot_id TEXT NOT NULL, dock_id TEXT NOT NULL,
    state TEXT NOT NULL, expires_at TEXT, reason TEXT NOT NULL, evidence TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS op_reservations_mission ON op_reservations (mission_id, mission_version);
CREATE TABLE IF NOT EXISTS op_holds (
    resource_id TEXT NOT NULL, reservation_id TEXT NOT NULL, active INTEGER NOT NULL,
    PRIMARY KEY (resource_id, reservation_id));
CREATE UNIQUE INDEX IF NOT EXISTS op_exclusive_hold ON op_holds (resource_id) WHERE active = 1;
CREATE TABLE IF NOT EXISTS op_claims (
    delivery_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, state TEXT NOT NULL,
    reason TEXT NOT NULL, decision_id TEXT, decided_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS op_decisions (
    decision_id TEXT PRIMARY KEY, mission_id TEXT, mission_version INTEGER, robot_id TEXT NOT NULL,
    stage TEXT NOT NULL, verdict TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS op_decisions_mission ON op_decisions (mission_id, created_at);
CREATE TABLE IF NOT EXISTS op_cancellations (
    mission_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, request_id TEXT NOT NULL, reason TEXT NOT NULL,
    requested_at TEXT NOT NULL, relayed_request_id TEXT);
CREATE TABLE IF NOT EXISTS op_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS op_events_subject ON op_events (subject, id);
```

| 表 | 作用 | 关键不变量 |
|---|---|---|
| `op_catalogs` | 每个加载过的目录版本原文与摘要 | 绑定引用的摘要必须能在此找到原文 |
| `op_bindings` | 任务 → 项目 / 站点 / 机场 / 机器人 / 执行后端 | 与 `requests`、`missions` 同一事务写入；写后不改；无行 = legacy |
| `op_dock_state` | 每个机场最新接受的报告投影 | 只接受同会话更大 seq；到达即过期、未来时间、旧 boot 不写入 |
| `op_dock_sessions` | 见过的 boot 会话 | 已被替代的 boot 再出现即拒收 |
| `op_dock_locks` | 维护 / 故障粘滞锁 | 遥测只能加锁；删除只由 admin 操作并写审计 |
| `op_dock_actions` | 开盖 / 关盖 / 充电请求、ACK 与结果 | ACK 与结果分列；同一活动同一动作只有一条 |
| `op_reservations` / `op_holds` | 活动预约与逐资源持有 | `op_exclusive_hold` 保证每项资源同时只有一个 `active=1`；状态变更用 CAS |
| `op_claims` | 投递的领取或作废 | 每个投递最多一个决定；作废的投递永不再交付 |
| `op_decisions` | 预览 / 准备 / 领取判定快照 | 领取阶段的等待记录只在原因集合变化时追加 |
| `op_cancellations` | 任务级取消意图 | 首个有效；与领取同一事务读取 |
| `op_events` | 状态接受 / 拒收、锁、动作、预约、领取、释放的审计流 | 只追加 |

新表都在服务端；机载 wire、任务包哈希与签名边界不变。

## 3. 事务与串行化边界

所有写入共用 `BusinessLedger` 的单连接与可重入锁；多语句写入使用 `BEGIN IMMEDIATE ... COMMIT`，异常即 `ROLLBACK`。

| 操作 | 同一事务内 | 失败时 |
|---|---|---|
| 提交任务 | 插入 `requests`、`missions`、`op_bindings` | 三者都不存在；同一幂等键返回原任务，绑定不同则拒绝 |
| 预约 / 刷新 | 过期软持有转 released → 读取或插入活动预约 → 插入持有 | 唯一索引冲突即整体回滚，返回当前持有者 |
| 领取 | 读取领取行、取消意图、审批、机场投影与锁、机体状态、持有 → 判定 → 写领取 / 作废、预约状态、判定与事件 | 回滚后本次不交付，下一次拉取重算 |
| 机场报告 | 会话 / seq / 时间检查 → 更新投影、会话、锁、动作结果与事件 | 拒收只写审计事件，不改投影 |
| 释放 | `UPDATE ... WHERE state IN ('occupied','uncertain')`，持有转 inactive，写证据与事件 | 影响行数不是 1 即不释放，保证恰好一次 |
| 取消 | 插入取消意图（已存在则返回原意图） | 与领取互斥：先提交者决定顺序，可复算 |

## 4. legacy 项目与授权映射

- 无 `op_bindings` 行的任务固定属于目录 `legacy_project.project_id`；不从请求、actor 或当前部署推断。
- 第一方身份在 legacy 项目只获得目录 `legacy_project.first_party_roles`；常驻入口配置为 `[viewer]`，历史任务只读。
- 其他项目只按成员文件授权；成员文件是受信部署输入，不可由 API 修改。旧 API 在目录模式下同样按任务项目检查角色；没有成员资格返回 `service.not_found` 或 `auth.project_denied`（提交类）。

## 5. 迁移、备份与失败恢复

1. 服务带 `--catalog` 启动时调用迁移；不带目录的 M2 模式不迁移，库保持 v1。
2. 读 `meta.schema_version`：`2` → 核对全部 `op_` 表存在后继续；非 `1` / `2` → 拒绝启动。
3. 文件库且已有任何任务 / 请求行时，先用 `sqlite3.Connection.backup` 写 `<state>/backups/ledger-v1-<UTC>.sqlite3`，对备份跑 `PRAGMA integrity_check` 并逐表核对行数，计算 SHA-256；任一不符即拒绝迁移，原库不动。
4. 在一个 `BEGIN IMMEDIATE` 事务内逐条执行 DDL、更新 `schema_version=2`，写入 `operations_migrated_at`、`operations_backup`、`operations_backup_sha256`；任何异常 `ROLLBACK`，版本保持 `1`，服务拒绝启动并报告原因。
5. 回退：部署上一版本即可继续使用 v2 文件（新表被忽略；迁移后创建的绑定任务在旧版本中会显示为无项目的 M2 任务，因此回退前应停止新派遣并记录）；或停服后用备份文件替换（丢失迁移后的数据，须显式确认后执行）。

## 6. 演练计划

本地 / CI 测试（批准前即可运行，只作用于临时目录）：

| 演练 | 通过判据 |
|---|---|
| 真实 v1 数据迁移 | 用 M2 闭环写出的库迁移；备份存在、完整性 ok、逐表行数一致；v1 表的 `iterdump` 迁移前后逐字相同 |
| 重复启动 | 第二次迁移为 no-op，不产生第二份备份 |
| 失败注入 | 让一条 DDL 失败：版本仍为 1、无 `op_` 表、v1 数据不变，服务拒绝启动 |
| 旧代码读新库 | 未改动的 v1 `BusinessLedger` 与 M2 模式服务能读取历史任务并创建新任务 |
| 备份恢复 | 用备份替换后回到 v1，数据与迁移前一致 |
| 并发持有 | 多线程同时预约同一资源只有一个成功；同键重试返回原预约 |

云端常驻任务台（批准后、激活 P1 版本前）：在隔离容器（无网络）中复制当前账本执行同一迁移，记录备份摘要、行数与完整性回执；通过后再激活，服务启动时对真实库再做一次带备份的迁移。回执只含摘要与计数，不含任务内容。

## 7. 批准范围

需要批准的是：在本项目的任务服务账本中新增第 2 节列出的表并把版本升为 2，以及按第 5 节对常驻任务台现有账本执行带备份的迁移。批准不包括删除或改写任何 v1 数据，也不包括其他应用的数据库。
