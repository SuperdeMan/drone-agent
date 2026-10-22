# 校准侧中性参考目标登记

你只登记真实物理目标，不标注正常/障碍/不可判路况。只能看assignment分配的两张原图、guide、mask与固定世界范围，不能读业务标签、模型提示/输出、源类别或其他目标答案。

实际查看原图及完整A/B区间的原生裁片，核对两端横截面、道路两侧和固定地标。保持已冻结世界端点，不能因为实体、阴影、树冠改选目标或缩短范围。目标轮廓沿可辨认的完整铺装道路/设施边界，允许合理简洁轮廓，不追求无意义逐像素描边；边界不足以支持登记时写unresolved，polygon=null，不能补造冠下边界。实体状态不是本任务输出，不因有实体而排除可登记道路。

worker_dir内唯一交付为reference-payload.json：

```json
{
  "format": "drone.p6-independent-neutral-reference-payload/v1",
  "registrar_id": "calibration_registrar_3",
  "assignment_sha256": "任务中的摘要",
  "status": "complete",
  "human_records_created": 0,
  "business_labels_created": 0,
  "formal_release_eligible": false,
  "rows": [{
    "sample_id": "任务中的ID",
    "status": "registered 或 unresolved",
    "target_brief": "目标位置、两端、完整范围与固定设施描述，不评价路况",
    "target_polygon": null,
    "identity_landmarks": ["固定地标"],
    "scope_evidence": "原生检查如何完整覆盖不变的世界范围",
    "uncertainties": [],
    "inspection_files": ["实际原图、guide、mask和原生裁片的完整路径"]
  }]
}
```

registered必须有有效归一化简单polygon；unresolved必须null。两个目标都实际完成后才status=complete。仅在自己的worker_dir写检查图和payload，使用UTF-8，勿命名inspect.py；Python完整路径 D:/drone-agent-data/p6-corpus-completion-20261018/tools-venv/Scripts/python.exe。不要改他人文件或运行存储CLI、来源抓取、项目测试；保存完整payload后结束并给SHA/路径。
