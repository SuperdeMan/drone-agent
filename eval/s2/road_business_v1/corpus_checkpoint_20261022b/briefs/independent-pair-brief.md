# 独立双图配对核验

只读自己的assignment、其一对真实原图/guide/mask/登记目标及来源日期元数据、绑定边界规则。不得读取任何首稿/二审路况答案、旧配对答案、待评模型提示/输出或源类别。两图是否相同状态须由你实际观察，不因存在配对分工就假定相同。

通过view_image实际看两个原图与覆盖完整固定A/B范围、两端横截面和两侧边缘的原生裁片；可只裁固定目标，不需穷举背景。世界端点与范围不能移动，不能因遮挡缩小目标；主路连通前场按可辨边缘及连续宽度，不包括整片前场。不得修改登记目标或业务标签。

分别判断same_asset、same_condition、distinct_captures，取true/false/null。same_asset根据道路形状、路口、建筑与固定设施对应，坐标辅助，不能只凭文件名/group ID。same_condition须确认完整可比目标的真实状态一致；两图同为某类障碍不自动证明状态一致，也不要求图像逐像素相同。遮挡、阴影或范围疑问使状态不足时写null，不将双方拒判算一致。

different captures的声明限于已绑定的真实无人机派生来源、不同父图摘要和可信不同作者日期组。该范围不要求补造UTC、航班ID、精确先后或原始相机帧；若给定日期/来源冲突，保持未知并说明。不能推断清障原因、维修或现场处置。

只在自己的worker_dir写检查图、脚本和pair-payload.json，使用UTF-8或英文ASCII文字，禁止PowerShell中文管道，勿命名inspect.py。不跑来源采集、存储CLI、项目测试。完整交付：

```json
{
  "format": "drone.p6-independent-pair-review-payload/v1",
  "reviewer_id": "assignment中的ID",
  "assignment_sha256": "实际assignment SHA256",
  "status": "complete",
  "full_assigned_pairs_checked": true,
  "human_reviews_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "pair_id": "assignment中的ID",
    "same_asset": true,
    "same_condition": null,
    "distinct_captures": true,
    "evidence": "实际固定地标、完整状态比对和来源日期依据；必须为一个字符串",
    "uncertainties": [],
    "inspection_files": ["两个原图、各自guide/mask及实际看过的原生裁片完整路径"]
  }]
}
```

uncertainties为字符串数组；evidence为单个可读字符串。全对实际检查后立即保存status=complete并结束。没有真人确认、真人计时或正式准入声明。
