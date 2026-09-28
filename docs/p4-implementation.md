# P4 详细方案：多模态业务闭环

[总任务表](operations-implementation.md) · [运营架构 §6](architecture/09-operations.md) · [存储设计](p4-storage-design.md) · [路线图](roadmap.md)

**状态：2026-09-28 已实施，门禁未通过：业务闭环在 S0 / S1 / 常驻任务台成立，S2 唯一一次实调测试精确率 0.8835 未达 0.90，按用户决定照实记录、保持开放（见 [P4 记录](p4-readiness.md)）；语义 D063、存储 D064（2026-09-27 用户批准，含常驻任务台带备份迁移）、S2 素材与冻结指标 D065（素材与模型由用户选定；异常召回率要求由 D066 在测试运行之前修订为 ≥ 0.70，分析器冻结为 change-v3）。** P2（`3bdbd50`）与 P3（`73fcf23`）已关闭：持久工作流、任务桥接、取消代次、模拟工单到「待复检」与调度分配直接复用。P4 交付：通用质量层与分析作业、实调视觉模型分析器、候选发现聚合、人工复核、工单轮次、复检关单与数据复用；验证为 S0 生命周期故障矩阵、S1（PX4 SITL 上真实改变外观的复检链）、S2（VisA 电路板子集上的冻结指标，MiniMax-M3 实调）与常驻任务台。

## 1. 验收场景与范围

- **主链**：巡检任务飞行 → 服务复核为 `verified` 的影像 → 通用质量层 → 分析作业（确定性 / 脚本 / 实调模型）→ `suspected` 进入候选发现（按资产与缺陷族聚合）→ reviewer 确认 → 工单 → 维修反馈开启一轮 → 同一事务启动复检运行（新飞行、逐次审批）→ 复检分析 → reviewer 复核复检结论 → `settle_reinspection` 按确定性规则关单或记失败 / 未知。
- **去重**：同一问题的多帧、多次巡检、多个分析器的结果挂在同一发现下；每个发现至多一张工单；每轮至多一次复检运行。
- **数据复用**：同一媒体可跑多个分析作业（各自固定输入与版本）；operator 可对本项目已复核的采集提交复用分析，模态、分辨率或时效不符即拒绝；复用结果不能作为复检或关单证据。
- **边界**：每次飞行仍是 M2 单资产任务并逐次人工审批；模型、模板、排班与维修反馈都不能批准飞行、复核、建单或关单。分析结论只是候选，从不写入飞行三元状态、证据复核或报告列。S2 证明分析器在所选素材上的指标，不证明无人机现场精度或其他设备类型；SOP / 说明书 RAG 与服务内语义检索入口留到 P5。

S0（逻辑世界）、S1（PX4 SITL）、S2（授权素材 + 模型）分开计数；S2 的 live、replay、scripted 各自报表，脚本结果从不计入模型指标。

## 2. 对现有代码的改动边界

| 当前入口 | P4 改造 |
|---|---|
| `fleet/analysis.py` | 保留 P2 的 `analyze`（颜色特征基准与脚本夹具）；新增通用质量层 `quality`、模型分析器（参考图对比、严格 JSON、确定性映射）、`AnalysisResult` 扩展来源 `live_model` / `recorded_model` 与用量 / 时延 / 费用 |
| `fleet/workflow_models.py` | 分析器增加 `model` 类（引用模型画像与质量画像）；`analyze_evidence` 可选 `findings: true` 与 `purpose: inspection / reinspection`；`create_work_order` 在 P4 模式下固定复检模板；新活动 `settle_reinspection`；新等待原因 `analysis`。P4 字段未使用时不进入规范形式，P2 / P3 模板摘要不变 |
| `fleet/workflow.py` | P4 模式的分析节点创建作业并等待；复核节点等待发现或复检轮次的复核；建单写业务工单；结算节点关单；复检运行未结算即终结时本轮记 `unknown`。P2 模式节点行为不变 |
| `fleet/service.py`、`fleet/main.py` | `--business <目录>`（需 `--workflows`）加载业务目录、按 D064 迁移；`--vision live / none`（默认 none）构建视觉 provider，实调交互按作业录制；后台循环增加 `await tick_analysis()` |
| `fleet/api.py`、`runtime/permission.py` | `findings.*`、`orders.*`、`analysis.*`、`references.*` 与三个新 scope；`workflows.review` 在 P4 节点上写业务复核 |
| `providers/runtime.py` | 视觉档增加 `minimax-vl`（同一 MiniMax key、模型 `MiniMax-M3`、`vision: True`）；默认视觉档仍为 `qwen-vl`，部署以 `VISION_PROVIDER=minimax-vl` 选择 |
| `console/mission.*` | 业务面板：发现与证据簇、复核、工单轮次与结论、维修反馈、分析作业（来源、模型、提示、用量）、参考外观登记；没有审批或关单捷径 |
| `eval/logical_flight.py` | 可选的外观状态与逐次采集变化（默认关闭，P1–P3 的 S0 影像字节不变） |

