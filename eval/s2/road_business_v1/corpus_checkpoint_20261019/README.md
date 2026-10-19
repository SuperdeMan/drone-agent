# 2026-10-19 来源与软件检查点

本目录保存 21 份已完成证据的原字节副本，来源位置、字节数和 SHA-256 见 [file-bindings.json](file-bindings.json)。这不是正式数据冻结，也不是 WP-P6-01 准出。

- `software-f893767.json` 对应旧派生来源候选；`crasar-source-audit-f893767.json` 记录 691 次真实源字节解码检查。云端源块和完整重提取仍在外部数据根。
- `software-4c23f6b.json`、同版本 XML / Ruff / result 对应 OAM 完整来源和主观察去重改动：2047 通过、1 项既有 Windows 跳过。`oam-source-audit-4c23f6b.json` 记录云端实际 28 次来源验证。
- `oam-local-cloud-compatibility.json` 证明 28 张原生图片、固定范围及有效区像素一致。PNG 压缩字节不同，保留原检查产物，正式来源审计使用云端提取和同环境重放。
- `annotation-provenance-60.json` 与 `reviews/` 保存 60 条 AI 二审及其绑定核对，既不冒充人类真值，也不授予资产独立性。
- `business-gate-4c23f6b.json` 为实际运行的 `missing` 记录，`wp_p6_01_closed=false`；尚缺正式冻结数据、任务、校准与测试证据。

状态和续办入口见[当前交接](../../../../docs/p6-road-corpus-handoff-2026-10-19.md)。图片、源文件、实际裁剪和后续采集不放入 Git；本目录不自动随运行中的标注更新。
