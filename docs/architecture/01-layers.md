# 分层职责、进程拓扑与层间接口

## 1. 各层职责

### L0 操作者与入口

- 任务控制台：提交目标、确认范围、审批任务版本、看进度与证据、发起暂停 / 取消 / 接管。
- 座舱 / 语音入口（`cockpit-agent`）：通过 A2A 调用 `drone-agent` 的协调器 **只提交任务请求与查询进度**；声纹或语音不能单独构成飞行授权。
- 所有入口的输出都是「任务请求」，没有任何入口能直接触达 L5/L6。

### L1 任务规划与协同（off-board）

| 组件 | 职责 | 不负责 |
|---|---|---|
| Planner | 理解目标、拆解任务、选择技能、解释异常、提出重规划；输出 `MissionSpec` 草案（结构化输出） | 扩大已批准空间范围；生成代码；决定安全动作 |
| Fleet Coordinator | 能力发现、任务分配（LLM 提议 + 规则 / 优化器裁决）、任务交接、时空资源预约 | 机器人底层运动控制 |
| Catalog | 技能清单（`SkillManifest`）、设备能力（`CapabilityDescriptor`）与实时状态（`RobotStatus`） | — |
| Mission Ledger | 任务版本、审批记录、事件流、证据索引的**业务账本**（云端可暂时不可用） | 飞行任务的权威状态（在机载 executive） |
| Evidence Verifier | 确定性检查判「完成」；VLM 判「疑似异常 / 进度」并带置信度 | 安全判定 |
| Planner 工具（MCP） | 只读：地图、资产库、天气、空域属性、历史任务 | 任何写控制的工具 |

### L2 确定性任务编译与准入

- **Compiler**：`MissionSpec` → `MissionPackage`。把目标与对象展开为技能调用 DAG，绑定参数、资源预约、能源预算、恢复策略引用；每个节点标注完成证据要求。
- **Admission**：对 `MissionPackage` 做能力（技能存在且版本兼容）、空间（在批准体积内、坐标系与地图版本一致）、时间、能源（含返航余量）、法规（`AirspaceConstraintProvider`）、资源冲突校验；通过后绑定 `ApprovalRecord`（审批人、任务版本哈希、有效期）。
- 准入在 off-board 执行一次；**机载 executive 接受任务时再按本机 `CapabilityDescriptor` 与本地约束复核一次**（纵深防御，不信任上行链路）。

### L3 任务执行（executive，每台机器人一个）

- Mission Executive：任务 DAG 调度；每个技能实例是带生命周期的长任务（状态机 / 行为树），支持进度、暂停、取消、超时、恢复。
- 持有**本地权威任务状态**与事件日志（本地持久化）；云端账本只做同步。
- 维护 `BeliefWorld` 的机载视图，产出 `WorldFact` 与 `Evidence`。
- 向 L5 提交的是「技能层意图」：模式请求、航线、局部目标、轨迹片段；不直接写飞控。

### L4 局部自主（autonomy）

- 感知、定位健康度、局部地图（占据 / ESDF）、短时域局部规划。
- 两种技能实现向下提交**相同类型的候选目标 / 轨迹**：确定性实现（默认）与学习型实现（插件，先影子运行）。
- 与 executive 的边界：executive 给「做什么、在哪个范围、多长时间」，autonomy 决定「此时此地怎么做」。

### L5 安全监督与控制出口（guardian，独立进程）

- 唯一持有飞控连接的进程；唯一的控制写入点（Control Egress）。
- 每条控制写入前检查：控制权租约有效、命令序号单调、任务版本匹配、观测新鲜、轨迹新鲜、executive 活性心跳、空间包络与围栏、能源与定位健康。
- Simplex 决策：通过则透传；不通过则切换到恢复策略图中的已验证基线行为（悬停 / 盘旋 / 返航 / 就地降落 / 交给飞控失效保护），并记录 `safety_intervention` 事件。
- executive 崩溃或失联时，guardian 独立执行恢复策略；**不等待任何模型回答**。
- 永远不禁用飞控原生失效保护，不使用 PX4 的失效保护延期。

### L6 本体适配器

- 把 Control Egress 的抽象命令翻译为平台协议：PX4（MAVSDK 3.17+；后续 px4_ros2 外部模式）、DJI Cloud API（航线 / 云台 / 相机 / Dock）、ArduPilot（AP_DDS / MAVLink）、Nav2（地面）。
- 每个适配器发布 `CapabilityDescriptor`（静态能力）与 `RobotStatus`（动态状态）；不支持的控制模式明确缺席，不做假实现。

## 2. 进程与部署拓扑

### 2.1 机载（每台无人机）

