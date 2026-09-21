# M2 实施与验收计划

本轮拆解路线图的 M2「接入受约束 Agent」。范围：在 M1 已验收的 SITL 单机运行时之上，加入语言规划、确定性编译与准入、审批与签名上行、证据验证、有界重规划、任务控制台 v0 与 A2A 入口。不接真机，不引入 ROS 2 / Offboard（M3），不做多机器人交接（M4）。M1 的运行时、契约、wire 冻结与 66 组证据保持不变；M2 只能对 `drone.*.v1` 做兼容增字段。

前置：M1 已关闭（`eefe76e`，[验收记录](m1-readiness.md)）。动手前先按「决策待办」补 `decisions.md`，再改 `architecture/02-contracts.md` 与 `03-safety.md` 中受影响的语义，最后改代码（D026）。

## 退出标准拆解

路线图的三条硬门槛，加上架构文档已对 M2 作出的承诺，拆成可检查项：

| 门槛 | 可检查形式 | 证据 |
|---|---|---|
| 对抗性规划测试全部被准入拦截 | `eval/adversarial/` 六类（错误坐标 / 能力 / 参数 / 顺序 / 提示注入 / 扩大范围）每类 ≥ 5 例，两条路径都跑：直接构造的 `MissionSpec` 进 Compiler / Admission（确定性）；自然语言经 Planner（录制回放 + 可选实调）。判据：0 例获得授权的 `MissionPackage`，0 条控制意图到达 guardian 出口，每例带结构化拒绝码 | `tests/admission/`、`tests/planner/` 全绿；guardian 账本 intent 计数为 0 |
| `unknown` 不进入依赖步骤 | M1 的 executive 门控保持不变；新增服务侧判据：Evidence Verifier、报告与重规划触发都不把 `unknown` / `unverified` 当作 `verified`；VLM 输出不能改变 `effect_verdict` | 契约测试；E2E 报告三列与真值核对 |
| `MissionSpec` 一次通过准入率有基线 | 固定自然语言请求集（≥ 30 条，覆盖 inspect / survey / recheck 与应拒答的请求）× 固定提示版本 × 固定模型版本；记录一次通过率、拒绝原因分布、重规划次数、每任务模型费用 | `eval/BASELINES.md` 追加，绑定软件 SHA、模型 ID、提示版本 |
| 远程任务入口有签名与机载验签（`07-deployment.md` §7、D024 重估触发器） | 篡改、过期、未签名、错机器人、错密钥的任务包在 executive 与 guardian 都被拒绝 | `tests/contracts/test_mission_package_authorization.py` 扩展 |
| 端到端仿真闭环 | 自然语言 → 审批 → 签名上行 → SITL 飞行（含 `skill.inspect.asset`）→ 证据 → 报告，多种子通过，错误成功报告 0，在线 / 回放一致 | `scripts/verify_m2_release.py`、`docs/m2-readiness.md` |

## 实施顺序

| 批次 | 指示周期 | 实现 | 验收 |
|---|---|---|---|
| A：规划层地基（不调模型） | 约 2 周 | Provider 移植与录制回放、结构化 Issue 与 scope、MCP 只读工具、Compiler、Admission、`AirspaceConstraintProvider` 桩、SITL 能耗估计 | 无密钥全绿；确定性对抗集 100% 拦截；白名单契约测试不变 |
| B：模型接入 | 约 2 周 | Planner 引擎、提示与上下文检索、`refusal` 与 schema 失败回退、自然语言对抗集、有界重规划 | 回放对抗集 100% 拦截或拒答；重规划不扩范围、不加 `allow_unverified` |
| C：服务与入口 | 约 2–3 周 | 签名与机器人身份、机载 uplink 进程、mission-service（业务账本 / 目录 / 传输 / 直通协调器）、Evidence Verifier 与报告、`skill.inspect.asset`、控制台 v0、A2A 入口 | 验签契约测试；A2A 与控制台不能触达控制；VLM 不改 verdict |
| D：准出 | 约 1 周 | 云端部署扩展、E2E 场景集、裁判扩展（报告三列）、准入率基线、发布门禁 | 多种子通过，错误成功 0，回放一致，基线入账 |

