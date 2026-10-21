# 道路材料增量检查点 · 2026-10-22

WP-P6-01 仍开放。本目录接续[2026-10-21 B](../corpus_checkpoint_20261021b/README.md)，保存当前增量证据。图片、源字节和原生裁片保留在数据根，不进 Git。本目录包含业务答案，不向后续盲标者或独立视觉核验者整包提供。

- 初稿 286 条，选中独立二审 193 条；正常 10、障碍 58、不可判 79、错目标 4、排除源 42。全部绑定检查通过，参考观察不等于正式独立资产。
- [本机普通188条](qa/ordinary-annotation-integrity.json)与云端事前任务2+1+1+1条分别执行；[汇总](annotation-integrity.json)是实际执行范围的并集，不声称同一次单主机执行。最终上下文为 v25。
- [3个候选](candidate-qualification.json)经独立身份/范围核验、新口径独立边界复核及云端 f132b98 核心逐条检查，样本级问题为0。1个校准、2个测试候选；整体仍为[missing](qa/candidate-audit/core-audit.json)，缺数量、类型、测试站点与真实跨次，协议未冻结；正式资产、配对及调用均为0。
- 主路连续宽度约定已经用户批准，[原未知证据](observers/physical_reviewer_1/identity-evidence-v1.json)保持原样，新判断来自另一独立观察者。接入口不纳入整片前场，遮住完整边界仍不可判；未降 D089 数量或质量门槛。
- 一次新分工夹带旧二审已停止且未产出结果，保留[退休记录](boundary/retirement.json)和原分工；干净新上下文重新完成全部3条，实际[选取检查](boundary/selection-check-after.json)通过。格式确认不能覆盖更新的真实语义复核。
- 两个校准参考经实际看图仍未解决，不生成错目标任务。11个既有登记任务的实际进度见[队列](work-queue/prior-task-progress.json)；原任务与参考不移动，不能用当前图另一路段替代。
- 既有837/837来源结果与软件2074通过/1项既有Windows跳过仍只由[父检查点](../corpus_checkpoint_20261021b/README.md)中 f132b98 的原回执负责，没有对882个窗口重跑全部来源或模型精度。当前只新增三候选源检查及增量标注绑定核对。
- 外置合并器曾因两个未选中快照的换行字节不同而停止，原失败和原字节保留；修正版只合并各分部真正选中的摘要绑定快照，不重写摘要。
- 一条观察者payload的证据字段为列表，严格入库已拒绝；原字节保留，由同一观察者仅作无损格式修正。过早依赖它的v6控制器在本机停止，未启动云端审核；成功复核另用v7，不倒改失败。

准确待办见[当前交接](../../../../docs/p6-road-corpus-handoff-2026-10-22.md)、[覆盖](candidate-coverage.json)、[结构缺项](admission-census.json)和[跨次覆盖](repeat-coverage.json)。真人候选计时延期至运营，现场清障未验证。

`file-bindings.json` 绑定全部叶文件的实际字节；`external-helper-bindings.json` 绑定外置工具。后续证据只增建，不覆盖本检查点。
