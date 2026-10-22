# P6工程交付关闭证据 · 2026-10-22

**工程已关闭，完整道路业务验收未通过。** 按用户批准的D091收口，准确软件候选为`900abf1a88e8c6e62f8c26c6ddfb8274ca130f15`；最终范围见[工程验收](../../../../docs/p6-engineering-readiness.md)。

- [scope.json](scope.json)：真实范围授权、原D089协议摘要、未验证边界及后续任务。
- [ci.json](ci.json)：准确候选的[CI #39792483834](https://github.com/SuperdeMan/drone-agent/actions/runs/39792483834)成功回执，绑定实际检出提交、全部步骤及原始元数据/日志摘要。
- [readiness.json](readiness.json)：实际工程门禁11/11通过，业务验收false；SHA256`a2e567771de3e77fce4a1e2ed36c4f0ce5926143564ccf1da1c1622d99c573ef`。
- [failed-65fc421.json](failed-65fc421.json)及对应公开CI元数据/摘要保留原2344通过/1失败；路径解析修正后由新的准确候选独立验证，不回写旧结果。
- `ci-run-source.json`、`ci-jobs-source.json`、`ci-pr-source.json`为公开API原始字节；[ci-log-excerpt.txt](ci-log-excerpt.txt)仅摘录实际检出与测试汇总，不称完整日志。完整日志保留在CI回执指向的外置路径，未导入Git。
- [D材料检查点](../corpus_checkpoint_20261022d/README.md)的750叶文件逐字节核验；331参考、93未完成首稿、19局部正记录及22负记录的边界不变。

本包不授予正式资产/配对、数据冻结、模型质量或画像采用资格；[完整业务验收](../../../../docs/road-business-validation-backlog.md)在运营前完成。CI只负责本候选的软件检查，不替代各工作包自己的SITL、模型或硬件证据。
