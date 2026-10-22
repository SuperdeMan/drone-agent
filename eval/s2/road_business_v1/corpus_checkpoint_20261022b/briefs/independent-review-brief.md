# 独立道路二审（两条）

只看assignment分配的两张原图、guide、mask、固定世界范围和各自独立AI首稿。不得读取其他初标或二审答案、其他日期业务答案、源类别、待评模型提示/输出或历史交接。首稿只是待核验建议，可接受或修订，不按配额或其他样本推断。

通过view_image实际看原图和覆盖完整固定A/B目标的原生裁片，尤其两端完整横截面与两侧边缘。可只对目标包围框作重叠原生裁片，不需穷举背景。保持世界端点与全长，不因遮挡、阴影、实体或源黑区移动/缩短目标。道路与出入口、支路或停车前场连通时，按接入口两侧可辨认主路边缘及连续宽度界定，不纳入整片前场；无法支持完整边界时保留不可判，不补造未知边界。

normal要求完整范围可判且未确认实体进入；obstacle要求真实实体进入道路，列debris/vegetation/other_entity及polygon；indeterminate用于任务成立但边界、实体或质量不足；excluded_source用于固定完整目标身份或源覆盖不成立。不设大小/占比门槛。普通车辆不自动构成障碍，树冠/影子不证明实际地面植被侵入；静态图不推断运动、违规或清障必要。不要自行构造wrong_target任务。

每条findings写2至4条具体观察。只修订确有依据的字段；uncertainties为字符串数组。所有原图、guide及实际看过的原生裁片列入inspected_files。只在自己的worker_dir保存检查图、辅助脚本和review-payload.json，不改其他文件或项目代码。UTF-8，勿用PowerShell中文管道，勿命名inspect.py，不抓源、不跑存储CLI或项目测试。

```json
{
  "format": "drone.p6-independent-observer-review-payload/v1",
  "reviewer_id": "assignment中的ID",
  "assignment_sha256": "实际assignment SHA256",
  "status": "complete",
  "full_assigned_target_checked": true,
  "human_reviews_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "assignment中的ID",
    "decision": "accepted或revised",
    "findings": ["实际检查事实"],
    "changes": {},
    "inspected_files": ["原图、guide及实际原生裁片完整路径"],
    "original_image_checked": true
  }]
}
```

accepted对应空changes，revised对应真正变更的非空changes；仅可含target_brief、target_polygon、verdict、objects、notes、uncertainties。两条均真正检查完后保存完整status=complete并立即结束。编排器仅确定性存储，存储时间不代表新看图或真人效率。
