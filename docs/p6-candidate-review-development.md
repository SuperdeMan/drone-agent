# P6 逐候选语义复核开发协议

本轮处理 [v7 的具体风险](p6-road-contact-development.md)：整图疑似及一段全局不确定性说明无法区分每个框中的实体、车辆、阴影和道路外物体。新提示 `road-candidate-review-dev-v1` 只复核上一批六个视图中的 33 个固定提案，不开展新的候选发现，也不把 v7 的环境负记录改成通过。

## 输入与输出

图像沿用六组原始视图 / 中性道路支持域图，JPEG 字节不变。每条请求追加固定提案的 ID 与归一化框，不提供旧类型、旧整图结论、旧解释、v5 AI 实体轮廓或人工答案。旧框是未经确认的模型提案；原始 v7 批次环境核对失败。新批明确记录这些前提，不把提案来源包装成独立真值。

每个提案必须且只能回答一次。返回 `entity_kind`（实体 / 车辆 / 阴影 / 表面变化 / 未知）、`target_relation`（目标内 / 外 / 边缘不明 / 未知）、`separability`（可单独辨认 / 混杂 / 未知）、`refined_bbox`、实体依据、位置依据及不确定性。细化框须位于原提案内，不移植到附近另一个对象；看不清或混有多个对象时保留未知 / 混杂，不强行圈出实例。物理对象能单独辨认时必须给出有效细化框；车辆在目标内并不自动证明障碍。

输出采用独立的 `schema_version=0.1.0`，不接受旧版 `verdict/obstacles` 格式，也不回填旧回答缺少的字段。未知 / 混杂或车辆阻碍关系未确定时必须写明原因。图片不可用时所有提案保留未知；目标不见时不允许声明内 / 外关系。多边形与实际业务真值仍由人工复核另行建立。

确定性汇总只给每项一个**模型开发分组**：

| 条件 | 分组 |
|---|---|
| 结构不合格、缺项、来源不匹配，或图片 / 目标不可确认 | `needs_review`，保留全部原提案 |
| 性质 / 关系 / 分离度未知、边缘不明、混杂或写有相关不确定性 | `needs_review` |
| 可单独辨认的阴影或表面变化，其他声明无不确定性 | `non_entity` |
| 可单独辨认且明确位于目标外 | `outside_target` |
| 车辆位于目标内 | `needs_review`，不由静态图自动认定阻碍 |
| 可见实体、明确目标内、可单独辨认且有有效细化框 | `candidate` |

没有“整图正常”分支，`scene_verdict` 永远为空；即使全部给定提案被排除，也不能推出未给出的物体不存在。33 个框不保证穷尽、独立或各自只有一个对象，因此不计算精确率、召回率、误报率或独立缺陷数。分组不进入业务服务，不成为操作员登记或正式标签。

## 运行窗口与冻结

- 复用云端不可变运行时与既有模型代理；新录制器只调用模型，不访问业务或飞行 API，不部署服务、不修改密钥。
- 在任何模型调用前，以非阻塞方式持有现有 `~/drone-agent/stack.lock`，使用与部署 / 仿真任务相同的互斥机制。锁忙就返回 `blocked_lock_busy`，模型调用为 0，不等待、不停止其他任务、不绕过锁。
- 在持锁区间记录容器身份、镜像、启动时间与重启次数，运行后再核对；核对范围不因结果变化而缩小。锁并不替代这些检查，也不保证未遵守锁的外部操作不会发生。
- 每个视图最多一次 provider 调用、共六次；保持 MiniMax-M3.1-Flash-Preview、thinking medium、8192 token 上限、180 秒超时、至少 30 秒启动间隔。连续三次调用失败即停止，缺席提案保持待复核，不自动重问。
- 配置、提案清单、解析与分组代码及录制器在调用前冻结。若没有取得稳定窗口，本轮完成离线开发与锁检查，实际模型结果保持缺失。

本协议仅用于已曝光材料上的开发核对，与在用画像及数据库契约分开。P6 / WP-P6-01 仍未准出，人工边界确认、正式素材许可、地点与批次隔离及清障前后序列仍待完成。产物见 [v8 包](../eval/s2/p6_candidates_v8/README.md)。

## 本轮执行状态

冻结代码 `70fcc3303c6569e5a1d5d80b461f5ba1938b0ccc`。2026-10-15 03:22:28 UTC 的尝试 `p6-candidate-review-dev-20261015T032224Z` 遇到既有项目锁占用，返回 `blocked_lock_busy`：没有取得锁、没有启动诊断容器、provider 调用为 0。没有排队、停止其他任务或自动重试，也没有运行前后容器核对；本轮不存在新的模型结论。

[原始回执](../eval/s2/p6_candidates_v8/attempts/20261015T032224Z-execution.json)和[尝试摘要](../eval/s2/p6_candidates_v8/attempt-summary.json)均保留。离线汇总将 33 个提案全部标为未获回答的待复核项，观察值与人工结论为空，整图结论为空；这不表示模型对 33 项作出了“未知”判断。[兼容性检查](../eval/s2/p6_candidates_v8/compatibility-audit.json)确认六条 v7 回答均不符合新格式，未被补字段或重标为 v8 回答。

Ruff 与 51 项定向测试通过，包含新增 31 项和既有 20 项。忙锁路径不进入预检 / Docker、持锁覆盖前后检查、容器变化仍失败、未知保持待复核、提案缺失或重复不能形成结论、排除全部提案不能形成整图正常，均有验证。冻结代码 Windows 全量 **1715 通过、1 项既有 Unix socket 跳过、0 失败 / 错误**，1092.553 秒；源码在测试期间未变。准确摘要见[检查回执](verification/p6-candidate-review-2026-10-15.json)。这些是工具检查，逐候选提示尚未完成实调验证。

另在云端独立测试文件上执行了真实 Linux flock 检查：[首次取得、竞争立即拒绝、释放后重取](../eval/s2/p6_candidates_v8/lock-selfcheck.json)均通过。该检查没有取得真实项目运行锁，没有启动容器或调用 provider；它只验证锁实现，不证明模型运行窗口已经可用。收尾时的项目锁可用性检查仍为忙。

## 操作入口

代码位于隔离工作树 `D:/drone-agent-data/p6-road-views-20261014/repo`。本机包位于 `D:/drone-agent-data/p6-candidate-review-20261015/diagnostic/`。以下命令均从该工作树运行，图像与运行输出不进入 Git。

```powershell
uv run --frozen python scripts/p6_candidate_review.py prepare --previous-batch eval/s2/p6_candidates_v7 --previous-inputs D:/drone-agent-data/p6-road-contact-20261015/diagnostic/inputs --config eval/s2/p6_candidates_v8/config.json --output <新的外部目录>
uv run --frozen python scripts/p6_candidate_cloud.py --package D:/drone-agent-data/p6-candidate-review-20261015/diagnostic --freeze 70fcc3303c6569e5a1d5d80b461f5ba1938b0ccc
uv run --frozen python scripts/p6_candidate_review.py replay --package <冻结包目录> --cloud-output <具体尝试下的cloud/output> --output <新的汇总JSON>
```

再次发起只允许在先前尝试已确证 `blocked_lock_busy`、0 调用且容器未启动时进行，每次生成新的尝试目录；程序不会循环重试。先前有调用、失败状态不明确或回执未拉回时，先核对原记录，不重新调用。重放命令需实际录制，本轮没有 `cloud/output` 可供重放。只改文档的后代提交可继续使用冻结版本，但脚本与输入字节必须和冻结提交一致。
