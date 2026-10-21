# P6 道路材料历史交接 · 2026-10-21

本页保留2026-10-21 B检查点；当前事实和后续工具见[2026-10-22交接](p6-road-corpus-handoff-2026-10-22.md)。

**WP-P6-01 仍开放，P6 未整体准出。** 当时事实以本页和[增量B检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/README.md)为准；[前一检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261021/README.md)与[2026-10-19历史交接](p6-road-corpus-handoff-2026-10-19.md)保持原样。正式独立资产、正式跨次配对、冻结数据和正式模型实调均为0。真人候选处理计时已延期至运营验证，现场清障效果未验证，其他D089门槛不变。

工作树为 `D:/drone-agent-data/p6-road-views-20261014/repo`，分支 `work/p6-road-views-20261014`；材料根为 `D:/drone-agent-data/p6-corpus-completion-20261018`，本轮工具在 `resume-20261021-b/`。原仓库的并行X1工作未混入本批，`de07338`已在本工作分支。

## 已完成与证据范围

| 项目 | 当前事实 | 边界 |
|---|---|---|
| 项目软件 | `f132b98fd0f6148ca3ab5f7e168de147433fc7be`：Ruff通过，2074测试通过、1项既有Windows Unix-socket跳过、0失败 | 完整回归只负责该候选，不转借给后来文档提交或业务准出 |
| CRASAR来源 | 新增Mussett46+Laura100；现有691加新增146，全837条在当前核心实际重读缓存源字节、核对原生窗口及既有独立回放，837/837通过 | 不等于882条全部来源通过；OAM45未纳入本轮全量复核，来源有效不等于独立资产或业务标签有效 |
| 观察索引 | 837条元数据绑定通过，旧691条采集行及旧库存摘要原样保留 | 几何分组、物理身份和独立性仍未核验；612条有可核对作者日期组、225条日期未知，不造UTC或飞行ID |
| 原图 | 旧500张加新增16张完整FC220原图，共516张；新增32个内嵌帧解码已核对 | 内嵌帧不是新采集；新增5张属于开发关联暂缓组，另11张只是候选 |
| 派生总量 | CRASAR837加OAM45，共882个窗口、809个几何保守组 | 窗口与几何组都不是已准入独立资产 |
| 标注 | 初稿275条，选中二审182条全部通过绑定；正常8、障碍55、不可判76、错目标2、排除源41 | 旧FloodNet415二审单独保留；不能当成已满足320个独立主资产 |

标注快照时间为 `2026-10-21T14:42:59.216006+00:00`；[进度与准确待办](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/progress.json)保留93条已保存但未二审首稿。前一检查点4条正常建议均由另一隔离AI实际核验，其中1条仅去掉静态照片不能支持的车辆运动判断；两张新增Laura主观察均经独立二审保留不可判，没有为配额改判。

[普通180条](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/qa/ordinary-annotation-integrity.json)在本机核对；[事前任务2条](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/qa/prior-annotation-integrity.json)在云端真实来源字节根核对。[合并回执](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/annotation-integrity.json)只取不重叠并集，保留各执行分部、历史未匹配记录和准确范围，不称为同一次单主机全量运行。

来源证据见[837条来源回执](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/qa/full-crasar-source-f132b98.json)、[实际执行](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/qa/full-crasar-source-execution.json)、[四个执行源文件摘要](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/qa/cloud-core-files-f132b98.json)及[索引验证](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/source-index/metadata-validation.json)。软件完整回归见[原回执](../eval/s2/road_business_v1/corpus_checkpoint_20261021/qa/full-pytest-result.json)，业务门禁仍为[missing](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/qa/business-gate-missing.json)。

## 覆盖缺口与隔离

[分区覆盖](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/candidate-coverage.json)按预登记用途列出参考观察和障碍类型，尚未授予任何正式资产。把旧组外标签与当前二审按最乐观方式计算、暂不扣重复资产、用途冲突、分区或站点限制，仍至少需要64条正常、42条障碍和38条错目标参考观察。它们可来自未标候选；这是观察缺口下界，不能当独立资产缺口或工时估计。

