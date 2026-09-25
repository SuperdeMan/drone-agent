# 分层职责、进程拓扑与层间接口

[返回架构总览](00-overview.md) · [部署](07-deployment.md) · [契约](02-contracts.md)

**当前范围：M2 + M3-SITL 单机 PX4 仿真。** P 系列运营扩展属于地面 L0–L2，尚未实现，详见 [运营层](09-operations.md)。机载局部自主已通过 [M3-SITL 记录](../m3-readiness.md)，JIL 与真机待 H1/H2。

## 1. 各层职责

### L0 操作者与入口

- 任务控制台：提交目标、确认范围、审批任务版本、看进度与证据、发起暂停 / 取消 / 接管。
- A2A 网关已实现任务提交、状态与报告查询；外部调用方无审批与控制权限。`cockpit-agent` 等语音入口属于待接入方，不能把网关可用视为这些项目已完成联调；声纹或语音不能单独构成飞行授权。
- 人工任务台还可提交审批与操作请求，但均经任务服务和机载复核，没有入口能直接触达 L5/L6。

### L1 任务规划与协同（off-board）

| 组件 | 职责 | 不负责 |
|---|---|---|
| Planner | M2 模型输出受约束草案；引擎确定性构建 `MissionSpec`，补齐身份、时间窗、能源预算与恢复策略引用 | 扩大已批准空间范围；生成代码；决定安全动作 |
| Fleet Coordinator | M2 为 `uav_01` 检查技能并直通分配，协同规则记为 `recorded_not_executed`；P1 增资源准入，P3 实现多站任务级分配 / 所有权 / 预约，X1 再加空地交接 | 机器人底层运动控制 |
| WorkflowPlanner / Runner（P2 设计） | 类型化业务草案、持久活动、定时与外部事件、等待 / 取消 / 对账；调用既有任务服务 | 审批未来全部飞行；直接控制机器人 |
| Operations（P1/P4 设计） | Site/Dock、项目权限、发现、复核、工单和复检，状态与证据分别记录 | 改写飞行效果或安全判定 |
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
- M2 使用登记表、飞控观测、任务状态和 `Evidence`；完整 `BeliefWorld` 双层表示按后续阶段引入，见[世界表示](05-world-model.md)。
- 向 L5 提交「技能层意图」；M2 使用已登记技能与相位（巡检的 approach / capture），模式与航线由 guardian / 适配器执行。M3 已增加局部目标与候选轨迹片段；executive 不直接写飞控。

### L4 局部自主（autonomy，M3-SITL 已验证）

- 感知、定位健康度、局部地图（深度体素与邻近障碍点）、短时域局部规划。
- 两种技能实现向下提交**相同类型的候选目标 / 轨迹**：确定性实现（默认）与学习型实现（插件，先影子运行）。
- 与 executive 的边界：executive 给「做什么、在哪个范围、多长时间」，autonomy 决定「此时此地怎么做」。

### L5 安全监督与控制出口（guardian，独立进程）

- 自有 PX4 路径唯一决定控制写入的进程；MAVLink 出口在进程内，M3 ROS 2 出口节点只转发其短时授权，是同一控制出口的物理延伸（D039 / D047）。
- 每条控制写入前检查：控制权租约有效、命令序号单调、任务版本匹配、观测新鲜、轨迹新鲜、executive 活性心跳、空间包络与围栏、能源与定位健康。
- Simplex 决策：通过则透传；不通过则切换到恢复策略图中的已验证基线行为（悬停 / 盘旋 / 返航 / 就地降落 / 交给飞控失效保护），并记录 `safety_intervention` 事件。
- executive 崩溃或失联时，guardian 独立执行恢复策略；**不等待任何模型回答**。
- 永远不禁用飞控原生失效保护，不使用 PX4 的失效保护延期。

### L6 本体适配器

- 当前 PX4 / MAVSDK 3.17.4 支持 mission_upload；M3 出口节点就绪时动态声明 external_mode。DJI 厂商任务网关、ArduPilot 和 Nav2 待对应 P5/X/H 阶段，见[扩展性](06-extensibility.md)。
- 每个适配器发布 `CapabilityDescriptor`（静态能力）与 `RobotStatus`（动态状态）；不支持的控制模式明确缺席，不做假实现。

