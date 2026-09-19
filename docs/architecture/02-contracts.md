# 契约规范

> 契约是本项目扩展性的来源。代码实现见 `src/drone_agent/contracts/`（Pydantic v2），契约测试见 `tests/contracts/`。本文定义语义；字段以代码为准，两者冲突时先改本文再改代码。

## 0. 通用规则

- 所有契约对象带 `schema_version`（语义化版本）。跨进程 / 跨机器人传输的对象只允许**向后兼容**的增字段；破坏性变更升主版本并保留一版兼容期。
- 所有带空间信息的对象必须带 `frame_id` 与 `map_version`；所有带时间信息的对象必须带 `timestamp` 与（若适用）`valid_until`。没有坐标系和有效期的位置不是事实。
- 身份字段：`robot_id`、`mission_id`、`step_id`、`command_id` 全局唯一（UUID 或带命名空间的字符串）。
- 通用观测只保留公共头（`robot_id / timestamp / frame_id / schema_version / source / valid_until`），本体状态按类型区分：`AerialState`、`GroundMobileState`、`ManipulatorState`。**不把无人机状态塞进 `extras`。**

## 1. CapabilityDescriptor（静态能力）与 RobotStatus（动态状态）

解决的问题：防止上层假设所有无人机 / 机器人能力相同；厂商差异通过能力协商暴露。

`CapabilityDescriptor`：

| 字段 | 说明 |
|---|---|
| `robot_id`, `embodiment` | `aerial_multirotor / aerial_vtol / aerial_fixed_wing / ground_wheeled / ground_legged / manipulator` |
| `interface_version` | 适配器契约版本 |
| `control_modes` | 集合：`mission_upload / offboard_position / offboard_velocity / offboard_trajectory / external_mode(px4_ros2) / none`；缺席即不支持 |
| `skills` | `[{skill_id, version}]` 本机真实可用技能 |
| `sensors`, `payloads` | 类型、坐标系、标称精度 |
| `limits` | 最大速度、最大高度、续航、抗风、载荷质量、最小转弯半径（固定翼）、是否可悬停 |
| `recovery_behaviors` | 平台支持的基线行为集合（`hold / loiter / rtl / land_here / land_at(site)`） |
| `vendor` | 厂商与型号；`vendor_constraints`（如「仅航线上传」「无 Offboard」） |

`RobotStatus`（1 Hz 级）：能源（电量、可用能量估计）、定位健康（fix 类型、协方差、EKF 状态）、通信质量、当前控制模式与飞行阶段、当前租约、当前技能实例、资源占用。

规则：Coordinator 只能把任务分配给 `skills ⊇ 所需技能 ∧ control_modes ⊇ 所需模式 ∧ limits 满足` 的机器人；缺能力时调整计划或拒绝，不做替代假接口。

## 2. MissionSpec（任务规格）与 MissionPackage（已编译任务包）

解决的问题：把自然语言变成可验证、可执行的任务；LLM 的输出永远停在这一层。

`MissionSpec`（Planner 输出，人可读，可审批）：

| 字段 | 说明 |
|---|---|
| `mission_id`, `mission_version` | 任何修改都产生新版本 |
| `goal` | 自然语言目标 + `goal_type`（inspect / survey / deliver / search / recheck …） |
| `targets` | 对象引用（资产 ID、坐标 + 坐标系 + 不确定性） |
| `spatial_scope` | **引用**已批准体积（`approved_volume_id`）+ 几何 + `frame_id` + `map_version`；Planner 不能新建体积 |
| `temporal_window` | 最早开始、最晚结束、时区 |
| `tasks` | 任务节点列表：`{task_id, skill_id, params, depends_on[], completion_evidence[]}` |
| `energy_budget` | 允许消耗与必须保留的返航余量 |
| `recovery_policy_ref` | 平台恢复策略图 ID（场景配置，不由 LLM 生成） |
| `collaboration_rules` | 交接触发条件、接收方能力要求、超时 |
| `provenance` | 模型 ID、提示版本、输入哈希、生成时间 |

`MissionPackage`（Compiler 输出，机器可执行）：展开后的 DAG（每节点绑定技能版本、参数、资源预约、超时、证据要求、失败恢复提示）、`approval: ApprovalRecord`、`package_hash`。机载 executive 只接受带有效 `ApprovalRecord` 且 `package_hash` 匹配的任务包。

`ApprovalRecord`：审批人、审批时间、`mission_version`、`package_hash`、有效期、审批范围（允许的机器人集合）。

## 3. SkillManifest v2（长时间物理技能契约）

解决的问题：技能是持续数秒到数分钟的物理过程，不是一次函数调用。

| 字段 | 说明 |
|---|---|
| `skill_id`, `version`, `embodiments` | 适用本体 |
| `params_schema` | JSON Schema；含单位与范围 |
| `preconditions` | 执行前必须成立的谓词（定位健康 ≥ x、电量 ≥ y、处于某飞行阶段、持有某资源） |
| `invariants` | **持续约束**：执行期间必须一直成立（在围栏内、观测新鲜、租约有效） |
| `resources` | `[{resource_id, mode: exclusive/shared}]`，如 `uav_01.motion: exclusive`、`uav_01.camera: shared(mode=still)` |
| `progress_schema` | 进度报告结构 |
| `cancel` | `cancel_safe_states[]`（可立即取消的内部状态）、`cancel_procedure`（取消时的收尾动作） |
| `pause` | 是否可暂停、安全等待条件、最长等待时间 |
| `completion_evidence` | 完成所需证据类型与判据（目标 ID 匹配、影像质量阈值引用、观测位姿有效、时间有效） |
| `failure_modes` | 已知失败模式与恢复提示 |
| `timeouts`, `estimates` | 超时；预计时长与能耗 |
| `implementation` | `deterministic / learned(shadow|limited|full)`；学习型实现必须声明影子运行状态 |

