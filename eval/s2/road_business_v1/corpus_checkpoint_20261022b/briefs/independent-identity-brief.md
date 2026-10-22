# 中性物理目标核验

你是独立AI物理目标核验者，身份为assignment.reviewer_id。只读assignment中的6个目标原图/guide/mask、固定世界范围、登记描述/轮廓和绑定的已批准边界约定。不得读业务类别、首稿/二审答案、旧身份核验答案、待评模型提示/输出或历史交接。核对物理目标和范围，不重新标路况。

逐张实际看原图，并看完整A/B范围、两端横截面和两侧边缘的原生裁片；只需覆盖固定目标，不必对其余背景逐像素穷举。保持端点及世界范围，不重新选择路段，不修改目标多边形。按接入口两侧可辨认主路边缘及连续宽度处理连通前场，不包括整片前场；无法支持完整边界时写null，不凭mask、源线或树冠补造边界。

逐条分别回答physical_target_identifiable、registered_scope_matches_physical_target、whole_pavement_target_outline_supported，值为true/false/null。第三项按批准的主路连续宽度口径解释。给出2至4个固定地标、范围依据和不确定性。对6个目标的15个无序对检查same_physical_target、scope_overlap；道路形状、路口、建筑及固定设施是视觉依据，真实地理坐标可作为辅助，不因文件名或group ID不同就断言独立。相同目标/重叠或证据不足如实说明。

只写自己的worker_dir检查图、辅助脚本和identity-payload.json，UTF-8；不要修改任何来源、目标、业务标签或项目代码，不抓源、不跑存储CLI或测试。完整payload如下，row覆盖全部6条，pairwise覆盖全部15对：

```json
{
  "format": "drone.p6-independent-physical-target-review-payload/v1",
  "reviewer_id": "physical_reviewer_2",
  "assignment_sha256": "实际assignment文件SHA256",
  "status": "complete",
  "human_records_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "assignment中的ID",
    "physical_target_identifiable": true,
    "registered_scope_matches_physical_target": true,
    "whole_pavement_target_outline_supported": null,
    "identity_landmarks": ["具体固定地标"],
    "evidence": "实际检查范围、真实道路身份及批准口径下的边界依据",
    "uncertainties": [],
    "inspection_files": ["原图、guide、mask及实际看过的原生裁片完整路径"]
  }],
  "pairwise": [{
    "sample_ids": ["分配ID1", "分配ID2"],
    "same_physical_target": false,
    "scope_overlap": false,
    "evidence": "实际固定地标/道路形状对比，并区分地理元数据辅助"
  }]
}
```

不得把核验结果直接写成grouping_verified=true或正式准入。实际完整检查后立即保存并结束，无需其他CLI/测试。
