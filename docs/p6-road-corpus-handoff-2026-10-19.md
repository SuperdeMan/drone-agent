# WP-P6-01 公开道路数据与验收交接 — 2026-10-19

**本轮已按用户要求结束在可交接的数据检查点；WP-P6-01 仍为 `missing`，未关闭。** 本页以 2026-10-19 20:27（UTC+8）的实际落盘统计为准。用户要求后续由其他 Agent 推进，因此当前标注批次和未完成下载均已停止，没有待续跑的正式模型尝试。正式数据尚未冻结，模型实调为 0。

## 接手先读与工作位置

1. 遵守 `CLAUDE.md`、[当前业务协议](p6-business-evaluation.md)及[派生来源契约](p6-derived-road-evidence.md)。独立 AI 初标与另一隔离 AI 二审获用户授权；真人处理耗时留待运营验证，现场清障效果不在本次 S2 关闭范围，其余 D089 门槛不变。
2. 使用隔离工作树 **`D:/drone-agent-data/p6-road-views-20261014/repo`**，分支 `work/p6-road-views-20261014`。不要把原项目主工作区的 X1 改动混进本批。
3. 下文相对数据路径均以 **`D:/drone-agent-data/p6-corpus-completion-20261018/`** 为根。图片和源 TIFF 不进 Git。原始素材数据根仍是 `D:/drone-agent-data/`。
4. 机器可读待办、97 条二审及来源摘要见[交接归档](../eval/s2/road_business_v1/corpus_handoff_20261019/README.md)；[pending-work.json](../eval/s2/road_business_v1/corpus_handoff_20261019/pending-work.json)列出每个待二审 ID、剩余初标 ID、文件路径和摘要。

## 已完成的检查点

| 项目 | 实际结果 | 证据边界 |
|---|---|---|
| CRASAR UAV 派生采集 | 34 幅父图，691 个窗口；全部取回本机 | 538 个主候选道路组、80 个额外参考组，另有 73 次候选重复观察；618 是几何组数，不是已准入资产数 |
| CRASAR 来源验证 | 34/34 纯缓存重提取，691/691 实际源字节解码通过 | 校验源码 `f893767a3fed845e45f3aff8589c68d60af625b9`；不是业务标签或同状态证明 |
| OAM 第一批 | 3 幅完整父图，29,509,296 字节；28/28 提取、重提取及云端来源核验 | 校验源码 `4c23f6be1da23821804e66aa815473a7cc165bce`；本机 / 云端 JPEG、RGB、世界范围、几何和解码 mask 一致，PNG 编码字节不同 |
| OAM Nepal 增量 | 2 幅完整父图，140,847,496 字节；17/17 提取、独立重提取及实际源解码通过 | 同一 `4c23f6b` 校验器，**本机**环境；尚未进入最终云端编码环境，也不是 17 个已准入道路资产 |
| 新候选软件回归 | Ruff 通过；Windows 全量 **2047 通过、1 项既有 Unix socket 跳过、0 失败 / 错误** | 准确候选 `4c23f6b`，UTC 08:08:35–08:25:38，墙钟 1022.843 秒，JUnit 1016.424 秒；期间源码不变 |
| 新派生候选初标 / 二审 | 736 个已提取视图；237 条当前图片 / 固定范围一致的首稿；97 条二审绑定全部通过 | 二审：正常 1、障碍 34、不可判 50、排除 12；接受 68 / 修订 29。663 是几何组数，不是正式独立资产数 |
| 最近结束的二审批次 | 第 5 核验者 17/17，第 6 核验者 20/20 | 第 5 的实际结果及 audit 均已保存，主流程复核后结束其会话；交接没有伪造其最终消息 |
| 最近结束的初标批次 | 第 10 初标 24/24、第 11 初标 16/16；第 12 初标在 9/17 的已保存小批次结束 | 第 12 剩余 8 个 ID 已列出；9 条只是初标，文件 / 几何检查不等于二审 |
| 中性参考登记 | 第 2 登记者检查 24/24，其中 9 个可登记完整目标、15 个未解决 | 这是预期资产参考，**尚未创建正式错目标事前任务**；前一登记者的 2 个历史已登记目标另行保留 |

