# 独立道路实体候选二审

只看任务分配的图片、guide、mask 和所附独立 AI 首稿。不能读取评测提示、待评模型输出、源类别、其他图片或其他日期的业务答案。当前参考方法由用户授权为独立 AI 初标后由另一独立 AI 看图核验；不是人类真值，不作正式准出结论。

必须实际查看原图和覆盖完整固定目标的原生像素裁片，尤其两端 A/B 横截面及两侧路缘。可以按目标包围盒加边缘生成原生 tile；不能只看概览缩略图。保持既定世界范围及 A/B 两端，不因遮挡、阴影、源黑区或障碍缩短或移动目标。多边形沿实际铺装道路/设施边界；不要追求无意义的逐像素细描。

`normal`：完整范围可判断，未确认符合口径的实体；`obstacle`：确认实体进入路面，列 `debris`、`vegetation`、`other_entity` 和归一化 polygon；`indeterminate`：目标成立但边界、实体或质量不足以确定，写明原因；`excluded_source`：固定范围的完整道路/设施身份或源覆盖不成立，通常 polygon=null。普通正常行驶/停放汽车不自动成为障碍；树冠、影子不证明实际植被侵入。固定铺装范围内清晰可见的行人可作 other_entity 候选，静态照片不证明实际阻路、违规或清障必要。不设最小物体尺寸或占比。

首稿类别仅是待核对建议，接受、修订、排除均可。每条 findings 用 2–4 个简洁事实，uncertainties 为字符串数组，缺项用 []。只修改有依据的字段，保留其余原件与检查图。不声明真人复核或人工效率。

在任务自己的 worker_dir 内生成检查图及 `review-payload.json`，不要修改其他文件或项目代码。使用显式 UTF-8 或 apply_patch，避免 PowerShell 中文管道，脚本勿命名 inspect.py。

唯一交付文件是下面的 JSON 对象（不是只有草稿数组）：

```json
{
  "format": "drone.p6-independent-observer-review-payload/v1",
  "reviewer_id": "以任务为准",
  "assignment_sha256": "以任务为准",
  "status": "complete",
  "full_assigned_target_checked": true,
  "human_reviews_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "以任务为准",
    "decision": "accepted 或 revised",
    "findings": ["实际检查事实"],
    "changes": {},
    "inspected_files": ["实际原图、guide和原生检查图的完整路径"],
    "original_image_checked": true
  }]
}
```

changes 仅含真正修订的 target_brief、target_polygon、verdict、objects、notes、uncertainties；accepted 时为空，revised 时非空。全部原图与 guide 必须在 inspected_files。payload 的 status=complete 表示你的视觉判断已经完成，可交给编排器确定性入库。时间由实际文件/存储操作记录，不填造看图时间。不需要另跑存储 CLI、来源采集或项目测试；保存这一张完整 payload 后结束。
