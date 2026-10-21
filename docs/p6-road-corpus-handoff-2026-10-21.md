# P6 道路材料当前交接 · 2026-10-21

**WP-P6-01 仍开放，P6 未整体准出。** 本页接续 [2026-10-19 历史检查点](p6-road-corpus-handoff-2026-10-19.md)，当前事实以本页和[本次证据快照](../eval/s2/road_business_v1/corpus_checkpoint_20261021/README.md)为准。正式资产、正式跨次配对、冻结测试和正式模型实调均为0；真人候选处理计时已由用户延期至运营验证，现场清障效果另列未验证，其他 D089 门槛不变。

工作树：`D:/drone-agent-data/p6-road-views-20261014/repo`，分支 `work/p6-road-views-20261014`。材料根：`D:/drone-agent-data/p6-corpus-completion-20261018`。原仓库的并行 X1 工作未混入本批。`de07338` 已进入本工作分支；旧 Claude 工作记录中的“未提交”“头部调查仍运行”仅代表其暂停时刻。

## 实际完成与准确范围

| 项目 | 本检查点事实 | 不代表什么 |
|---|---|---|
| 项目软件 | `f132b98fd0f6148ca3ab5f7e168de147433fc7be`：Ruff 通过，2074 测试通过、1 项既有 Windows Unix-socket 跳过、0 失败 | 不转借给后来的提交，也不是业务准出 |
| 新 UAV 派生来源 | Mussett Bayou Fire46 + Hurricane Laura100，共146个固定目标窗口，实际源字节、纯缓存重提取和当前来源校验146/146通过 | 窗口数不等于独立资产数；不新增真实跨次计数 |
| 事前任务来源 | 11个任务的预期参考与当前影像共22张，按同候选重新核验22/22通过 | 事前任务尚不是错目标参考标签 |
| 原图增量 | 新16张完整 FC220 原图，CRC、HTTP范围、文件摘要及32个内嵌帧解码通过；旧500张加增量为516张 | 内嵌帧不算新采集；5张增量属于开发关联暂缓组，另11张只是候选 |
| 派生候选总量 | 882个窗口、809个几何保守组 | 尚未形成正式准入的独立资产集合 |
| 已保存标注 | 首稿271条；选中二审174条，绑定检查全部通过 | 包含排除源；不能当成已满足320个独立主资产 |

标注快照时间为 `2026-10-21T12:52:01.135045+00:00`。选中二审为正常4、障碍55、不可判74、排除源41；[逐条记录与准确待办](../eval/s2/road_business_v1/corpus_checkpoint_20261021/progress.json)中还有97条已保存首稿未完成二审。旧 FloodNet415 的二审单独保留，不混入此处计数。只对最终选中的正式样本强制完成参考标签链，不为清空候选队列而优先复核无用的排除源。

[按预登记分区的候选覆盖](../eval/s2/road_business_v1/corpus_checkpoint_20261021/candidate-coverage.json)分别列出当前观察类别和障碍类型；其中拟议分区及几何组仍不是已核验资产。正常样本、独立错目标任务及合格跨次仍须重点补齐，不能用其他类别的富余抵消。

把旧组外标签与当前选中二审都按最乐观方式计算、暂不扣去重复资产和分区限制，仍至少需要补 68 条正常、42 条障碍和 40 条错目标参考观察。它们可来自已有未标注候选；这是观察缺口的乐观下界，实际独立资产缺口仍须在准入时确定，不能据此估算成“只剩清空二审队列”。

现有待二审首稿中，只有 4 条首稿自报正常；[二审排期清单](../eval/s2/road_business_v1/corpus_checkpoint_20261021/work-queue/pending-second-review-priority.json)给出准确 ID、首稿摘要与来源目录。该排期不接受首稿结论，不向盲初标者提供，独立核验仍可修订或排除。

本次来源检查在 `f132b98` 上准确覆盖新增146张及事前任务两侧22张：当前校验器实际重读缓存源字节并校验既有独立提取回放，原提取回执另行保留。该结论不把旧版本对691张 CRASAR 或45张 OAM 的成绩扩写为当前候选全量通过；本次软件回归也只对 `f132b98` 负责。证据分别见 [软件回归](../eval/s2/road_business_v1/corpus_checkpoint_20261021/qa/full-pytest-result.json)、[Mussett46](../eval/s2/road_business_v1/corpus_checkpoint_20261021/qa/mussett-source-provenance.json)、[Laura100](../eval/s2/road_business_v1/corpus_checkpoint_20261021/qa/laura-source-provenance.json)、[任务两侧22](../eval/s2/road_business_v1/corpus_checkpoint_20261021/qa/prior-source-provenance.json)及[当前来源执行回执](../eval/s2/road_business_v1/corpus_checkpoint_20261021/qa/current-source-execution.json)。

## 数据选择、隔离与尚未通过的门槛

