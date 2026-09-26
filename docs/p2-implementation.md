# P2 详细方案：持久工作流 v1

[总任务表](operations-implementation.md) · [运营架构 §4](architecture/09-operations.md) · [存储设计](p2-storage-design.md) · [路线图](roadmap.md)

**状态：2026-09-26 设计，实施中。** P1 已在 `b49701b` 关闭，项目身份、预约、领取闸门与任务级取消意图直接复用。P2 交付可重启的巡检—分析—人工复核—模拟工单流程，并联通维修反馈与复检请求；关单质量与真实分析器属于 P4。决策见 D057（语义）与 D058（存储，2026-09-26 已获批准）。

## 1. 验收场景与范围

- **主链**：人工 / 定时 / 事件触发 `asset_check` → 提交单资产巡检 → 人工审批（逐任务，D032）→ 领取闸门 → 飞行与服务复核 → 标注来源的分析 → 人工复核 → 模拟工单 → 维修反馈 → 复检运行（新飞行、新分析）→ 报告。
- **巡回**：`campus_round` 把两处登记标记串成两个子任务（同一机器人，前一任务对账释放后才提交下一任务）。
- **边界**：每次飞行仍是 M2 单资产任务并逐次审批；模板、排班、事件与模型草案都不能批准飞行。分析只用带标注的脚本夹具或确定性颜色特征，结果记为 `scripted` / `deterministic`，不宣称 VLM 准确率。维修反馈只把工单转为待复检；P2 不关单。

S0 用 P1 的三站逻辑世界（真实机载 uplink / guardian / executive + 逻辑飞行与逻辑机场），S1 用 PX4 SITL 上的 `uav_01` 与逻辑机场 `dock_s1`。二者分开计数。

## 2. 对现有代码的改动边界

| 当前入口 | P2 改造 |
|---|---|
| `fleet/service.py` | 新增确定性提交 `submit_workflow`：模板生成的窄草案经同一编译 / 准入 / 软预约，请求、任务、绑定与版本在一个事务内写入；按（请求者，幂等键）对账查询。审批、领取、对账与报告路径不变 |
| `fleet/dispatch.py` / `operations_store.py` | 不改语义；工作流取消复用 `record_cancel` 写入的任务级取消意图 |
| `fleet/api.py`、`runtime/permission.py` | `workflows.*` 方法与五个新 scope；reviewer 获得复核权；`event:` 身份是只能报事件的后端 |
| `console/mission.*` | 工作流面板：模板、运行时间线、等待 / 失败原因、子任务链接、复核与维修反馈、排班启停、草案预览；不增加审批捷径或飞控接口 |
| `fleet/main.py` | `--workflows <目录>`（需同时带 `--catalog`）；启动时迁移工作流扩展表（D058） |
| `admission/models.py` | 请求入口枚举追加 `workflow`（服务侧，非 wire） |

新增：`fleet/workflow_models.py`（WP-P2-01，已完成）、`fleet/workflow_store.py`、`fleet/workflow.py`、`fleet/analysis.py`、`fleet/work_orders.py`、`planner/workflow_draft.py`、`eval/p2_world.py`、`eval/judge_p2.py`、`configs/workflows/`、`configs/scenarios/p2_suite.yaml`、`scripts/remote_p2.py`、`sim/compose.p2.yaml`、`scripts/verify_p2_release.py`。

## 3. 语义要点（D057）

**模板**：`drone.workflow-catalog/v1`，绑定运营目录 ID；节点为 8 种白名单活动之一，参数有类型、拒绝未知字段；条件只有对 `suspected`（分析）与 `decision`（复核）的相等谓词；有向无环，最多 40 个节点、10 个任务；创建工单必须以复核确认为条件；`request_reinspection` 只能启动带内部触发、自身不再请求复检的固定版本模板。目录加载时按运营目录与站点登记表核对机器人、体积与资产。运行固定目录与模板摘要；同一（项目、流程、版本）内容变化即拒绝启动。

**触发**：去重键 `(project, workflow, version, source, event_id)`。人工触发用操作者的请求 ID；定时触发用发生时刻的 UTC 值，只启动启动窗内最近一个到期时刻，其余记错过，补跑需显式上限；时区取固定版本 tzdata，空档 fold=0 顺延、重叠只跑第一次；排班默认停用，启停写操作者、时间与原因。事件只接受模板中绑定的 `event:` 身份，载荷键必须恰为声明输入。内部触发以工单为身份，每张工单一个复检运行。

**权限与身份**：运行的子任务请求者为 `workflow:<run_id>`、入口 `workflow`、规划来源 `deterministic`。有外部效果的活动执行时重查授权：人工运行的发起者、排班的启用者仍须在该项目有 operator；事件运行须其绑定仍在固定模板中。复核只接受项目 reviewer；维修反馈、启动、取消、排班与草案需 operator；viewer 只读；A2A 与第三方一律不可用。

