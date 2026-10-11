# 精细空域预约（WP-P6-06，D084）验收记录

**结论**：候选 `6544a9a`，门禁 八项全部通过（[门禁结果](verification/fine-reservations-2026-10-12/release.json)）。

**按预登记规则，走廊不采用**：
- 长航线档上，走廊使只因空域等待的时长从 261.3 s 降到 52.2 s（−80%），排空时长 202.1 → 187.5 s（−7%）。
- 规则要求降幅超过两次外包矩形运行之差（噪声）的 3 倍，即 211.1 s；实际降了 209.1 s，差 2 s。

**安全前提成立**：两种模型在 S0 矩阵、规模阶梯、长航线档与 P3 S1 中都没有违例。S1 中每个空中的 Gazebo 真值样本都落在本活动当时所持的单元内，到未持有单元的最小余量约 3.3 m。

范围是软件 / SITL：
- 走廊是调度目录 v2 的可选模型，现有目录（P3 证据目录、任务台 `p5_desk_v1`）都不切换；
- 不改表；
- 时间维度与高度分层没有做（理由见[设计](fine-reservations-design.md) §7）。

## 版本与门禁

| 项 | 值 |
|---|---|
| 设计 | [精细空域预约设计](fine-reservations-design.md)，2026-10-11 用户批准建议范围（D084） |
| 候选 | `6544a9a`：走廊模型、真实轨迹核对、利用率指标、长航线阶梯、预登记规则 `eval/airspace_choice.py`、S1 选项与门禁。独立审查无阻断项，其中几处应修已在候选中改正 |
| 部署 | `20261011T203555Z-2f24bc29`（`6544a9a`），云端检查 1530 项全部通过，主机隔离成立 |
| 第一轮 | 预登记 `30b55f4`，**作废、不计入**，原因见下文 |
| 第二轮 | 预登记 `69228e2`（[attempt-2/](verification/fine-reservations-2026-10-12/attempt-2/)），噪声：等待 70.4 s、排空 0.9 s |
| 门禁 | `scripts/verify_fine_reservations.py`：八项全部通过（范围、云端检查、预登记、规则决定、S1、回归、任务台、历史记录）；当场重跑的 P1 S0（42/42）与两种模型的 P3 S0 矩阵（各 45/45）在高 QoS 下进行 |

## 第一轮作废

- **现象**：第一轮比较在当地深夜运行。现有 P3 阶梯的 100 节点档在两种模型下都超时。
- **对照**：D084 之前的 `73fdb4b`，以及 10-10 在这台开发机上通过过的 `cbc8a41`，在同样条件下都以同样方式失败（约 100 个任务完成、约 30 个结果不确定）。
- **原因**：开发机无人操作时，Windows 对后台 Python 进程启用降速节能（EcoQoS）。用 `cbc8a41` 的 `decide()` 对它 10-10 记录的同一批快照重算，各种条件下的耗时中位数：

  | 条件 | 耗时中位数 |
  |---|---|
  | 系统默认 | 44–59 ms |
  | 本进程声明不降速（高 QoS） | 26 ms |
  | 10-10 记录 | 20 ms |

  在高 QoS 下，`cbc8a41` 的 100 节点档重新通过：完成 174 个，结果不确定 0 个，排空 527.5 s。
- **处理**：
  - 第一轮全部结果与三份对照归档为不计入（[attempt-1-void/](verification/fine-reservations-2026-10-12/attempt-1-void/)）；
  - 第二轮沿用同一候选与同一规则，每个本机 S0 进程都以高 QoS 运行，并重新测量噪声、冻结阈值；
  - 第一轮长航线档的结果在第二轮预登记之前已被看到，已披露。
- **高 QoS 的做法**：用 `PYTHONPATH` 加载 `scripts/high_qos/sitecustomize.py`（准出后入库；本次运行用的是功能相同的本地副本，系统调用与掩码一致、注释不同，摘要见第二轮预登记），它在本进程调用 `SetProcessInformation(ProcessPowerThrottling)`，子进程会各自执行一次。只作用于这些进程，不改系统设置。门禁当场重跑的矩阵也在高 QoS 下运行。

## 第二轮比较（预登记规则）

各档对种子求和：长航线档为种子 7 / 19 / 41，现有阶梯为种子 7。

