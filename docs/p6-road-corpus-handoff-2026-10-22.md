# P6 道路材料当前交接 · 2026-10-22

**WP-P6-01 仍开放，P6 未整体准出。** 本页和[2026-10-22检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261022/README.md)是当前接手入口；[2026-10-21历史交接](p6-road-corpus-handoff-2026-10-21.md)与[增量B](../eval/s2/road_business_v1/corpus_checkpoint_20261022/../corpus_checkpoint_20261021b/README.md)保留各自证据范围。当前有3条完成单条审核的候选；正式独立资产、正式跨次配对、冻结数据及正式模型实调仍为0。真人候选计时延期至运营，现场清障未验证，其余D089门槛不变。

工作树 `D:/drone-agent-data/p6-road-views-20261014/repo`，分支 `work/p6-road-views-20261014`；材料根 `D:/drone-agent-data/p6-corpus-completion-20261018`，本轮工具 `resume-20261021-c/`。原仓库的并行X1工作未混入。检查点按本地日期命名，原始回执保留实际UTC时间。

## 已完成与准确范围

| 项目 | 当前事实 | 验证边界 |
|---|---|---|
| 独立AI参考观察 | 首稿286条、选中二审193条；正常10、障碍58、不可判79、错目标4、排除源42 | 相对B增加11条首稿和11条二审；另有3条旧目标按新口径独立复核，未增加观察数；不是193个已准入资产 |
| 标注绑定 | 普通188条在本机；事前任务2+1+1+1条在云端实际源字节根核对，193/193通过 | [汇总](../eval/s2/road_business_v1/corpus_checkpoint_20261022/annotation-integrity.json)是不同实际执行分部的并集；无待完成重核、历史未匹配或范围错误 |
| 单条候选审核 | 3个真实不同路段已核对身份、固定范围及批准口径下的边界；云端f132b98核心3条均无样本级问题 | 1个校准、2个测试；[整体报告](../eval/s2/road_business_v1/corpus_checkpoint_20261022/qa/candidate-audit/core-audit.json)仍为`missing`，未冻结且覆盖不足；三者间身份比较不是稳定性配对 |
| 校准参考登记 | Laura预留参考20条中本轮实际检查2条，均`unresolved`；18条尚未检查 | 不生成新错目标任务，不把未解决的参考强行画成完整目标 |
| 观察来源 | CRASAR837加OAM45，共882窗口、809几何组；完整原图仍516张，内嵌帧32个 | 本轮未新增采集；窗口、内嵌帧及几何组不算独立资产 |
| 软件候选 | `f132b98fd0f6148ca3ab5f7e168de147433fc7be`已有Ruff通过、2074测试通过/1项既有Windows Unix-socket跳过/0失败 | 本批只改文档、协议材料与外置数据工具；逐一核对原软件快照596个文件：595个摘要不变，1个协议草案仅增加已批准的`target_boundary_policy`；不把旧全套成绩转借给文档提交 |

当前[进度](../eval/s2/road_business_v1/corpus_checkpoint_20261022/progress.json)仍有93条已保存首稿待二审（没有正常建议），[文字检查](../eval/s2/road_business_v1/corpus_checkpoint_20261022/annotation-text-audit.json)最终疑点0；旧损坏首稿保持原样。供盲标者使用的新中立池为[Laura74条](../eval/s2/road_business_v1/corpus_checkpoint_20261022/work-queue/laura-neutral-primary.json)和[Mussett35条](../eval/s2/road_business_v1/corpus_checkpoint_20261022/work-queue/mussett-neutral-primary.json)，不含业务类别，也不是新采集。

[结构普查](../eval/s2/road_business_v1/corpus_checkpoint_20261022/admission-census.json)中只有101条具有格式有效的完整任务多边形；50条不可判仍缺这项，另外42条排除源不应进入正式业务分母。有效多边形只说明结构有效，不能证明物理身份或边界。拒判类也必须有事前完整模型任务，不能利用缺轮廓暴露答案；无法从当前图可靠登记时，应先取得真实参考再定义任务，不能据首稿标签补造边界。

既有CRASAR837/837来源复核仍由[B检查点原回执](../eval/s2/road_business_v1/corpus_checkpoint_20261022/../corpus_checkpoint_20261021b/qa/full-crasar-source-f132b98.json)负责：691旧窗口、Mussett46、Laura100，在f132b98实际读取缓存源字节、原生窗口及独立回放。OAM45未纳入那一轮；本轮新增的实际来源检查只覆盖3条候选和事前任务绑定。软件完整回归及业务门禁分别见[原软件记录](../eval/s2/road_business_v1/corpus_checkpoint_20261022/qa/software-f132b98.json)与[missing](../eval/s2/road_business_v1/corpus_checkpoint_20261022/qa/business-gate-missing.json)，不声称全量模型质量已验证。