**执行内核**：服务进程内一个引擎，按运行加租约（默认 10 s，接管时 fencing 代次加一）；节点推进是带 `state_version` 的 CAS，开始有外部效果的节点与 outbox 行同事务写入；审核 / 维修等待用持久截止时刻。outbox 至少一次投递，消费者按唯一键去重（任务服务的请求幂等键、工单幂等键、触发主键）。

**任务桥接**：`submit_mission` 的 outbox 投递调用确定性提交，事务开头核对 outbox 行仍由本 worker 领取且取消代次未变；响应丢失时按（请求者，活动键）对账，不换新 ID。`await_mission` 只在任务终态且本任务预约已释放（或从未领取）后结束：目标 `completed` 且服务复核通过 → 完成并输出证据；明确未完成（拒答、驳回、取消、投递过期、未完成列）→ 失败；不确定列 → `outcome_unknown`。D032 的有界重试留在任务内部，工作流不另建任务重飞。

**取消**：先持久化 `cancel_requested` 与取消代次，同一事务作废未领取的 outbox 并为已存在的子任务写入 P1 取消意图；之后不开始任何新节点。已领取或在飞的效果继续对账：子任务全部终态且预约已释放才转 `cancelled`，否则保持 `cancelling` 并显示等待原因。迟到的成功只记录，不启动后继。停用排班与取消本次运行分开。

**流程结果**：全部节点完成或因条件为假（含其下游）跳过 → `completed`；任一节点 `outcome_unknown` 或因此跳过 → `outcome_unknown`；否则有失败 → `failed`。`build_report` 以 `requires: done` 在末尾汇总：逐资产的任务状态与三列报告、证据复核、分析结论与来源、复核决定、工单与复检链接。

**草案（WP-P2-07）**：`workflows.draft` 让规划模型只填一个窄的 `submit_workflow_draft`（资产、可选每日时刻与时区、是否在确认后建单、备注），确定性编译为 `WorkflowSpec` 并用同一校验器检查；结果只返回并记审计，不存为可运行模板，也没有激活接口。生效必须经版本化配置评审。

## 4. API 与入口

| 方法 | scope（角色） | 参数 |
|---|---|---|
| `workflows.list` | `workflow.read`（viewer 起） | `project_id` |
| `workflows.get` | `workflow.read` | `project_id`, `run_id` |
| `workflows.start` | `workflow.run`（operator） | `project_id`, `workflow_id`, `request_id`, `inputs` |
| `workflows.cancel` | `workflow.run` | `project_id`, `run_id`, `request_id`, `reason` |
| `workflows.schedule` | `workflow.run` | `project_id`, `workflow_id`, `trigger_id`, `action`, `reason` |
| `workflows.review` | `workflow.review`（reviewer） | `project_id`, `run_id`, `node_id`, `decision`, `request_id`, `note` |
| `workflows.repair` | `workflow.run` | `project_id`, `order_id`, `request_id`, `note` |
| `workflows.draft` | `workflow.draft`（operator） | `project_id`, `text` |
| `workflows.event` | `workflow.event`（只限 `event:` 后端） | `project_id`, `workflow_id`, `trigger_id`, `event_id`, `payload` |

不可读的项目、运行与工单一律 `service.not_found`。任务台 hri.v0 增加 `workflows` / `workflow` 下行帧与 `workflow_start`、`workflow_cancel`、`workflow_schedule`、`workflow_review`、`workflow_repair`、`workflow_draft`、`workflow_watch` 上行帧；审批仍在原任务视图按任务包哈希进行。

## 5. 工作包落位

| 工作包 | 落位 | 完成判据 |
|---|---|---|
| WP-P2-01 定义与校验 | `fleet/workflow_models.py`、`configs/workflows/` | 已完成：拒绝环、自环、未知活动、自由代码 / URL / 表达式、越项目机器人与资产、无确认的工单、链式复检、超预算；排班语义与 DST 测试 |
| WP-P2-02 持久内核 | `fleet/workflow_store.py`、`fleet/workflow.py` | 四个崩溃窗口恢复；过期 worker 写入被拒；状态与 outbox 不半提交；迁移演练（D058 已批准） |
| WP-P2-03 触发 | `fleet/workflow.py` | 同事件 100 次一个运行；重启不补飞；伪造事件拒绝 |
| WP-P2-04 任务桥接 | `fleet/service.py`、`fleet/workflow.py` | 丢响应不产生第二任务；未知等待；逐次审批 |
| WP-P2-05 取消 | `fleet/workflow.py` | 取消 / 派遣并发、ACK 丢失、重启、迟到成功、未知结果均无后继飞行；收尾未知不显示 cancelled |
| WP-P2-06 业务活动 | `fleet/analysis.py`、`fleet/work_orders.py` | 分析来源标注；工单重试不重复；反馈不等于复检通过 |
| WP-P2-07 入口与草案 | `console/`、`planner/workflow_draft.py` | 等待 / 失败有原因；草案不可生效；注入不扩大范围、不得批准权 |
| WP-P2-08 门禁 | `p2_suite.yaml`、`verify_p2_release.py`、`p2-readiness.md` | 下列矩阵全过；S1 链路 × 3 种子；假成功与重复派飞 0 |

