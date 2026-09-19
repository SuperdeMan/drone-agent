# drone-agent 路线图

> 推进原则：按「通过什么验证才能进入下一阶段」推进，不按「演示界面能动」推进。周期按 1–3 人团队估算，是指示性排期而非承诺；退出标准是硬门槛。研究支线（VLA / 世界模型 / 合成数据）与工程主线并行，不是 M1–M4 的前置条件。

## 阶段总览

| 阶段 | 名称 | 指示周期 | 核心交付 | 退出标准（硬门槛） |
|---|---|---|---|---|
| **M0（已完成）** | 领域边界与基础契约 | 2026-09-19 完成（原估约 3 周） | 独立仓库、规范、架构文档、六类契约、proto 草案、技能草案、注入矩阵、复用清单、未解锁环境冒烟 | 258 项测试通过；红线测试、通用/飞行边界与实际环境证据齐备 |
| **M1** | 无大模型的单机安全闭环 | 2026-10 → 2026-11 底（约 6–8 周） | PX4 SITL + Gazebo；executive / guardian 双进程；PX4 适配器（MAVSDK 3.17+）；技能 takeoff / fly_route / capture_image / return_home / land；恢复策略图 v1；MCAP + 事件流记录与回放；独立裁判；故障注入库 | 关键故障场景（超时、重复命令、旧 epoch、模式被切、取消 / 暂停、上行中断、观测 / 轨迹过期、心跳丢失、低电、围栏逼近）全部通过；错误成功报告 = 0；多种子稳定 |
| **M2** | 接入受约束 Agent | 2026-12 → 2027-01（约 6–8 周） | Provider 移植；Planner（结构化输出 `MissionSpec`）；Compiler / Admission / ApprovalRecord；有界重规划；Evidence Verifier（确定性 + VLM 业务判断）；任务控制台 v0；A2A 任务入口 | 对抗性规划测试（错误坐标 / 能力 / 参数 / 顺序、提示注入、扩大范围）全部被准入拦截；`unknown` 不进入依赖步骤；`MissionSpec` 一次通过准入率有基线 |
| **M3** | 局部自主与降级 | 2027-02 → 2027-04（约 8–12 周） | ROS 2 Jazzy 集成（uXRCE-DDS、px4_ros2 外部模式路径）；感知 / 定位健康 / 局部 ESDF / 短时域规划；CBF 约束过滤；能源可达性与恢复策略 v2；机载容器（arm64）与 Jetson-in-the-loop；Zenoh；机载小 VLM 事件检测；`AirspaceConstraintProvider` 桩 → 真实接口 | 观测过期、任务卡住、网络中断、计算过载四类场景行为可验证；安全监督周期 p99 达标；guardian 是否需重写为 C++/Rust 有测量结论 |
| **M4** | 两条验证线并行 | 2027-04 → 2027-06（约 8–10 周） | **A**：单机受限真机（Pixhawk 6 级 + Jetson Orin NX 或 VOXL 2），UOM 报备，共因故障清单；**B**：一架 UAV + 一台 rover 的联合仿真（同一 Gazebo 世界，PX4 rover SITL），Coordinator 四项协同能力，交接协议，空间对齐，复核证据闭环 | A：RC 接管、飞控失效保护、伴飞计算机断电 / 串口拔出全部真机验证；B：交接成功率、重复执行数、任务丢失数、空间标注误差有基线，复核证据闭环通过 |
| **M5** | 真实空地协同 | 2027-07 → 2027-09（约 8–12 周） | 明确授权与受控范围内的「巡检—发现—复核—报告」；Nav2 地面平台接入；报告三列（已完成 / 未完成 / 不确定） | 部分完成不被报告为全部完成；设备失联、退出、交接失败时正确收尾；三分类结果与仿真基线可比 |
| **M6** | 模型与平台扩展 | 2027-Q4 起，持续 | VLA / 世界模型插件（影子 → 有限接管）；第二平台（DJI Cloud API）；多机调度（LLM 提议 + 优化器裁决）；数据飞轮；抽出 `agent-kernel`（触发器见 D001） | 相同任务与裁判下新增能力有可量化收益且无不可接受的安全 / 可靠性退化 |

## M0 · 领域边界与基础契约（已完成，2026-09-19）

已完成（2026-09-19）：

- [x] 仓库与规范：`CLAUDE.md`、`AGENTS.md`、`docs/decisions.md`（D001–D022）
- [x] 架构文档 `docs/architecture/00–08`
- [x] 前沿调研与 GPT-6 Pro 评估摘要 `docs/research/`
- [x] 六类契约 Pydantic 模型 `src/drone_agent/contracts/` 与契约测试 `tests/contracts/`
- [x] 复用清单 `docs/reuse-from-embodied-agent.md`
- [x] 示例配置：`configs/planner_tools.yaml`、`configs/recovery_policies/multirotor_campus_v1.yaml`、`configs/platforms/px4_sitl_multirotor.yaml`

本次补齐：