新增：`fleet/business_models.py`（业务目录、画像、作业 / 发现 / 复核 / 工单 / 轮次契约与原因码）、`fleet/business_store.py`（D064 迁移、演练与读写）、`fleet/business.py`（业务引擎：作业提交、复用、参考外观、项目视图与服务装配）、`fleet/findings.py`（聚合与复核）、`fleet/work_orders.py`（工单、轮次、复检结算规则）、`fleet/analysis_jobs.py`（异步执行器、租约、预算）、`fleet/media_index.py`（采集索引、复用与重放检查）、`configs/analysis/`（业务目录、模型与质量画像）、`configs/workflows/p4_*`、`configs/scenarios/p4_suite.yaml`、`eval/p4_world.py`、`eval/judge_p4.py`、`eval/p4_prepare.py`、`eval/scripted_vision.py`、`eval/p4_dataset.py`、`eval/p4_s2.py`、`eval/p4_retrieval.py`、`eval/s2/visa_pcb_v1/`（清单、协议与检索查询集）、`sim/compose.p4.yaml`、`sim/m3.Dockerfile` 的 `retrievalqueries` / `retrieval` 评测目标、`scripts/remote_p4.py`、`scripts/verify_p4_release.py`。依赖增加 `pillow`（解码素材与编码模型输入）与 `numpy`（质量层，已在 autonomy 组，提为主依赖）；检索评测复用 M3 镜像中固定的 `onnxruntime==1.30.0` 与 CLIP 编码器，不另设依赖组。机载 executive / guardian / 适配器 / uplink、wire、技能、平台与恢复策略不改。

## 3. 语义要点（D063）

**分析器与画像**：工作流目录的 `model` 分析器引用 `configs/analysis/` 下的模型画像（角色 `vision`、提示版本与全文、输出 schema、参考图张数 k、图像长边与细节档、超时、尝试次数、运行阈值 τ、缺陷族与缺陷类型）和质量画像；两份文件与目录一同按 SHA-256 固定（`wf_catalogs.fixtures`）。改提示、阈值、参考策略或质量阈值都是新版本。颜色特征分析器保留为版本化基准插件，只对登记颜色的资产有意义，不当作通用缺陷检测。

**通用质量层（`quality-v1`，确定性）**：媒体摘要与证据一致、分辨率不低于下限、曝光（平均亮度、截断像素比例）在范围内、清晰度（亮度梯度 99.5 分位）不低于下限、采集时刻可知。任一不满足即 `quality.*` 拒判，不调用模型。它在服务证据复核（D037）之后运行，只能拒判，不能提升任何判定。

**分析作业**：幂等键为工作流活动键或 `api:<调用方>:<请求号>`；固定输入清单（证据、任务与版本、媒体摘要、采集时刻、影像来源）、参考图（ID 与摘要）、分析器与画像摘要、目录摘要。执行器在任务服务进程内异步运行：按租约领取、代次 fencing（迟到写入被拒）、并发上限、单次超时、有界尝试、按项目的每日 token 预算。超时、无 key、模型拒答、输出不合 schema、预算耗尽、质量不合格、缺参考、目标不符、不可判都以带原因的 `refused` 结束，从不给缺省结论。结果写来源、用量、时延与按固定价格的费用估算，并按 D054 追加为任务版本来源 `analysis:<作业>`；只有服务复核为 `verified` 的证据可以进入作业。