## 6. 故障与验收矩阵

S0（`eval/p2_world.py`，每项 × 种子 7 / 19 / 41，独立裁判 `eval/judge_p2.py` 在线与按录制各判一次）：

| ID | 注入 / 操作 | 期望 |
|---|---|---|
| P2-F01 | `campus_round`：两处标记依次巡检，红色疑似 → 复核确认 → 工单 | 两次飞行；第二任务在第一任务对账后提交；蓝色不进入复核；运行完成 |
| P2-F02 | `asset_check`：工单 → 维修反馈 → 复检运行；工单创建回执丢失后重投；重复反馈 | 一张工单；反馈只转待复检；复检运行新飞行一次；两次运行完成 |
| P2-F03 | 四个崩溃窗口：outbox 提交前、outbox 提交后未投递、任务已受理但响应丢失、节点完成入账前 | 每个活动恰一个任务；四个运行都完成 |
| P2-F04 | 同一事件 100 次；同一人工请求 ID 重复；不同事件 ID | 各自只一个运行；新事件新运行 |
| P2-F05 | 未绑定身份的事件、跨项目事件、多余载荷键、载荷越界、人员调用事件方法、事件身份读取 | 全部拒绝，无运行 |
| P2-F06 | 排班启用；到期；启动窗内重启；停机越过启动窗；停用；viewer 启用 | 每个到期时刻至多一个运行；错过的只记录；停用后不触发；越权拒绝 |
| P2-F07 | 引擎未投递前取消 | 无任务；outbox 作废；运行取消 |
| P2-F08 | 领取后投递前取消；投递后审批前取消 | 前者不建任务；后者任务取消意图阻止审批与领取；运行取消 |
| P2-F09 | 飞行中取消 | 转发取消；对账释放后才 `cancelled`；无分析、无后继任务 |
| P2-F10 | 飞行中取消 + 账本行丢失 + 服务重启 | 预约不确定期间保持 `cancelling`；重启后不派新任务；行到达后取消完成 |
| P2-F11 | 任务飞完后、引擎记录前取消（迟到成功） | 迟到结果只记录；无分析与后继；运行取消而非完成 |
| P2-F12 | 媒体上传丢失（证据同步中）后恢复 | 等待证据期间运行不完成、不分析；证据复核通过后才分析 |
| P2-F13 | 审批人驳回第一任务 | 该分支失败、另一分支继续；运行失败；报告不写已完成 |
| P2-F14 | 双 worker：持租约者冻结、租约过期、另一 worker 接管后原 worker 恢复写入 | 旧代次写入被拒；每个活动恰一个任务 |
| P2-F15 | 跨项目读取 / 启动 / 取消 / 复核 / 反馈 / 排班 / 事件；viewer 启动；无 reviewer 的复核；A2A 启动；伪造模板；草案注入 | 全部拒绝且无数据；伪造模板加载失败；草案只作为草案返回 |

S1（`scripts/remote_p2.py`，PX4 SITL + 逻辑机场）：`p2_s1_chain` × 7 / 19 / 41（`asset_check` 红色：两次飞行，含复检），`p2_s1_cancel_in_flight` × 7（`campus_round` 飞行中取消，第二任务从不提交），`p2_s1_restart` × 7（飞行中重启任务服务，同一运行完成）。S1 另跑 M2 飞行裁判与 P1 派遣检查。

裁判计数：`duplicate_dispatch`（同一活动键多于一个任务或一次飞行）、`post_cancel_dispatch`（取消后新建任务、领取或起飞）、`post_cancel_successor`（取消后开始非收尾节点）、`false_success`（运行完成但存在未证实 / 未知巡检，或报告虚报）、`false_order`（无 reviewer 确认的工单或重复工单）、`project_escape`、`lost_run`（受理的触发没有运行或运行停在非预期状态）。P1 的派遣 / 释放计数同时核对。

## 7. 门禁与接手

`scripts/verify_p2_release.py` 判据：scope（相对 `b49701b` 的改动在 P2 路径内，机载未改则不需 M1 回归）、checks（云端全量）、adversarial（M2 语料 + 工作流草案语料，授权 / 可生效草案 0）、s0_matrix（当场运行 P2-F01–F15 × 3）、p1_regression（当场运行 P1 S0 矩阵）、s1、m2_regression（18/18）、desk（带工作流目录激活、迁移演练与实际迁移、页面 / 脚本 / 健康一致、工作流入口拒绝注入与控制帧）、desk_session（经任务台运行一条工作流到模拟工单）、historical（M3 / P0 / P1 记录不变）。缺证据为 `missing`。

P3 接手物：运行 / 节点 / outbox 的持久边界与 fencing、触发去重与排班、任务桥接与取消代次、工单与复检链接。P2 完成不证明多机调度、VLM 识别、复检关单质量或机场硬件。
