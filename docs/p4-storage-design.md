# P4 存储设计：业务扩展表（WP-P4-02 / 04 / 05 前置）

[P4 方案](p4-implementation.md) · [决策 D063 / D064](decisions.md) · [P3 存储设计](p3-storage-design.md) · [P2 存储设计](p2-storage-design.md) · [P1 存储设计](p1-storage-design.md)

**状态：2026-09-27 已批准（用户批准，含常驻任务台账本的带备份迁移），实施中。** 本地只在临时目录运行第 6 节的演练；常驻任务台账本在激活 P4 候选时先做副本演练再迁移。 本文列出现有 schema、P4 新增表、事务与唯一约束、与既有表的连接、迁移 / 备份 / 回退和演练。

## 1. 现有 schema（schema v2 + 工作流扩展 v1 + 调度扩展 v1）

| 部分 | 表 | P4 是否改动 |
|---|---|---|
| v1（M2） | `meta`、`requests`、`missions`、`versions`、`operations`、`deliveries`、`events`、`evidence`、`verifications`、`facts`、`issues`、`robots`、`reports` | 否。复检运行提交的任务仍是普通请求 / 任务行；分析来源按 D054 追加到 `versions.decision` 的来源命名空间（`analysis:<作业>`），不改已有记录；证据、复核与报告行只读 |
| v2（P1） | 12 张 `op_` 表 | 否。审计沿用 `op_events`（主题 `finding:`、`order:`、`job:`、`reference:`） |
| 工作流 v1（P2） | 9 张 `wf_` 表 | 否（不改表、列与索引）。P4 模式节点的输出多几个键（JSON，列不变）；复检运行由维修反馈经同一 `wf_triggers` 主键去重启动（来源 `internal:<工单>`，事件 `r<轮次>`）；P2 模板的 `wf_analyses` / `wf_reviews` / `wf_work_orders` 保持原语义，只服务 P2 模板 |
| 调度 v1（P3） | 5 张 `sc_` 表 | 否 |

`meta.schema_version` 保持 `2`，`workflow_schema` 保持 `1`，`scheduling_schema` 不要求存在；业务扩展另记 `meta.business_schema = 1`（理由同 D058 / D060：旧版本可直接部署在迁移后的库上回退）。

## 2. 新增表（业务扩展 v1，纯增量）

```sql
CREATE TABLE IF NOT EXISTS bz_catalogs (
    catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, body TEXT NOT NULL, profiles TEXT NOT NULL,
    loaded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bz_jobs (
    job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL,
    requested_by TEXT NOT NULL, purpose TEXT NOT NULL, analyzer TEXT NOT NULL, analyzer_sha256 TEXT NOT NULL,
    catalog_sha256 TEXT NOT NULL, asset_key TEXT NOT NULL, evidence_id TEXT NOT NULL, media_sha256 TEXT NOT NULL,
    inputs TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, attempts INTEGER NOT NULL,
    owner TEXT, owner_epoch INTEGER NOT NULL, lease_until TEXT, verdict TEXT, source TEXT, result TEXT,
    finding_id TEXT, order_id TEXT, round INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bz_jobs_state ON bz_jobs (state, created_at);
CREATE INDEX IF NOT EXISTS bz_jobs_asset ON bz_jobs (asset_key, created_at);
CREATE INDEX IF NOT EXISTS bz_jobs_media ON bz_jobs (project_id, media_sha256);
CREATE TABLE IF NOT EXISTS bz_references (
    reference_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_key TEXT NOT NULL, evidence_id TEXT NOT NULL,
    media_sha256 TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, registered_by TEXT NOT NULL,
    registered_at TEXT NOT NULL, revoked_by TEXT, revoked_at TEXT, UNIQUE (asset_key, evidence_id));
CREATE TABLE IF NOT EXISTS bz_findings (
    finding_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_key TEXT NOT NULL, family TEXT NOT NULL,
    cluster_key TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, first_job TEXT NOT NULL,
    last_job TEXT NOT NULL, jobs INTEGER NOT NULL, review_id TEXT, order_id TEXT, body TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS bz_open_finding ON bz_findings (cluster_key)
    WHERE state IN ('candidate', 'confirmed');
CREATE INDEX IF NOT EXISTS bz_findings_project ON bz_findings (project_id, created_at);
CREATE TABLE IF NOT EXISTS bz_reviews (
    review_id TEXT PRIMARY KEY, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, project_id TEXT NOT NULL,
    reviewer TEXT NOT NULL, request_id TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL,
    basis TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (subject_kind, subject_id));
CREATE TABLE IF NOT EXISTS bz_orders (
    order_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL, asset_key TEXT NOT NULL, review_id TEXT NOT NULL, catalog_sha256 TEXT NOT NULL,
    reinspection TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, round INTEGER NOT NULL,
    created_by TEXT NOT NULL, body TEXT NOT NULL, closure TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, closed_at TEXT);
CREATE INDEX IF NOT EXISTS bz_orders_project ON bz_orders (project_id, created_at);
CREATE TABLE IF NOT EXISTS bz_rounds (
    order_id TEXT NOT NULL, round INTEGER NOT NULL, feedback TEXT NOT NULL, state TEXT NOT NULL,
    reinspection_run TEXT UNIQUE, conclusion TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (order_id, round));
```