- [x] proto 骨架：`proto/drone/control/v1`（executive ↔ guardian）、`proto/drone/fleet/v1`（车队协议）、共享契约与自动字段清单；48 个模型 / 316 个字段；编译和 wire 边界测试通过
- [x] Linux 开发环境：Docker Linux 后端、ASCII 暂存、固定版本镜像构建与 compose 冒烟通过；PX4 v1.17.0 / Gazebo Harmonic 8.15.0，连续未解锁遥测与世界时钟验证通过，见 [核对记录](m0-readiness.md)
- [x] 在 embodied-agent 的 `decisions.md` 追加 D006 重估记录：D016，维持复制改造，公共内核等待双场景契约验证
- [x] M1 技能清单：五个 [SkillManifest 草案](../configs/skills/) 与 [行为说明](m1-skill-catalog.md)；恢复策略 15 条边的 [注入矩阵](../configs/scenarios/m0_fault_matrix.yaml) 和 8 类运行时故障，静态覆盖通过
- [x] 补齐契约回归：撤销后旧代次、续期身份、嵌套参数、审批哈希/有效期/范围、任务空间/时间/能源边界与 UTC 哈希、编译 DAG；恢复场景引用与实际验证记录分离（D021）

M0 已关闭：`ruff`、默认 importlib 模式下的 258 项测试、proto 编译与未解锁环境冒烟均通过。15 条恢复边全部仍是待故障注入验证的草案；M1 任务闭环尚未实现。证据见 [核对记录](m0-readiness.md)。

## M1 · 无大模型的单机安全闭环

任务：

1. `sim/`：三镜像 compose（sim / ground / aircraft），PX4 v1.17 SITL + Gazebo Harmonic，faster-than-real-time 时钟。
2. `guardian`：安全监督器（新鲜度 / 活性 / 租约 / 围栏 / 能源）、恢复策略图执行、Control Egress、PX4 适配器（MAVSDK 3.17+：mission_upload + 遥测；offboard 仅接口预留）。
3. `executive`：任务 DAG 调度、技能生命周期状态机、本地权威任务状态与事件日志、MCAP 录制。
4. 五个基础技能（`skill.flight.takeoff / fly_route / capture_image / return_home / land`），每个带完整 `SkillManifest`。
5. `eval`：场景 DSL、故障注入库、独立裁判（读 Gazebo 真值）、`eval/BASELINES.md` 首条。
6. 回放工具：任意任务事件流离线重放并重新裁判。

退出标准见总览；额外要求：guardian 与 executive 的 proto 契约冻结为 `v1`。

## M2 · 接入受约束 Agent

任务：Provider 移植（`embodied-agent/providers`，含限流 / 健康 / 超时）；Planner（`claude-opus-5`，adaptive thinking，结构化输出 `MissionSpec`；`refusal` 回退处理）；MCP 只读工具（资产库、地图）；Compiler / Admission；ApprovalRecord 与控制台审批流；Evidence Verifier；有界重规划；A2A 入口（cockpit-agent 调用）；对抗性规划测试集。

## M3 · 局部自主与降级

任务：`ros2_ws/` 节点（感知、定位健康、局部地图、局部规划）；px4_ros2 外部模式作为第二控制路径（不使用失效保护延期）；CBF 约束过滤；能源可达性模型；机载容器多架构构建与 Jetson-in-the-loop；Zenoh 跨机器人通信；机载 VLM 事件检测；ROS 2 Lyrical 切换评估（D008）；`LocalPolicy` 影子运行框架（研究支线接入点）。

## M4 · 两条验证线并行

A（真机）：硬件选型与组装、UOM 报备、地理围栏与返航点验证、共因故障清单、首飞降速、受限场景任务。
B（联合仿真）：PX4 rover SITL 同世界、Coordinator 四项协同能力、交接协议、`SpatialAlignment` 服务、复核证据闭环、协同指标基线。

## M5 · 真实空地协同

Nav2 地面平台接入（Unitree Go2 或轮式 rover）；真实场景巡检—发现—复核—报告；报告三列；覆盖矩阵扩展（环境、天气、光照、故障）。

## M6 · 模型与平台扩展

VLA / 世界模型插件按影子 → 有限接管 → 正式推进；DJI Cloud API 适配器（能力协商）；多机调度；数据飞轮（MCAP → 派生数据集 → 策略回归）；`agent-kernel` 抽出（D001 触发器）。

## 研究支线（与 M2 起并行）

- Pegasus / Isaac Sim 5.1 感知线搭建；Cosmos 3 合成数据评估。
- UAV VLN / VLA（AutoFly、DreamFly 类）在评测集上的影子运行与差异分析。
- 世界模型（WorldFly 类）作为 `WorldPredictor` 的候选评估价值。
- 多机器人场景图（MR-COGraphs 类）在空地对齐中的带宽 / 精度权衡。

## 节奏与机制

- 每阶段结束：更新 `CLAUDE.md` 当前阶段、`roadmap.md` 勾选、`eval/BASELINES.md` 追加。
- 季度技术雷达（1/4/7/10 月）：复查 `docs/research/frontier-2026.md` 与 `decisions.md` 重估触发器。
- 任何阶段的「跳过」必须在 `decisions.md` 记录理由。
