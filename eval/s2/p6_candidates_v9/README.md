# P6 实体群候选开发包

承接 v8 的协议问题，独立提示 `road-candidate-groups-dev-v1` 将实体与道路关系、单件 / 群组定位和描述不确定性分开。沿用六个已曝光视图及 33 个原提案，只提供 ID 和框；不把历史答案当真值，不重算 v8。见[开发协议](../../../docs/p6-candidate-groups-development.md)。

代码及输入冻结于 `6af7036b0490b3ad47b9505793b1b9a0738f866f`：[配置](config.json)、[协议](protocol.json)、[计划与源文件挂载映射](plan.json)、[输入清单](input-manifest.json)保持原字节。[调用前缺席快照](pending-review.json)保留 33 项历史无回答状态，不能作为当前模型结果；[兼容性检查](compatibility-audit.json)确认六条 v8 回答均不接受为新协议回答。

批次 `p6-candidate-review-dev-20261015T051249Z` 已完成：六次调用、六条回答均合格，云端 / 本地严格回放各六条；[环境回执](attempts/20261015T051249Z/cloud/execution.json)确认持锁期间 9 个驻留容器前后一致。[当前汇总](attempts/20261015T051249Z/replay-summary.json)为 20 项候选、3 项范围外、10 项待复核，均非人工结论；[原始录制](attempts/20261015T051249Z/cloud/output/recordings/)按字节保留。

[分析](attempts/20261015T051249Z/analysis.json)仍发现 `12892-p01` 的候选窗口与既有可能道路草稿不相交；条件几何和 AI 视觉意见均不作真值计分。[复核队列](attempts/20261015T051249Z/review-queue.json)保留全部 33 项，独立人工结论、复核人和时间均为空。[分析驱动](tool-source/analyze_live.py.txt)只定位风险，不改变模型分组。

源码：[实体群协议与回放](../../../scripts/p6_candidate_groups.py)、[独立启动器](../../../scripts/p6_candidate_groups_cloud.py)。沿用原 [录制器](../../../scripts/p6_candidate_runner.py)及[项目锁执行器](../../../scripts/p6_candidate_remote.py)，原脚本字节不变。新协议在容器内按冻结映射命名为 `p6_candidate_review.py`，真实来源及 SHA-256 写入计划，不混同于原 v8 契约。

本机包：`D:/drone-agent-data/p6-candidate-groups-20261015/diagnostic/`。已有六次调用，不能再次启动；只允许离线回放。原图、掩码、传输包和日志留在仓库外；仅元数据与脱敏录制归档。Ruff 和 79 项相关测试通过，其中新协议 28 项；冻结代码 Windows 全量 1743 通过 / 1 项既有 Unix socket 跳过，见[验证回执](../../../docs/verification/p6-candidate-groups-2026-10-15.json)。

没有独立人工业务真值，不计算精确率 / 召回率，不改变在用画像；`candidate` 仅为模型开发分组，所有实例框与人工结论为空，P6 未准出。