技能实例生命周期：

```text
accepted → preparing → running → verifying → completed
          ↘ pause_requested → paused → running
          ↘ cancel_requested → recovering → cancelled
          ↘ blocked | failed | outcome_unknown
```

「用户取消了任务」与「飞行器已进入安全状态」是两个不同事件（`cancel_requested` vs `recovered_to(<state>)`）。

## 4. WorldFact / Observation

解决的问题：避免把陈旧、预测或不确定的信息当事实。

| 字段 | 说明 |
|---|---|
| `fact_id`, `subject`, `predicate`, `value` | 三元组 |
| `frame_id`, `map_version`, `position`, `covariance` | 空间与不确定性 |
| `timestamp`, `valid_until` | 时间与有效期 |
| `source` | `sensor / model / human / sim_truth`；模型来源带模型版本 |
| `confidence` | [0,1] |
| `world_kind` | `truth / belief / predicted`；`truth` 只允许出现在裁判进程；`predicted` 不能作为 `preconditions` 的依据 |
| `evidence_refs` | 关联证据 |

## 5. ExecutionEvent / Evidence 与三元状态

解决的问题：防止「调用成功 = 任务成功」。

三元状态：

| 维度 | 取值 | 谁产生 |
|---|---|---|
| `execution_status` | `pending / accepted / running / succeeded / failed / cancelled / timeout / unknown` | executive |
| `effect_verdict` | `verified / unverified / refuted / unknown` | Evidence Verifier（确定性检查为主） |
| `safety_verdict` | `proceed / hold / recover / abort` | guardian |

放行规则（DAG 边）：后继节点运行 ⇔ 前驱 `execution_status = succeeded` ∧ `effect_verdict = verified`（除非该边在 `MissionPackage` 中显式标注 `allow_unverified`，且这类标注在准入时被人审批）∧ 当前 `safety_verdict = proceed`。任何 `unknown` 都阻断依赖链，并进入对账流程。

`ExecutionEvent` 类型（最小集）：`command_accepted / command_rejected / skill_started / skill_progress / skill_paused / skill_cancel_requested / skill_completed / skill_failed / effect_verified / effect_refuted / effect_unknown / safety_intervention / lease_issued / lease_expired / lease_revoked / recovered_to / handoff_offered / handoff_accepted / handoff_completed / handoff_timeout`。

`Evidence`：类型（影像 / 视频片段 / 遥测切片 / 点云片段）、媒体引用与哈希、采集位姿（+ 协方差）、时间窗、关联对象 ID、质量指标、生成它的技能实例。

## 6. Authority / TaskLease 与幂等键

解决的问题：重复执行、旧指令重放、多控制源争用。

`TaskLease`：

| 字段 | 说明 |
|---|---|
| `robot_id`, `mission_id`, `mission_version` | 绑定对象 |
| `lease_epoch` | 控制权代次；每次重新授予 +1，旧代次命令一律拒绝 |
| `holder` | 控制源 ID（executive 实例 / 人工接管 / 协调器） |
| `issued_at`, `expires_at` | 租约必须续期；过期后 guardian 进入恢复策略，**但过期只是软件状态，不代表机器人已物理停止** |
| `resources` | 租约覆盖的资源 |
| `command_seq` | 单调递增序号（每 epoch 从 0 起） |

幂等键 = `(mission_id, mission_version, step_id, command_id, robot_id, lease_epoch)`。网络超时后的正确动作是**状态对账**（查询该幂等键的执行状态），不是自动重发。

控制权仲裁优先级（高 → 低）：飞控原生失效保护 > RC / GCS 人工接管 > guardian 恢复策略 > 持有效租约的 executive > 其他。

## 7. RecoveryPolicy（平台专用恢复策略图）

场景配置文件，按平台与本体定义；不由 LLM 生成。节点是已验证基线行为（`hold / loiter / rtl / land_here / land_at(site) / handover_to_fc_failsafe`），边由「触发条件 × 当前上下文（飞行阶段、定位健康、能源、通信、空间位置）」决定。详见 `03-safety.md`。

## 8. 契约测试（M0 交付）

| 测试 | 断言 |
|---|---|
| `test_unknown_is_never_success` | 任何 `effect_verdict ∈ {unknown, unverified}` 的前驱都不能放行后继（无 `allow_unverified` 时） |
| `test_stale_epoch_rejected` | `lease_epoch < current` 的命令被 Control Egress 拒绝并记录 |
| `test_model_cannot_reach_egress` | `MissionSpec` 中的任何字段都无法映射为直接控制命令；Planner 工具清单不含写控制工具 |
| `test_spatial_scope_must_reference_approved_volume` | 未引用已批准体积的 `MissionSpec` 不能通过准入 |
| `test_capability_negotiation` | 需要 `offboard_velocity` 的任务不能分配给只声明 `mission_upload` 的机器人 |
| `test_skill_resources_exclusive` | 同一机器人上两个声明 `motion: exclusive` 的技能不能同时 `running` |
| `test_predicted_world_not_precondition` | `world_kind = predicted` 的事实不能满足 `preconditions` |
| `test_schema_backward_compatible` | 新版本对象能被旧版本解析（增字段） |
