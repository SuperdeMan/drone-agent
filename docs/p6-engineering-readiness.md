# P6 工程交付验收（D091）

**范围已获用户批准，工程关闭检查待完成，当前尚未关闭。** 本页只负责“道路业务评测工程与公开素材交付”。完整道路业务效果验收移交[后续任务](road-business-validation-backlog.md)，原 D089 未通过；真人效率与现场清障继续未验证。

用户于 2026-10-22 回复“采用工程交付范围关闭（建议）”。[范围记录](../eval/s2/road_business_v1/engineering_closeout_20261022/scope.json)绑定该授权、未变的原协议摘要及后续待办；[已采用方案](p6-engineering-closeout-proposal.md)说明工程交付与业务验收的边界。

## 验收材料

| 材料 | 实际交付及边界 |
|---|---|
| 评测工程 | 授权来源核验、原生像素/独立回放、AI 标注绑定、数据准入、任务制备、真实录制、严格回放、计分及原严格业务门禁；功能测试与实际业务评测分开 |
| 参考交付 | [D检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261022d/README.md)固定331条已二审参考；93条首稿作为未完成库存移交，未计为最终标签 |
| 准入局部结果 | 19观察局部副本逐观察/逐对问题0，但仍15项全局缺失；22观察负结果、来源hold及历史失败原样保留，正式资产/配对/冻结/待评模型调用均0 |
| 历史软件证据 | `cdf5382d4b4ce54600921532ecbe2a5591775b58`的Ruff、2174通过/1项既有Windows跳过，仅对原候选负责；合入主线X1及工程门禁后的新候选另由准确CI记录负责 |
| 既有工作包 | WP-P6-02–07继续分别按[路线图](roadmap.md)中的准确版本验收，不重写其历史或扩大软件/SITL结论 |

## 工程关闭检查

独立入口为 `scripts/verify_p6_engineering_delivery.py`，只核验工程交付完整性。它要求范围批准、原D089协议摘要不变、后续任务存在、固定归档750份叶文件逐字节一致、331条绑定结果完整、19局部审核保留全局缺口，以及准确候选的完整持续检查。

CI必须完成锁文件环境、Ruff、契约字段、全量pytest和客户端测试。候选与CI合并提交均绑定，Git边界同时拒绝源码变化和未提交源码；持续检查不替代任何原有SITL、业务或硬件门禁。

```powershell
uv run python scripts/verify_p6_engineering_delivery.py --scope eval/s2/road_business_v1/engineering_closeout_20261022/scope.json --ci <真实CI回执.json> --candidate <准确候选SHA> --output <工程结果.json>
```

完成检查后，本页补入准确候选、真实CI、工程结果与摘要，再同步路线图和入口。可使用的最终表述为“P6工程范围交付完成，道路识别业务效果尚未验收”；原 `verify_p6_road_business.py` 的 `wp_p6_01_closed` 保留D089严格业务含义，不由工程检查置为true。
