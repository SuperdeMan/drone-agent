# P6 道路材料当前交接 · 2026-10-22 B

**WP-P6-01仍开放，P6未整体准出。** 当前事实以本页和[B检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/README.md)为准；[前一检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/../corpus_checkpoint_20261022/README.md)及更早历史保持原样。正式资产、正式配对、冻结数据、正式模型调用仍为0；真人候选计时延期至运营，现场清障未验证，其余D089门槛不变。

工作树`D:/drone-agent-data/p6-road-views-20261014/repo`，分支`work/p6-road-views-20261014`；材料根`D:/drone-agent-data/p6-corpus-completion-20261018`，本批工具`resume-20261022/`。原仓库并行工作未混入。下述“通过”均限定到对应记录的范围，不代表业务模型成绩。

## 当前完成点

| 证据 | 当前事实 | 范围 |
|---|---|---|
| 独立AI参考观察 | 首稿290、选中二审197；正常10/障碍60/不可判81/错目标4/排除源42 | 本批新增4个观察；另重核4个既有边界，不增加观察数。两条原正常改为不可判 |
| 实际绑定 | 197/197通过；本机普通192、云端事前任务2+1+1+1 | [汇总](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/annotation-integrity.json)保留真实分部，最终context v29；无待完成重核、历史未匹配或范围错误 |
| 独立物理核验 | 6个真实路段及固定范围获支持，15个无序对均为不同目标且范围不重叠 | 4个完整边界候选通过；另外2条保留边界未知，业务标签已改不可判 |
| 新跨次核验 | 新检查2对；1对同资产/同状态/不同作者日期获支持，另1对同状态为null | 正常对已补分组证据并经当前核心复核；树冠遮挡对不重跑凑通过 |
| 合并候选小子集 | 6观察、5主资产候选、1跨次配对候选，逐观察/逐对问题0 | [核心报告](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/qa/integrated/core-audit.json)整体仍`missing`；未满足D089全量覆盖和冻结，不是6个独立资产 |
| 来源字节 | 本批实际重核8个不同观察，源派生检查8条获证；组合审核6条是其中子集 | [先前报告](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/qa/partial-source-execution.json)与[组合后执行](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/qa/integrated-source-execution.json)另存，不计成14张素材 |

本批四张跨日期观察的两侧分在不同初标上下文，再由各自另一隔离AI二审；配对核验者看两原图及完整固定范围，不读双方业务答案。新正常对的两张图属于同一资产，仅一张`primary`、另一张`repeat`，不累计成两个正常主资产。[当前进度](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/progress.json)仍有93条首稿待二审（30障碍建议/42不可判建议/21排除源建议，无正常建议）；旧损坏首稿保留，[最终文字检查](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/annotation-text-audit.json)疑点0。

## 边界、分组与可复用证据

沿用用户批准的主路连续宽度口径：不变A/B世界范围内，用接入口两侧可辨主路边缘及连续宽度处理连通前场，不纳入整片前场，不补造真实被树冠/阴影遮住的完整边界。固定地理横截面可以在路中，不强求恰为自然路端；不新增物体大小或占比门槛。

新物理核验对4条边界提出未知后，按事前登记计划对全部4条另起隔离上下文重看，未向新核验者提供旧二审或物理核验答案。[新边界证据](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/boundary/payload-check.json)保留真实分歧和具体依据；两条Laura原正常因树冠遮挡改为不可判，另两条得到支持。旧未知、旧正常和新判断都保留，不按多数投票，不由编排者代行看图。这两条不可判仍是候选，本轮完整边界子集未采用它们，并不新增规则排除边界不可判类；后续仍可在核对完整登记任务、来源和分组后用于拒判评测。

新正常对的物理身份和完整状态来自独立配对者的实际原图核验；相对既有4候选的独立性另由受信源配准证明：两张完整原生影像包络与那4张完整影像包络全部分离。[组合提案](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/grouping/composition-proposal.json)保持原分组未动，另由只读独立元数据/契约审查核对49份文件摘要、复算6个包络和8项分离关系后支持；[采用证据](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/grouping/composition-evidence.json)明确区分视觉身份、地理非重叠和元数据审查。审查者及编排者没有新增视觉判断或真人确认，不能把该方法用于未看图几何组的自动准入。

组合前[跨次来源报告](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/qa/repeat-source/core-audit.json)的4个`grouping_not_verified`保留；组合后另建子集和真实云端审核，[资格记录](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/candidate-qualification.json)为6观察/5主资产/1配对，逐条问题0、整体仍缺失。2个测试地点的完整边界子集与新增正常对所在地点现在在这个小子集内构成3个测试地点；这只对该5主资产小子集有效，不能外推整套197观察已满足站点、独立性或样本量。

新障碍对虽支持同一路段和不同作者日期，但冠下完整状态不足，`same_condition=null`保留；旧配对1也保持未知。新正常对范围为`distinct_author_dated_derived_captures_unordered`，不补造UTC、飞行ID、精确先后、原始帧或清障原因。73个几何计划现有9对双方道路状态二审，3对仅为同确定性类型/范围候选；独立同状态结论不由类别相同自动推出，正式配对仍0。

## 覆盖缺口及下一步