批次 A 与 B 的确定性部分在 Windows 本机完成（纯 Python）；B 的实调需要 API key（只从环境读）；C 的 mTLS 与容器网络、D 的飞行都在云端（D023）。

## 工作包

### 批次 A

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M2-01 Provider 移植 | 从 `embodied-agent/src/embodied/providers/{llm,runtime,ratelimit,health,cache}.py`（当前 HEAD `24d780a`，移植时以实际 commit 为准）复制到 `src/drone_agent/providers/`，文件头标注来源（其 LLM 段本身移植自 car-agent，来源链一并写明）。默认 `claude-opus-5`、adaptive thinking、结构化输出；`refusal` 停止原因映射为明确失败而非重试；限流 / 健康 / 超时保留；新增录制回放 provider（fixture 带模型与提示版本哈希）；密钥只从环境读，日志脱敏 | — | `tests/providers/`：限流、超时、健康、refusal、回放一致性；无 `ANTHROPIC_API_KEY` 时全绿；`test_no_forbidden_terms` 绿 |
| WP-M2-02 结构化 Issue 与 scope | 移植 `car-agent runtime/issues.py`（代码、严重度、范围、受控恢复动作）到 `runtime/issues.py`；移植 `embodied-agent safety/{scopes,permission}.py` 到 `runtime/permission.py`；scope 目录 `mission.*` / `flight.*` / `camera.*` / `payload.*`；第三方与工具类身份对 `flight.*` 硬拒 | — | 准入拒绝码、A2A 与控制台鉴权共用同一码表；scope 覆盖规则单测 |
| WP-M2-03 MCP 只读工具 | `planner/tools/`：本地 MCP 服务器进程暴露 `assets.lookup`、`map.query`（读登记表与资产库）；`weather.current` / `missions.history` 读本地记录；`airspace.status` 由 WP-06 的桩回答；工具返回统一包成数据（带来源与哈希），不作为指令；白名单仍是 `configs/planner_tools.yaml` | — | 工具无写副作用（文件系统与网络快照前后一致）；`test_model_cannot_reach_egress` 不变；工具返回中的注入文本进入对抗集 |
| WP-M2-04 Compiler | `admission/compiler.py`：`MissionSpec` → `MissionPackage`。从登记表解析 `approved_volume_id`；用 `CapabilityDescriptor` 绑定技能版本、用 `SkillManifest` 绑定资源 / 超时 / 证据要求；单机器人 `uav_01` 由目录分配；补齐或校验 `takeoff → … → return_home → land` 框架节点；`recovery_policy_ref` 由场景配置写入并校验 Planner 未改；`inspect` 目标展开为 `skill.inspect.asset` 节点；能源预算按 WP-07 的估计核算 | 02 | 编译结果通过 M1 `Registry.validate_package`；编译是纯函数（同输入同 `package_hash`）；Planner 写入的 `recovery_policy_ref` 与场景不同即拒绝 |
| WP-M2-05 Admission | `admission/admission.py`：能力（技能 ⊆ 描述符、控制模式）、空间（体积存在、frame / map_version 一致、目标在体积内）、时间（窗口合理、在审批期内）、能源（预算 + 返航余量 vs 估计，缺估计为 unknown → 拒绝）、法规（WP-06；仿真模式需场景显式 `airspace_mode: simulation`，否则 fail closed）、资源（并行分支无独占冲突）、参数（`params_schema` 用 jsonschema 校验）、`allow_unverified_from` 必须带人工审批标记；输出 `AdmissionResult{accepted, issues[]}` | 02, 04 | 确定性对抗集六类 100% 拦截；每例拒绝码与预期一致；unknown 判定不放行 |
| WP-M2-06 `AirspaceConstraintProvider` 桩 | `admission/airspace.py`：接口 `query(volume_id) → {airspace_class, filing_status, remote_id_required, source, checked_at}`；M2 实现固定返回 `simulated_unfiled`；真实接口在 M3 定义（WP-M3-19）、M4-A 接入 | — | 真实模式下桩返回导致准入拒绝；仿真模式必须显式声明 |
| WP-M2-07 SITL 能耗估计 | 从 M1 66 组 MCAP / ULog 的电量遥测按技能统计消耗分数，写入 `configs/skills/*.yaml` 的 `estimated_energy_fraction`，标注 `sim_only` 与来源运行 ID；不宣称真机能耗 | — | 估计有来源与置信区间；Admission 用保守上界；`tests/contracts/test_m0_assets.py` 同步更新 |