本次后续只处理数据与文档，仓库运行代码未再改变。`f893767` 的 2021 通过 / 1 跳过和 `4c23f6b` 的 2047 通过 / 1 跳过各自绑定自己的版本，不转借给后续文档提交。模型提示和生产画像均未改动。[门禁记录](../eval/s2/road_business_v1/corpus_checkpoint_20261019/business-gate-4c23f6b.json)仍明确 `wp_p6_01_closed=false`。

额外 85 张 FC220 FloodNet 原始成员已取得，451,197,627 字节，与既有 415 张及本批内部无字节重复；合并清单为 **500 张完整原图、108 条仅文件头记录**。新 85 张仍未标注、分组或拆分。MPO 主帧固定索引 0，170 个内嵌帧不能计成 170 次采集；整包 ZIP 摘要未验证。入口 `floodnet-remaining-85/merged-candidate-inventory.json`，SHA-256 `ccf7c38e4585f230c5bc8839f31c73f120f8029daa941a305171b59c8604983a`。

旧 FloodNet 415 张二审中有 335 条可继续做身份准入的观察。另对 44 张远端候选做了实际身份检查，形成 8 个保守证据组，不能计成 8 个已确认独立道路。权威文件为 `asset-identity/floodnet-remote-1/identity-review.revised-utf8.json`；原中文编码损坏版本保留，修订时间与重写依据明确，主流程检查 135 个文件摘要全部一致。#6337 开发曝光及其关联组、UAVVaste 缺少地理信息等未解决项继续保留。

## 标签与来源的关键注意事项

