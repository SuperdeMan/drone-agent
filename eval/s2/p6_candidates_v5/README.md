# P6 道路边界与实体草稿

本轮于 2026-10-14 开始，2026-10-15 完成草稿核对。用户确认的口径是“暂无大小 / 占用比例门槛，先按目标内实体遮挡候选推进”。本包是已曝光样本上的 AI 草稿，不是独立人工标注或正式评测集。详见[开发记录](../../../docs/p6-road-boundaries-development.md)。

- [policy.json](policy.json)：本轮业务口径，与 v3 / v4 的 substantial / large object 实验定义分开。
- [drafts.json](drafts.json)：6 个目标的道路内域 / 可能外域、13 组拟定实体轮廓，逐一关联 v4 的全部 18 个原始候选框。
- [audit.json](audit.json)：以当前草稿为条件，4 组内域相交、2 组范围外、7 组边缘不确定；不生成整图正常结论或业务真值。
- [review-queue.json](review-queue.json)：13 项待独立业务复核任务，优先 7 项边缘问题和 `12892` 的 E12；真实复核人、结论、曝光情况和耗时均为空。
- [preview-manifest.json](preview-manifest.json) / [visual-review.json](visual-review.json)：19 张图的摘要、图例与实际检查范围。
- [history/](history/)：保留前两版草稿。第一版被包含关系检查拒绝；随后修正可能外域，并按原生像素网格收紧 E12 内域。
- [tool-source/](tool-source/)：最终草稿生成与渲染脚本的原始源码快照；历史草稿以保存的 JSON 为准。

数据图像保留在仓库外：`D:/drone-agent-data/p6-boundary-drafts-20261014/`。最终预览目录为 `previews/f8af01bdbf2d-ad8f2f47/`，包含 6 张目标总图和 E01–E13 原图 / 草稿对照卡。图例：蓝色为拟定道路内域、黄色为可能道路范围，紫色为实体内域 / 可能范围；每图明确标注 AI 草稿、非真值。原图与源掩码没有改写。

`drafts.json` 的 `date` 是本轮开始日；最终修订与图像检查完成日期见 `visual-review.json`。草稿作者看过源标注和旧模型回答；未来复核必须记录提示曝光，不能将本包包装成盲评留出集。

从仓库根目录离线复算，`--output` 指定新的文件：

```powershell
uv run python scripts/p6_boundary_drafts.py --drafts eval/s2/p6_candidates_v5/drafts.json --index eval/s2/p6_candidates_v3/input-index.json --previous-summary eval/s2/p6_candidates_v4/summary.json --data-root D:/drone-agent-data/p6-materials-20261013-round3 --output <新的回执JSON>
```

这个命令不调用模型或业务服务。它要求源摘要一致、几何有效、内域被外域包含、18 个候选全部且仅绑定一次；不允许输入草稿自行授予准出资格。源支持域只扩大可能道路范围，不把其孔洞当作已知道路外；1 像素是数值接触误差带，不是业务大小门槛。
