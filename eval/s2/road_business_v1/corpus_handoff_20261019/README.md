# P6 数据批次交接检查点 — 2026-10-19

本归档记录用户要求结束在最近小批次并交接后的实际状态，**不是正式评测集冻结或 WP-P6-01 准出**。完整接续说明见[当前交接](../../../../docs/p6-road-corpus-handoff-2026-10-19.md)。原 16:55 检查点另存，未覆盖。

- 736 个派生视图、237 条当前有效首稿、97 条独立 AI 二审；二审来源绑定 97/97 通过。
- 二审为正常 1、障碍 34、不可判 50、排除 12；未准入独立资产、真实跨次配对或正式模型实调。
- 140 条首稿待二审，当前初标小批次剩 8 条；85 张新增 FloodNet 原图尚未初标。
- 新参考图 24 张完成中性检查，9 个目标可登记；仍没有正式错目标事前任务。
- 未完成的 Santolan 下载已停止，已取得部分字节保留，没有伪记为完整对象。

| 文件 | 用途 |
|---|---|
| [pending-work.json](pending-work.json) | 精确待办 ID、外部路径 / 摘要、下载停止状态及条件性工作量估算 |
| [progress.json](progress.json) | 20:27（UTC+8）实际保存计数及完整性检查 |
| [annotation-integrity.json](annotation-integrity.json) / [reviewed-candidates.json](reviewed-candidates.json) | 97 条选择后的二审来源校验及候选；`grouping_verified=false` |
| `reviews/` | 各核验者的实际记录，含被后续修订替代的旧行；有效选择由 supersession plan 固定 |
| [annotation-context.json](annotation-context.json) / [review-registration.json](review-registration.json) | 实际隔离分工登记 v10；AI 来源不伪装真人 |
| [reference-targets-2.json](reference-targets-2.json) | 中性预期参考目标，不能直接当 wrong_target 标签 |
| [floodnet-identity-review.json](floodnet-identity-review.json) | 44 张原图的保守身份关系、未决项及真实编码修订披露 |
| [oam-nepal-source-audit.json](oam-nepal-source-audit.json) | 17/17 实际来源解码通过；仅本机环境、无业务准入 |
| [external-helper-bindings.json](external-helper-bindings.json) | 仓库外续办辅助脚本的准确路径与 SHA-256 |
| [file-bindings.json](file-bindings.json) | 28 份原字节证据副本的来源、长度和 SHA-256 |

代码与软件回归候选仍为 `4c23f6be1da23821804e66aa815473a7cc165bce`：2047 通过、1 项既有 Windows 跳过。此归档的文档提交不继承该提交的完整回归结论。原图、TIFF、字节块、检查裁图和大部分辅助产物保存在 `D:/drone-agent-data/`；归档文件里的外部路径按其原始记录保留，未改写历史哈希。
