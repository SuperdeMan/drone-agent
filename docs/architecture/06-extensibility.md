# 扩展性：插件点、能力协商与模型接入

## 1. 插件点清单

扩展性来自稳定契约 + 明确的插件点。每个插件点给出：接口、默认实现、第一个替代实现、进入条件。

| 插件点 | 接口（契约） | 默认实现 | 计划中的替代实现 | 进入条件 |
|---|---|---|---|---|
| `PlatformAdapter` | `CapabilityDescriptor` 发布；`ControlIntent` → 平台命令；遥测 → `RobotStatus` | PX4 via MAVSDK 3.17+（M1） | PX4 via px4_ros2 外部模式（M3）；DJI Cloud API（M6）；ArduPilot AP_DDS（M6）；Nav2 地面（M4-B） | 适配器一致性测试套件通过（模式切换、失效保护上报、能力声明真实） |
| `Skill` | `SkillManifest v2` + 生命周期实现 | takeoff / fly_route / capture_image / return_home / land（M1） | inspect_asset（M2）、search_area、track_target、recheck_asset（地面） | 清单完整；取消 / 暂停语义有测试；完成证据判据可执行 |
| `LocalPlanner` | 输入局部地图 + 目标 → 候选轨迹片段（带 `valid_until`） | 确定性短时域规划（M3） | 学习型 VLN/VLA 策略 | 影子运行通过评测集；差异分析报告 |
| `LocalPolicy`（学习型） | 同上 + `implementation.learned(shadow/limited/full)` | 无 | AutoFly / DreamFly 类导航策略 | shadow → limited（有限场景）→ full，各阶段有准出指标 |
| `WorldPredictor` | 观测序列 → `PredictedWorld` 事实 | 无 | WorldFly 类世界模型；轨迹预测 | 只用于候选评估；永不作为前置条件 |
| `PerceptionProvider` | 传感器流 → `WorldFact` / 检测 | YOLO 级检测器（M3） | 开放词汇检测、机载小 VLM 事件检测 | 输出带协方差与置信度 |
| `EvidenceVerifier` | `Evidence` + 判据 → `effect_verdict` | 确定性检查器（M1） | VLM 判定（仅业务层，M2） | 确定性部分覆盖所有安全相关判据 |
| `LLMProvider` | 复用 `embodied-agent` Provider 抽象（来源链 car-agent `llm-gateway`） | MiniMax-M3（OpenAI 兼容，沿用 car-agent 配置；结构化规划关思考，强制函数调用 + JSON 抢救，D029） | 同一注册表中的其他 OpenAI 兼容厂商（DeepSeek、Qwen、MiMo）；机载小模型 | 输出通过 `MissionSpec` schema 校验 |
| `PlannerTool`（MCP） | 只读工具：地图、资产、天气、空域、历史 | 本地资产库 + 地图服务 | UOM / UTM 查询、气象 API | 工具无副作用；工具清单进入 `test_model_cannot_reach_egress` |
| `ConstraintProvider` | 准入 / 运行期约束来源 | 地理围栏、能源模型（M1） | `AirspaceConstraintProvider`（UOM 报备、空域属性、Remote ID）（M4 前）、气象 | 约束可离线评估、可回放 |
| `FleetTransport` | 车队协议消息的传输 | 进程内 / gRPC（M1–M2） | Zenoh（M3）、MQTT（DJI/Dock，M6） | 消息 schema 不变 |
| `Storage` | 本地事件日志 + MCAP；云端同步 | M1：fsync JSONL 哈希链 + MCAP 文件 | 业务 SQLite、对象存储、时序库按阶段引入 | 本地写路径永不依赖云端 |
| `Judge`（评测） | TruthWorld + 事件流 → 三分类结果 | 场景裁判（M1） | 更多场景、真机日志裁判 | 与被测 agent 进程隔离 |

## 2. 厂商扩展 = 能力协商