| 表 | 作用 | 关键不变量 |
|---|---|---|
| `bz_catalogs` | 每个加载过的业务目录原文、摘要与所引用画像文件的摘要 | 作业与工单固定的目录摘要必须能在此找到原文；同一目录 ID 内容变化即记录为新摘要，已开始的对象保留旧摘要 |
| `bz_jobs` | 分析作业：用途（`inspection` / `reinspection` / `reuse`）、分析器与画像摘要、资产键、证据与媒体摘要、输入清单、状态与版本、尝试次数、执行租约、结论、来源与结果、所挂发现 / 工单轮次 | `idempotency_key` 唯一：同一活动或请求重试返回原作业；结果写入按（`owner`, `owner_epoch`, `state='running'`）CAS，旧执行器的迟到写入 0 行；只引用服务复核为 `verified` 的证据 |
| `bz_references` | 资产的登记参考外观（证据、媒体摘要、登记 / 撤销人与时间） | 同一资产同一证据只登记一次；撤销不删除行；作业固定所用参考的 ID 与摘要 |
| `bz_findings` | 候选发现：聚合键、状态与版本、首末作业、证据簇大小、复核与工单链接 | `bz_open_finding`：同一聚合键（项目、资产范围、资产、缺陷族）同时至多一个 `candidate` / `confirmed` 发现，并发挂载只产生一个；状态只由复核与关单改变 |
| `bz_reviews` | 人工复核：对象（发现或复检轮次）、复核人、请求号、决定、依据摘要 | `UNIQUE(subject_kind, subject_id)`：首个决定有效，同一请求号重复返回原记录；只写入该项目 reviewer 角色的第一方身份 |
| `bz_orders` | 工单：发现、确认复核、固定的业务目录与复检模板、状态与版本、当前轮次、关单结论 | `finding_id` 唯一：每个发现至多一张工单；只由 `confirmed` 发现创建；状态变更按 `state_version` CAS |
| `bz_rounds` | 工单轮次：维修反馈、复检运行、结算结论与原因 | 主键（工单，轮次）；`reinspection_run` 唯一：每轮至多一次复检运行；结论只写一次 |

新表都在服务端；机载 wire、任务包哈希与签名边界不变。业务对象契约见 `fleet/business_models.py`（WP-P4-02，不涉及存储）。

## 3. 事务与串行化边界

所有写入仍经 `BusinessLedger` 的单连接、可重入锁与 `BEGIN IMMEDIATE`；多进程（S0 的并发用例）由 SQLite 写锁串行化。

| 操作 | 同一事务内 | 失败时 / 保证 |
|---|---|---|
| 分析节点开始 | 运行租约 fencing → 插入 `bz_jobs`（幂等键 = 活动键）→ 节点转 `waiting(analysis)` | 过期 worker 写入 0 行；不会出现有节点无作业 |
| 作业领取 | `queued`，或 `running` 且租约已过期 → `running`，`owner_epoch + 1`，`attempts + 1`，写租约 | 同一时刻一个执行器；超过尝试上限转 `refused`（`analysis.attempts_exhausted`） |
| 作业结果 | CAS（owner、代次、`running`）→ 写结论与来源 → 疑似则挂到唯一未结发现或新建 `candidate`（部分唯一索引）→ 追加任务版本来源记录 → 审计 | 迟到写入被拒；不会出现有结果无来源、或同一聚合键两个未结发现 |
| 复核 | 核对对象状态与复核人角色 → 插入 `bz_reviews` → 发现 `candidate → confirmed / dismissed`（CAS） | 首个决定有效；对象已结束则拒绝 |
| 建单 | 核对发现为 `confirmed` 且复核人为 reviewer → 插入 `bz_orders`（发现唯一、幂等键唯一）→ 发现记录工单 | 重复投递返回原工单；取消后 outbox 守卫拒绝新建 |
| 维修反馈 | 工单 CAS（`open` / `reinspection_failed` / `reinspection_unknown` → `reinspection_requested`，轮次 + 1）→ 插入 `bz_rounds` → 以 `wf_triggers` 主键 `internal:<工单>` / `r<轮次>` 启动复检运行 → 记录 `reinspection_run` | 同一请求号重复返回原轮次；并发反馈只成功一个；不会出现有轮次无运行 |
| 复检结算 | 读取本轮反馈、复检运行的巡检输出、复检作业与复核 → 按 `reinspection-v1` 判定 → 轮次结论（只写一次）→ `passed` 时工单 `closed`、发现 `resolved` | 条件复算与状态改变在同一事务；任一 CAS 失败整体回滚 |
| 复检运行终结 | 运行转终态时，未结算的轮次 → `unknown`（`reinspection.run_ended`），工单转 `reinspection_unknown` | 与运行终态同一事务 |
| 参考登记 / 撤销 | 核对证据属本项目本资产且已复核 → 插入 / 更新 `bz_references` + 审计 | 重复登记返回原记录 |