### 批次 B

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M2-08 Planner 引擎 | `planner/engine.py`（参考 `embodied cognition/engine.py` 骨架）：提示版本化（`planner/prompts/<version>.md`）、上下文检索（资产、体积、技能目录、能力、历史）、结构化输出到 `MissionSpec` schema、`Provenance` 填模型 / 提示版本 / 输入哈希；schema 无效重试 ≤ 2 次后失败；`refusal` → `PlannerOutcome.refused`；不暴露任何写工具 | 01, 03 | 回放测试覆盖成功 / 无效 / 拒答；输出要么通过 `MissionSpec` 校验要么被拒；实调 smoke 用环境开关，默认关闭 |
| WP-M2-09 自然语言对抗集 | `eval/adversarial/*.yaml`（版本化 `eval.adversarial/v1`）：每例含请求、注入的工具返回、期望拒绝层（planner / compiler / admission / onboard）与拒绝码；`tests/admission/test_adversarial.py` 跑确定性层，`tests/planner/test_adversarial_replay.py` 跑录制回放 | 05, 08 | 六类 × ≥ 5 例；确定性层 100% 拦截；回放层 100% 拦截或拒答；实调结果单独记录，不计入门禁 |
| WP-M2-10 有界重规划 | `planner/replan.py`：触发事件（`skill_failed` / `effect_refuted` / `anomaly_candidate` / 能力变化）→ 只重生成受影响子图 → `mission_version + 1` → 重新 Compiler / Admission；`configs/approval_policy.yaml` 定义可自动批准的改动范围（参数微调、节点重试），其余回控制台；每任务重规划上限；`spatial_scope` 不得变化；不得新增 `allow_unverified_from`。机载切换版本要求新 `lease_epoch`，且当前技能处于 `cancel_safe_state` 或节点间隙 | 决策待办 ③；04, 05, 08 | 扩范围或加 `allow_unverified` 的重规划被拒；版本切换只在安全点；重规划次数进入指标 |

