# 独立物理目标身份核对

只看分配的原图、guide、mask、固定世界范围、中性目标描述和轮廓，不读任何业务标签、评测提示、模型输出或源类别。你核对物理目标及其范围，不重新判断路况，也不代替真人。

逐张实际查看原图和覆盖整个A/B范围的原生裁片，核对轮廓是否对应一个能用道路形状、交叉口、建筑或设施定位的真实完整铺装路段。保持既定世界端点，不重新选路段、不缩小范围。如果完整横截面/两侧路缘无法确认，明确 unknown，不能凭指南线或源mask推断冠下道路。源mask有效只说明有像素。

分别回答：physical_target_identifiable、registered_scope_matches_physical_target、whole_pavement_target_outline_supported，用 true/false/null。给出2至4个固定地标、实际身份依据和未解问题。对分配的目标逐对检查是否有相同物理目标或重叠范围；不能仅因不同文件名、不同crop或group ID而宣称独立。地图坐标和来源是元数据辅助，未亲自观察的其他道路不作身份结论。

只在自己worker_dir写检查图及完整 identity-payload.json：

```json
{
  "format": "drone.p6-independent-physical-target-review-payload/v1",
  "reviewer_id": "physical_reviewer_1",
  "assignment_sha256": "以任务为准",
  "status": "complete",
  "human_records_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "以任务为准",
    "physical_target_identifiable": true,
    "registered_scope_matches_physical_target": true,
    "whole_pavement_target_outline_supported": null,
    "identity_landmarks": ["固定地标"],
    "evidence": "实际看到的范围和物理目标依据",
    "uncertainties": ["未知项"],
    "inspection_files": ["原图、guide、mask和实际原生裁片完整路径"]
  }],
  "pairwise": [{
    "sample_ids": ["分配ID1", "分配ID2"],
    "same_physical_target": false,
    "scope_overlap": false,
    "evidence": "图上固定设施/道路形状比对，区分图像依据与坐标辅助"
  }]
}
```

pairwise 覆盖分配目标的全部无序对，布尔不确定时写null。不得修改目标描述、多边形或业务标签，不把核对结果直接写成grouping_verified=true。Python完整路径 D:/drone-agent-data/p6-corpus-completion-20261018/tools-venv/Scripts/python.exe。使用UTF-8，勿命名inspect.py。全部实际检查后保存status=complete并结束，无需存储CLI、抓源或项目测试。