## 4. 与既有数据的连接

- 作业输入：`evidence` / `verifications`（只读）；项目由 `op_bindings` 的任务绑定决定；影像来源由 D054 来源记录决定；复检作业的证据必须来自本轮复检运行经 `requests.requested_by = workflow:<run_id>` 查得的任务。
- 资产键：`<项目>/<范围>/<资产>`。加载了调度目录（P3 共享坐标，多站同名资产已核对为同一实体）时范围为 `*`，否则为任务绑定的站点，因为 P1 / P2 目录中不同站点的同名资产是不同实体。
- 分析来源：结果同时以 `analysis:<作业>` 追加到任务版本的不可变来源记录（D054），报告与证据页照常显示；飞行三元状态、证据复核与报告列不受影响。
- 复检运行：`wf_runs` / `wf_nodes` 的普通运行，触发来源 `internal:<工单>`、事件 `r<轮次>`、发起者为反馈人；其任务仍逐个人工审批。

## 5. 迁移、备份与失败恢复

1. 只在服务带 `--business` 启动时迁移（`--business` 需要 `--workflows` 与 `--catalog`）。不带 `--business` 的服务保持 P3 行为，不创建 `bz_` 表。
2. 前置：`schema_version = 2`、12 张 `op_` 表、`workflow_schema = 1` 与 9 张 `wf_` 表齐全，否则拒绝启动。`business_schema = 1` → 核对 7 张 `bz_` 表后继续；为其他值 → 拒绝启动。
3. 文件库且已有任务 / 请求行时，先用 `sqlite3.Connection.backup` 写 `<state>/backups/ledger-v2-bz-<UTC>.sqlite3`，对备份跑 `PRAGMA integrity_check` 并逐表核对行数，计算 SHA-256；任一不符即拒绝迁移，原库不动。
4. 一个 `BEGIN IMMEDIATE` 事务内执行全部 DDL，写入 `business_schema = 1`、`business_migrated_at`、`business_backup`（路径、摘要、行数）；任何异常 `ROLLBACK`，服务拒绝启动并报告原因。
5. 回退路径：
   - **部署上一版本（P3 候选）**：P3 代码只检查 `schema_version`、`op_` / `wf_` / `sc_` 表，忽略 `bz_` 表，可直接启动，已有数据不丢。限制：固定 P4 工作流目录的活动运行在 P3 代码中无法解析（含新活动与 `model` 分析器），会以 `service.degraded` 报告；回退前应取消或等待 P4 运行结束，并记录。
   - **停服恢复备份**：用 `ledger-v2-bz-*.sqlite3` 替换，丢失迁移后的全部数据，须显式确认后执行。
   - 不把 `schema_version` 升为 `3` 的理由同 D058：旧代码遇到未知版本会拒绝启动，回退只剩恢复备份。

## 6. 演练计划

本地 / CI（批准前即可运行，只作用于临时目录）：

| 演练 | 通过判据 |
|---|---|
| 真实数据迁移 | 用 P2 / P3 S0 用例写出的账本迁移；备份存在、完整性 ok、逐表行数一致；非 `bz_` 对象的 `iterdump`（去掉 `business_` meta 键）迁移前后逐字相同 |
| 重复启动 | 第二次迁移为 no-op，不产生第二份备份 |
| 失败注入 | 让一条 DDL 失败：`business_schema` 不存在、无 `bz_` 表、原数据不变，服务拒绝启动 |
| 旧代码读新库 | P3 的工作流 / 调度存储与迁移在迁移后的库上报告 `current`，P2 / P3 S0 用例在迁移后的库上通过 |
| 备份恢复 | 用备份替换后回到无 `bz_` 表的状态，数据与迁移前一致 |
| 并发 | 两个执行器抢同一作业只一个结果；两个挂载同时开启同一聚合键只一个未结发现；两个 reviewer 首个有效；两个反馈只开一轮 |

云端常驻任务台（批准后、激活 P4 版本前）：在隔离容器（无网络）中复制当前账本，依次执行 P1、P2、P3、P4 四部分迁移演练，记录备份摘要、行数与完整性回执；通过后再激活，服务启动时对真实库再做一次带备份的迁移。回执只含摘要与计数，不含任务内容。

## 7. 批准范围

需要批准的是：在本项目任务服务账本中新增第 2 节的 7 张表、6 个索引与 `business_*` meta 键（`schema_version` 保持 2），以及按第 5 节对常驻任务台现有账本执行带备份的迁移。批准不包括删除或改写任何 v1 / v2 / 工作流 / 调度表与行，也不包括其他应用的数据库。