### 批次 C

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M2-11 签名与机器人身份 | `runtime/signing.py`：mission-service 签发密钥对任务包规范字节与 `ApprovalRecord` 签名；`ApprovalRecord` / `MissionPackage` 兼容增 `signature`、`signer_key_id`（签名字段不进入 `package_hash`）；机载在配置阶段固化公钥，不在空中轮换；executive 与 guardian 各自验签（纵深）；mission-service ↔ 机器人 mTLS，证书由部署脚本签发到容器挂载卷 | 决策待办 ①；02 | `generate_contract_fields.py --check` 与 `freeze_wire.py --check` 通过（只增字段）；篡改 / 过期 / 未签名 / 错机器人 / 错密钥全部被拒；密钥不进镜像与日志 |
| WP-M2-12 mission-service 骨架 | `fleet/`：`ledger.py` 业务账本（SQLite，活跃态 `(mission_id, idempotency_key)` 唯一索引；非权威）、`catalog.py`（`CapabilityDescriptor` + `RobotStatus`）、`transport.py`（`FleetTransport` gRPC 服务端 / 客户端；测试用进程内实现）、`coordinator.py` 直通版（单机器人）、`service.py` 装配入口；部署为 ground 镜像里的一个进程 | 决策待办 ②；11 | 账本不可用时机载任务不受影响（停掉服务后 M1 场景仍通过）；事件 / 证据 / 状态经传输入账并可查询 |
| WP-M2-13 Evidence Verifier 与报告 | `fleet/verifier.py`：确定性重检（媒体哈希、位姿协方差、时间窗、质量阈值、目标 ID）复算机载 `effect_verdict`，不一致即 `unknown` 并告警；VLM 业务判断（疑似异常 / 进度）输出 `WorldFact(source=model, world_kind=belief, confidence, model_version)`，低于阈值只作 `candidate`；`sim_truth` 来源拒绝。`fleet/report.py` 生成三列报告：已完成 = `succeeded ∧ verified`，未完成，不确定（含 `unknown` / `unverified`） | 01, 12 | 契约测试：VLM 输出不能改变 `effect_verdict`；无协方差或无时间的事实不入场景图；报告与机载账本、离线回放一致 |
| WP-M2-14 `skill.inspect.asset` | `configs/skills/inspect_asset.yaml` + `mission/`（执行器状态机与 `verify.py` 谓词）：接近（登记表中该资产的观察航点，mission_upload）→ 观察 → 影像质量检查 → 有界补拍（≤ N）→ 产出 `Evidence`；资源 `motion` 独占 + `camera` 独占；取消 / 暂停语义按 `03-safety.md`；平台能力表加入该技能 | 04 | 云端 SITL 单技能场景多种子通过；补拍上限触发后为 `unverified` 而非成功；`test_m0_assets` 的技能数与平台表同步 |
| WP-M2-15 任务控制台 v0 | `console/`：移植 `embodied hri/server.py` 的 hri.v0 WS 协议；功能：提交目标、查看 `MissionSpec` 与编译后包（含版本差异）、审批（生成绑定 `package_hash` 的 `ApprovalRecord`，审批人身份来自会话）、进度（`ExecutionEvent` 流）、证据（影像 + 三元状态）、暂停 / 取消（走 M1 操作者通道）、报告三列；单页静态前端，不引入前端框架 | 11, 12 | 审批后改动的包在机载失败；控制台无任何直达 guardian 的路径；取消经 executive 操作通道 |
| WP-M2-16 A2A 入口 | `fleet/a2a.py`：Agent Card + 提交任务请求 / 查询进度 / 取报告；调用方身份与 scope（`mission.submit` / `mission.read`）；载荷中出现控制级键或 `flight.*` scope 即拒；语音 / 声纹不构成授权 | 02, 12 | 携带控制意图的请求被拒并记 Issue；`cockpit-agent` 只能拿到任务状态与报告 |
| WP-M2-17 机载 uplink 进程 | `runtime/uplink.py`：机载唯一对外网络端点，经 mTLS 接收签名任务包与操作请求、上报事件 / 证据 / 状态；验签后经私有 UDS 交给 executive 与 guardian；executive 保持无网络（M1 边界） | 决策待办 ②；11 | 无 uplink 时机载按已授权包继续或安全收尾；uplink 进程崩溃不影响飞行；uplink 不能提交控制意图 |

### 批次 D

| 工作包 | 内容与落位 | 前置 | 验收判据 |
|---|---|---|---|
| WP-M2-18 云端部署扩展 | `sim/compose.m2.yaml`：ground 镜像运行 mission-service + 控制台；aircraft 容器增加仅到 ground 的受限上行网络（mTLS），仍无宿主端口；`scripts/dev_stack.py m2` 入口；API key 由部署脚本以环境变量注入 ground 容器 | 12, 17 | 部署回执记录应用 SHA、控制面哈希、证书指纹；其他容器身份前后一致 |
| WP-M2-19 E2E 场景集 | `configs/scenarios/m2_suite.yaml`：正常自然语言请求（多种子、多措辞）、应拒答请求、对抗请求、补拍触发、重规划触发（注入模糊影像 → `refuted` → 子图重规划）、服务掉线（机载继续并事后同步）；裁判扩展：报告三列与真值核对 | 14, 18 | 全部场景多种子通过；错误成功报告 0；在线 / 回放一致 |
| WP-M2-20 基线与门禁 | `scripts/verify_m2_release.py`：同一 SHA 上 A–D 所有判据；`eval/BASELINES.md` 追加准入率、拒绝分布、重规划次数、费用；`docs/m2-readiness.md` | 09, 19 | 门禁通过后才更新 `CLAUDE.md` 当前阶段与路线图勾选 |