- 新事件的世界范围在业务标注前登记，每个既有保守道路组只保留一个目标。Mussett整事件预留测试侧；Laura整事件预留校准侧，其中20个范围只作预期资产参考、不进入主观察，剩80个为主观察候选。没有更改旧目标来避开障碍、缺图或树冠。
- [分组扩展](../eval/s2/road_business_v1/corpus_checkpoint_20261021/source-extension/grouping-rules-v2.json)沿用既有1km包络连通规则，保留原地点 ID 与拆分；CRASAR共41个父图、16个拟议地点，校准与测试父图道路包络的最小间距约379.7km。这是元数据一致性证据，尚不自动授予每条道路的真实身份，也不覆盖仍未拆分的 FloodNet/OAM。
- 新原图先用官方源文件头、相机近似覆盖与公开道路几何安排检查顺序；2026年的 OSM 只提示2017年照片中可能有道路，不能作状态真值或独立资产证明。新增原图批按最长完整主铺装道路选择；这只是该补充池的采样范围，不倒改旧候选的目标。
- `#6337` 属于按同型号、同日、相邻间隔不超过15分钟形成的开发关联暂缓组。旧正常130条不能原样填入正式分母；其中82条同属此暂缓组，组外最多48条正常观察仍待真实资产去重与现行口径核对。该序列不是已验证的飞行 ID，不虚构 UTC。旧合并清单的500完整/108仅头记录保持历史原样，新增完整图另建索引，不能仍把108当当前仅头数量。
- [旧335条批次映射复核](../eval/s2/road_business_v1/corpus_checkpoint_20261021/legacy/floodnet-development-hold-impact.json)中，140条明确落入开发暂缓组、182条明确在组外、13条暂无有效映射；后13条全部是不可判标签，单独挂起。旧“组外不可判最多124条”的乐观上界包含这13条，不能写成124条已明确在组外；原标签未改。
- 当前73个几何跨次候选中，7对两侧均完成独立二审，1对满足当前参考标签同为可判且类型相同、固定世界范围相同；它们仍须独立核验物理身份、来源及拆分后才可准入。未通过、源无效区和类型变化都保留，不拿几何对数代替30对真实同状态配对。
- 其中首对另由第三个隔离上下文逐图核验：确认同一物理路段和不同作者日期的派生采集，但因树冠及阴影遮住局部地面，`same_condition=null`，不能证明完整目标同状态，故仍不准入。该结果不应被两侧都标为 debris 代替；[双图核验与检查回执](../eval/s2/road_business_v1/corpus_checkpoint_20261021/pairs/pair-candidate-check-v1.json)保留不确定性，不由编排者改判来凑数。
- [源有效区排序](../eval/s2/road_business_v1/corpus_checkpoint_20261021/work-queue/repeat-alpha-margin-priority.json)有22对在不变端点范围周围10m内源有效区充分，仅用于检查优先级；它既不是物体大小门槛，也不是配对准入结论。后续中立 ID 池为 [repeat-next-neutral-pool.json](../eval/s2/road_business_v1/corpus_checkpoint_20261021/work-queue/repeat-next-neutral-pool.json)。
- 首批11个事前任务均为测试侧：真实参考A与未初标的预留当前图B在首次初标前确定，构造方法披露，不声称自然发生过操作者拍错。3个旧参考多边形未通过当前几何校验，另1个端点仍有疑问，均未用于这11个任务。校准侧参考登记与最终40条错目标仍缺。

## 本轮发现及处理的材料问题

采集算法已完成46/100窗口，但旧包装器在 Python 退出阶段因缓冲 stdin 守护线程报错，进程返回非零。失败记录原样保留，独立离线提取和来源校验另行成功；没有把该进程记作通过。外置入口 `resume-20261021/cloud_safe_entry_v1.py` 改用无缓冲输入，并在保持控制器 stdin 打开的情况下实际重提取1张缓存影像，退出0、原生RGB/JPEG/mask摘要一致；QA提取不增加业务样本。

部分首稿中文经 PowerShell 管道变为问号。原稿与快照未改；进入当前选中二审的受损说明已由独立核验者逐张对原图补写可读中文，并记为 revised；尚未二审的首稿继续保留待办。已经保存的最终记录如需修正，先保留旧记录及其检查图，再写真实修订与摘要。[文字检查](../eval/s2/road_business_v1/corpus_checkpoint_20261021/annotation-text-audit.json)核对本次选中的不可覆盖二审快照，最终疑点字段为0；“可按UTF-8解析”不被当成文字未损坏。使用 `apply_patch` 或明确UTF-8文件生成批次，避免默认编码管道。

严格绑定另发现部分 `uncertainties` 是单个字符串，及一次文字修订后的时间字段不一致。原始首稿、旧核验、看图证据与时间均保留；确定性管线仅把字符串无损包装成数组，再由原独立核验上下文确认现有结论。新格式确认时间不冒充新看图，不改类别、对象或坐标，不增加样本计数；记录见证据目录 `schema/`。项目最终标签入口还新增损坏文字门禁，55项针对性检查及上述完整回归通过。

最后两条核验由独立观察者18、19实际生成完整批次，编排器仅按原批次确定性入库，未改判定、对象、坐标或说明。[观察者原批次和入库回执](../eval/s2/road_business_v1/corpus_checkpoint_20261021/observer-payloads)保留实际字节摘要、文件时间、存储时间及首条旧记录；`reviewed_at`/`final.inspected_at`在这两条表示入库时间，不声称观察者执行了存储CLI或发生额外看图，也不作为真人计时证据。

