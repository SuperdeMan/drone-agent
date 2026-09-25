# 契约规范

[返回架构总览](00-overview.md) · [安全体系](03-safety.md) · [Wire 协议](../../proto/README.md)

> 契约是本项目扩展性的来源。代码实现见 [contracts](../../src/drone_agent/contracts/)（Pydantic v2），契约测试见 [tests/contracts](../../tests/contracts/)。本文定义语义；字段以代码为准，两者冲突时先改本文再改代码。

阅读边界：本页同时定义当前运行时与后续扩展所需的领域对象；存在消息或字段不代表对应执行能力已实现。M2 的接收行为见第 10 节，M3-SITL 局部自主与外部模式见第 11 节；多机器人交接待 P3/X1。第 12 节的运营对象尚未实现。

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

解决的问题：把自然语言变成可验证、可执行的任务；LLM 只产出类型化草案。运营流程的 WorkflowSpec 另见第 12 节，不能获得控制权限。

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

M0 接收边界补充：任务包必须保留 `spatial_scope`、`temporal_window`、`energy_budget`，三项都进入审批哈希；旧草案可解析，但缺任一边界不得授权执行。任务包自身的 `package_hash`、审批中的哈希与重新计算值三者必须相同；审批尚未生效或当前不在任务时间窗也必须拒绝。时间窗与审批时间必须带时区。空机器人列表表示没有授权对象，不能当作通配符。编译后的 DAG 仍需检查重复节点、未知依赖、环和 `allow_unverified_from` 的直接依赖关系。任务参数中的控制级键按任意嵌套深度检查；字典或列表包装不能绕过边界。

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
| `implementation` | `deterministic / learned(shadow / limited / full)`；学习型实现必须声明影子运行状态 |

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
| `command_seq`（命令信封字段） | 单调递增序号（每 epoch 从 0 起）；不复制进租约，guardian 按代次维护接收水位 |

幂等键 = `(mission_id, mission_version, step_id, command_id, robot_id, lease_epoch)`。网络超时后的正确动作是**状态对账**（查询该幂等键的执行状态），不是自动重发。

控制权仲裁优先级（高 → 低）：飞控原生失效保护 > RC / GCS 人工接管 > guardian 恢复策略 > 持有效租约的 executive > 其他。

撤销租约后保留最高已见代次；重新授予必须使用更高代次。同代次只允许同一机器人、任务版本、holder 与资源集合续期，不能换所有者或重新开始去重窗口。闸门持有租约快照，外部修改对象不能静默改变权限。幂等键编码必须无歧义，身份字段中的冒号不能产生碰撞；意图有效期为 `[issued_at, valid_until)`。

## 7. RecoveryPolicy（平台专用恢复策略图）

场景配置文件，按平台与本体定义；不由 LLM 生成。节点是已验证基线行为（`hold / loiter / rtl / land_here / land_at(site) / handover_to_fc_failsafe`），边由「触发条件 × 当前上下文（飞行阶段、定位健康、能源、通信、空间位置）」决定。详见 `03-safety.md`。

`fault_injection_scenario` 仅表示计划中的场景引用。验证记录必须另外绑定场景、该边内容哈希、软件版本、验证时间和证据引用；没有记录或内容变化后的边均为未验证。M0 交付的是草案与注入矩阵，不宣称已通过 SITL 故障验证。

## 9. M0 wire 草案与 M1 冻结边界

- `proto/drone/contracts/v1/contracts.proto` 承载六类契约的共享消息；`proto/drone/control/v1/control.proto` 定义 guardian 本地控制、心跳及命令对账接口；`proto/drone/fleet/v1/fleet.proto` 仅定义任务包、事件、证据、事实、状态与交接消息，不提供控制接口。
- `scripts/generate_contract_fields.py` 从 Pydantic 导出字段清单到 `proto/contract-fields.json`；字段号在 proto 中显式维护，不能因 Python 字段重新排序而重编号。`scripts/generate_proto.py` 编译到被忽略的 `gen/`，契约测试检查字段覆盖与 wire 的缺省语义。
- proto3 标量显式 `optional`，枚举零值统一 `UNSPECIFIED`，不能默认成功、verified 或 proceed。缺省对象必须经过 Pydantic 与业务接收检查，不能把 protobuf 能解析当作授权。
- 自由 JSON 字段以 UTF-8 JSON `bytes` 传输，保留整数精度；解码后重跑领域模型校验。时间为 `google.protobuf.Timestamp`，跨进程单调时钟与 UTC 的转换由 M1 运行时处理。
- 审批哈希中的任务时间窗先规范化为 UTC，避免同一时刻的时区写法在 Timestamp 编解码后造成哈希漂移；显式 `tz` 元数据仍作为任务内容参与哈希。
- 领域载荷版本保留 `schema_version=0.1.0`；`drone.*.v1` 线路布局已由 `proto/v1-wire-lock.json` 冻结，`scripts/freeze_wire.py --check` 与契约测试阻止字段/枚举重编号和 RPC 签名破坏。M1 本地运行时已经实现；是否达到阶段准出标准仍以路线图和完整运行证据为准。