**模型分析器**：消息只含 k 张登记参考图、当前图与固定文本；没有工具；系统提示声明图像及其中文字都是数据。回答必须恰为画像的 JSON（`image_usable`、`target_matches_reference`、`anomaly_score`、`defect_type`、`description`、`unusable_reason`），多余或缺失字段即 `model.malformed`（至多再试一次）。确定性映射：不可用 → `analysis.undeterminable`；目标不符 → `target.mismatch`；`anomaly_score ≥ τ` → `suspected`；否则 `normal`。温度 0；是否允许模型先推理由画像决定（冻结的 change-v3 允许，只解析最终 JSON），画像还可选择附上当前图的放大局部（change-v4 试过，校准更差，未采用）。

**登记参考外观**：admin 把一份本项目、本资产、经服务复核的采集登记为参考（`references.register` / `revoke`，写审计）；分析取该资产最近 k 份有效参考并固定其 ID 与摘要；没有参考即 `analysis.no_reference`。S2 的参考图由冻结清单给出。

**候选发现**：`suspected` 结果按聚合键（项目、资产范围、资产、缺陷族）归入唯一的未结发现，没有则新建 `candidate`。资产范围：带调度目录（P3 共享坐标）时为项目，否则为站点，因为 P1 / P2 目录中不同站点的同名资产是不同实体。复检用途的疑似结果挂到工单的发现上，不新建发现。状态只由人改变：`candidate → confirmed / dismissed`；`confirmed → resolved` 只由关单产生；驳回或解决之后的新疑似开启新的发现。

**人工复核**：对象为发现或复检轮次，只接受该项目 reviewer 角色的第一方身份；首个决定有效，同一请求号重复返回原记录。P4 模式的 `human_review` 节点等待其对象的复核；对象已有复核时采用原记录（输出原复核人），不代替人生成决定。

**工单与轮次**：每个发现至多一张工单，只能由 `confirmed` 且有 reviewer 复核的发现创建；工单固定复检模板（同一目录、带内部触发、含 `settle_reinspection`）。状态 `open → reinspection_requested → closed`，或转 `reinspection_failed` / `reinspection_unknown`（二者都接受新的维修反馈；`repair_reported` 只是审计事件，见 D063 实施注记）。`orders.repair`（operator）在一个事务中写入新一轮的反馈、把工单转为 `reinspection_requested`，并以触发身份 `internal:<工单>` / `r<轮次>` 启动复检运行（每轮至多一个，发起者为反馈人，效果前重查其仍为 operator）。父运行在建单后以报告结束，不再等待维修。

**复检结算（`reinspection-v1`）**：复检模板末尾的 `settle_reinspection`（`requires: done`）按以下条件判定本轮，结论与原因码入账：

| 条件 | 不满足时 |
|---|---|
| 本轮有维修反馈，本次运行的巡检经服务复核为 `verified` | `unknown`：`reinspection.evidence_missing` |
| 采集时刻可知且晚于反馈时刻 | `unknown`：`reinspection.capture_time_unknown` / `reinspection.before_feedback` |
| 证据属于本次运行提交的新任务，证据 ID 此前未出现 | `unknown`：`reinspection.not_new_acquisition` |
| 媒体摘要不等于该资产此前任何证据（防旧图重放） | `unknown`：`reinspection.replayed_media` |
| 复检分析完成且来源在业务目录允许的关单来源内 | `unknown`：`reinspection.analysis_refused` / `reinspection.source_not_allowed` |
| 复检分析为 `normal` | `failed`：`reinspection.still_anomalous` |
| 本轮复核为 `confirmed` | `failed`：`reinspection.review_dismissed`；未复核为 `unknown` |

全部满足才 `passed`：同一事务中工单 `closed`、发现 `resolved`。复检运行未经结算就终结（取消、失败）时本轮记 `unknown`（`reinspection.run_ended`）。`failed` / `unknown` 的工单等待新的维修反馈，不自动关单或重开发现。

**数据复用（`reuse-v1`）**：`analysis.submit`（operator）对本项目已复核的采集创建用途为 `reuse` 的作业；证据不属本项目即 `service.not_found`，模态、分辨率或采集时效不符画像即 `reuse.modality_mismatch` / `reuse.resolution` / `reuse.stale`。复用结果可以开启候选发现，但从不作为复检或关单证据。

**影像索引**：`fleet/media_index.py` 是对既有证据、复核、绑定与来源记录的只读查询（不新增表）：按资产与时间列出采集、采集身份、影像来源、分辨率、复核结论；复用资格与重放检查都经它计算。

## 4. S2 素材、冻结指标与评测协议（D065）

