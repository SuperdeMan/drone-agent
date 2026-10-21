# 道路材料增量检查点 · 2026-10-21 B

WP-P6-01 仍开放。本目录接续[前一检查点](../corpus_checkpoint_20261021/README.md)，保存本轮独立观察者原批次、确定性入库记录、当前来源复核和准确待办。图片、原始源字节块及检查裁片保留在数据根，不进入 Git。

- 初稿 275 条；选中二审 182 条，实际绑定检查全部通过；类别见 [progress.json](progress.json)。这是参考观察计数，独立资产与正式配对仍为0。
- CRASAR 索引扩为691+46+100=837条，[元数据验证](source-index/metadata-validation.json)逐条核对收据绑定并保持旧691条库存不变；分组、独立性及物理身份不据此通过。OAM45条不在该索引。
- 当前 `f132b98fd0f6148ca3ab5f7e168de147433fc7be` 实际核对837/837条 CRASAR 源字节、原生窗口与既有独立回放，[来源回执](qa/full-crasar-source-f132b98.json)单独负责这一范围；不是882条全部来源通过，也不是资产准入或道路识别精度通过。
- [普通样本检查](qa/ordinary-annotation-integrity.json)在本机执行；[事前任务检查](qa/prior-annotation-integrity.json)在云端真实来源字节根执行。合并仅取不重叠记录的并集，[annotation-integrity.json](annotation-integrity.json)保留真实执行分部，不声称同一次单主机运行。
- 先登记任务的目标描述与多边形原样保留；wrong_target 另需真实预期参考及独立核验的缺席证据。11个任务仍为构造并披露的测试侧候选，不声称自然发生的操作者拍错；剩余分工见 [任务队列](work-queue/prior-task-progress.json)。
- 本轮 AI 观察者明确完成 payload 后由编排器确定性保存；原批次、摘要、文件时间与存储时间均保留。没有编排者视觉改判、真人真值、真人耗时或额外看图声明。
- 软件完整回归只绑定 `f132b98`：2074通过、1项既有Windows Unix-socket跳过、0失败。本目录后来的文档提交不转借该成绩；[业务门禁](qa/business-gate-missing.json)仍为 missing。正式数据未冻结、校准/测试未执行、正式模型调用为0。

当前接手入口为[道路材料交接](../../../../docs/p6-road-corpus-handoff-2026-10-21.md)。D089 的样本量、类型、站点、隔离、真实跨次与质量门槛全部保留；真人候选计时继续延期至运营，现场清障效果未验证。

`file-bindings.json` 对本目录叶文件的实际字节负责；`external-helper-bindings.json` 指向数据根的工具。后续新增证据使用新的目录和文件，不覆盖本检查点。
