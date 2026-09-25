# 姊妹项目复用评估与迁移清单

> 移植任何代码前必读。评估基于 2026-09-19 对 `embodied-agent`（模板仓库）与 `car-agent`（座舱）的静态阅读；GPT-6 Pro 评估的复用建议见 `research/gpt6pro-review-digest.md`。规矩沿用 embodied-agent `docs/reuse-from-car-agent.md` §4。

## 1. 结论

- **模板是 `embodied-agent`，不是 `car-agent`**：它已经证明 car-agent 的契约能跨领域存活，且工具链（uv / ruff / pytest / hatchling / proto 先行）直接沿用。
- **优先复用的是工程模块与验证方法**：Provider 族、SkillManifest / Registry 的声明式思想、Plan / Step / verify 的三态判定、`runtime/`（事件、账本、gRPC 工厂、issues）、`security/`（scope / permission）、hri.v0 语音协议、评测任务与基线账本格式。
- **不复用**：机械臂 HAL 的观测 / 动作形状、Guard 的闩锁 halt 语义、座舱任务账本作为飞行控制账本、车辆 VAL 内容、座舱微服务拓扑。
- 复用方式：**复制改造、零运行时依赖**（D001 / D018）。

## 2. 分模块判定

判定等级：A 直接挪用（改名与术语）· B 改造复用（保留框架，重写语义）· C 只参考设计 · D 不复用。

| 来源模块 | 判定 | 目标位置（里程碑） | 改造要点 |
|---|---|---|---|
| `embodied-agent/src/embodied/providers/llm.py`、`runtime.py`（BaseProvider、build_provider、LLMRuntime 目录、限流 / 健康 / 超时） | **A** | `src/drone_agent/providers/`（M2） | 已按 D029 改为 MiniMax-M3，工具调用 + JSON 抢救，保留拒答与 env 切换；去掉 MuJoCo 相关 |
| `providers/audio.py`（ASR / TTS / 流式桥） | A | `providers/`（M2，按需） | 语音只提交任务请求；声纹不参与授权（继承座舱红线） |
| `providers/policy.py`、`perception.py`（ONNX 策略、感知 provider 骨架） | B | `autonomy/` 插件接口（M3） | 输出改为带协方差与置信度的 `WorldFact`；策略输出改为候选轨迹片段 |
| `skills/manifest.py`、`registry.py`（SkillManifest、ParamSpec、Termination / Recovery / Safety、require_confirm 门在注册表） | **B** | `contracts/skill.py`（M0 已重定义）、`mission/skills/`（M1） | 扩为 SkillManifest v2：持续约束、资源独占、取消 / 暂停语义、完成证据、学习型实现的影子状态；`require_confirm` 门保留在执行出口以下 |
| `cognition/plan.py`、`plan_builder.py`（Plan / Step / StepResult / param_refs / fingerprint） | B | `admission/compiler`（M2）、`mission/executive`（M1） | `StepStatus` 拆为三元状态（D006）；fingerprint 改为六元幂等键；DAG 只管依赖，持续执行交给技能状态机 |
| `cognition/executor.py`（DagExecutor：拓扑分层、去重、确认门、验证） | B | `mission/executive`（M1） | **重写**：UNSAT 报告不得保留 OK；`_replay_prior()` 的「超时未重发」必须返回 `unknown`；并行以资源声明为准；取消 / 恢复语义按 `03-safety.md` |
| `cognition/verify.py`（SAT / UNSAT / UNKNOWN，缺观测不定罪，`$param:` 引用） | **A** | `mission/verify.py`（M1） | 三态思想原样保留，映射到 `effect_verdict`；判据来自技能 `completion_evidence` |
| `cognition/engine.py`（PlannerEngine：有界重规划、tier 预算、grounded report） | B | `planner/`（M2） | 重规划只重生成受影响子图且需准入；报告改为三列（已完成 / 未完成 / 不确定） |
| `cognition/world_state.py`（WorldSnapshot、Region、谓词注册表） | C | `contracts/world.py`（M0 已重定义） | 保留「谓词注册表 + 一个裁判」的思想；快照结构完全重写为带坐标系 / 版本 / 协方差的 `WorldFact` |
| `control/hal.py`（Embodiment 协议、Observation / ActionCommand） | **D**（形状）/ C（协议思想） | `adapters/`（M1） | `qpos`/`joint_targets` 形状不迁；保留「sim / real 同接口」的协议思想，重做为 `PlatformAdapter` + `CapabilityDescriptor` |
| `safety/guard.py`（GuardLimits、EE 围栏、闩锁 halt） | C | `guardian/`（M1） | 围栏与来源白名单思想保留；**闩锁 halt 不迁**（空中「停止写入」≠ 安全状态），改为恢复策略图 + Simplex 决策 |
| `safety/guardian.py`、`control/service.py`（三进程活性链、看门狗唯一执法点、supervisor 过期判定） | B | `guardian/`（M1） | 「看门狗是唯一执法点」保留；执法动作从 halt 改为恢复策略；把飞控连接放进 guardian 进程 |
| `safety/scopes.py`、`permission.py`（scope 覆盖规则、trust level、第三方硬拒） | A | `runtime/permission.py`（M2） | scope 目录改为 `flight.*` / `gimbal.*` / `camera.*` / `payload.*` / `mission.*`；第三方 / 工具类硬拒所有 `flight.*` |
| `runtime/obs/events.py`、`logging.py`、`tracing.py`、`redact.py`（Sink 协议、有界队列、OTel 可选） | **A** | `runtime/obs/`（M1） | subject 改 `robot.*` / `mission.*`；关联键加 `mission_id / step_id / command_id` |
| `runtime/ledger.py`（任务账本：状态、幂等键、拉模式取消、best-effort） | C | `fleet/ledger`（M2，云端业务账本） | **只作业务账本**；飞行任务的权威状态在机载 executive 本地持久化；取消走本地即时通道，不走心跳拉取 |
| `runtime/grpcio.py`（gRPC 工厂：keepalive、优雅关闭） | A | `runtime/ipc.py`（M1） | executive ↔ guardian 本地 IPC |
| `proto/embodiedrpc/{control,safety}/v1` | C | `proto/drone/control/v1`（M1） | 参考 `InvokeSkill` 流式事件与 `Heartbeat / EStop` 形状；重定义为 `ControlIntent`、`Lease`、`SafetyVerdict` |
| `hri/server.py` + `console/`（hri.v0 WS 协议、push-to-talk、confirm_request 后台任务） | A | `console/`（M2） | 协议原样；`confirm_request` 语义扩为审批 `MissionSpec` 版本；地图 / 任务 / 控制权 / 飞行状态呈现重做 |
| `eval/tasks/*.yaml`（eval.task/v1 不可变任务 + 裁判谓词 + 阈值）、`eval/BASELINES.md` | **A** | `eval/`（M1） | 裁判改读 Gazebo 真值；结果改三分类 |
| `data_engine/`（采集、录制、LeRobot 转换） | C | `runtime/recording/`（M1）、业务派生索引（P4/P5）；训练导出（X3） | 原始层改 MCAP + ULog + 事件流；LeRobot 只做派生视图 |
| `docs/reuse-from-car-agent.md` §4 迁移规矩、`tests/test_no_forbidden_terms.py` | A | 本文 §4、`tests/contracts/test_no_forbidden_terms.py`（M0） | 词表换成机械臂 + 座舱语义 |
| `car-agent/runtime/issues.py`（跨服务结构化 Issue：代码、严重度、范围、受控恢复动作） | A | `runtime/issues.py`（M2） | 客户端只执行受控 recovery kind 的思想原样；代码集换为飞行域 |
| `car-agent/runtime/contract_version.py`（一处字面量、两端共用） | A | `contracts/version.py`（M0，已内建 `schema_version`） | — |
| `car-agent/agents/_sdk/ledger.py` + `ledger_schema.sql`（活跃态唯一索引防双执行） | C | `fleet/ledger`（M2） | 「活跃态 (user_id, idempotency_key) 唯一」的并发教训沿用；其余不迁 |
| `car-agent/orchestrator/edge/val.py`（VAL：唯一能碰车的组件） | C | `guardian/egress`（M1） | 「唯一触达点」思想 = 本项目的单一控制出口；内容不迁 |
| `car-agent/orchestrator/edge/scope_gate.py`（T0 授权闸先于确认闸） | C | `admission/`（M2） | 顺序保留：能力 / 授权闸在审批确认之前 |
| `car-agent/llm-gateway/s2s/`（S2S 唯一工具 `escalate`） | C | `console/`（M2+） | 语音会话不持有任何执行工具 |
| car-agent 座舱微服务拓扑、K8s / Helm、NATS、支付、记忆图谱 | D | — | 不迁 |