## 设计边界

- 模型只在 mission-service 侧调用；机载进程在 M2 不调用任何模型。Planner 的上下文由检索构建，不把整个登记表或场景图塞进提示。
- 提示注入的防线是结构而不是措辞：工具返回被包装为数据，但真正的保证是 Compiler / Admission / 机载复核三层，模型输出不可能超出登记表中的体积、技能目录与参数 schema。对抗集要同时验证「模型被骗」与「被骗后仍被拦」两种情况。
- Provider 测试默认走录制回放；实调只在显式环境开关下运行，其结果单独记录、不计入门禁。密钥只从环境读取，不进代码、镜像、日志与录制文件。
- 签名保证任务包与审批在传输中未被改动且来自受信签发者；它不替代机载复核，机载仍按本机 `CapabilityDescriptor` 与登记表重新校验。M2 不做空中密钥轮换。
- 业务账本是最终一致的镜像；权威状态仍在机载 JSONL。报告由机载事件与证据哈希生成，账本不可用时任务照常完成或安全收尾。
- 协调器在 M2 是直通：单机器人 `uav_01`；`collaboration_rules` 被解析、校验并入账，但交接不执行（M4）。
- 重规划是新版本、新代次；旧版本命令在 guardian 被拒是既有红线；版本切换只发生在安全点，且不能扩范围。
- 空域约束在 M2 只有桩；仿真模式必须由场景显式声明，真实模式 fail closed；这是 D016「M4 真机前必须接入」的前置形态。
- 能耗估计来自 SITL 遥测统计，只用于仿真准入，不能宣称真机能耗；M3 的能源可达性模型替换它。

## 决策待办（动手前补 `decisions.md`）

| 编号 | 议题 | 阻塞 |
|---|---|---|
| ① | 任务包 / 审批签名算法、密钥保管与轮换、mTLS 证书签发与机器人身份 | WP-M2-11 |
| ② | aircraft 容器增加受限上行网络后如何保留 M1「executive 无网络」的边界；建议独立 `uplink` 进程持网络、经 UDS 交给 executive | WP-M2-12、17、18 |
| ③ | 审批策略：可自动批准的重规划改动范围与审计要求 | WP-M2-10 |

## 云端执行边界

沿用 D023：只对 `drone-agent-cloud` 工作区操作；mission-service 与控制台跑在 ground 容器，不开放宿主端口，控制台经 SSH 隧道访问；API key 由部署脚本以环境变量注入，不写入快照或 Compose 文件。模型调用产生费用，E2E 批次前记录预算；录制回放覆盖门禁所需的全部场景，实调只做抽样核对。

## 验收记录要求

在 M1 要求之上，每个 E2E 场景另记：模型 ID、提示版本、输入哈希、Planner 重试次数、准入拒绝码、审批人与审批时间、签名密钥 ID、重规划次数与触发事件、模型费用。对抗集记录每例的拒绝层与码；未被拦截的例子是阻断项，不是统计项。证据浏览器（`08-evaluation.md` §7）增加规划事件带与审批表（Planner 输入哈希、拒绝码、审批人、签名密钥 ID），E2E 场景的人工核对以生成页面为入口，判定仍来自裁判。

## 不在 M2 范围

Offboard / 外部模式与 ROS 2（M3）；机载 VLM（M3）；多机器人交接与空间对齐（M4-B）；真机与 UOM 报备（M4-A）；第二平台与多机调度（M6）。