归档初检曾把已批准协议数据也要求为原软件快照字节，因而停止；核对确认只有该新增边界字段，原有所有协议字段与D089门槛逐项相同。初检失败、原软件协议字节及准确595/596范围见[范围纠正](../eval/s2/road_business_v1/corpus_checkpoint_20261022-scope-correction.json)。本批未重新执行完整软件回归，也不把旧成绩转借给新协议。

## 主路边界及三条候选

用户已采用“接入口两侧可辨认的主路边缘及连续宽度”口径。固定A/B世界范围不动，道路与出入口、支路或停车前场相连时不纳入整片前场；两侧边缘不足以支持连续主路宽度，或树冠/阴影遮住完整横截面时，仍不可判。该约定已进入[协议](p6-business-evaluation.md)、[标注指南](p6-road-labeling-guide.md)和协议草案，未改变D089数量、类型或质量门槛。

独立身份核验先确认三个路段及范围，但在此前“全幅材料外轮廓”解释下保留3个未知；[原始证据](../eval/s2/road_business_v1/corpus_checkpoint_20261022/observers/physical_reviewer_1/identity-evidence-v1.json)未覆盖。用户确认后，新的干净隔离核验者实际重看原图及完整原生裁片，分别说明前场连接、阴影下可辨边缘和交叉口连续宽度，3条边界均有支持；其中1条收回路口圆角外扩，固定端点不变。[新证据](../eval/s2/road_business_v1/corpus_checkpoint_20261022/boundary/payload-check.json)与[候选资格登记](../eval/s2/road_business_v1/corpus_checkpoint_20261022/candidate-qualification.json)另存，未倒改未知。

`identity-candidate-corpus-v2/`已有3条摘要绑定的manifest/labels/未冻结protocol，并在云端真实源字节根执行核心审核；后续可复用这些证据接入统一资产登记，仍须核对完整语料的角色、隔离和覆盖。主观察总快照的`grouping_verified=false`未批量改写；结构普查另行核对并标明3条已有候选证据，不能把它们当作从未完成视觉核验，也不能据此放行完整语料。当前测试候选只来自2个站点，不能把含校准的3个地点冒充达到测试至少3地点。

## 覆盖、真实跨次和错目标

测试仍须260个独立主资产（正常100/障碍100/不可判30/错目标30），校准60个（20/20/10/10）；测试debris、vegetation、other_entity各至少30，至少3个核验地点、单站不超过50%。真实同资产同状态配对须30对/15资产。误报≤5/100、类型漏检≤20%、可判覆盖≥90%、不可判/错目标误判正常为0、跨次可判且一致≥90%全部保留。

[覆盖统计](../eval/s2/road_business_v1/corpus_checkpoint_20261022/candidate-coverage.json)区分预登记用途和拟议分区；按最乐观假设把旧组外标签与现有二审都当成独立且可自由分配的有效观察，仍至少缺正常62、障碍39、错目标36条参考观察。该数未扣重复、用途冲突或站点限制，只是观察缺口下界，不能当作独立资产缺口或工时估计。

[跨次覆盖](../eval/s2/road_business_v1/corpus_checkpoint_20261022/repeat-coverage.json)：73个几何计划中7对有双方道路状态二审，1对暂为同确定性类型/范围候选，正式配对仍0。此前独立配对核验虽支持同路段与不同作者日期，但因遮挡不能确认同状态，原`same_condition=null`继续保留。22对“中心线两侧10m源像素可用”仅安排检查优先级，不是物体门槛。事前目标任务即使当前图有道路，也不能作为该图所在物理路段的状态标签；新统计已排除这类记录。

11个事前登记任务均为披露构造的测试侧候选，保持真实参考A与当前B、目标描述和多边形不动，不声称自然操作者拍错。[队列](../eval/s2/road_business_v1/corpus_checkpoint_20261022/work-queue/prior-task-progress.json)现有5条已独立初标/核验，其中4条wrong_target，其余按证据保留不可判；尚有6条未初标。校准任务仍0。原`prior_annotator_1`的8条准备分工从未启动，已完成的5条由实际新分工承接，剩余预留不释放，不能再次整批启动。

主观察与参考用途在业务标注前登记：Mussett整事件预留测试侧、Laura整事件预留校准侧；Laura20参考/80主观察不因结果改角色。`#6337`同型号/同日/间隔≤15分钟仅是开发关联暂缓组，非实际飞行ID；旧正常130中82暂缓、最多48条组外仍待去重及现口径核对。旧335映射中的13条未知均不可判，不能把可能组外124写成已知组外124。其余隔离、许可、未知作者日期及Santolan不完整对象边界沿用[历史交接](p6-road-corpus-handoff-2026-10-21.md)，未补造采集时间或把不完整源计可用。

## 证据问题及工具入口