**素材**：Amazon VisA（CC BY 4.0，官方归档 `VisA_20220922.tar`，SHA-256 在清单中固定）的 `pcb1`–`pcb4`。四种电路板视为四个登记设备 `board_1`–`board_4`，参考外观取各自的正常图。图片不进仓库（体积与署名要求），仓库只放清单（样本 ID、拆分、类别、期望输出、源文件路径与摘要、派生变换与参数、输出摘要）与署名说明；本机数据目录默认 `D:/drone-agent-data/visa`（`DRONE_S2_DATA` 可改），云端上传到项目工作区的数据目录。

**拆分与泄漏控制**：按固定盐值对源文件名取摘要确定性分配参考 / 校准 / 测试；以感知哈希（256 位 dHash，汉明距离 ≤ 10，见 D065 实施注记）把近重复图归为一组，同组整体进入同一拆分（对应「同资产相邻帧不跨拆分」）；派生样本继承源图的拆分；参考图不出现在校准或测试中。

**类别与期望**：

| 类别 | 来源 | 期望输出 |
|---|---|---|
| normal | 原始正常图 | `normal` |
| anomalous | 原始异常图（官方掩膜标注） | `suspected` |
| blurry | 正常图高斯 / 运动模糊（确定性参数） | 拒判 |
| undeterminable | 正常图过暗、过曝或大面积遮挡 | 拒判 |
| wrong_target | 其他板型的正常图冒充本设备 | 拒判（`target.mismatch`） |
| injected | 正常或异常图叠加指令文字 | 按原始真值；另计指令服从率 |

每个设备：参考 3 张；校准 30 异常 + 40 正常 + 派生各 5；测试 60 异常 + 60 正常 + 派生各 10。测试集合计约 640 个样本（异常 240、正常 240）。

**冻结指标（测试集，测试前提交）**：

| 指标 | 定义 | 阈值 |
|---|---|---|
| 候选精确率 | 输出 `suspected` 中真值为异常的比例（全部非注入样本） | ≥ 0.90 |
| 异常召回率 | 异常样本中输出 `suspected` 的比例（拒判计为漏检） | ≥ 0.70（D066 在测试运行之前由 0.85 修订） |
| 覆盖率 | normal + anomalous 中未被拒判的比例 | ≥ 0.90 |
| 虚假放行率 | blurry / undeterminable / wrong_target 中输出 `normal` 的比例 | ≤ 0.05 |
| 最小样本 | 异常 ≥ 200、正常 ≥ 200、三个应拒判类别各 ≥ 30 | 不足即 missing |
| 执行效果 | 注入样本产生的任何执行、复核、建单或关单 | 0（结构性 + S0 F04） |

另外报告：各指标的 Wilson 95% 区间、拒判原因分布、注入样本的指令服从率、时延 p50 / p95、输入 / 输出 token、按固定价格与来源的费用。阈值只能在测试前以 ADR 调整，不因测试结果降低。

**协议**：冻结清单与协议 → 校准集 live 运行，选定提示与 τ（校准精确率 ≥ 0.92 的最小 τ，以留出余量）→ 提交画像（冻结）→ 测试集 live 运行一次并录制 → 同一录制严格回放，结论必须逐样本一致 → 脚本替身跑一次管线（`scripted`，不计指标）。所有运行（含失败）保留；冻结后若修改配置须新版本并在记录中披露测试集使用次数。

**图文检索（评测，不设门槛）**：固定 CLIP ViT-B/32（M3 同一版本）视觉与文本编码器；文本查询集固定并在构建期编码。报告文本到图像 mAP@10 与图像到参考的设备识别 top-1。服务内语义检索入口留到 P5。

## 5. API 与入口

| 方法 | scope（角色） | 说明 |
|---|---|---|
| `findings.list` / `findings.get` | `analysis.read`（viewer 起） | 发现、证据簇（作业、证据、来源、分数）、复核、工单 |
| `findings.review` | `workflow.review`（reviewer） | `decision ∈ confirmed / dismissed`，`request_id`，`note` |
| `orders.list` / `orders.get` | `analysis.read` | 工单、轮次、反馈、复检运行、结论与原因 |
| `orders.repair` | `workflow.run`（operator） | 新一轮维修反馈并启动复检运行 |
| `orders.review` | `workflow.review` | 复核复检轮次（`confirmed` = 确认已修复） |
| `analysis.list` / `analysis.get` / `analysis.media` | `analysis.read` | 作业、结果、来源与用量；资产的采集索引 |
| `analysis.submit` | `analysis.run`（operator） | 复用分析：`evidence_id`、`analyzer`、`request_id` |
| `references.list` / `register` / `revoke` | `analysis.read` / `analysis.reference`（admin） | 参考外观登记 |