测试仍须正常100/障碍100/不可判30/错目标30，校准20/20/10/10；测试debris、vegetation、other_entity各至少30；至少3个已核验站点、单站不超过50%；真实同资产同状态配对至少30对、覆盖15个资产。误报≤5/100、各类型漏检≤20%、可判覆盖≥90%、不可判/错目标误判正常为0、跨次可判且一致≥90%全部保留。

- 新来源在业务标签前冻结世界范围；每个既有保守道路组一个目标。Mussett整事件预留测试侧，Laura整事件预留校准侧；Laura20个范围仅作预期资产参考，80个为主观察候选，不为缺图、树冠或实体改端点。
- 分组扩展沿用1km包络连通规则，保留既有地点ID与拆分；CRASAR共41个父图、16个拟议地点，校准/测试父图道路包络最小间距约379.7km。这是元数据一致性证据，不是每条道路真实身份，也不覆盖未拆分的FloodNet/OAM。
- `#6337`同型号、同日、相邻间隔≤15分钟形成开发关联暂缓组，不是实际飞行ID。旧正常130中82条属于此暂缓组，不能进入正式分母；组外最多48条正常仍须去重与现口径核对。
- [旧335条映射](../eval/s2/road_business_v1/corpus_checkpoint_20261021/legacy/floodnet-development-hold-impact.json)有140条明确组内、182条明确组外、13条未知，未知13条均不可判。旧“可能组外不可判124”含这13条，不能写成124已明确组外；旧500完整/108仅头清单保持历史原样，增量另建索引。
- 公开2026年OSM只为2017年照片提供道路检查提示，不能作状态真值或独立性证明；新原图按最长完整主铺装道路取目标只是补充池采样范围，不倒改旧候选。

## 真实跨次与错目标

73个几何跨次候选中7对两侧均二审、仅1对参考标签可判且类型相同。该对另由第三个隔离上下文看两图，确认同一物理路段与不同作者日期，但树冠/阴影遮住地面，`same_condition=null`，仍不准入。不能用两侧同为debris替代同状态，见[双图核验](../eval/s2/road_business_v1/corpus_checkpoint_20261021/pairs/pair-candidate-check-v1.json)。

[源有效区排序](../eval/s2/road_business_v1/corpus_checkpoint_20261021/work-queue/repeat-alpha-margin-priority.json)有22对在不变端点周围10m内源像素充分，只用于检查优先级，不是物体大小门槛或准入结论；[剩余中立ID池](../eval/s2/road_business_v1/corpus_checkpoint_20261021/work-queue/repeat-next-neutral-pool.json)保留原选择。

11个事前任务均为测试侧，真实参考A与未初标当前B在首次标注前登记，构造方法披露，不声称自然操作者拍错。首两条由独立初标者保留不可判，再由另一隔离AI比对道路、交叉口、建筑与园区设施，确证预期目标缺席后修订wrong_target；原任务描述及多边形完全未改，云端核心2/2通过。校准侧任务仍为0，正式错目标资产仍为0；3个旧参考多边形失败及另1个端点疑问均未进入这11个任务。

[任务队列](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/work-queue/prior-task-progress.json)保存准确沿革：`prior_annotator_1`原准备8条从未启动，前2条已移交并完成于`prior_annotator_2`，原8条不得整批重复启动，剩6条预留继续保留；另3条登记任务未初标。`bounded_annotator_16`准备的7张原图仍未启动。

## 已处理问题与保留的失败

原稿、检查图和旧快照不覆盖。中文问号损坏由真正看图的独立观察者重写，不由编排者补写业务说明；最终[文字检查](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/annotation-text-audit.json)疑点为0。旧uncertainties字符串无损包装后由原独立核验上下文实际重确认，格式确认不冒充新看图；详情保留在前一检查点的schema目录。

本批独立观察者明确完成payload后，编排器仅确定性保存，未改业务字段；[原批次、分工与入库回执](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/observers)保留实际字节、文件时间与存储时间。`reviewed_at/final.inspected_at`表示入库时间，不声明额外看图、观察者执行了存储CLI或真人计时。

独立元数据审查发现旧外置汇总器可接受0/0并丢弃待核对项，使用前已修复：定向分工必须全部实际核对、准确预期条数、拒绝空集、保留历史未匹配项并拒绝未完成重核。[三类负向探针](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/qa/scope-guard-probes.json)全部按预期拒绝；旧结果保留，不增加业务样本。

