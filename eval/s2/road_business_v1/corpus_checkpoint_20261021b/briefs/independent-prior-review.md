# 独立先登记目标二审

只读取 assignment 指定的两条当前图、真实 expected_reference_image、对应 prior_task JSON 及所附独立 AI 首稿。不得读取评测提示、待评模型输出、源类别、其他日期的业务答案。你是另一名独立 AI 视觉核验者；不代表真人真值或人工效率。

必须实际检查当前图与真实参考图的原图和覆盖完整目标的原生像素裁片。登记任务 target_brief、target_polygon 必须原样保留，不能将它们改成当前图里另一条道路。先登记的预期目标与当前实际观察区域是两个概念，不能仅因当前图有道路就认定目标正确，也不能仅因道路不可判就认定错目标。

只有在比较道路形状、交叉口、建筑和周边固定设施后，能够明确确认预期目标不在当前图中，才使用 wrong_target。否则保留 indeterminate，说明证据不足。normal/obstacle 仍须完整固定目标实际成立且可判；无物体尺寸门槛；正常汽车不自动构成障碍，树冠/阴影不证明地面植被侵入。

在自己的 worker_dir 保存检查图和 review-payload.json。使用 UTF-8，不改任何其他文件或代码，脚本勿命名 inspect.py。唯一交付为：

```json
{
  "format": "drone.p6-independent-observer-prior-review-payload/v1",
  "reviewer_id": "prior_verifier_3",
  "assignment_sha256": "以任务为准",
  "status": "complete",
  "full_assigned_target_checked": true,
  "human_reviews_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "以任务为准",
    "decision": "accepted 或 revised",
    "findings": ["2至4条具体比对事实"],
    "changes": {},
    "inspected_files": ["当前原图、真实参考原图、实际检查裁片的完整路径"],
    "original_image_checked": true,
    "task_mismatch": {
      "prior_task_sha256": "assignment 中对应值",
      "expected_target_present": false,
      "expected_reference_checked": true,
      "evidence": "对比哪些固定特征后确认预期目标缺席；不能只写与参考图不同"
    }
  }]
}
```

若不裁定 wrong_target，task_mismatch 可为 null，并在 findings 说明不确定性。changes 仅含真正改变的 verdict、objects、notes、uncertainties；禁止更改目标描述/多边形。accepted 对应空 changes，revised 对应非空。全部原图均列入 inspected_files。uncertainties 必须为字符串数组，不臆造时间。status=complete 表示视觉判断已完成，可由编排器按原始 payload 确定性入库；不需要另跑存储 CLI 或项目测试。