## 3. 迁移执行清单（按阶段）

| 阶段 | 迁移项 | 完成判据 |
|---|---|---|
| M0 | 术语纪律测试；迁移规矩 | `tests/contracts/test_no_forbidden_terms.py` 绿 |
| M1 | `runtime/obs`、`runtime/grpcio`、`cognition/verify.py` 三态、eval 任务格式与基线账本、hri.v0 协议文档 | 每项带测试；文件头标注来源 commit；`uv run pytest -q` 绿 |
| M2 | Provider 族、scopes / permission、hri server、`runtime/issues.py`、PlannerEngine 骨架 | 同上；Planner 输出通过 `MissionSpec` schema 校验；对抗性测试绿 |
| M3 / H1 / X3 | policy / perception provider 骨架 | 输出带协方差与置信度 |

### 执行记录

| 日期 | 迁移项 | 来源 commit | 改造摘要 | 测试 |
|---|---|---|---|---|
| 2026-09-20 | M1 事件/IPC/三态验证/评测 | 不适用：本仓原生实现 | 沿用设计思想，按飞行语义重写权威日志、恢复、控制出口与证据验证；未复制姊妹项目代码 | 400 项测试与完整 SITL 矩阵，见 [M1 验收](m1-readiness.md) |

## 4. 迁移规矩（写给未来的每次移植）

1. **复制改造，不做跨仓依赖**：drone-agent 永不 import embodied-agent / car-agent；文件头注明来源与改造说明（`# Ported from <repo> <path> @ <commit>, changes: ...`）；License 同为 Apache-2.0。
2. **测试随行**：源模块自带测试的一起搬并改造；搬完 `uv run pytest -q` 绿才算完成。
3. **禁止搬运 `.env`**：只允许参考 `.env.example` 的变量名。
4. **概念改名彻底**：机械臂与座舱术语不得残留（契约测试扫描）。
5. **能不搬就不搬**：清单外模块想搬时，先在 `decisions.md` 记一条再动手。
6. **语义不随代码一起搬**：`OK` / `halt` / `cancel` 这类词在源仓库的含义不自动成立；每个迁入的状态或动作都要对照 `02-contracts.md` 与 `03-safety.md` 重新定义。

## 5. 运营路线补充（D049）

本页旧迁移分期统一使用 M 编号，避免与 P0–P5 产品里程碑重名。P 线优先扩展本仓已有 fleet、console、Provider 与证据链，不新建跨仓运行依赖；复制新的工作流 / RAG / 工单代码仍须先按本页规矩核对来源与语义。公共内核继续按 D001 双场景契约验证触发，平台范围扩大本身不触发抽库。
