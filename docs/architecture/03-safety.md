# 安全体系

> 原则：最终安全判断与控制权不交给任何 LLM。安全体系按运行时保障（Runtime Assurance, RTA）/ Simplex 架构组织：高性能路径（技能 + 局部规划 / 学习策略）、已验证基线（恢复策略图 + 飞控原生模式）、决策模块（guardian 内的安全监督器）。

## 1. 四道约束

| 道 | 位置 | 内容 | 时机 |
|---|---|---|---|
| ① 任务准入 | L2 Admission（off-board）+ 机载 executive 复核 | 设备、空间、时间、能力、参数、资源、能源、法规；审批绑定版本 | 任务开始前 |
| ② 运行期约束 | L5 guardian 安全监督器 | 定位健康、能源余量、空间包络与围栏、传感器有效、轨迹有效、控制权与序号、新鲜度、活性 | 每条控制写入前 + 周期性（≥ 10 Hz） |
| ③ 本地恢复 | L5 guardian 恢复策略图 | 上层不可用时按已验证策略处理；不等待模型 | 约束违反 / 心跳丢失 / 租约过期 |
| ④ 飞控与人工接管 | L6 + 飞控 + RC/GCS | 飞控原生失效保护（链路丢失、低电、围栏、EKF 失效）；人工接管永远可用 | 任何时候 |

约束②③④中没有 LLM。模型可以在 L1 做风险解释与建议，输出只影响任务规划，不影响 guardian。

## 2. 「停止」不是一个 API：用户语义 → 平台动作

| 用户 / 任务语义 | 系统必须决定 | 默认映射（多旋翼；固定翼 / VTOL 另配） |
|---|---|---|
| 暂停任务 | 是否具备安全等待条件、等多久、之后走哪条恢复路径 | 空中：`hold`（位置保持）≤ T_pause，超时 → `rtl`；地面：`hold` |
| 取消任务 | 如何停止业务动作并进入已验证的恢复流程 | 当前技能 `cancel_procedure` → `rtl` 或 `land_at(site)` |
| 失去定位 | 哪些模式仍可用 | GNSS 丢失但视觉定位可用：`hold` 并降级速度；全部丢失：交给飞控失效保护（PX4 定位失效 → 下降 / 着陆） |
| 能源不足 | 还能到达哪些安全位置，余量是否够 | 按可达性表选 `rtl` / `land_at(nearest_site)` / `land_here` |
| 失去通信（上行） | 继续、等待或恢复的条件 | 已授权任务包允许继续 ∧ 本机健康 → 继续；否则 `hold` → `rtl` |
| 紧急停止（人工） | — | RC / GCS 接管；空中断电（kill）只能由飞控 / 人工触发，**executive 与 guardian 不提供 kill 接口** |

「异常就悬停」「异常就返航」都不是默认；每条边都由上下文决定。

## 3. 恢复策略图（RecoveryPolicy）

结构：

```text
节点 = 已验证基线行为：hold | loiter | rtl | land_here | land_at(site) | handover_to_fc_failsafe
边   = 触发条件 × 上下文守卫 → 目标节点
上下文 = {flight_phase, localization_health, energy_state, comms_state, spatial_zone, wind}
```

示例（多旋翼，园区巡检）：

| 触发 | 守卫 | 动作 |
|---|---|---|
| executive 心跳丢失 > 2 s | 空中 ∧ 定位健康 | `hold` 10 s → `rtl` |
| executive 心跳丢失 > 2 s | 起飞阶段（< 3 m） | `land_here` |
| 观测过期（> 500 ms 无有效位姿） | Offboard 中 | 退出 Offboard → `hold`（飞控内部模式） |
| 轨迹片段过期（> 200 ms） | Offboard 中 | 退出 Offboard → `hold` |
| 租约过期 | 任意 | `hold` → 对账 → 未续期 30 s → `rtl` |
| 电量 < 返航余量 + 裕度 | 任意 | `rtl`；若 `rtl` 不可达 → `land_at(nearest)` |
| 围栏预测越界（前瞻 2 s） | 任意 | 拒绝当前意图；`hold` |
| 飞控进入失效保护 | 任意 | 记录 `safety_intervention`；guardian 停止写入；不尝试覆盖 |

每条边用故障注入逐个验证（`tests/fault_injection/`），未验证的边不能进入生产配置。切回高性能路径（`hold → running`）也有准入条件：观测新鲜、租约有效、executive 心跳恢复、且当前技能实例处于 `cancel_safe_state` 或明确的 `resume` 状态——恢复是可逆的，但不是自动的。

M0 的 `configs/recovery_policies/` 文件均为草案：写了 `fi.*` 名称不代表执行过该场景。`configs/scenarios/m0_fault_matrix.yaml` 记录前置上下文、注入点、期望行为、所需证据和适用阶段；只有绑定当前边内容的成功验证记录才能通过生产准入。矩阵静态覆盖、策略选择单测和 SITL 故障验证必须分别报告。Offboard 场景在 M0/M1 只检查拒绝未声明能力与策略分支，物理控制路径在启用后另验。