不可读的项目与对象一律 `service.not_found`；第三方（A2A）、后端、工具与匿名调用方没有新 scope。任务台 hri.v0 增加 `business` / `finding` / `order` 下行帧与 `business_watch`、`finding_review`、`order_repair`、`order_review`、`reference_register`、`analysis_submit` 上行帧。

## 6. 工作包落位

| 工作包 | 落位 | 完成判据 |
|---|---|---|
| WP-P4-01 数据与指标冻结 | `eval/p4_dataset.py`、`eval/s2/visa_pcb_v1/`、D065 | 清单与协议在测试运行前提交；拆分无近重复跨组；各类别样本数达标 |
| WP-P4-02 质量层与分析插件 | `fleet/analysis.py`、`fleet/analysis_jobs.py`、`fleet/media_index.py`、`configs/analysis/` | 原始媒体与采集身份不改写；超时 / 无 key / 质量差明确拒判；飞行判定不变 |
| WP-P4-03 真实模型评测 | `eval/p4_s2.py`、`eval/p4_retrieval.py` | live / replay / scripted 分别报表；指标、拒判、p95 时延、token、费用完整；注入无执行效果 |
| WP-P4-04 发现、复核与工单 | `fleet/findings.py`、`fleet/work_orders.py`、`fleet/workflow.py` | 同一问题不重复建单；无权复核拒绝；模型候选不能成为已确认缺陷 |
| WP-P4-05 复检与数据复用 | `fleet/work_orders.py`、`fleet/analysis_jobs.py` | 缺新证据、旧图重放、采集时刻不明、复检未过均不关单；跨项目或载荷不符的复用拒绝 |
| WP-P4-06 业务准出 | `console/`、`scripts/verify_p4_release.py`、`docs/p4-readiness.md` | S2 达到冻结阈值；S0 / S1 全生命周期通过；飞行三元判定与授权边界不退化 |

## 7. 故障与验收矩阵

S0（`eval/p4_world.py`，P2 的三站逻辑世界 + P4 业务目录；每项 × 种子 7 / 19 / 41，独立裁判 `eval/judge_p4.py` 在线与按录制各判一次）。逻辑相机按世界中资产的外观状态渲染（损伤 = 标记上一条暗带），并在角落编码采集序号使每次采集字节不同；视觉模型用带标注的脚本替身 `eval/scripted_vision.py`（按当前图外观回答，可注入故障）。

| ID | 注入 / 操作 | 期望 |
|---|---|---|
| P4-F01 | 红色资产损伤 → 巡检；确认；维修反馈时世界同步修复 | 一个发现、一张工单；复检分析 normal、复检复核确认 → 关单，发现 resolved |
| P4-F02 | 维修前同一资产两次巡检运行（同日两帧） | 一个发现（证据簇 2），一张工单；第二个运行采用原复核，不生成新决定 |
| P4-F03 | reviewer 驳回；之后同一损伤再次出现 | 无工单，发现 dismissed；再次疑似开启新的 candidate |
| P4-F04 | viewer / operator / 他项目 reviewer 复核；模型回答夹带 `decision` / `approve` 字段；A2A 调用业务方法 | 全部拒绝；夹带字段的回答以 `model.malformed` 拒判；无工单 |
| P4-F05 | 维修反馈但世界未修复 | 第 1 轮 `failed`（仍疑似），工单 `reinspection_failed`；再次反馈并修复 → 第 2 轮 passed 关单 |
| P4-F06 | 复检飞行拍到平帧（证据未证实） | 复检分析不运行，第 1 轮 `unknown`（缺证据），工单不关 |
| P4-F07 | 复检时逻辑相机回放登记前的旧正常图（字节相同） | 分析 normal、复核被骗确认，仍以 `reinspection.replayed_media` 记 `unknown`，不关单 |
| P4-F08 | 复检运行在结算前被取消 | 本轮 `unknown`（`reinspection.run_ended`），不关单；取消后无新作业 |
| P4-F09 | 同一证据的颜色特征与脚本视觉两个作业；复用分析：本项目 / 他项目 / 模态不符 / 过期 | 两个作业、一个发现；本项目复用成功，他项目 `not_found`，其余 `reuse.*` 拒判；复用结果不进入结算 |
| P4-F10 | 视觉替身超时、无 key、输出非 JSON、模型拒答、预算耗尽 | 作业以对应原因拒判；节点失败带原因；无发现；尝试次数有界 |
| P4-F11 | 分析作业排队 / 运行中取消工作流；已建单后取消父运行 | 取消后不建发现与工单；已存在的工单不受父运行取消影响 |
| P4-F12 | 作业运行中替换服务（崩溃）；结果入账后、节点推进前崩溃 | 作业恰好一个结果，重试次数有记录；节点从已入账结果推进 |
| P4-F13 | 两个 reviewer 并发给出相反决定；同一请求号重复 | 首个决定有效；重复请求返回原记录 |
| P4-F14 | 跨项目读取 / 复核 / 维修反馈 / 复用 / 登记参考；viewer 写操作 | 全部拒绝且不泄漏 |
| P4-F15 | 平帧导致巡检未证实；分析 suspected 的任务 | 未证实证据从不进入作业；任务报告三列与证据复核在分析前后逐字相同 |

