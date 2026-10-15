# P6 公开素材开发交接 — 2026-10-15

**v9 的 33 个提案已接入 v10 定位对照页，能够按 ID 同步查看全图和局部；WP-P6-01 和 P6 整体未准出。** 用户已确认仅用公开数据推进，并明确“暂无门槛，先按目标内实体遮挡候选推进”。模型分组仍为 20 项候选、3 项范围外、10 项待复核；全部 33 项未经人工确认。新页只读，没有新模型调用或标签，历次负记录保留。

| 内容 | 当前事实与证据 |
|---|---|
| 素材 | 62 张原图，26 条开发条目；没有新增原图或独立地点证明。许可差异、清障前后序列仍未解决。见[来源审查](p6-materials-review.md)。 |
| 模型诊断 | [10 条初始诊断](p6-materials-development.md)、[24 条整图 / 分块比较](p6-road-views-development.md)和[新口径六条](p6-road-contact-development.md)均保留录制。新口径六例全部疑似、共 33 框，发现宽框及范围混淆风险；环境核对失败，不能作为隔离通过的评测。分块与新提示均未设为默认策略。 |
| 新业务口径 | 目标内的可见实体遮挡可进入候选，不设已批准的最小尺寸或占用比例；影子、破损表面与实体不混同，边缘不清楚时保持待复核。旧探针的“大件 / 大量堆积”口径不追溯改写。 |
| 边界草稿 | 6 个目标、18 个原始候选 → 13 组 AI 拟定实体；条件几何为 4 组内域相交、2 组范围外、7 组边缘不确定。源支持域只扩大可能范围，不把孔洞判作道路外。见[完整记录](p6-road-boundaries-development.md)。 |
| 关键发现 | `12892` 的木条确实伸入可见路面，按新口径应保留候选；不能把源 Road-Clear 或整图 normal 当作负例真值，也不能把三个 normal→suspected 都叫误报。 |
| 新口径限制 | `12892` 的新回答虽为疑似，六个返回框均不与 v5 E12 的 AI 内域外接框交叠；整图疑似不能证明已检测到特定木条。全部 33 框的正确性仍待业务复核，不计算准确率。 |
| 历史逐候选实调 | [v8 协议及锁守卫](p6-candidate-review-development.md)冻结于 `70fcc33`，六条实调仅两条协议合格：26 项整条隔离，7 项观察仍受混杂或不确定性阻断。环境核对通过，协议不采用；原录制、忙锁及缺席快照不改写。 |
| 当前实体群实调 | [v9](p6-candidate-groups-development.md)冻结于 `6af7036`：将实体 / 目标关系与材质、单件分离分别表达，不让模型改写坐标，原框只作复核窗口。六条回答合格，云端 / 本地严格回放各六条，9 个驻留项目容器前后一致；[33 项汇总](../eval/s2/p6_candidates_v9/attempts/20261015T051249Z/replay-summary.json)中 20 候选 / 3 范围外 / 10 待复核。 |
| 定位风险 | `12892-p01` 声称位于上方道路，但其窗口与已有可能道路草稿无交叠，需核对坐标与实际对象对应；`12892-p06` 需区分小实体与标记 / 表面变化。条件几何与 AI 视觉意见不作业务真值。全部 33 项保留在[人工确认队列](../eval/s2/p6_candidates_v9/attempts/20261015T051249Z/review-queue.json)，未按模型排除项减少人工待办。 |
| 复核工具 | [离线复核页面与包清单](../eval/s2/p6_candidates_v6/README.md)已接通 6 项道路 + 13 项实体；支持原图缩放圈绘、AI 提示暴露记录、道路版本依赖、导入导出和离线几何校验。初始人工记录 0，QA 夹具不减少人工待办。见[操作说明](p6-boundary-review-guide.md)。 |
| 定位对照工具 | [v10 定位页](p6-proposal-viewer-guide.md)保持六个目标 / 33 个提案，提供原始分辨率、实际模型原图和道路提示切换，双视图 ID、局部缩放 / 平移及 12 项优先筛选。按原字节复制 16 份不同图像；框为页面叠加，不是实体标注，也未追溯加入 v9 请求。 |
| 验证 | v9 冻结于 `6af7036`：Ruff、79 项相关测试（新增 28）和 Windows 全量 1743 通过 / 1 项既有 Unix socket 跳过，见[本次回执](verification/p6-candidate-groups-2026-10-15.json)。模型实调、条件几何和软件测试分别报告；v8 的[原工具回执](verification/p6-candidate-review-2026-10-15.json)和[实调回执](verification/p6-candidate-live-2026-10-15.json)保留。工具验证不能替代模型精度或发布门禁。 |
| 定位页验证 | v10 冻结于 `d8944c8`：Ruff、12 项新增 Python 检查、7 项前端逻辑检查、[110 项浏览器检查](../eval/s2/p6_candidates_v10/browser-qa/final.json)及 Windows 全量 1755 通过 / 1 项既有 Unix socket 跳过，见[最终回执](verification/p6-proposal-viewer-2026-10-15.json)。此前 CSP 检查脚本、隐藏 SVG 和响应布局负记录均保留，早期通过数不作为最终布局验收。 |

