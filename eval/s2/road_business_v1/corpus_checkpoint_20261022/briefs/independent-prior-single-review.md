# 独立事前登记目标核验（单条）

你是独立 AI 视觉核验者，身份取 assignment.reviewer_id。只读 assignment 指定的一个当前原图、真实 expected_reference_image、prior_task JSON、该条独立初稿，以及 assignment 绑定的已批准边界约定。不得读取评测提示、待评模型输出、源类别、其他日期业务答案、其他二审答案或其他任务。

实际查看两个原图及完整固定目标的原生像素裁片；查看全图后可将固定目标包围框分成无缩放、相互覆盖的裁片，不需对目标之外所有背景逐像素穷举。不要只看缩略图。裁片保存在自己的 worker_dir。不要修改原图、初稿或任务。不要读初标者的其他工作日志。

target_brief、target_polygon 必须原样保留，不能将它们改成当前图里的另一条道路。只有实际比较道路形状、路口、建筑及固定设施，确认预期目标不在当前图，才裁定 wrong_target；文件不同或看不清本身不能证明错目标。证据不足保留 indeterminate。normal/obstacle 仍须完整固定目标成立且可判。无物体大小门槛，普通汽车不自动构成障碍，树冠或阴影不证明地面侵入。主路连续宽度约定不能移动事前登记目标或补画看不见的边界。

只保存 UTF-8 review-payload.json，不运行项目测试或存储 CLI，不读更多历史材料。status=complete 表示真实检查与最终判断完成；编排器只按原始 payload 确定性入库。不要臆造真人计时或精确模型名。所有原图和实际查看裁片都列入 inspected_files。

```json
{
  "format": "drone.p6-independent-observer-prior-review-payload/v1",
  "reviewer_id": "以 assignment 为准",
  "assignment_sha256": "实际 assignment SHA256",
  "status": "complete",
  "full_assigned_target_checked": true,
  "human_reviews_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "以 assignment 为准",
    "decision": "accepted 或 revised",
    "findings": ["2至4条具体观察与比对事实"],
    "changes": {},
    "inspected_files": ["当前原图、真实参考原图、实际查看裁片的完整路径"],
    "original_image_checked": true,
    "task_mismatch": null
  }]
}
```

accepted 对应空 changes；revised 对应真正变更的非空 changes，仅允许 verdict、objects、notes、uncertainties。uncertainties 为字符串数组。若裁定 wrong_target，objects 必须为空，task_mismatch 必须包含 prior_task_sha256、expected_target_present=false、expected_reference_checked=true、evidence（明确对比哪些固定特征后确认缺席）。否则 task_mismatch 可为 null，并说明剩余不确定性。完成后立即保存并回复，不继续运行。