来源采集包装器曾在Python退出时因缓冲stdin守护线程失败，46/100实际窗口、原非零退出、后续纯缓存独立提取与来源验证分别保留；未把失败改判成功。修正版无缓冲入口已实际缓存重提取1张且退出0，RGB/JPEG/mask摘要一致，不增加业务样本。Santolan完整对象仍失败：本机10,552,590字节、云端13,631,488字节，预期253,851,020；部分文件不拼装为完整GET200，其321个规划组不计可用。

原Claude仅准备未启动的初标02–11共100条预留已释放，不是停止100条运行中标注；旧启动日志、分工和停止边界均保留。Mussett初标20分配4、保存3，未保存1条已释放，3条二审均已完成。独立初标/二审不得读取评测提示、待评模型输出、源类别或其他日期业务答案；编排者已见评测提示，不能代行看图裁定。

## 后续执行入口

1. 先核对独立主资产、用途冲突、站点、采集与近重复隔离，再补类别与类型缺口。93条待二审首稿已无正常建议，不能以清空队列替代覆盖。[Laura78张](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/work-queue/laura-neutral-primary.json)与[Mussett39张](../eval/s2/road_business_v1/corpus_checkpoint_20261021b/work-queue/mussett-neutral-primary.json)是无类别中立初标池，不是新采集或已准入资产。
2. 按固定范围源有效区排序补跨次初标与独立二审，另由隔离上下文核验同资产同状态及不同采集，达到30对/15资产；保留失败和不确定性。
3. 继续剩余9个登记事前任务，登记Laura真实参考并补校准10、测试30条错目标。已完成两条仅是参考观察，不能自动授予独立资产。
4. 达到覆盖后冻结数据清单和真实代码候选，完成同候选检查、真实校准、冻结画像、唯一正式测试、严格回放及`verify_p6_road_business.py`。D090仅允许未录得回答时有限重试，已有回答和测试曝光不得删掉重跑。

数据解释器为材料根`tools-venv/Scripts/python.exe`（含Pillow/Shapely/rasterio）；仓库`.venv`用于SSH编排。辅助脚本勿命名`inspect.py`，中文使用明确UTF-8文件，避免PowerShell默认编码管道。

- 派生初标用旧目录`resume-20261021/prepare_derived_assignment_v2.py --author <新ID> --frames <目录> --ids-file <中立池> --limit 2`；新池在`resume-20261021-b/next-neutral-first-pools-v2/`，键为入口要求的`selected_sample_ids`。原图二审用`prepare_raw_review_v2.py`，派生二审用`prepare_review_assignment_v2.py`并指定真实frames。
- 真正启动`fork_turns=none`上下文后才`register_actual.py`登记，再生成新的context/registration；本检查点为v22。新普通批次用`strict_batch_io_v1.py`检查；完成payload可用新B目录`ingest_completed_payload_v2.py`确定性保存，事前任务二审专用`ingest_completed_prior_review_v1.py`，均不代行视觉。
- 当前绑定入口为新B目录`check_current_ai_records_v7.py`：普通观察`--exclude-prior --expected-count <准确条数>`；本批事前任务使用`run_prior_audit_cloud_v3.py`在云端严格检查2条并同步完整选取上下文。合并用`combine_annotation_audits_v2.py --expected-total <准确总数>`，进度用`snapshot_progress_v5.py`，覆盖用`candidate_coverage_v3.py`；文字检查沿用旧目录`audit_annotation_text_v2.py`。本批实际结果在新B目录`annotation-integrity-combined-v1/`。
- 新来源索引用`resume-20261021-b/formal_observation_index_v2.py`，可`--check-only`，支持明确`--core-script`且保留旧API。旧`claude-orchestrator/tools/formal_lib.py`仍只读691条，新入口为837条，顶层/库存均`grouping_verified=false`、`formal_release_eligible=false`，不覆盖旧库存。
- 最终准入与制备在云端真实源字节根`/home/ubuntu/drone-agent-data/p6-corpus-completion-20261018-cloud/data`运行；本机产物只增上传，同名先核对摘要，保留PNG编码差异及历史路径，不改摘要消除冲突。外置工具准确字节见检查点`external-helper-bindings.json`。

本页与归档只结束这一批可核查材料工作，WP-P6-01仍需正式数据与实调门禁；真人效率和现场清障效果继续未验证。
