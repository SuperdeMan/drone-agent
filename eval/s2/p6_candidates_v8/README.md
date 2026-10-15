# P6 逐候选语义复核开发包

新任务固定 v7 的 33 个未经确认的框，逐项区分实体性质、目标内外关系、是否混杂及不确定性。它不发现新对象，不把旧回答补成新协议，不计算识别准确率或召回率，不生成整图正常。见[协议](../../../docs/p6-candidate-review-development.md)。

源批次的环境核对失败仍保留；本批必须在模型调用前非阻塞取得项目已有 `stack.lock`，拿不到锁就以零调用退出。源码、提示、输入及后续回执在此独立归档，v1–v7 不改写。独立人工业务真值仍未建立，P6 未准出。

## 当前状态：锁忙，未调用模型

- 冻结提交：`70fcc3303c6569e5a1d5d80b461f5ba1938b0ccc`。[配置](config.json)、[协议](protocol.json)、[计划](plan.json)及[输入提案清单](input-manifest.json)在尝试前提交。
- 尝试 `p6-candidate-review-dev-20261015T032224Z`：[启动记录](attempts/20261015T032224Z-launch.json)、[原始执行回执](attempts/20261015T032224Z-execution.json)。返回 `blocked_lock_busy`，模型调用 0、诊断容器未启动，未进行环境前后核对。
- [待复核清单](pending-review.json)保留全部 33 项；它是未调用后的缺失状态汇总，不是模型输出，更不是人工标注。[兼容性检查](compatibility-audit.json)保存六条旧格式回答被拒绝的结果。
- [尝试摘要](attempt-summary.json)绑定上述文件摘要；[工具检查](../../../docs/verification/p6-candidate-review-2026-10-15.json)与实际模型效果分别记录。新提示未采用，在用画像未改变。
- 冻结代码 `70fcc33`：Ruff、51 项定向、Windows 全量 1715 通过 / 1 项既有跳过；[Linux 独立锁检查](lock-selfcheck.json)验证三项锁行为，其[驱动源码](tool-source/lock-selfcheck.py.txt)按摘要保留。全部检查均未产生新的业务真值。

本机包：`D:/drone-agent-data/p6-candidate-review-20261015/diagnostic/`。源码入口为 [p6_candidate_review.py](../../../scripts/p6_candidate_review.py)（协议 / 汇总）、[p6_candidate_runner.py](../../../scripts/p6_candidate_runner.py)（录制）、[p6_candidate_remote.py](../../../scripts/p6_candidate_remote.py)（持锁执行）及 [p6_candidate_cloud.py](../../../scripts/p6_candidate_cloud.py)（冻结绑定与传输）。取得空闲窗口后的用法见[操作说明](../../../docs/p6-candidate-review-development.md#操作入口)。