| 平台 | 可声明的 `control_modes` | 可声明的技能 | 不可声明 |
|---|---|---|---|
| PX4（MAVSDK） | `mission_upload`、`offboard_position/velocity/trajectory` | 全部基础技能 | — |
| PX4（px4_ros2） | 上述 + `external_mode` | 同上 + 自定义外部模式 | 失效保护延期（禁止） |
| DJI Cloud API（Dock / Pilot） | `mission_upload`（航线）、`camera`、`gimbal`、`live_stream`、`dock` | fly_route（航线）、capture_image、return_home、land（Dock）| `offboard_*`；连续轨迹控制 |
| ArduPilot | `mission_upload`、`offboard_*`（Guided） | 基础技能 | — |
| 固定翼 / VTOL | 按平台 | `hold` 不存在 → 恢复策略图用 `loiter` | 悬停类技能 |
| Nav2 地面 | `nav_to_pose`、`follow_path` | recheck_asset、nav_to、dock | 三维避障 |

规则：缺少某种能力时，Compiler 换用替代技能（如 DJI 上的 `inspect_asset` 编译为航线 + 定点拍摄）或 Admission 拒绝；**不用看起来相同的接口掩盖实现差异**。

## 3. 模型接入边界

### 3.1 LLM Planner

- 输入：任务请求 + 检索到的上下文（资产、区域、历史、技能清单、设备能力）。
- 输出：`MissionSpec` 草案，通过结构化输出（schema 强约束）产生；再经 Compiler / Admission。
- 允许的工具：MCP 只读工具。工具清单是配置项，进入契约测试。
- 有界重规划：只在事件（异常、任务失败、能力变化）触发时重新生成受影响的子图，且新版本仍需准入（可配置为「小改动自动批准」，范围由审批策略定义）。
- 模型选择与调用方式复用 `embodied-agent` 的 Provider；默认 MiniMax-M3（2026-09-23 用户更正，沿用 car-agent 配置，D029）。结构化规划关思考；强制函数调用未被遵守时从正文抢救 JSON，再做客户端校验；拒答与内容过滤映射为明确的规划失败，不重试、不跨厂商回退。
- M2 的上下文由引擎经 MCP 只读工具检索后以数据块注入，模型只拿到输出函数（D034）；工具返回是数据，真正的防线是 Compiler / Admission / 机载复核。

### 3.2 VLM 验证与事件检测

- 云端 / 地面：Evidence Verifier 的业务判断（疑似异常、进度）。
- 机载（M3 起）：≤ 8B 级 VLM 做事件相关性分类（CoMuRoS 模式），只产生 `WorldFact(candidate)` 与重规划触发，不产生控制。

### 3.3 学习型局部技能与世界模型

进入路径固定为：离线回放 → 仿真评测 → 影子运行（只记录不执行，对比确定性规划器）→ 有限接管（白名单场景 + 更严格包络）→ 正式。每一步的指标见 `08-evaluation.md`。不在飞行中在线训练，不无审批热换策略。

## 4. MCP 与 A2A 的位置

| 协议 | 用于 | 禁止 |
|---|---|---|
| MCP | L1 Planner 的只读工具 | 任何能写控制、写任务包、写租约的工具；社区 ROS 2 MCP 服务器（直接发布 `cmd_vel`）不得接入 |
| A2A | `cockpit-agent` 等外部 agent 作为任务入口：提交请求、查询进度、接收报告 | 传递控制意图；绕过准入与审批 |

## 5. 从单机到多机

- M1–M3：单机，Coordinator 退化为直通。
- M4-B：一架 UAV + 一台 rover，Coordinator 实现四项协同能力。
- M6：多机调度：分配器从「规则」升级为「LLM 提议 + 优化器裁决」；时空资源预约扩展到空域走廊；控制权模型不变。

## 6. 通用内核（agent-kernel）的抽出条件

当机械臂（`embodied-agent`）与无人机（本项目）都通过同一份契约测试（`tests/contracts/`）后，把以下内容抽为版本化包：契约模型、Provider、技能注册与检索、规划输出校验、事件与证据模型、评测三分类。内核不依赖 PX4、MuJoCo、机械臂驱动或车辆 VAL。