设计依据：[protobuf 字段存在性](https://protobuf.dev/programming-guides/field_presence/)。

## 8. 契约测试（M0 交付）

M1 运行时接收约束与持久化/IPC 实施见 [M1 实施计划](../m1-implementation.md)。本地可信任务包、登记表内容哈希、认证后的 executive 身份共同限定控制权限；单独携带租约不是任务授权。命令回执只表示命令处理结果，技能物理效果必须另验。新增观测、状态查询与任务暂停/恢复接口保留现有字段号及安全缺省语义；v1 在真实双进程验证后冻结。

### M1 仿真实时入口补遗（D027）

实时入口仅能启动部署版本自带的固定巡检任务，不能接收任务包、航点、控制意图或任意命令。浏览器动作经本机 HTTP → SSH → 仿真运行目录的 `operator.json` → executive → 既有 `MissionOperation`；wire 不变。新的操作信封带 `request_id`、`action`、`mission_id`、`mission_version`、`lease_epoch`、`step_id`、`valid_until`。任务/步骤已变、过期、生命周期不允许或 guardian 拒绝时写 `operator_rejected`，不能使 executive 崩溃或报告动作成功。已有可信 M1 注入文件继续兼容。

提交、机载接受、安全收尾、独立裁判结果分开展示。云端运行 ID 是启动幂等键；重复请求返回原运行，不再次起飞。刷新或关闭页面不隐含暂停/取消；断开后的状态显示为陈旧，恢复连接先读权威状态，不自动重发。

D028 将相同页面协议增加云端入口：Tailscale Serve → ASGI 网页服务 → 有界 JSON/Unix socket 任务代理 → 原操作者通道。代理只允许 `live_status/live_start/live_operate` 三个方法，各方法拒绝额外字段；连接以 Unix peer UID 校验。该传输仅在仿真云主机本机使用，不替代 M2 签名上行或改变 `drone.*.v1`。

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

## 10. M2 增量（D029–D034）

M2 只对 `drone.*.v1` 追加字段、消息、枚举值与 RPC；领域载荷 `schema_version` 仍为 `0.1.0`，已冻结的字段号、枚举与签名不变（`freeze_wire.py --check`）。

**跨到机器人的契约（进入 wire）**

| 对象 | 追加 | 语义 |
|---|---|---|
| `ApprovalRecord` | `signature`、`signer_key_id` | Ed25519 签在「审批陈述」上：域分隔前缀 `drone-agent/approval-statement/v1\n` + 去掉这两个字段后的规范 JSON（时间统一 UTC、键排序）。陈述含 `package_hash`，所以签名同时绑定可执行内容与授权；签名字段不进入 `package_hash`（D030） |
| `SkillManifest` | `intent_phases[]`（`phase`、`preconditions[]`）、`energy_estimate` | 多相位技能（`skill.inspect.asset`：`approach` → `capture`）的每个控制意图声明所属相位；guardian 按清单而不是技能名绑定载荷与前置条件。能耗估计带 `basis: sim_only`、来源运行、样本数、均值与上界；`estimated_energy_fraction` 取上界（D034） |
| `Evidence` | `mission_id`、`mission_version`、`robot_id` | 证据离开机载后仍能关联到任务版本；机载本地证据可以不填 |
| `OperatorRequest`（新） | — | 服务到机器人的暂停 / 恢复 / 取消：`request_id`、机器人、任务与版本、`lease_epoch`、`step_id`、`action`、`valid_until`、`requested_by`。uplink 转写进 M1 操作者信箱，executive 与 guardian 仍各自复核绑定与时效 |
| `EventType` | `mission_accepted`、`mission_finished`、`operator_request_accepted`、`operator_request_rejected`、`runtime_record` | uplink 把机载账本行映射为 `ExecutionEvent`；`event_id` 取账本行哈希，`data` 保留原账本种类、序号、前项哈希与原始数据，业务账本据此复核哈希链；无专门类型的行用 `runtime_record`，不丢弃 |
| `drone.fleet.v1` | `FetchDeliveries`、`AcknowledgeDelivery`、`PublishMedia`；`DeliveryKind`、`Delivery`、`DeliveryBatch`、`DeliveryRequest`、`DeliveryAck`、`EvidenceMedia` | 机器人主动拨出（mTLS）拉取已签名任务包与操作请求，按游标至少一次投递、按投递 ID 幂等确认；证据影像随元数据与 SHA-256 上传。`DeliveryKind` 零值不代表任何投递 |

**服务侧对象（不进入 wire，仍带 `schema_version`）**：`MissionRequest`（请求 ID、原文、请求者身份与信任级别、入口、指定体积与可选资产范围、幂等键）、`AdmissionResult`（`accepted` + `Issue[]` + 各层检查记录）、`Issue`（受控码表见 `runtime/issues.py`，准入、A2A 与控制台鉴权共用）、规划结果（`planned` / `refused` / `failed`，带尝试次数、通道与费用）、三列报告。

**规划草案**：模型输出的是 `submit_mission_draft` 函数参数，schema 由引擎按请求范围生成（体积、资产、技能用枚举约束；每个对象 `additionalProperties: false`）。草案不是契约：引擎确定性地把它变成 `MissionSpec`，任务 ID / 版本、`Provenance`、`recovery_policy_ref`、时间窗与能源预算由服务按场景配置填写，不由模型决定；Compiler 再补齐框架节点并校验。

**机载接受（M2 签名模式）**：除 §2 的 M0/M1 接收边界外，还要求审批签名有效、签名密钥在本机 `trust.json` 中、陈述与任务包一致、同一任务不回退版本、控制权代次高于本机持久化水位。未签名的包只能在显式的 M1 本地信任模式下运行。

### M2 评审补充（D037）

服务镜像中的账本必须从序号 0 连续且逐行摘要一致，才可驱动完成、操作和重规划。影像技能需要对应影像的服务复核；尚未上传或复核的影像不能被 `service_verdict` 缺省值当作成功。此时任务为 `verifying`（证据同步中），迟到影像补齐后重新推导，不能沿用先前的成功状态。重复的相同证据元数据保持原媒体关联，冲突内容拒绝。

机载影像的字节摘要不等于采集身份：不同任务 / 版本 / 步骤可以得到相同图像。uplink 上报时以机器人、任务、版本及完整原始证据契约的规范摘要生成全局证据 ID；字节摘要继续用于媒体去重和完整性核验。上传进度按该证据身份保存，不能仅按像素摘要忽略新的采集元数据。

统一入口 `/fixed/` 复用 M1 HTTP 协议，根路径复用 M2 hri.v0；没有新增跨到机器人的协议、控制意图或审批捷径。

## 11. M3 增量（D038–D048，M3-SITL 已验证）

M3 不改 `drone.contracts.v1`、`drone.control.v1` 与 `drone.fleet.v1` 已冻结的字段号、枚举与签名；新增内容只追加。

**本地自主层协议 `drone.autonomy.v1`（D039）**：只在同一台机器的进程之间使用，不进入车队协议，也不经过任何网络。传输为私有 Unix 流套接字上 4 字节大端长度前缀的 `AutonomyFrame`，单帧上限 64 KiB，按角色分套接字并检查对端 UID。M3-SITL 通过（2026-09-25）后已写入 `proto/v1-wire-lock.json`，与其他 v1 包一样字段号与类型不得再改，只能追加。

| 消息 | 方向 | 语义与接收检查 |
|---|---|---|
| `LocalTask` | guardian → 规划节点 | 「做什么、在哪个范围、多长时间」：绑定任务版本、步骤、代次与唯一 `task_id`；目标取自登记表的局部目标点，范围为批准体积，限速不超过技能参数；`active=false` 撤回任务 |
| `TrajectorySegment` | 规划节点 → guardian | 候选轨迹片段：`task_id`、代次、坐标系与地图版本必须与当前任务一致；`valid_until` 为生成后 400 ms；带来源、实现类型与学习阶段；状态为 `ok / reached / no_path / overloaded`。片段只是候选，guardian 过滤后才可能转发 |
| `ObstacleSet` | 局部地图 → guardian | 邻近障碍点、膨胀半径、位置标准差、置信度与 `valid_until`（500 ms）；缺不确定性即拒收 |
| `LocalizationReport` | 定位健康节点 → guardian | GNSS 与视觉定位健康、EKF 融合标志与位置标准差；过期视为未知，未知不满足任何「健康」守卫。`px4_status_age_s` 是所有输入话题中最新一条 PX4 消息的年龄，四个输入话题都至少到过一次之前为 1e6；报告自身新鲜但该年龄超过 0.3 s（DDS 链路丢失或尚未建立）时同样视为没有报告，不能解读为 GNSS 失效。`estimator_flags_age_s` 是最新估计器标志的年龄（从未收到为 1e6），超过 1.5 s 时报告无法说明融合状态，同样视为没有报告（D048） |
| `AuthorizedSetpoint` | guardian → 出口节点 | 唯一能让设定值到达飞控的消息：机器人、任务版本、步骤、`lease_epoch`、单调 `command_seq`、`issued_at`、`ttl_ms`（≤ 500）、NED 目标与限速 |
| `AuthorizationRevoked` | guardian → 出口节点 | guardian 结束外部控制并自己经 MAVLink 指挥 PX4（D047）：机器人、`lease_epoch`、与授权同一序号空间的 `command_seq`、`issued_at`（≤ 500 ms）、原因。节点丢弃当前授权、停止发布，撤销状态下不发任何命令，1 s 后仍见模式激活只在 arming check 中报告不可运行；更高序号的新授权清除撤销 |
| `EgressStatus` | 出口节点 → guardian | 注册状态、外部模式 nav_state、是否激活、PX4 链路与消息兼容性、各类拒绝、看门狗与撤销计数。PX4 链路以 `vehicle_status` 判定：PX4 变化即发、否则每 500 ms 发布，连续 1 s 收不到才算丢失 |
| `BeliefFact` | 感知 / 事件检测 → executive | 封装一条 `WorldFact`；`sim_truth` 来源、带位置却缺协方差、模型来源缺模型版本的一律拒收。`replan_trigger` 只在候选事实上保留，executive 记入账本并附当时步骤；任务服务在版本结束后把它转成 `candidate_event` 重规划触发器，所得版本一律需人工批准（D044） |

**外部模式能力**：`CapabilityDescriptor.control_modes` 中的 `external_mode` 由适配器按出口节点实时状态声明（已注册、状态新鲜、兼容性检查通过），不是静态配置；缺席时需要它的技能不能编译或准入。

**新增技能**：`skill.flight.goto_local`（需要 `external_mode`）经局部规划飞到登记表中的局部目标点，完成判据为新鲜位姿在容差内保持；参数只引用登记的目标点，不接受任意坐标。编译器在机器人声明 `external_mode` 且资产登记了局部观察点时，将资产巡检选择为 `skill.inspect.asset_local`（局部接近 + 拍摄相位）；显式指定航线参数时仍走 M2 航线版 `skill.inspect.asset`。没有局部能力时只有登记航线与 mission_upload 能力同时存在才回退，否则拒绝。实际选择见 [compiler.lower_inspection](../../src/drone_agent/admission/compiler.py)。

**恢复策略**：`RecoveryTrigger` 已有 `trajectory_stale`，另追加 `progress_stalled`、`compute_overloaded`、`autonomy_unavailable`（只追加枚举值）。策略按任务包的 `recovery_policy_ref` 加载，M3 场景为 `multirotor_m3@v2`。

## 12. P 系列运营契约增量（D049–D053，设计）

字段与生命周期设计见 [运营层 §2–7](09-operations.md)。本节是与既有契约的连接规则；P0 已实现服务侧 SourceContext、RunHeader、ModelUse、EvidenceOrigin、AnalysisOrigin 与 RunProvenance（`fleet/provenance.py`）；复用既有 JSON 字段，SQL schema 与 wire 不变。P1 的资源模型仍待实施。

| 连接 | 约束 |
|---|---|
| WorkflowRun → MissionRequest | 固定项目、流程 / 节点身份与幂等键；一个业务活动可等待多版本飞行，但通信重试不得创建新 mission |
| ResourceReservation → MissionPackage | 云端预约不等于 TaskLease；资源 / 机器人绑定改变需重新编译、准入和审批；投递前重查状态 |
| TaskAssignment → TaskLease | 云端 assignment_epoch 与机器人 lease_epoch 独立；只有机载现有授权路径能产生控制权 |
| RunProvenance → 证据 / 报告 | 执行、影像、规划、分析各自来源，受信后端生成；聚合保留子记录；旧数据缺失不得补成 live |
| InspectionFinding / ReviewRecord → 业务报告 | 疑似和人工确认是业务状态，不能修改 execution_status / effect_verdict / safety_verdict |
| WorkOrder → Reinspection | 维修反馈只进入待复检；新证据才能支撑闭环，证据未知不关单 |
| VendorMissionGateway → 厂商接口 | 后续独立高层任务契约、能力与责任档案；不假设厂商接受 PX4 包或部署本项目 guardian |

新增服务侧对象统一带 schema_version，项目权限由服务端校验。P0/P1 的运营元数据留在云端；跨到机载的任何新增执行绑定必须另行定义兼容字段、进入哈希 / 签名边界并跑 wire 检查，不能藏入自由 JSON 绕过冻结。