一次新边界分工意外夹带旧二审字段，核验者报告后立即停止，未保存任何二审结果；[退休记录](../eval/s2/road_business_v1/corpus_checkpoint_20261022/boundary/retirement.json)绑定原分工、停止回执和事前替换计划。随后严格按中立首轮分工白名单重建，另起`fork_turns=none`上下文完成全部3条。[完成后选取检查](../eval/s2/road_business_v1/corpus_checkpoint_20261022/boundary/selection-check-after.json)确认新语义复核优先于旧格式确认，没有把退休上下文记成独立结果或清除其他未完成任务。

外置合并器曾遇到两个**未选中**快照的Windows/Linux换行不同而停止；[原失败](../eval/s2/road_business_v1/corpus_checkpoint_20261022/history/interim-union-byte-conflict.json)及两份原字节均保留。v3只合并各分部实际选中的摘要绑定快照，仍拒绝空检查、缺条数、重复观察和未完成重核，未改摘要消除冲突。

一条事前任务payload把`task_mismatch.evidence`写成列表而非字符串，严格入库已拒绝；原始字节保留，由原核验者无损连接原有证据字符串，未新增看图或改判。过早依赖入库结果的v6控制器因缺少`reviews.json`在本机停止，未上传或启动云端审核；实际成功审核另用v7，原失败不改写。[拒绝记录](../eval/s2/road_business_v1/corpus_checkpoint_20261022/history/prior-format-rejection.json)与原核验者格式修正记录均保留。

所有首稿/二审原payload、分工、检查文件摘要和确定性入库回执保留在[观察者目录](../eval/s2/road_business_v1/corpus_checkpoint_20261022/observers)。初标23和事前初标3都在完整payload已声明完成并存储后才被编排器停止；没有把未完成批次宣称完成。`reviewed_at/final.inspected_at`是存储时间，不是额外看图或真人计时。编排者已看过待评提示，始终不代行留出图视觉裁定。

- 数据工具用材料根`tools-venv/Scripts/python.exe`（Pillow/Shapely/rasterio，未安装pyproj）；SSH用仓库`.venv/Scripts/python.exe`。不修改`.env`，不自动启动本地真栈或常驻服务。
- 完成payload原样存储用B目录`ingest_completed_payload_v2.py`；事前任务二审用B目录`ingest_completed_prior_review_v1.py`，只处理实际完成批次。每次真实启动独立上下文后才`resume-20261021/register_actual.py`，再`context_v2.py --version <新版本>`；本批为v25。
- 本机普通绑定：C目录`check_current_ai_records_v8.py --context-version <版本> --exclude-prior --expected-count <准确条数> --output <新目录>`；采用`review_selection_v4.py`。事前任务云端：C目录`run_prior_audit_cloud_v5.py --context-version <版本> --version <唯一版本> --worker <实际核验ID> --expected-count <完整分工条数>`。旧v4控制器的协议来源v1已过时，不用于新口径。
- 汇总用C目录`combine_annotation_audits_v3.py --input <每个不重叠分部> --expected-total <准确总数> --output <新目录>`；不可再用v2对未选中快照作合并。当前结果为C目录`annotation-integrity-final-v1/`，事前任务分部v3/v4/v5/v7。
- 进度`snapshot_progress_v5.py`、覆盖`candidate_coverage_v3.py`、结构缺项`admission_readiness_census_v3.py`、跨次`repeat_coverage_report_v2.py`、事前队列`snapshot_prior_tasks_v3.py`；文字检查沿用`resume-20261021/audit_annotation_text_v2.py`。新普查按所有事前任务的预期资产统计，不能按当前B的道路分组冒充目标身份；另绑定3条已有候选证据，保持正式准入为0。
- 来源索引仍用B目录`formal_observation_index_v2.py`（837条）；原691条工具/库存不覆盖。新记录只增上传云端`/home/ubuntu/drone-agent-data/p6-corpus-completion-20261018-cloud/data`，同名先核对摘要；保留PNG编码差异，不重写历史摘要。外置工具准确字节见[绑定清单](../eval/s2/road_business_v1/corpus_checkpoint_20261022/external-helper-bindings.json)。

## 后续最近完成点

1. 复用3条已完成单条审核的候选，扩大独立身份/固定范围核验，补齐可用完整模型任务与统一资产登记，避免重复计参考资产；优先核对正常和各障碍类型缺口，而不是仅清空93条无正常建议的首稿。
2. 从已登记中立池小批取样并由另一隔离AI核验，保持世界范围和角色不变。跨次按固定范围有效源区优先补双方状态，再独立核对同资产、同状态及真实不同采集，保留遮挡未知，完成30对/15资产。
3. 完成剩6个事前任务；从18条未检查校准参考继续中立登记，获得完整可信目标后再在未初标B上提前登记校准错目标任务。2个已检查但未解决参考不自动放行。
4. 满足独立资产、类型、站点、隔离与跨次覆盖后，冻结数据和真实代码候选，执行同候选检查、真实校准、冻结画像、唯一正式测试、严格回放及`verify_p6_road_business.py`。D090仅允许未录得回答时有限重试，不能删掉已有回答或曝光后重跑。当前`missing`不应改成通过。