## 4. 活性、心跳与新鲜度是三件事

已知陷阱：MAVSDK Offboard 插件以 20 Hz 自动重发最近一次设定值；PX4 Offboard 存活只要求 ≥ 2 Hz 的信号。因此业务控制卡住时，飞控看到的链路可能仍然「活着」。

guardian 独立检查四项，任一失效即按恢复策略处理：

| 检查 | 含义 | 来源 |
|---|---|---|
| 观测新鲜 | 最新位姿 / 定位健康的 `timestamp` 在阈值内 | 飞控遥测 + autonomy |
| 轨迹新鲜 | 最新 `ControlIntent` 的 `valid_until` 未过 | executive |
| 任务活性 | executive 的业务心跳（含当前技能实例状态）在阈值内 | executive |
| 租约有效 | `TaskLease` 未过期、`lease_epoch` 匹配 | executive / Coordinator |

过期的意图不重发；guardian 在切换到基线行为前先停止向飞控发送 Offboard 设定值（让飞控自己退出 Offboard），避免用旧设定值「维持」一个已经无效的控制回路。

## 5. 并发与幂等属于物理安全

- 每个技能在 `SkillManifest.resources` 声明资源；executive 的 DAG 并行只在资源不冲突时发生。`uav_01.motion` 永远独占。
- 命令幂等键 = `(mission_id, mission_version, step_id, command_id, robot_id, lease_epoch)`；guardian 对重复键返回既有结果，不重复执行。
- 网络超时后：先查询该幂等键的状态（对账），只有状态为「未收到」才重发；状态「未知」时进入 `outcome_unknown` 并阻断依赖链。
- 旧 `lease_epoch` 的命令一律拒绝并记录 `command_rejected(reason=stale_epoch)`。
- 撤销不能清除代次历史；同代次续期不改变控制身份与资源，且不清除已处理命令。闸门对象寿命之外的代次持久化、客户端身份认证及对账结果账本属于 M1，M0 纯逻辑闸门不构成完整运行时。

## 6. 三元状态与放行

| 场景 | execution_status | effect_verdict | safety_verdict | 后继是否运行 |
|---|---|---|---|---|
| 起飞命令超时，未重发 | `timeout` | `unknown` | `hold` | 否；对账 |
| 起飞命令被接受，高度未达标 | `succeeded`（命令层面） | `refuted` | `proceed` | 否；技能失败处理 |
| 拍照成功但影像模糊 | `succeeded` | `unverified` | `proceed` | 否；补拍（技能内部） |
| 到达航点并验证 | `succeeded` | `verified` | `proceed` | 是 |

`succeeded` 描述的是命令与技能过程，不是物理效果；报告中「已完成」列只接受 `succeeded ∧ verified`。

## 7. 安全边界：谁能做什么

| 角色 | 能 | 不能 |
|---|---|---|
| LLM Planner | 生成 / 修改 `MissionSpec` 草案；解释异常；建议重规划 | 扩大批准空间；生成代码；触达 L5/L6；决定恢复动作 |
| VLM Verifier / 事件检测 | 输出带置信度的业务判断（疑似异常、进度） | 输出 `safety_verdict`；触发恢复 |
| executive | 调度技能、提交意图、取消 / 暂停、记录证据 | 写飞控；禁用失效保护；kill |
| guardian | 过滤意图、切基线行为、拒绝命令 | 修改任务内容；禁用飞控失效保护；使用 PX4 失效保护延期 |
| 适配器 | 协议翻译 | 自行决策 |
| 飞控 / 人工 | 一切 | — |

## 8. 机载安全进程 ≠ 独立故障隔离

M1 mission-upload 运行时的具体执行边界见 [实施计划](../m1-implementation.md)。恢复请求的优先级不得被后到的业务请求降低；暂停或恢复必须有新鲜物理观测，unknown 回执必须阻断后继。飞控模式未经 guardian 请求发生变化时撤销业务控制权，不自动重夺。guardian 重启保留命令与最高代次历史，禁止自动重放尚无确定回执的控制命令。

围栏前瞻采用当前速度的有限时域投影；仅当当前技能为 `land`、处于原生 LAND 模式且在已验证/预约降落点上方时，下降投影在该点地面高度终止，避免把正常着陆误判为穿过地面。当前实际位置、水平预测与上界仍完整检查；其他地点或模式不享有该路径约束。裁判另外检查接地下降速度，不能用此模型掩盖坠落。

`guardian` 与 `executive` 分进程只隔离软件故障；供电、计算与通信仍是共因。M4 真机验证清单必须包含：伴飞计算机断电、串口拔出、RC 接管、飞控失效保护触发四类共因测试，验证飞控原生路径不依赖任何机载软件。