```text
┌──────────────── 伴飞计算机（Jetson Orin 级；M1 为工作站上的容器）────────────────┐
│                                                                                  │
│  executive 进程（Python）            guardian 进程（Python@M1；M3 评估 C++/Rust）│
│  ├ Mission Executive                 ├ Safety Supervisor                          │
│  ├ Skill Runtime（状态机/BT）        ├ Constraint Filter（围栏/包络；M3 起 CBF）  │
│  ├ BeliefWorld（机载视图）           ├ Recovery Policy Graph                      │
│  ├ Evidence Recorder（MCAP+事件流）  ├ Control Egress（租约/序号/新鲜度）         │
│  └ Uplink/Downlink 客户端            └ Platform Adapter（MAVSDK ↔ 飞控）           │
│          │  本地 IPC（gRPC/UDS；每条命令带 lease_epoch + seq）                    │
│          └──────────────────────────────►                                        │
│                                                                                  │
│  autonomy（ROS 2 节点，M3 起）：感知 · 定位 · 局部地图 · 局部规划 · 学习策略插件   │
│  edge-inference 服务（M3 起）：小 VLM 事件检测（TensorRT/ONNX）                   │
└──────────────────────────────────────────────────────────────────────────────────┘
                     │ 串口 / 以太网（MAVLink 2；M3 起 uXRCE-DDS）
                     ▼
              飞控（PX4 v1.17）— 姿态/位置控制、任务模式、失效保护、RC 接管
```

隔离原则：

| 故障 | 期望行为 |
|---|---|
| executive 崩溃 | guardian 检测心跳丢失 → 按恢复策略图处理（默认：安全等待 → 返航） |
| guardian 崩溃 | 飞控 Offboard 信号丢失 → 飞控原生失效保护接管；RC 可随时接管 |
| 上行链路断 | executive 继续已授权任务包或按失联策略收尾；不需要 L1 |
| 伴飞计算机整体失电 | 飞控原生失效保护 |

### 2.2 地面 / 云端

```text
mission-service（Python）：Planner · Coordinator · Catalog · Ledger · Compiler · Admission · Evidence Verifier
console（Web）
数据平台：MCAP/ULog 归档 · 回放 · 评测裁判 · 数据集导出
```

`mission-service` 不在飞行实时依赖链上；它不可用时，机载运行时仍能完成或安全收尾已授权任务。

### 2.3 通信与中间件

| 范围 | 选择 |
|---|---|
| 机器人内部（executive ↔ guardian） | 本地 IPC（gRPC over UDS）；不经过网络 |
| 机器人内部（autonomy 节点） | ROS 2（每台机器人独立 ROS_DOMAIN_ID） |
| 机器人 ↔ mission-service、机器人 ↔ 机器人 | 车队协议（protobuf/JSON Schema 定义的 `MissionPackage` / `ExecutionEvent` / `WorldFact` / `RobotStatus`），传输可插拔：Zenoh（ROS 2 机器人）、MQTT（DJI Cloud API 类平台、Dock）、gRPC |
| 飞控 | MAVLink 2（MAVSDK 3.17+）；M3 起 uXRCE-DDS |

DDS 发现流量不跨无线链路；跨机器人一律经 Zenoh 路由或车队协议。

## 3. 层间接口一览

| 接口 | 方向 | 载荷 | 频率 / 性质 |
|---|---|---|---|
| Entry → Planner | 下 | 任务请求（自然语言 + 结构化上下文） | 事件 |
| Planner → Compiler | 下 | `MissionSpec`（草案，带模型与提示版本） | 事件 |
| Compiler/Admission → Executive | 下（uplink） | `MissionPackage` + `ApprovalRecord` + `TaskLease` | 事件；机载复核 |
| Executive → Guardian | 下（本地） | `ControlIntent`（模式请求 / 航线 / 局部目标 / 轨迹片段）+ lease_epoch + seq | 意图级：事件；轨迹片段：≤ 10 Hz |
| Guardian → Adapter → FC | 下 | 平台命令；Offboard 设定值 | 平台要求（PX4 Offboard ≥ 2 Hz 存活；MAVSDK 20 Hz 重发） |
| FC → Guardian → Executive | 上 | 遥测、模式、失效保护状态、健康 | 5–50 Hz |
| Autonomy → Executive | 上 | 局部地图摘要、候选目标 / 轨迹、定位健康 | 1–10 Hz |
| Executive → Ledger / Console | 上（downlink） | `ExecutionEvent`、`Evidence` 引用、`WorldFact`、`RobotStatus` | 事件 + 1 Hz 状态 |
| Robot ↔ Robot | 横向 | `WorldFact`（场景图增量）、交接协议消息 | 事件 |

## 4. 一个任务的生命周期（示例）

用户：「检查 A 区三处设备的外观，发现疑似异常后安排地面机器人复核，最后给我带照片的报告。」

1. Planner 产出 `MissionSpec`：目标、对象 `asset_01..03`、空间范围引用 `inspection_volume_A`、完成要求「每个对象有位置与时间绑定的有效影像」、协同规则「疑似异常进入地面复核队列」、恢复策略引用。
2. Compiler 展开为 DAG：`takeoff → fly_route(pre-validated) → inspect_asset ×3 → return_home → land`，每个 `inspect_asset` 标注证据要求与资源（`uav_01.motion` 独占、`uav_01.camera` 占用）。
3. Admission 校验并绑定审批；签发 `TaskLease(lease_epoch=n)`。
4. 机载 executive 复核并 `accept`；逐节点执行；`inspect_asset` 内部状态机：接近 → 观察 → 影像质量检查 → 补拍 → 产出 `Evidence`。
5. Evidence Verifier：确定性检查通过 → `effect_verdict=verified`；VLM 标记「疑似异常，置信度 0.7」→ Coordinator 生成地面复核任务并交接。
6. 地面机器人重新判断可达性，近距离复核，产出证据。
7. 报告分别列出：已完成、未完成、不确定的部分。任何 `UNKNOWN` 都不会出现在「已完成」列。
