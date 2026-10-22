# P6 逐候选语义复核开发包

新任务固定 v7 的 33 个未经确认的框，逐项区分实体性质、目标内外关系、是否混杂及不确定性。它不发现新对象，不把旧回答补成新协议，不计算识别准确率或召回率，不生成整图正常。见[协议](../../../docs/p6-candidate-review-development.md)。

源批次的环境核对失败仍保留；本批必须在模型调用前非阻塞取得项目已有 `stack.lock`，拿不到锁就以零调用退出。源码、提示、输入及后续回执在此独立归档，v1–v7 不改写。独立人工业务真值仍未建立，P6 未准出。

## 当前状态：六次实调完成，协议暂不采用

- 冻结提交：`70fcc3303c6569e5a1d5d80b461f5ba1938b0ccc`。[配置](config.json)、[协议](protocol.json)、[计划](plan.json)及[输入提案清单](input-manifest.json)在尝试前提交。
- 第二次尝试 `p6-candidate-review-dev-20261015T040711Z`：[启动记录](attempts/20261015T040711Z/launch.json)、[执行回执](attempts/20261015T040711Z/cloud/execution.json)。项目锁持续覆盖前后检查，9 个驻留项目容器的名称、ID、镜像、启动时间与重启次数一致。
- MiniMax-M3.1-Flash-Preview 六次调用、六条回答、调用失败 0；[运行记录](attempts/20261015T040711Z/cloud/output/run.json)与[原始录制](attempts/20261015T040711Z/cloud/output/recordings/)保留。云端 / 本地严格回放各六条；只有两条通过冻结格式与一致性校验。
- [本次汇总](attempts/20261015T040711Z/replay-summary.json)：7 项保留合格模型观察，26 项因所在回答不合格而整条隔离；33 项均待复核。不能称为 33 条模型未知，不计算准确率或召回率。
- [失败分析](attempts/20261015T040711Z/analysis.json)定位重复 JSON 键、细化框越界和车辆不确定性原因缺失；[分析驱动](tool-source/analyze_live.py.txt)只做定位。当前规则还把材质不明与混杂实体群一并阻断，需与业务候选口径分开设计。没有修补输出、局部挽救或重问。
- [实调验证回执](../../../docs/verification/p6-candidate-live-2026-10-15.json)绑定运行时、冻结源码、录制与元数据摘要。新提示未采用，在用画像未改变，P6 未准出。
- 冻结代码 `70fcc33`：Ruff、51 项定向、Windows 全量 1715 通过 / 1 项既有跳过；[Linux 独立锁检查](lock-selfcheck.json)验证三项锁行为，其[驱动源码](tool-source/lock-selfcheck.py.txt)按摘要保留。全部检查均未产生新的业务真值。

## 历史零调用尝试

第一次尝试 `p6-candidate-review-dev-20261015T032224Z` 的[启动记录](attempts/20261015T032224Z-launch.json)、[执行回执](attempts/20261015T032224Z-execution.json)和[摘要](attempt-summary.json)原样保留：`blocked_lock_busy`、调用 0、诊断容器未启动、未进行环境核对。[缺席快照](pending-review.json)对应这一次尝试，不能作为最新模型结果。[兼容性检查](compatibility-audit.json)保存六条旧格式回答被拒绝的结果。[原工具检查回执](../../../docs/verification/p6-candidate-review-2026-10-15.json)同时保存此前全量回归和忙锁记录，不改写为实调回执。

本机包：`D:/drone-agent-data/p6-candidate-review-20261015/diagnostic/`。源码入口为 [p6_candidate_review.py](../../../scripts/p6_candidate_review.py)（协议 / 汇总）、[p6_candidate_runner.py](../../../scripts/p6_candidate_runner.py)（录制）、[p6_candidate_remote.py](../../../scripts/p6_candidate_remote.py)（持锁执行）及 [p6_candidate_cloud.py](../../../scripts/p6_candidate_cloud.py)（冻结绑定与传输）。当前包已有调用，不能再次启动；离线回放见[操作说明](../../../docs/p6-candidate-review-development.md#操作入口)。源图、掩码、传输包与日志留在仓库外。
