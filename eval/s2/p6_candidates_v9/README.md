# P6 实体群候选开发包

承接 v8 的协议问题，独立提示 `road-candidate-groups-dev-v1` 将实体与道路关系、单件 / 群组定位和描述不确定性分开。沿用六个已曝光视图及 33 个原提案，只提供 ID 和框；不把历史答案当真值，不重算 v8。见[开发协议](../../../docs/p6-candidate-groups-development.md)。

当前为调用前准备：[配置](config.json)、[协议](protocol.json)、[计划与源文件挂载映射](plan.json)、[输入清单](input-manifest.json)已生成，模型尚未调用。[缺席快照](pending-review.json)保留 33 项无回答状态，不能当作模型未知；[兼容性检查](compatibility-audit.json)确认六条 v8 回答均不接受为新协议回答。

源码：[实体群协议与回放](../../../scripts/p6_candidate_groups.py)、[独立启动器](../../../scripts/p6_candidate_groups_cloud.py)。沿用原 [录制器](../../../scripts/p6_candidate_runner.py)及[项目锁执行器](../../../scripts/p6_candidate_remote.py)，原脚本字节不变。新协议在容器内按冻结映射命名为 `p6_candidate_review.py`，真实来源及 SHA-256 写入计划，不混同于原 v8 契约。

本机包：`D:/drone-agent-data/p6-candidate-groups-20261015/diagnostic/`。调用前冻结本包和代码；项目锁忙零调用退出，不循环重试。原图、掩码、传输包和日志留在仓库外；仅元数据与脱敏录制归档。Ruff 和 79 项相关测试已通过，其中新协议 28 项；全量测试及实际模型结果分别记录。

没有独立人工业务真值，不计算精确率 / 召回率，不改变在用画像；`candidate` 仅为模型开发分组，所有实例框与人工结论为空，P6 未准出。