- [既有标注规范](p6-business-evaluation.md#标注规范草案)规定普通车辆不自动构成障碍。此前分工简写遗漏这一例外；第 4 核验者已对事先固定的 18 条记录全部重核并替换，旧记录保留，当前 97 条只计选中的有效版本。剩余首稿与旧 FloodNet 相关条目仍须按同一口径核对，尤其第 8 初标的已保存 24 条不能因会话结束自动认定已修订。
- 完整固定目标不能因存在障碍、缺图或树冠而缩小。主分类与对象级定位分开：检查所有实际类型，但不冒称完成逐碎石 / 逐叶的实例穷尽标注。树冠投影、阴影和实际占路实体应区分。
- `source-sampling-priority-v1/registration.json` 如实披露：实现方使用部分源灾损注释安排**既有固定范围**的检查优先级；不作业务真值，不交给独立标注者，不改范围。不能宣称本轮全部采样都未使用源标签。
- OAM 增量按具名道路或无名道路连通 / 近并行关系保守合组后，预登记一个世界范围。338 是元数据规划组数，其中 Santolan 的 321 组尚未取得完整来源；当前仅完成 Nepal 的 17 个窗口。部分已查看图存在 OSM 与实际路面偏移，不能将邻近可见道路自动平移成原目标。
- `annotation-integrity-v7/`、`annotation-context-v10.json` 和 `review-registration-v10.json` 是本检查点实际版本。不要覆盖原稿、检查图、来源回执、旧编码损坏文件或不可覆盖快照。辅助脚本避免命名 `inspect.py`，否则可能遮蔽 Python 标准库；已有同名文件保留，验证时从总任务根运行。

## 精确待办与推进顺序

| 待办 | 准确剩余 |
|---|---|
| 完成已保存首稿的独立二审 | **140 条**：初标者 3 为 19、5 为 20、7 为 28、8 为 24、10 为 24、11 为 16、12 为 9；逐条 ID 在归档待办中 |
| 接续当前 OAM 初标 | 第 12 初标剩 **8 条**，用新隔离上下文；原 9 条保留，不重计 |
| 原图新增批次 | FloodNet 新 85 张尚未初标；`raw-85-priority-v1.json` 预选 24 张，但未分派。该筛选只用相机元数据，不能证明资产独立 |
| 独立资产 / 地点准入 | 尚未形成正式准入集合；先核清每类去重后可用数，不能拿 736 个窗口或 500 张原图当独立资产数 |
| 错目标 | 校准 10 + 测试 30；须在当前图初标前登记真实预期资产任务，随后独立核验不同参考图和目标缺席依据；不能倒填任务时间 |
| 真实跨次 | 至少 30 对、15 个资产；73 对仅有来源日期候选，正式准入仍为 0。`repeat-review-priority-v1.json` 已预选 40 对供检查 |
| 正式评测 | 满足测试 260 / 校准 60、类型覆盖与地点隔离后，冻结数据、实际校准、选择并冻结画像、唯一正式测试、严格回放和最终门禁 |

旧 32 条与新增二审 34 条障碍观察合计最多 66 条；即使全部独立可用，距离校准加测试至少 120 个障碍主资产仍至少差 **54 个**。正常观察去重后的数量和测试植被类型 ≥30 个仍未证明；不能用首稿数量填补。估算 12–24 小时有效处理 / 2–4 个工作日的前提是新来源能凑齐合格素材且正式模型成绩达标，不能据此承诺关闭日期。

## 下载停止点与复现环境

Santolan 父图 `6024868fe316a7000568a583` 需要完整 253,851,020 字节，目前**未取得完整对象**。本机 urllib 尝试保留 41,943,040 字节，curl 在 300 秒上限退出，保留 50,064,209 字节；云端 SSH 等待超时后下载进程仍继续，交接时已按实际打开的本任务文件精确停止 PID 1331709，保留 147,849,216 字节。停止后无仍打开该文件的进程，未触其他服务。回执为 `oam-expansion-v1/download-handoff-stop.json`，部分摘要只证明已保存部分，不能当作完整 SHA。

现有 OAM 来源契约要求完整 GET 200。直接续传部分文件不能自行产生这项证明；如后续改用分段来源证据，必须真实实现和验证对应契约，不能伪写 200 或整图摘要。原获取脚本多数用排他创建，**不要直接重跑覆盖原路径**。保留失败与中止记录，先检查实际状态再建新尝试目录。

云端任务数据根为 `/home/ubuntu/drone-agent-data/p6-corpus-completion-20261018-cloud/data/`；CRASAR 源块 / 重提取和 OAM 第一批 28 张在此。Nepal 17 张目前仅本机：后续另建 staging 版本进入同一云端环境，再比对原生 JPEG / RGB / 解码 mask，保留原检查文件，不能改历史 PNG 摘要来消除编码差异。

本机辅助解释器：`tools-venv/Scripts/python.exe`，Pillow 13.0.0、NumPy 2.5.4、rasterio 1.5.2、Shapely 2.2.0；没有 pyproj，用 rasterio 坐标转换。现有云端环境 Pillow 12.3 / NumPy 2.5.3。SSH 编排使用工作树 `.venv` 的 `dev_stack.Connection.from_environment()`，不要输出连接密钥或复制其他项目 `.env`。

## 可直接接续的入口

以下命令在上述数据根执行。先核对 [external-helper-bindings.json](../eval/s2/road_business_v1/corpus_handoff_20261019/external-helper-bindings.json) 的实际脚本摘要；若这些新任务名已被后来者使用，改用新版本，不覆盖文件。

```powershell
$p6Python = '.\tools-venv\Scripts\python.exe'
& $p6Python corpus_progress_v2.py
# Prepare the remaining8 inputs for a new isolated author. / 为新隔离标注者准备剩余8条。
& $p6Python prepare_annotation_assignment.py --author bounded_annotator_13 --frames oam-expansion-v1/nepal-frames-v1 --role single_observation_candidate --limit 8 --ids-file annotation/bounded_annotator_12/orchestrator-stopped.json
# Prepare the19 unreviewed drafts from author3. / 准备第3初标者尚未二审的19条。
& $p6Python prepare_review_assignment.py --reviewer bounded_verifier_7 --author bounded_annotator_3 --skip-assigned --limit 20
```

准备 assignment 不等于已标注。必须真正启动 `fork_turns=none` 的独立上下文，只交付该 assignment、中性业务定义及必要图像，不提供待评模型提示 / 输出或源类别；初标和二审由不同 Agent 完成。新分工实际启动后再生成新版本 context / registration，完成后运行 `check_current_ai_records.py --output <新目录> --context-version <新版本>`。OAM 批次的二审要指定其正确 `--frames` 目录，不能用 CRASAR 默认路径。140 条待二审也不能由一次形式检查代替逐图查看。

16:55 的 21 文件[历史检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261019/README.md)保持原样；当前新增[28 文件交接归档](../eval/s2/road_business_v1/corpus_handoff_20261019/README.md)及 SHA 清单单独保存。本批未推送、未部署，未改常驻服务、系统配置、密钥或 CI。