## 2. 进程与部署拓扑

### 2.1 当前机载角色（M2 基础进程与 M3 增量，均在云端仿真）

| 进程 | 职责与连接 | 隔离边界 |
|---|---|---|
| `uplink` | 主动经 mTLS 连接任务服务；验签并写入 inbox / 操作者信箱；上传事件、证据与状态 | 独立进程；无飞控连接，不挂载 guardian 控制套接字 |
| `executive` | 接受已批准任务、独立验签；维护权威任务状态；调度技能与本地证据记录 | `network_mode: none`；通过私有 gRPC/UDS 向 guardian 提交租约、心跳、意图与操作 |
| `guardian` | 独立验签与授权复核；监督租约、活性、新鲜度与边界；选择恢复策略 | 唯一持有 PX4/MAVSDK 连接；不依赖规划模型 |
| PX4 SITL | 飞控原生任务执行、控制与失效保护 | 与业务运行时分开；保留人工接管路径 |

uplink 与执行进程通过私有卷交接任务和事件，**网络客户端不在 executive 内**（D031）。executive 创建并续期本地 `TaskLease`，guardian 依据已验证任务包、身份与持久化代次水位接受或拒绝；租约不是服务端经 mTLS 下发的飞行授权。代码入口：[uplink](../../src/drone_agent/runtime/uplink.py)、[executive](../../src/drone_agent/mission/executive.py)、[guardian](../../src/drone_agent/guardian/core.py)。

M3 已增加 ROS 2 定位、局部导航、感知、CLIP 事件检测及 C++ 出口节点，按角色私有套接字交互。SITL 测量支持维持 Python guardian，须独占 CPU 配额；H1 在 Jetson 重测，H2 验证设备侧部署。amd64 仿真不证明硬件已验收。

| 故障 | 当前边界 |
|---|---|
| executive 崩溃 / 心跳丢失 | guardian 依据飞行阶段、定位与能源选择已验证恢复边；不存在统一的“先悬停再返航”默认动作 |
| 任务服务或 uplink 不可用 | 不阻断本地监督；执行已授权任务包允许的继续 / 收尾策略，恢复连接后对账与补齐事件 |
| guardian 崩溃 | 业务控制出口失去写入能力；实际后续行为取决于飞控模式与原生失效保护配置，不能用尚未启用的 Offboard 丢信号规则解释 M2 |
| 伴飞计算机整体失电 / 串口断开 | 属于 H2（原 M4-A）的共因故障真机验证；当前 SITL 证据不证明设备侧故障后的物理行为 |

### 2.2 当前地面 / 云端

| 组件 | 职责 |
|---|---|
| `mission-service` | Planner、直通 Coordinator、Catalog、SQLite 业务账本、Compiler / Admission、审批签名、证据复核与报告 |
| Web 任务台 / A2A 网关 | 只经任务服务 API；人工审批绑定包哈希，第三方 Agent 只提交与查询 |
| 模型出站代理 | 允许列表 CONNECT 隧道，供任务服务访问已配置的模型端点（D036） |
| 仿真监管者 | 常驻任务台中按已验签任务包启停飞行，持本项目锁；网页 / 监管者重启不重飞（D035） |
| 真值采集 / Judge / Viewer | 隔离采集真值、在线与回放裁判、只读证据展示；真值不提供给被测执行进程 |

`mission-service` 不在飞行实时依赖链上。D037 的统一 8448 入口中，`/fixed/` 和 `/` 分别连接原 M1 与 M2 执行链，旧 8447 兼容入口保留；本机任务台不连接机器人，常驻云端任务台才执行仿真飞行。网络与容器落位见[部署](07-deployment.md)。

### 2.3 通信与中间件

| 范围 | 当前实现 | 后续路线 |
|---|---|---|
| executive ↔ guardian | 本地 gRPC over UDS + local credentials，不经过网络 | 保持本地控制边界 |
| uplink ↔ mission-service | 主动拨出 mTLS gRPC（默认）或 Zenoh（TLS + mTLS + 按证书名的访问控制，D046）；两者承载同一套车队协议报文：任务包、操作请求、事件、媒体与状态 | 其他厂商传输按平台接入 |
| 任务台 ↔ mission-service | hri.v0 WebSocket → 服务 API；常驻部署使用私有 UDS | 按入口权限扩展 |
| 飞控 | MAVLink 2 / MAVSDK 3.17.4 航线路径；M3 uXRCE-DDS / px4_ros2 外部模式 | 两条链路由 guardian 仲裁，互斥写入 |
| autonomy 内部 / 跨机器人 | M3 ROS 2 节点与本地自主层 IPC 已实现；跨机器人调度尚未实现 | P3/X1 每台独立域，跨机器人走车队协议，DDS 发现不跨无线链路 |