## 接下来推进

1. 打开 `D:/drone-agent-data/p6-boundary-review-20261015/review-v2/index.html`，先登记六个道路范围，再复核实体，优先 E02、E04–E09 七组边缘问题及 E12。导出后按操作说明做离线校验，处理待办、几何冲突和未确认分组。当前实际人工业务记录仍为 0。
2. 打开 `D:/drone-agent-data/p6-proposal-viewer-20261015/viewer-v4/index.html`，按[定位对照说明](p6-proposal-viewer-guide.md)逐项核对窗口、实际对象及所选道路，优先 `12892-p01`、10 项模型待复核和小白块辨认问题。本页不保存结论，33 个提案与 v6 的 13 组实体 ID 不自动互换；独立标签和实体归组仍需另行建立。后续如改变模型输入，须独立冻结，不能把包含旧模型解释的页面截图作为留出材料。
3. 解决正式素材许可、地点 / 采集批次隔离，并从未用于开发的材料建立校准与测试；已有照片及邻近场景均不能包装为留出集。
4. 冻结正式样本量、指标和阈值后再评测。真实人工复核耗时、跨次稳定性与清障前后效果仍缺证据，不用重复视图、合成图或不同道路替代。

## 位置与版本

- 当前 P6 工作树：`D:/drone-agent-data/p6-road-views-20261014/repo`，本地分支 `work/p6-road-views-20261014`；与原 `main` 工作树的 X1 改动分开。
- 最新草稿 / 预览：`D:/drone-agent-data/p6-boundary-drafts-20261014/`。目录名沿用本轮开始日，最终核对完成于 2026-10-15；源图、掩码和预览留在仓库外。
- 既有道路 / 实体登记包：`D:/drone-agent-data/p6-boundary-review-20261015/review-v2/`；浏览器 QA 使用独立会话，截图与模拟导出另存于同级 `browser-qa-responsive/`，不混入人工标签。旧包保留，不覆盖既有缓存或可能存在的操作员记录。
- 最新只读定位包：`D:/drone-agent-data/p6-proposal-viewer-20261015/viewer-v4/`；来源、浏览器负记录及最终 110 项回执见 [v10 归档](../eval/s2/p6_candidates_v10/README.md)。截图位于同级 `browser-qa-final-v4/`，其中实际看图范围单列，不计作业务标注。
- 历史 v7 模型诊断：`D:/drone-agent-data/p6-road-contact-20261015/diagnostic/`；完整状态仍为 `not_qualified_environment_changed`，未追溯改判。
- 历史 v8 模型诊断位于 `D:/drone-agent-data/p6-candidate-review-20261015/diagnostic/`；[v8 归档](../eval/s2/p6_candidates_v8/README.md)保留环境通过 / 协议不采用的实调和首次忙锁零调用记录。
- 最新 v9 模型诊断：`D:/drone-agent-data/p6-candidate-groups-20261015/diagnostic/attempts/p6-candidate-review-dev-20261015T051249Z/`；状态为执行、环境核对及协议格式通过，业务质量待复核。元数据、脱敏录制与 33 项队列位于 [v9 归档](../eval/s2/p6_candidates_v9/README.md)。
- 已推送批次 `973e12d` 的范围与 CI 记录见[上一份交接](p6-materials-handoff-2026-10-14.md)；后续本地开发分别保留冻结与结果提交。旧 CI 不验证这些新提交。
- 本批新增只读开发定位工具及相关测试与证据；未修改在用画像、业务服务、控制链、数据库、密钥或 CI，未发出模型调用、服务部署或飞行命令。工具验证与业务真值分别记录；正式阶段状态继续以[路线图](roadmap.md)和各 readiness 为准。
