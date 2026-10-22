# 固定范围的独立边界复核

遵守同目录independent-review-brief.md的全部隔离、实际原图、原生裁片、实体判断和payload要求。只看assignment分配4个目标、各自原始首稿和绑定的已批准边界约定；不能看旧二审、物理核验答案、其他日期答案或待评模型内容。

道路与接入口、支路、前场相连时，用两侧可辨认主路边缘及连续宽度界定，不纳入整片前场。影子本身不自动意味着不可判；实际仍能看清完整主路边缘时按图上事实判断。树冠/阴影/水或材料混淆确实挡住完整横截面时，不补造隐藏边界，保持未知。无需逐像素唯一或每一顶点精确，但必须支持完整固定道路范围。

固定A/B是事前登记地理横截面，可位于道路中部，不要求恰好是自然路端或设施边界；须确认它对应同一道路且完整截面可判。世界端点、长度不动。允许有依据修正原多边形，不能为实体或遮挡缩小/换目标。接受、修订、排除均按你实际观察，不预设正常或一致意见。

rows仍采用independent-review-brief.md的六字段schema，顶层额外提供boundary_evidence数组，每个目标一条：

```json
{
  "sample_id": "assignment中的ID",
  "policy_sha256": "assignment绑定摘要",
  "fixed_world_endpoints_unchanged": true,
  "whole_target_boundary_supported_under_approved_convention": null,
  "basis": "实际哪些边缘/横截面可见或被遮住，按原生裁片说明具体范围；不能只说同意首稿",
  "uncertainties": []
}
```

支持字段为true/false/null。normal/obstacle仍须有效完整目标，未知不能靠两侧可见宽度自动补造。使用apply_patch或ASCII英文/Unicode转义写UTF-8 JSON；禁止PowerShell中文管道。仅写自己worker_dir的检查图、脚本与完整review-payload.json，不抓源、不跑CLI/项目测试，实际完整检查后保存status=complete并立即结束。
