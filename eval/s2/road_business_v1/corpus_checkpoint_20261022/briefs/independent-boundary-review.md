# 已确认主路连续宽度口径的独立二审

先遵守旧目录独立二审brief的所有原图、完整范围、无尺寸门槛与隔离要求。另读assignment绑定的boundary-policy-approved-v1.json，这是用户已确认的业务口径，不是模型答案。

出入口/支路/前场与主路连通但没有独立路缘时，按接入口两侧可辨认的主路边缘及主路连续宽度界定主路，不自动纳入整片前场。此为功能边界的明确约定，不要求每一点有唯一的材料分界。两侧不能建立可信连续边缘，或树冠/阴影仍遮住完整横截面时，不补造边界，继续不可判。固定世界端点不变，不能因为实体改变范围；允许有依据地修订原多边形，但不能沿实体规避风险。

只看分配的原图/guide/mask、所附首稿和这一已确认口径，不读旧二审结论、物理核验答案、评测提示/输出或源类别。实际查看完整固定A/B原生范围，然后独立接受、修订或排除，不预设正常或其他类别。

交付仍为原brief的完整review-payload.json，rows保持原来的六字段schema。在顶层额外保存boundary_evidence数组，每条为：

```json
{
  "sample_id": "任务中的ID",
  "policy_sha256": "assignment中的摘要",
  "fixed_world_endpoints_unchanged": true,
  "whole_target_boundary_supported_under_approved_convention": null,
  "basis": "实际图像中哪些主路边缘可连续界定；若树冠或阴影使横截面无法判断则说明，不补造",
  "uncertainties": []
}
```

支持字段必须true/false/null按证据给出，不按类别或数量目标决定。修改都在rows.changes实际记录；normal/obstacle须有效目标，实体类型规则不变。身份/范围需要进一步澄清时保留不确定性；不得直接写grouping_verified或正式准出。

只在worker_dir保存实际原生裁片与完整payload，全部完成后status=complete并直接结束，无需存储CLI/项目测试/抓源。Python完整路径 D:/drone-agent-data/p6-corpus-completion-20261018/tools-venv/Scripts/python.exe。原图与旧文件不覆盖，UTF-8写入。
