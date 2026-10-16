# P6 道路业务评测准备

**WP-P6-01 未关闭。** 2026-10-16 当前工具、追加素材、两批完整目标标注页、软件验证及实际缺口结果见[最新归档](closeout_20261016/README.md)。现场清障效果已按用户确认移出本 S2 工作包关闭条件，数值目标不变。 用户于 2026-10-15 指定本人复核，并采用 D089 的第一版验收目标；协议处于 `targets_approved_data_pending`。本目录包含已采用目标、数据 / 计分工具的输入及实际来源采集记录，没有正式测试成绩。

| 文件 | 用途与状态 |
|---|---|
| `protocol-draft.json` | D089 数量、指标和用户确认；数据清单尚未冻结，不得提前改为 `frozen` |
| `development-manifest.json` | 既有 26 条开发条目；真实资产 / 地点 / 批次、完整目标业务标签待补 |
| `exposure-registry.json` | 62 张已用于开发的原图摘要；旧图及关联近邻不能冒充未见留出 |
| `labels-template.json` | 26 行空白模板，人工标签仍为 0 |
| `review-sessions-template.json` | 空白计时集合；标注制作、AI 与 QA 用时不能混入真实候选复核 |
| `development-audit-initial.json` | 本轮早期准入核对，保留原负结果与原工具边界 |
| `development-audit-final.json` | `b56b851` 对照 D089 的实际缺口核对，结果 `missing` |
| `source-acquisition/` | FloodNet 作者 / 镜像版本、许可声明、下载和范围校验、元数据、失败尝试与工具快照 |

代码入口为 `scripts/p6_business_eval.py` 的 `audit`、`score`、`assess`。五类指标保留分子 / 分母、描述性统计区间和提示暴露；原始结果表无实调来源认证能力，目标比较即使满足数值也不自动授予准出。接口与完整目标见[实施文档](../../../docs/p6-business-evaluation.md)，当前数量与剩余工作见[交接](../../../docs/p6-materials-handoff-2026-10-16.md)。

源图片、压缩范围、完整原图和原始下载前缀在 `D:/drone-agent-data/p6-business-eval-20261015/`。本目录不含原图。仅取得 JPEG 文件头的条目不是完整素材，目录中 `train` / `validation` / `test` 是源数据命名，不是本项目评测拆分；GPS 连接组仅为防泄漏复核线索，不是已确认的独立地点或资产。

FloodNet 来源：Maryam Rahnemoonfar、Tashnim Chowdhury、Argho Sarkar、Debvrat Varshney、Masoud Yari、Robin Roberson Murphy，*FloodNet: A High Resolution Aerial Imagery Dataset for Post Flood Scene Understanding*，IEEE Access 9 (2021)，89644–89654，DOI [10.1109/ACCESS.2021.3090981](https://doi.org/10.1109/ACCESS.2021.3090981)。作者[固定版本声明](https://github.com/BinaLab/FloodNet-Supervised_v1.0/tree/1cba183788e5661c1ec326d7ded1f2536f82e9ad)采用 [CDLA-Permissive-1.0](https://cdla.dev/permissive-1-0/)。镜像由 [TorchGeo](https://huggingface.co/datasets/torchgeo/floodnet/tree/c5ea256ffb7832d5ae78cda3f4b812521faf32c5)维护；原图字节和 EXIF 保留，自建清单与检查结果均与原始业务标签分开。