Santolan完整对象仍未取得。新本机完整GET在600秒上限失败，保存10,552,590字节；新云端完整GET读超时，保存13,631,488字节。预期完整大小253,851,020字节，两个新尝试及此前部分文件全部保留，未续拼或伪写完整GET200，不计其321个规划组为可用材料。

## 后续推进顺序

1. 先汇总最终可用参考标签的独立资产覆盖，优先补正常、障碍和各类型缺口；测试仍须100/100/30/30，校准20/20/10/10，测试每类型至少30。保存首稿、排除源、几何组和相邻照片都不能扩大分母。
2. 按源有效区排序完成另一采集的初标和另一AI二审，独立检查两图确为同一固定目标；保留同状态、不同日期、实际像素与来源证明，满足至少30对/15资产。源日期只有作者日期的，明确为无序派生采集，不冒充原始单帧或精确飞行时间。
3. 完成Laura预留参考的中性目标登记，补齐校准10与测试30个错目标；既有11个任务在 [prior-tasks/index.json](../eval/s2/road_business_v1/corpus_checkpoint_20261021/prior-tasks/index.json)。`annotation/prior_annotator_1/assignment.json`准备了8条但尚未启动，`annotation/bounded_annotator_16/assignment.json`准备了7张原图但尚未启动，不能登记成已完成。Mussett初标20按实际保存边界停止：分配4张、保存3张、未保存1张已释放；核验者18处理前两张，最后一张按回答前登记的移交交给另一隔离核验者19，原任务保留。合计实际保存 3/3 条二审，未完成部分以进度清单为准。损坏首稿只能由真正看图的独立核验者重建，编排者不补写业务说明。
4. 最终数据准入和制备在云端实际源字节根运行：`/home/ubuntu/drone-agent-data/p6-corpus-completion-20261018-cloud/data`。本机辅助产物只增上传，同名文件先核对摘要；保留既有PNG编码差异和历史路径，不改摘要消除冲突。现有 `claude-orchestrator/tools/formal_lib.py` 默认只读旧691张；接入新来源时明确扩展索引与分组/采集记录，不能自动把元数据规则标成已验证身份。
5. 满足覆盖后冻结清单与实际代码候选，做同候选必要检查、真实校准、冻结画像、唯一正式测试、严格回放及最终 `verify_p6_road_business.py` 门禁。D090只允许未录得回答时有限重试，已有回答和测试曝光不得删掉重跑。

原先仅准备、从未启动的 Claude初标02–11共100条预留已释放，原分工和启动日志均保留；这不是停止了100条正在运行的标注。部分长批次已按实际保存边界停止，未保存部分交给新的小批隔离上下文；[closeout目录](../eval/s2/road_business_v1/corpus_checkpoint_20261021/closeout)与[实际上下文登记](../eval/s2/road_business_v1/corpus_checkpoint_20261021/actual-contexts.jsonl)记录谁真正启动、保存多少，准备分工不等于执行。

## 接续工具与检查口径

工作在上述材料根执行。数据解释器为 `tools-venv/Scripts/python.exe`，含Pillow/Shapely/rasterio；仓库 `.venv` 用于SSH编排，不能假定它具有全部图像工具。避免把辅助脚本命名为 `inspect.py`，也不要从存在该同名文件的子目录启动验证。

- 派生首标：`resume-20261021/prepare_derived_assignment_v2.py --author <新ID> --frames <实际视图目录> --ids-file <中立ID池> --limit 2`；它保留错目标当前图预留，旧准备脚本不理解该类任务。
- 原图二审：`resume-20261021/prepare_raw_review_v2.py`；派生二审：`resume-20261021/prepare_review_assignment_v2.py`（优先无损 canonical 首稿）并指定真实 `--frames`。只有真正启动`fork_turns=none`的上下文后才用`register_actual.py`登记，再生成新版本context/registration。新批保存先用 `strict_batch_io_v1.py` 核对数组容器、核心结构及文字编码；旧写入器不覆盖。
- 当前绑定检查入口为 `check_current_ai_records_v5.py`，文字检查用 `audit_annotation_text_v2.py`，进度用 `snapshot_progress_v4.py`，均在`resume-20261021/`下。使用新的输出目录，不覆盖本检查点。独立初标/二审不得得到评测提示、待评模型输出、源类别或其他日期参考答案；编排者已见评测提示，不能代替它们看图裁定。
- 本检查点实际绑定输入为 `annotation-integrity-v16/`、`annotation-context-v18.json` 与 `review-registration-v18.json`；新上下文登记后用 `resume-20261021/context_v2.py --version <新版本>`生成新文件。格式确认沿用原观察者的真实上下文，另外披露 reuse，不能登记成新的独立看图。

本页与归档只关闭这一批可复核材料工作；WP-P6-01的业务准出仍由正式数据与实调门禁决定。真人效率和现场清障效果继续明确为未验证。