| 世界与指标 | 外包矩形 | 走廊 | 判据 |
|---|---|---|---|
| 长航线档：只因空域等待 | 261.3 s | 52.2 s | 降幅至少 30%（78.4 s），**且超过 3 倍噪声（211.1 s）**：降 209.1 s，**不通过** |
| 长航线档：排空 | 202.1 s | 187.5 s | 变差不超过 5%：通过 |
| 长航线档：每次飞行持有的单元·秒（三个种子的均值） | 约 1942 | 约 548 | 只报告 |
| 现有阶梯排空（10 / 30 / 100 节点） | 33.0 / 102.1 / 512.5 s | 34.5 / 96.4 / 534.2 s | 变差不超过 10%：通过 |
| P3 S0 矩阵（15 例 × 3 种子） | 45/45 | 45/45 | 全部通过 |
| 安全计数与真实轨迹违例 | 0 | 0 | 全为 0：通过 |

规则结论 `adopt: false`：输入身份、安全、长航线排空与现有阶梯四项通过，长航线等待一项不通过。

- **噪声**：两次相同的外包矩形运行，等待合计分别为 175.3 s 与 245.7 s；按种子求和的等待本身噪声很大。排空的噪声只有 0.9 s。
- **走廊在现有 P3 世界里**：与离线估算一致，不减少冲突对。10 / 30 / 100 节点档的等待与排空，两种模型相近。

## S1：Gazebo 真值上的真实轨迹核对

两架 PX4 SITL 同在一个 Gazebo 世界，P3 S1 的三个场景共 5 次运行，候选 `6544a9a`，部署 `2f24bc29`：

| 模型 | 通过 | 回放一致 | 安全计数 | 空中真值样本 / 用例 | 轨迹违例 | 到未持有单元的最小余量 |
|---|---|---|---|---|---|---|
| 外包矩形 | 5/5 | 5/5 | 0 | 551–1081 | 0 | 3.28–3.70 m |
| 走廊 | 5/5 | 5/5 | 0 | 551–1079 | 0 | 3.32–3.72 m |

P3 站点对上走廊只比外包矩形少角上的单元，所以飞行中的余量与外包矩形相近。回执在 [s1/](verification/fine-reservations-2026-10-12/s1/)。

## 任务台

- **激活**：任务台与控制台入口都已激活到候选，任务台继续使用 v1 调度目录，行为不变。
- **入口核对**：页面、脚本与健康检查正常，伪造的身份头被忽略。
- **回执**：在 [desk/](verification/fine-reservations-2026-10-12/desk/)，Tailnet 主机名已替换为 `<tailnet-host>`。

## 采用与否意味着什么

- **代码保留**：走廊模型保留在代码中，调度目录 v2 可以声明它，但没有任何目录使用，也不推荐新目录使用。
- **新增并已验证的部分**：
  - 真实轨迹核对（两种模型都适用）；
  - 利用率指标；
  - 长航线阶梯与预登记规则。
- **再评估的条件**：若出现长航线的现场站点需要走廊，应先预登记一个统计功效更高的评测（例如更多种子），再决定采用，并披露这是新一轮。

## 接手项

- **门禁的路径处理**：`--evidence` 传相对路径时，`relative_to` 报错；本次传绝对路径运行。下一次改门禁时修正。
- **本机 S0 的时间敏感性**：阶梯与矩阵对操作系统的降速节能敏感。本机运行应以高 QoS 进行，并先测一次 `decide()` 速度；可以考虑让 S0 汇总记录运行时的速度基准。
- **云主机 SSH**：握手阶段被随机丢弃。sshd 的 MaxStartups 被公网扫描占满，LoginGraceTime 420 s。本次用本地重试包装处理；是否调整 sshd 属于共享主机的系统配置，由用户决定。
- **时间维度与高度分层**：维持设计 §7 的判断与重估触发器。

## 复现

```powershell
uv run python experiments/fine_reservations/estimate.py
uv run pytest tests/fleet/test_corridors.py tests/eval/test_airspace_choice.py tests/eval/test_judge_p3.py tests/test_fine_reservations_gate.py
# 本机 S0 一律以高 QoS 运行：$env:PYTHONPATH = "<repo>\scripts\high_qos"（sitecustomize.py，只作用于本进程）
uv run python -m drone_agent.eval.p3_world --part ladder --suite configs/scenarios/p6_airspace_suite.yaml --footprint box-v1|corridor-v1 --sha <sha> --output <dir>
uv run python -m drone_agent.eval.p3_world --part ladder|s0 --footprint box-v1|corridor-v1 --sha <sha> --output <dir>
uv run python -m drone_agent.eval.airspace_choice decide --box-long … --corridor-s1 … --frozen <preregistration.json>
uv run python scripts/dev_stack.py p3 --scenario all --footprint box-v1|corridor-v1
uv run python scripts/verify_fine_reservations.py --sha <full sha> --deployment … --s1-box … --s1-corridor … --desk … --desk-http … --desk-spoof … --evidence <absolute attempt-2 path> --output docs/verification/fine-reservations-2026-10-12/release.json
```
