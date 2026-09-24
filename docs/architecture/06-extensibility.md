# 扩展性：插件点、能力协商与模型接入

[返回架构总览](00-overview.md) · [平台能力声明](../../configs/platforms/px4_sitl_multirotor.yaml) · [路线图](../roadmap.md)

当前范围为 M2 的单机 PX4 仿真；“后续方向”不表示已有适配器、同名模块或经过验证的能力。

## 1. 插件点清单

扩展性来自稳定契约与明确的责任边界。下表把当前实现与后续方向分开；新增实现仍须通过对应的契约和场景验证。

| 扩展点 | 当前实现 | 后续方向与进入条件 |
|---|---|---|
| `PlatformAdapter` | [PX4 / MAVSDK 3.17.4](../../src/drone_agent/adapters/px4_mavsdk.py)，仅 `mission_upload` | px4_ros2 外部模式（M3）；PX4 rover（M4-B）、Nav2（M4-B 后续 / M5 首批）；其他厂商待接入。逐平台核验模式、失效保护与能力声明 |
| `Skill` | 起飞、登记航线、拍照、返航、降落（M1）及两相位资产巡检（M2） | 搜索、地面复核等；清单、取消 / 暂停与完成证据必须可验证 |
| `LocalPlanner` / `LocalPolicy` | 未实现 | M3 确定性局部规划与学习策略影子框架；有限接管按后续阶段单独验收 |
| `WorldPredictor` | 未实现 | 研究插件；预测事实只用于候选评估，不能满足前置条件 |
| `PerceptionProvider` | 当前采集相机影像并做确定性质量检查，尚无通用检测器 | M3 感知 / 定位 / 机载事件检测；输出须符合世界事实契约 |
| `EvidenceVerifier` | 机载检查与服务端复核；M2 提供可选 VLM 业务备注入口 | 扩充业务判断；VLM 始终不能改变 `effect_verdict` 或决定安全动作 |
| `LLMProvider` | MiniMax-M3 默认规划；OpenAI 兼容 HTTP、函数调用 + JSON 抢救、录制回放（D029） | 其他厂商按固定模型基线验证，不能转借 MiniMax 的实调结果 |
| `PlannerTool`（MCP） | 引擎经最小 stdio MCP 子集读取登记表、资产、历史与场景记录；模型只提交草案 | 接入真实天气、空域等数据源，保留只读工具边界 |
| `ConstraintProvider` | 围栏、能源门控、SITL 能耗上界；空域仿真桩拒绝真实模式 | M3 真实空域接口定义与能源可达性，真机报备按 M4-A 验证 |
| `FleetTransport` | M2 mTLS gRPC 拉取任务、上传事件 / 媒体 / 状态；M3 Zenoh（同一报文，D046） | 其他厂商传输；不破坏 wire 兼容与授权语义 |
| `Storage` | fsync JSONL 哈希链、MCAP、ULog；M2 SQLite 业务账本与媒体文件 | 对象存储 / 时序库按需引入，本地写路径不依赖云端 |
| `Judge` | M1 / M2 独立真值裁判与回放 | M3–M5 新场景、多机器人和真机日志裁判；与被测运行时隔离 |

## 2. 厂商扩展 = 能力协商

当前实际能力以 [CapabilityDescriptor 配置](../../configs/platforms/px4_sitl_multirotor.yaml)和适配器运行时报告为准；当前唯一启用的控制模式为 `mission_upload`。飞控 SDK 支持某接口，不代表本项目已经启用该能力。

| 平台 / 路径 | 本项目状态 | 能力映射边界 |
|---|---|---|
| PX4 / MAVSDK | 已实现，单架多旋翼 SITL | `mission_upload` + 六个技能；相机缺席时适配器撤下依赖相机的技能 |
| PX4 / px4_ros2 | M3 计划 | 外部模式 / 局部控制路径须单独接入并验证，当前不声明 `offboard_*` 或 `external_mode` |
| PX4 rover / Nav2 | M4-B / M5 计划 | 地面导航与复核技能分别适配；不能据二维导航能力声明三维避障 |
| DJI Cloud API / ArduPilot | 后续平台方向 | 按目标厂商、机型和实际 SDK 接口建立能力映射，不复用当前 PX4 的验证结果 |
| 固定翼 / VTOL | 尚未适配 | 是否可悬停及恢复行为由本体能力明确声明 |

`control_modes` 只能使用[契约枚举](../../src/drone_agent/contracts/common.py)；相机、云台、直播、Dock 等应按技能或载荷能力表达，不能写成不存在的控制模式。缺能力时当前 Compiler / Admission 拒绝；针对未来平台的替代技能编译须随适配器实现与验证。

## 3. 模型接入边界

### 3.1 LLM Planner

- 输入：任务请求 + 检索到的上下文（资产、区域、历史、技能清单、设备能力）。
- 输出：模型调用 `submit_mission_draft` 提交受约束草案；引擎确定性构建 `MissionSpec`，再经 Compiler / Admission。
- 允许的工具：MCP 只读工具。工具清单是配置项，进入契约测试。
- M2 有界重规划：新版本重新准入；自动批准只覆盖符合原审批边界的原样重试，每任务最多 2 次，只在落地后以更高代次执行。操作者取消不触发自动重试；扩大范围、换目标或改参数需要重新人工审批（D032、D035）。
- 模型选择与调用方式复用 `embodied-agent` 的 Provider；默认 MiniMax-M3（2026-09-23 用户更正，沿用 car-agent 配置，D029）。结构化规划关思考；强制函数调用未被遵守时从正文抢救 JSON，再做客户端校验；拒答与内容过滤记为 `refused`，技术失败另记为 `failed`；拒答不重试、不跨厂商回退。
- M2 的上下文由引擎经 MCP 只读工具检索后以数据块注入，模型只拿到输出函数（D034）；工具返回是数据，真正的防线是 Compiler / Admission / 机载复核。

### 3.2 VLM 验证与事件检测

- 云端 / 地面：Evidence Verifier 的业务判断（疑似异常、进度）。
- 机载（M3 起）：≤ 8B 级 VLM 做事件相关性分类（CoMuRoS 模式），只产生 `WorldFact(candidate)` 与重规划触发，不产生控制。

### 3.3 学习型局部技能与世界模型

进入路径固定为：离线回放 → 仿真评测 → 影子运行（只记录不执行，对比确定性规划器）→ 有限接管（白名单场景 + 更严格包络）→ 正式。每一步的指标见[评测体系](08-evaluation.md)。不在飞行中在线训练，不无审批热换策略。

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