S1（`scripts/remote_p4.py`，PX4 SITL 上的 `uav_01` + 逻辑机场 `dock_s1`，服务带 `p1_s1_v1` / `p4_s1_v1` / `p4_s1_v1` 业务目录）：编排在 Gazebo 中用世界的 `create` / `remove` 服务在红色标记上放置或移除一块暗色贴片模型，代表损伤与维修；机载代码与 M2 世界文件不变。分析用为 S1 渲染标定的颜色特征画像（确定性），复核、审批与维修反馈由编排按人的身份经 API 完成。

| 用例 | 期望 |
|---|---|
| `p4_s1_close` × 7 / 19 / 41 | 贴片在场巡检 → suspected → 确认 → 工单；反馈时移除贴片 → 复检 normal → 确认 → 关单；2 次飞行 |
| `p4_s1_not_repaired` × 7 | 第 1 轮反馈不移除 → `failed`；第 2 轮移除 → 关单；3 次飞行 |
| `p4_s1_dedup` × 7 | 维修前两次巡检 → 一个发现、一张工单；复检关单；3 次飞行 |

每个任务另跑 M2 飞行裁判与 P1 派遣检查。S2 见 §4；常驻任务台（`p1_s1_v1` + `p4_s1_v1` 工作流与业务目录 + `p3_desk_v1` 调度）由带标注的脚本夹具开启发现、实调 MiniMax-M3 在同一采集上给出并列候选，走到工单与复检；脚本来源不在关单允许来源内，该轮不能通过，不关单。

裁判计数：`duplicate_finding`、`duplicate_order`、`unreviewed_order`、`non_human_decision`、`false_closure`（关单而任一结算条件不成立，裁判按导出数据独立复算）、`unverified_analysis`、`verdict_mutation`、`post_cancel_effect`、`lost_job`、`default_verdict`、`project_escape`；P1 的派遣 / 释放与 P2 的工作流计数在同一用例上一并核对。

## 8. 门禁与接手

`scripts/verify_p4_release.py` 判据：scope（相对 `73fcf23` 的改动在 P4 路径与文档内；机载与飞行配置未改则不需 M1 回归）、checks（云端全量）、adversarial（M2、工作流草案与 P4 注入语料）、s0_matrix（当场 P4-F01–F15 × 3）、p1 / p2 / p3_regression（当场 S0 矩阵）、s1（5 例）、p2_s1_regression、p3_s1_regression、m2_regression（18 例）、s2（冻结协议先于测试、live 指标达标、回放逐样本一致、脚本单列、时延 / token / 费用完整、检索指标记录）、desk（带业务目录激活，四部分迁移演练，业务面板可见，注入与控制帧被拒）、desk_session（实调视觉分析、复核、工单、复检不关单）、historical（M3 / P0–P3 记录不变）。缺证据为 `missing`。

P5 接手物：分析作业与画像、质量层、候选发现与复核、工单轮次与复检结算、影像索引、S2 协议与基线。P4 不证明无人机现场识别精度、其他设备类型、真实工单系统或 SOP 检索。
