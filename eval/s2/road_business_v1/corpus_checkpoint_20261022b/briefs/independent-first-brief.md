# 隔离道路初标（两条）

你是用户授权的独立AI初标者，身份取assignment.author_id。只读自己的assignment、所分配的两张原图/guide/mask/固定世界范围，以及下列已批准规则。不得读取其他初标/二审答案、其他日期业务答案、待评模型提示或输出、源类别、材料总表或历史交接。不要按配额或期望标签裁定。

实际查看每张原图，并查看覆盖固定A/B范围、两端完整横截面和两侧边缘的原生像素裁片。可以只裁完整目标包围框并用重叠原生裁片覆盖，不需要穷举目标之外背景。裁片只保存在自己的worker_dir，原图/guide/mask不能覆盖。不要仅凭缩略图或mask确认道路及边界。

保持固定世界范围和端点，不因树冠、阴影、实体或源黑区移动/缩短目标。道路与接入口、支路或停车前场连通时，按两侧可辨认的主路边缘和连续宽度界定主路，不纳入整片前场；不能建立可信边缘或完整横截面仍被遮住时保持不可判，不补造未知边界。目标轮廓简洁即可，不要求逐像素描边。

完整范围可判、未确认实体进入为normal；确认实体进入道路为obstacle，并用debris/vegetation/other_entity及归一化polygon描述；道路任务成立但实体、边界或质量证据不足为indeterminate；固定目标的完整道路身份或源覆盖不成立为excluded_source。不设物体大小/占比门槛。普通汽车不自动算障碍，树冠/阴影不证明地面侵入；静态图不推断运动、违规或清障效果。普通初标不产生wrong_target。

只做这两条观察，不抓源、不运行存储CLI/项目测试，不读额外历史。UTF-8或apply_patch写JSON，不走PowerShell中文管道，脚本不命名inspect.py。唯一完整交付为worker_dir/first-payload.json：

```json
{
  "format": "drone.p6-independent-observer-first-payload/v1",
  "author_id": "assignment中的ID",
  "assignment_sha256": "实际assignment文件SHA256",
  "status": "complete",
  "full_assigned_targets_checked": true,
  "human_reviews_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "assignment中的ID",
    "image_sha256": "assignment中的图片摘要",
    "target_brief": "固定目标的中性描述",
    "target_polygon": null,
    "verdict": "normal/obstacle/indeterminate/excluded_source之一",
    "objects": [],
    "notes": "实际观察依据",
    "uncertainties": [],
    "inspected_at": "datetime.now(UTC).isoformat()的实际完成时间",
    "inspection_files": ["实际原图、guide及看过的原生裁片完整路径"]
  }]
}
```

每个对象仅含defect_type和polygon；仅obstacle有非空objects，normal/obstacle必须有完整有效目标polygon。uncertainties必须为字符串数组。实际检查完两条后立即保存status=complete并结束；不声明真人身份、效率或额外看图。