## 3. 当前层间接口一览

| 接口 | 载荷与边界 |
|---|---|
| Entry → mission-service / Planner | 带请求者、空间与资产范围的任务请求 |
| Planner → Compiler / Admission | 引擎从草案构建的 `MissionSpec`；编译补齐框架技能并检查准入 |
| Service → uplink → 机载 inbox | 已批准、签名的 `MissionPackage`；三处机载验签 |
| Service → uplink → 操作者信箱 | 绑定任务、版本、代次、步骤与时效的暂停 / 恢复 / 取消请求；executive 与 guardian 复核 |
| Executive → Guardian | 本地租约、心跳、`ControlCommandEnvelope` 中的技能意图、操作与命令对账 |
| Guardian → Adapter → PX4 | 已授权技能对应的原生任务 / 模式 / 相机操作；M3 的局部候选另经 guardian 过滤与短时授权后由出口节点转发 |
| 飞控 / 相机 → Guardian → Executive | 遥测、模式、健康与采集证据 |
| 机载账本 / 媒体 → uplink → Service | 事件、证据与约 1 Hz 状态；服务去重、核对哈希链并复核证据 |

## 4. 一个任务的生命周期（示例）

### 4.1 当前 M2：单资产巡检

1. 操作者在 `campus_training` 内选择 `asset_red` 并提出拍照请求。
2. Planner 提交窄草案；引擎构建规格，Compiler 补齐起飞、返航和降落，Admission 检查各项约束。
3. 操作者批准确切版本与 `package_hash`；任务服务生成 Ed25519 签名并等待机器人拉取。
4. uplink、guardian、executive 各自验签；executive 申请本地租约，guardian 检查通过后执行。
5. `skill.inspect.asset` 沿登记观察航线接近并采集影像；任务服务复核证据，输出三列报告。
6. 独立裁判用仿真真值核对报告，并从 MCAP 回放复判。失败后的原样重试受审批策略限制，新版本只在落地后执行；操作者取消不触发自动重试。

### 4.2 扩展场景：空中巡检与地面复核（X1 / H3）

近期首场景是 [P2/P4 的园区巡检—复核—工单—复检](09-operations.md)；以下为后置的空地扩展，包含尚未实现的多目标协同与交接；当前 M2 的能源估计仅准入单资产示例，不能把此流程当作可执行演示。

用户：「检查 A 区三处设备的外观，发现疑似异常后安排地面机器人复核，最后给我带照片的报告。」

1. Planner 产出 `MissionSpec`：目标、对象 `asset_01..03`、空间范围引用 `inspection_volume_A`、完成要求「每个对象有位置与时间绑定的有效影像」、协同规则「疑似异常进入地面复核队列」、恢复策略引用。
2. Compiler 展开为 DAG：`takeoff → fly_route(pre-validated) → inspect_asset ×3 → return_home → land`，每个 `inspect_asset` 标注证据要求与资源（`uav_01.motion` 独占、`uav_01.camera` 占用）。
3. Admission 校验并绑定任务审批；机载接收后按控制权协议建立新的本地租约。跨机器人任务所有权由 P3 建立，X1 扩展空地交接。
4. 机载 executive 复核并 `accept`；逐节点执行；`inspect_asset` 内部状态机：接近 → 观察 → 影像质量检查 → 补拍 → 产出 `Evidence`。
5. Evidence Verifier：确定性检查通过 → `effect_verdict=verified`；VLM 标记「疑似异常，置信度 0.7」→ Coordinator 生成地面复核任务并交接。
6. 地面机器人重新判断可达性，近距离复核，产出证据。
7. 报告分别列出：已完成、未完成、不确定的部分。任何 `UNKNOWN` 都不会出现在「已完成」列。