D089仍要求测试260个独立主资产（100正常/100障碍/30不可判/30错目标），校准60个（20/20/10/10）；测试debris、vegetation、other_entity各至少30，至少3个核验地点、单站≤50%；真实同状态配对30对/15资产。误报≤5/100、类型漏检≤20%、可判覆盖≥90%、不可判/错目标误判正常为0、跨次可判且一致≥90%全部保留。

[观察覆盖](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/candidate-coverage.json)中校准正常仅1条，测试正常8条但包含同资产跨次，不能按8个主资产计。测试障碍类型观察为debris25/vegetation12/other_entity11，均非正式独立资产计数。最乐观地暂不扣重复、用途和站点限制，并加旧组外标签，仍至少缺正常62、障碍37、错目标36条参考观察；这是观察缺口下界，不是资产缺口或工时。[结构普查](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/admission-census.json)明确6条已有候选证据和剩余缺项，不能把现有边界或完整任务未知当作已满足。

1. 优先补校准正常及独立主资产。未标中立池仍为前一检查点Laura74、Mussett35；来源角色/固定世界范围保持，不能把测试事件转校准凑数。已核验两条Laura灰标签保留，不为数量改回正常。现有未标校准池或新增公开UAV来源按同样隔离方式补齐。
2. 复用已完成的5主资产/1配对小子集，继续扩真实同状态和不同采集证据至30对/15资产；缺少实际视觉身份或完整状态时不能只用几何推进。现有新正常对不再重复当作新素材计数。
3. [事前任务队列](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/work-queue/prior-task-progress.json)仍为测试11条、已初标/二审5条（4错目标、1不可判），剩6条；校准0。Laura20个预期参考已有2个未解决、18个未检查，先取得可信完整参考再在未初标B上登记任务。原未启动8条分工已由新分工完成5条，剩余预留不释放，不整批重复启动。
4. 满足覆盖、角色冲突、独立分组和真实跨次后冻结真实代码候选及数据，完成同候选检查、真实校准、冻结画像、唯一正式测试、严格回放和`verify_p6_road_business.py`。保留D090有限重试及曝光记录规则；当前不提前冻结或实调。

来源总量仍为CRASAR837+OAM45=882窗口、809几何组；完整原图516，内嵌帧32不作新采集。旧837全量来源结果只由`f132b98`历史回执负责，OAM45不在该范围；本批没有新增素材下载。`#6337`开发关联暂缓组、旧正常130中82暂缓、组外最多48待核对，及335条旧映射的13条未知规则均未改变；旧细节见[前批交接快照](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/history/handoff-before-b.md)。

## 工具、失败与准确版本

- 本批外置工具在材料根`resume-20261022/`，数据解释器`tools-venv/Scripts/python.exe`；SSH用仓库`.venv/Scripts/python.exe`。全量绑定入口为`check_current_ai_records_v9.py`加`review_selection_v5.py`，最终context v29；最新边界32语义复核优先，完成前保留4条待重核，完成后为0。
- 云端旧事前任务绑定用`run_prior_audit_cloud_v6.py --context-version v29 --worker <ID> --expected-count <准确条数> --version <唯一版本>`；本批最终分部v1–v4。之前C目录v8–v11-20261022的context v28检查只保留历史，不混入最终197并集。合并继续用C目录`combine_annotation_audits_v3.py --expected-total 197`。
- 配对原payload用`ingest_pair_payload_v2.py`确定性保存；`pair-context-v2.json`登记独立来源，`check_pair_candidate_v2.py`只核对单对绑定/状态。分组组合须有真实视觉身份、来源和独立契约审查；`build_integrated_pair_candidate_v2.py`及`run_integrated_audit_cloud_v2.py`用于本轮明确子集，不替代全量D089门禁。
- 进度/覆盖/跨次/事前队列仍使用v5/v3/v2/v3工具；最新结构普查`admission_readiness_census_v5.py`区分6个观察与5个资产，主快照未批量改分组字段。新首稿/二审的完整payload按B目录`ingest_completed_payload_v2.py`保存，后续使用新的ID、目录及实际上下文登记。
- 身份核验说明曾被PowerShell编码损坏，严格入库已拒绝并保存原字节；原隔离核验者根据其先前真实观察重建ASCII说明，未重看、未改6条及15对判断。[文字重建核对](../eval/s2/road_business_v1/corpus_checkpoint_20261022b/qa/identity-text-reconstruction.json)保留60个文本字段及全部判断一致性，不能写成从问号无损恢复。一次组合适配器因预期`decision`而实际报告使用`verdict`停止，未创建候选或改分组；随后按真实schema严格适配，原失败保留。
- 元数据审查继承准确输入/核心摘要相符的真实云端来源验证，未声称自己完整重跑来源；本机补充TIFF头重开受Windows自定义opener限制的失败也已披露。当前源字节与分组/标签/配对结论只按各自实际审核范围报告。

冻结核心仍为`f132b98fd0f6148ca3ab5f7e168de147433fc7be`，业务核心SHA`d0aacfc56845ff6e8af5f02e5366a30b41fb90d9f7bea97f4c0b5e023a3e2fe2`；云端本批逐次核对4个冻结核心文件。原软件快照596个文件中595个摘要相同，唯一差异为此前用户批准的`target_boundary_policy`协议新增字段，原有协议键值/D089门槛未变。Ruff和2074通过/1项既有Windows跳过仅负责原`f132b98`快照，本批无应用代码改动、不借用旧全套成绩；正式模型调用0。真人效率、现场清障继续未验证。
