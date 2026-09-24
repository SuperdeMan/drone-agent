# 技术决策记录（Decision Log）

> 只增不删。每条含：日期、状态、决策、理由、替代方案、重估触发器。架构级变更先在此增条目，再改代码。

## D001 · 独立仓库；复用姊妹项目采用「复制改造」，抽公共内核有明确触发器

**日期**：2026-09-19 · **状态**：生效

**决策**：`drone-agent` 独立于 `embodied-agent` 与 `car-agent`。复用沿用 embodied-agent D006 的「复制改造、零运行时依赖、文件头标注来源、测试随行、术语改名彻底」规则。公共内核（`agent-kernel`，名字避开 embodied 的进程名 `agent-core`）**不在现在启动**；抽出条件是机械臂与无人机两个真实场景通过同一份契约测试（`tests/contracts/`），届时把契约模型、Provider、技能注册与检索、规划输出校验、事件与证据模型、评测三分类迁入版本化包，内核不依赖 PX4 / MuJoCo / 机械臂驱动 / 车辆 VAL。

**理由**：无人机在持续执行、故障处置、空间表示、飞控接入与评测上需要独立领域边界；embodied-agent 当前 HAL / 世界快照 / halt 语义带明显机械臂语义，用 `if embodiment == "drone"` 塞进去会把跨本体变成特殊分支集合。embodied D006 写明「出现第三个消费者时重估抽库」，本仓库就是第三个消费者；重估结论是「先双场景验证契约，再抽」。

**替代方案**：合并进 embodied-agent（否：领域耦合）；立刻新建 agent-kernel 仓库（否：抽象过早，靠猜测设计内核）。

**重估触发器**：两仓库契约测试双通过；或第三个本体（如 VTOL、人形）出现。需在 embodied-agent 的 `decisions.md` 追加一条记录本次重估（待其维护者执行）。

## D002 · 项目定位：安全约束任务运行时，不是「语音控制无人机」

**日期**：2026-09-19 · **状态**：生效

**决策**：定位为「面向空地异构机器人的安全约束任务运行时，以无人机为首要本体」。产品价值在任务编译、本地约束执行、证据确认与任务交接；语音只是入口之一。

**理由**：「语音 + 飞控 API 封装」没有护城河，也无法承载空地协同。三个姊妹项目应是三种场景对同一套「理解—执行—验证」能力的验证，按本体区分的是控制、安全与物理世界建模。

**替代方案**：做无人机聊天助手（否）。

## D003 · 四级执行分工 + executive / guardian 双进程

**日期**：2026-09-19 · **状态**：生效

**决策**：任务规划（L1）→ 确定性编译与准入（L2）→ 任务执行（L3）→ 局部自主（L4）→ 安全监督与控制出口（L5）→ 本体适配（L6）。机载最小进程拓扑：`executive`（L3 + 证据 + BeliefWorld）与 `guardian`（L5 + L6），后者是唯一持有飞控连接的进程；executive 失效时 guardian 独立执行恢复策略。上层向下传递任务及其边界，不传每帧动作。

**理由**：与 2026 年前沿（Gemini Robotics ER 2 编排 / 执行分离、P–C–A 综述四件套、RTA/Simplex）一致；embodied D008 的三进程活性链证明了「看门狗是唯一执法点」可行，飞行场景需要把执法点与飞控连接放在同一进程以缩短恢复路径。

**替代方案**：「大脑—驱动」两层（否：LLM 延迟进入控制回路）；全部 ROS 2 节点（否：M1 不需要，且通用运行时不应依赖全部 ROS 消息）。

**重估触发器**：M3 测得 guardian 在 Python 下无法满足安全监督周期 p99 预算 → 用 C++/Rust 重写 guardian（接口不变）。

## D004 · LLM 输出类型化 MissionSpec，不输出代码或平台命令

**日期**：2026-09-19 · **状态**：生效

**决策**：Planner 通过结构化输出产生 `MissionSpec` 草案；Compiler 展开为 `MissionPackage`；Admission 校验并绑定审批。Planner 不能新建空间体积，只能引用已批准体积；恢复策略由场景配置指定。有界重规划只重生成受影响子图且仍需准入。

**理由**：CoMuRoS / RobotFleet 等让机器人侧 LLM 生成可执行 Python 的做法不可审计、不可准入；ARIES-Mission2 证明「语义接地 + 确定性优化」分离可行。

**替代方案**：LLM 生成技能调用序列直接执行（否：绕过编译与准入）。

## D005 · 单一控制出口与控制权租约

**日期**：2026-09-19 · **状态**：生效

**决策**：所有控制写入经过 guardian 内的 Control Egress；命令携带 `(mission_id, mission_version, step_id, command_id, robot_id, lease_epoch, command_seq)`；旧 epoch 拒绝、序号单调、超时先对账再重发。控制权优先级：飞控失效保护 > RC/GCS > guardian 恢复 > 持有效租约的 executive。

**理由**：MAVSDK、ROS 2 节点、多个技能多头写入是无人机项目最常见的事故源；MAVSDK Offboard 20 Hz 自动重发会掩盖业务卡死，必须独立检查活性 / 新鲜度 / 租约。

**替代方案**：每个技能各自持有 MAVSDK 连接（否）。

## D006 · 三元状态：execution_status / effect_verdict / safety_verdict

**日期**：2026-09-19 · **状态**：生效

**决策**：命令执行状态、物理效果判定、安全判定分离；`unknown` / `unverified` 不放行后继（除非任务包中的边显式 `allow_unverified` 且经审批）；报告「已完成」只接受 `succeeded ∧ verified`。

**理由**：embodied-agent `executor.py` 中 UNSAT 报告策略保留 `StepStatus.OK`、`_replay_prior()` 把「超时未重发」返回 OK 的语义在飞行场景不可接受（起飞超时 ≠ 已起飞）。

**替代方案**：沿用单一 `StepStatus`（否）。

## D007 · 三种世界与 BeliefWorld 双层表示

**日期**：2026-09-19 · **状态**：生效

**决策**：TruthWorld（仅裁判）、BeliefWorld（运行时）、PredictedWorld（候选评估，不作前置条件）。BeliefWorld = 局部几何层（占据 / ESDF，机载不共享）+ 语义场景图（可共享增量）；所有事实带坐标系、地图版本、时间、有效期、协方差、来源、置信度。

**理由**：继承 embodied 「agent 只见感知、裁判只见真值」；多机器人开放词汇场景图是空地共享世界表示的当前最优实践；世界模型预测不能冒充观测。

## D008 · 首个飞控适配：PX4 v1.17 + MAVSDK-Python 3.17.x（v4 发布后迁移）；ROS 2 基线 Jazzy；Gazebo Harmonic

**日期**：2026-09-19 · **状态**：生效

**决策**：M1 用 PX4 v1.17.x SITL + MAVSDK-Python 3.17.x（PyPI `mavsdk`，gRPC 封装 + 自带 `mavsdk_server`；2026-09-17 核实 PyPI 最新为 3.17.4，`mavsdk-grpc` 3.17.4 已出现，说明包拆分已开始，但 v4 原生绑定尚未发布）。适配器代码隔离在 guardian 内，v4 发布后迁移（`System()` → `Mavsdk`、插件改为类）只动适配器；依赖锁 `mavsdk>=3.17,<5`。M3 引入 ROS 2 Jazzy（Ubuntu 24.04，LTS 至 2029-05）+ uXRCE-DDS + px4_ros2 外部模式作为第二控制路径；Gazebo Harmonic。M3 评估切换 Lyrical Luth（2026-05 LTS 至 2031-05）的条件：px4_msgs、Nav2、BehaviorTree.ROS2、rmw_zenoh 均有稳定发布。版本锁在 `configs/platforms/`。

**理由**：PX4 是开放度最高的飞控且 v1.17（2026-05）成熟；MAVSDK 正在换包名与 API，不按 PyPI 实际发布状态锁定会踩迁移坑（本条初稿曾误把 main 文档的 v4 当作已发布，`uv sync` 失败后纠正）；Lyrical 刚发布四个月，生态包可能滞后。

**替代方案**：ArduPilot 起步（否：AP_DDS 生态较小；列为第二平台）；直接 px4_ros2 起步（否：部分接口实验性，M1 不需要）。

## D009 · 安全体系按 RTA/Simplex 组织；恢复策略图是场景配置

**日期**：2026-09-19 · **状态**：生效

**决策**：四道约束（准入、运行期、本地恢复、飞控 + 人工）；guardian 是 Simplex 决策模块，恢复策略图（`configs/recovery_policies/`）是已验证基线行为集合，边由触发条件 × 上下文守卫决定；每条边须有故障注入场景；切回高性能路径有准入条件。最终安全判断不交给 LLM。不禁用飞控失效保护，不使用 PX4 失效保护延期，不提供 kill 接口。

**理由**：RTA 是既有学科，给评审与认证提供语言；「异常就悬停 / 返航」不是安全策略。

## D010 · 仿真两条线：PX4 SITL + Gazebo 主线；Pegasus / Isaac 后置

**日期**：2026-09-19 · **状态**：生效

**决策**：飞控与系统验证线（PX4 SITL + Gazebo）从 M1 起是 CI 主线；感知与学习数据线（Pegasus 5.1 / Isaac Sim 5.1、Cosmos 3）M3 起按需引入。空地联合仿真优先在同一 Gazebo 世界用 PX4 多旋翼 + PX4 rover SITL。容器形态借鉴 aerial-autonomy-stack：sim / ground / aircraft 三镜像，aircraft 多架构。

**理由**：先验证真实飞控逻辑与故障行为；高保真渲染不是任务闭环的前置条件；跨仿真器时空同步在早期是纯成本。

## D011 · 空地协同：协调器分配任务，各机器人本地闭环；首场景「巡检—发现—复核—报告」

**日期**：2026-09-19 · **状态**：生效

**决策**：共享任务、`WorldFact`、`Evidence`，不共享运动控制；四项能力（能力发现、空间对齐、任务所有权与交接、时空资源预约）从 M4-B 起实现；控制权强一致（租约 + epoch），地图事实最终一致；地面导航用 Nav2 适配器。

**理由**：混合层级优于全去中心化（CoMuRoS 结论）；「空中给全局语义、地面做局部执行」有实验路径；地面可通行性必须由地面机器人自己判断。

## D012 · MCP 只用于规划层只读工具；A2A 只用于任务入口

**日期**：2026-09-19 · **状态**：生效

**决策**：Planner 工具（地图、资产、天气、空域、历史）经 MCP 接入，全部只读，白名单在 `configs/planner_tools.yaml` 并进入契约测试；社区 ROS 2 MCP 服务器（直接发布 `cmd_vel`）不得接入。`cockpit-agent` 等外部 agent 经 A2A 提交任务请求与查询进度，不传控制意图。工具返回视为数据，不作为指令。

**理由**：MCP / A2A 是 2026 主流互操作协议，但它们的社区默认用法与本项目的单一控制出口冲突。

## D013 · 评测三分类 + 独立裁判 + 故障注入

**日期**：2026-09-19 · **状态**：生效

**决策**：结果分为任务完成 / 合理拒绝或安全中止 / 不安全或错误行为；裁判只读 TruthWorld 且与被测 agent 进程隔离；场景 DSL + 故障注入库从 M1 起；`eval/BASELINES.md` 只增不改。学习型组件走回放 → 仿真 → 影子 → 有限接管 → 正式。

**理由**：只看成功率奖励虚报，只看事故率奖励拒绝一切；有限测试集零违规不是实飞安全证明。

## D014 · Python 3.12 单包 + uv / ruff / pytest；proto 先行；ROS 2 节点独立

**日期**：2026-09-19 · **状态**：生效

**决策**：`src/drone_agent/` 单包多子模块，`uv` 管理，`ruff`（120 列）+ `pytest`（importlib 模式，asyncio auto），hatchling 构建；契约用 Pydantic v2；跨进程契约 proto 先行（M1）；ROS 2 节点放 `ros2_ws/`，用系统 Python，与本包只通过 proto / IPC 交互。Python 3.12 与 Ubuntu 24.04 / ROS 2 Jazzy 系统 Python 一致。

**理由**：与 embodied-agent 工具链一致，降低跨仓库维护成本；本包不 import rclpy 避免虚拟环境与系统 Python 冲突。

## D015 · 数据记录：MCAP + ULog + 任务事件流；训练格式是派生视图

**日期**：2026-09-19 · **状态**：生效

**决策**：原始层保留多频率传感器（MCAP）、飞控日志（ULog）、任务事件流（JSONL / SQLite），以 `mission_id + 单调时间` 关联；每次任务绑定任务与审批版本、模型 / 策略 / 软件 / 飞控配置版本；LeRobot 等训练格式按需导出。不在飞行中在线训练或无审批热换策略。

**理由**：训练格式固定采样率会丢失飞控与安全分析需要的信息；embodied D003 的 LeRobot 对齐在这里只适用于派生层。

## D016 · 法规接入点：AirspaceConstraintProvider，M4 真机前必须接入

**日期**：2026-09-19 · **状态**：生效

**决策**：准入层预留 `AirspaceConstraintProvider` 插件（空域属性查询、UOM 报备状态、Remote ID）；M1–M3 用桩实现；M4-A 真机前接入 UOM 流程（实名登记、飞行活动申请、起飞确认）。

**理由**：《无人驾驶航空器飞行管理暂行条例》2024-01-01 施行；2026-05-01 起未实名登记锁飞；法规是准入约束而非事后补丁。

## D017 · 第二平台优先级：DJI Cloud API > ArduPilot；通过能力协商接入

**日期**：2026-09-19 · **状态**：生效（M6 执行）

**决策**：M6 第二平台优先 DJI Cloud API（MQTT：航线、相机、云台、直播、Dock），其适配器只声明真实能力（无 `offboard_*`）；ArduPilot（AP_DDS / MAVLink）次之。Compiler 按能力换用替代技能或拒绝，不做假接口。

**理由**：国内低空经济场景以 DJI 与 Dock 自动化为主；能力协商是本项目扩展性的核心承诺。

## D018 · 术语纪律与移植规则

**日期**：2026-09-19 · **状态**：生效

**决策**：`src/` 中禁止 `qpos`/`qvel`/`gripper`/`ee_pose`/`joint_targets`/`cockpit`/`cabin`/`座舱`（契约测试扫描）；`vehicle` 不禁，因 PX4 / MAVLink 用它指飞行器本身（`VehicleStatus`、`vehicle_command`），禁了会把飞控适配器逼成别扭的改名；移植文件头 `# Ported from <repo> <path> @ <commit>, changes: <summary>`；测试随行；严禁复制 car-agent `.env`；清单外模块想搬先在本文件记一条。

**理由**：与 embodied-agent 的移植规矩一致；防止机械臂 / 座舱语义渗入飞行代码。

## D019 · README 中英双版；代码注释与 docstring 中英双语

**日期**：2026-09-19 · **状态**：生效

**决策**：`README.md` 为英文（GitHub 默认入口），`README.zh-CN.md` 为中文，顶部互链、内容同步；`src/` 与 `tests/` 的 docstring 与注释中英双语：docstring 英文段在前、中文段在后（同一 docstring 内空行分隔），行内注释 `# English / 中文`，Pydantic `Field(description=...)` 同样双语。`docs/` 其余文档仍以中文为主。术语纪律的契约测试同样扫描中文注释。

**理由**：用户明确要求；仓库公开在 GitHub，英文入口便于外部读者，中文保证与姊妹项目及团队沟通一致；契约语义对中英文读者都无歧义。

**替代方案**：单一双语 README 文件（否：过长，两种语言互相打断）；只英文注释（否：用户要求）。

**代价与约束**：注释体积约翻倍，必须两种语言一起维护；行宽以可读为准（ruff 未启用 E501）。

## D020 · M0 补齐 wire 草案、技能清单与可复现的仿真入口

**日期**：2026-09-19 · **状态**：生效

**决策**：按 M0 待办提前创建 `proto/`、`scripts/`、`sim/`；共享消息放 `drone.contracts.v1`，本地控制放 `drone.control.v1`，车队任务放 `drone.fleet.v1`。Pydantic 自动导出字段清单，proto 显式保留字段号，grpcio-tools 编译到 `gen/`。五技能草案放 `configs/skills/`，注入矩阵放 `configs/scenarios/`。仿真先提供一个真实 PX4/Gazebo 服务，M1 再按实现增加其余进程。

**理由**：原文将 proto 与 sim 目录归 M1，同时 M0 待办要求其骨架与冒烟；本条明确「准备性资产」与「运行时实现」的边界。静态模型测试、wire 编译、容器启动和物理故障验证各自留证，不能互相替代。

**替代方案**：预建空 executive/guardian/ground 服务（无验证价值）；把全部载荷塞进一个不透明 JSON 消息（失去 wire 边界）。

**重估触发器**：M1 跨进程运行及对账实现完成后冻结 v1；增加传输或第二平台时重新检查 wire 兼容性。

**跨仓同步结果**：embodied-agent 已追加 D016，完成 D001 原记录中「待其维护者执行」的 D006 重估；结论维持复制改造，公共内核仍等待双场景共同验证。

## D021 · 修正 M0 接收边界与恢复验证证据语义

**日期**：2026-09-19 · **状态**：生效

**决策**：租约撤销保留代次历史，同代次仅续期同一身份，闸门防止可变对象绕过；幂等键使用无歧义编码。任务包保留空间/时间/能源边界并纳入哈希，缺边界只允许解析、不允许授权；校验三方哈希、审批与任务时间窗、明确机器人范围与编译 DAG；参数控制键递归检查。恢复策略的场景名仅代表计划，生产验证必须有绑定当前边内容的记录。

**理由**：原 212 项契约测试未覆盖撤销后重授旧代次、嵌套控制参数及任务包自身哈希；配置中仅填场景名即被 `unverified_edges()` 视为验证完成，也没有真实注入证据。这些是现有安全语义的实现缺口，须在 M0 关闭前补回归。

**替代方案**：保持测试全绿而延迟修正（否：会把错误契约传入 M1）。本次是未发布 0.1.0 草案的收紧，不放宽已有红线。

**重估触发器**：M1 加入持久化账本、认证与真实故障注入后，验证记录需与运行产物及完整策略版本交叉核验。

## D022 · 用固定官方开发镜像构建 PX4 v1.17.0

**日期**：2026-09-19 · **状态**：生效

**决策**：SITL 源码固定为 PX4 `v1.17.0`，tag 解引用 commit `d6f12ad1c4f70ad3230afd7d86e971421e02fef4`。amd64 工具链使用 `px4io/px4-dev-ros2:main-jazzy` 对应 digest `sha256:558f27556d5ddb09a5082ec5a4407e4497b3d5bed646278242488010f267ae23`，Dockerfile 固定 digest，不跟随可变 tag。仅在隔离容器内构建；不安装主机全局工具。

**理由**：[官方运行镜像说明](https://docs.px4.io/main/en/simulation/px4_sitl_prebuilt_packages) 给出按版本发布的路径，但 Docker Hub 查询与 `docker manifest inspect` 均未找到 Gazebo 的 `v1.17.0`（也无 `1.17.0`）镜像；当前可见稳定目标源码仍有 [v1.17.0 release](https://github.com/PX4/PX4-Autopilot/releases/tag/v1.17.0)。不能把最新 1.18 预发行镜像当作 1.17 基线。

**替代方案**：改用 latest/alpha 镜像（否：偏离 D008）；改成只有 SIH 的冒烟（否：不能验证 Gazebo 链路）。

**重估触发器**：官方发布可核实的同版本预编译镜像后，可按 digest 替换构建路径；飞控版本变化仍另记 ADR。

## D023 · 默认使用云端工作区做 Linux 构建与仿真联调

**日期**：2026-09-19 · **状态**：生效

**决策**：按用户要求，当前先部署已有的契约验证与 PX4/Gazebo 仿真工作区，未来云端任务服务随 M2 实现接入。默认联调入口 `scripts/dev_stack.py` 使用现有服务器；共享 SSH 连接参数，应用目录、Compose project、网络、资源限额和产物独立。真机控制链仍保留在设备侧；云端运行的控制链只连接模拟飞控。

**边界**：服务器路径为 SSH 用户家目录下 `drone-agent/`；SITL 限 1.5 CPU / 2 GiB，验证任务限 1 CPU / 1 GiB；不开放新端口、不修改 car-agent `.env`、容器、数据库、安全组、Tailscale、systemd 或 CI/CD。连接参数只从 `DRONE_AGENT_*` 环境读取，缺省时可复用现有 `CAR_AGENT_*` SSH 参数；无跨仓代码依赖。

**实现与证据**：应用快照固定已提交 SHA，部署控制文件另行绑定 SHA-256；默认 dry-run，显式 apply 后执行远端校验与验收。首次复用 M0 镜像的传输是引导过程，不在本机重新构建真栈。服务器执行契约测试和未解锁 SITL 冒烟，记录实际镜像、源码、控制文件与既有容器前后身份。脚本不自动 commit、push 或清理数据。

**理由**：当前已有服务器 4 vCPU / 约 8 GiB，实查约 5.1 GiB 可用内存与 58 GiB 可用磁盘；能够承载受限单机仿真。当前源码尚无可部署的 Planner/mission-service，不能提前声明业务服务就绪。采用 car-agent 的不可变快照、私网入口与独立验证方法，不复用它的完整服务拓扑。

**重估触发器**：M1 加入双进程与故障注入、M2 加入任务服务、或负载超过配额时重评资源与拓扑；实际扩容、系统设置或生产数据操作仍单独授权。

## D024 · M1 采用本地追加日志、认证 IPC 与分层仿真证据

**日期**：2026-09-19 · **状态**：实施中

**决策**：按 [M1 实施计划](m1-implementation.md) 实现运行时。用 fsync JSONL 保存权威状态与控制收据，无数据库 schema 变更；命令发送前持久化，重启后的未决命令为 unknown。gRPC local credentials 与私有 UDS 目录约束本地连接，启动时装载可信任务包和登记表。恢复策略使用独立的 M1 mission-upload 配置，M0 草案保持历史语义。三镜像按实际职责构建，地面裁判独享真值产物。

**理由**：当前没有数据库和远程任务签发服务；用本地可信配置完成可验证的最小闭环，避免把 M2 的服务与密钥管理提前引入。必须把已验证的有限仿真能力与未来的 Offboard、视觉定位和真机能力区分。

**替代方案**：复用业务数据库（不符合飞行权威状态本地化）；超时自动重发（违反未知效果边界）；用模拟图片或遥测代替 Gazebo 真值（不能验收）。

**重估触发器**：M2 远程任务入口落地时增加签名与证书身份；M3 增加视觉定位/Offboard 时扩展策略并重新验证；日志容量超出任务级文件适用范围时再评估存储。

## D025 · 固定 M1 的 SDK 与线路兼容边界

**日期**：2026-09-19 · **状态**：生效

**决策**：MAVSDK-Python 精确固定 `3.17.4`，保留该版 gRPC API；其 `remaining_percent` 是 0–100，接收端归一化，多个新鲜电量报告取保守值。`drone.*.v1` 的字段号、枚举和 RPC 签名锁定于 `proto/v1-wire-lock.json`；领域载荷版本仍为 `0.1.0`，缺版本/不支持版本不准入。停止将先前的 `<5` 依赖范围当作当前运行条件。

**理由**：实际安装包已提示 `mavsdk` 名称向原生绑定迁移；保持宽上界会让后续解析依赖时越过未经适配的 API。线路布局冻结与软件版本、领域载荷版本是不同维度，不能互相代替。

**替代方案**：本轮迁移原生 v4（不属于 M1 所需闭环，另做适配器变更）；仅靠文档说明字段不可重编号（缺少自动检查）。

**重估触发器**：迁移 SDK 或修改 wire 主版本时，重跑适配器与完整场景矩阵，并提供一版兼容或明确迁移流程。

## M1 决策实施状态补记（2026-09-20）

D024 的实施已完成并通过完整 SITL 验收，状态转为生效；D025 的 SDK 与线路锁定已执行。运行版本、证据、时钟/资源和真实硬件边界统一见 [M1 验收记录](m1-readiness.md)，不改写前述决策形成时的历史状态。

## D026 · M2–M4 按实施计划拆解；模块落位与验证边界先于代码

**日期**：2026-09-21 · **状态**：生效

**决策**：M2、M3、M4 分别以 [m2-implementation.md](m2-implementation.md)、[m3-implementation.md](m3-implementation.md)、[m4-implementation.md](m4-implementation.md) 为可执行拆解，形式沿用 M1（批次 × 工作包 × 验收判据 × 决策待办）。本条固定拆解中已经做出的落位选择：

- M2 创建 `providers/`、`planner/`、`admission/`、`console/`，并提前创建 `fleet/`（业务账本、目录、传输、直通协调器、A2A 入口）；`fleet/` 在 M4 扩展协调器四项能力、交接、空间对齐与预约。业务账本与机载权威账本（`runtime/ledger.py`）分居两处，名字不共用。
- 机载新增 `uplink` 进程作为唯一对外网络端点，验签后经 UDS 交给 executive 与 guardian；executive 保持 M1 的无网络边界。具体网络与身份方案在 M2 决策待办中另立条目。
- 控制台 v0 是 `src/drone_agent/console/` 内的 hri.v0 WS 服务 + 单页静态前端，不新增顶层目录，不引入前端框架。
- Provider 测试默认录制回放，实调只在显式环境开关下运行且不计入门禁；密钥只从环境读取。对抗性规划语料版本化在 `eval/adversarial/`，测试在 `tests/admission/` 与 `tests/planner/`。
- 空域约束在 M2–M3 是桩与录制后端；仿真模式必须由场景显式声明，真实模式 fail closed；真实报备在 M4-A 接入（D016）。
- M3 的自主层拆为本包 `autonomy/`（插件接口、桥接、edge-inference）与 `ros2_ws/`（ROS 2 节点，系统 Python / C++），二者只经 proto / IPC 交互（D014）；px4_ros2 外部模式节点是 guardian 控制出口的物理延伸而非第二仲裁者，其进程边界与看门狗语义在实现前另立条目。
- M3 先测量再决定：算力拓扑（D023 触发器）、监督周期预算与 guardian 语言（D003 触发器）在批次 A 得出结论后才进入功能批次。
- M4 两线证据分别留存，不能互相追认；A 线共因故障按台架 → 系留 / 低空 → 受限任务逐级放开。
- 每份计划的「决策待办」是该阶段动手前必须补的本文件条目；未补条目不写对应代码。

**理由**：路线图的 M2–M4 只有一段式任务列表，缺少落位、前置与可检查的验收判据；M1 的经验是先固定批次与证据要求，再实现。提前创建 `fleet/` 是因为 M2 的业务账本、传输与 A2A 入口就是车队协议的服务端，放进 `runtime/` 会与机载权威账本混淆。M3 的算力需求超出当前共享云主机，若不先决策会把 D023 的默认云端联调写成不可执行的计划。

**替代方案**：一份合并的 M2–M6 计划（否：M3 起依赖硬件与算力决策，合并会把未决事项写成计划）；等各阶段开始时再拆（否：M4-A 的采购与报备必须在 M3 期间启动，需要现在就看到依赖）。

**重估触发器**：任一阶段的决策待办得出与本条落位冲突的结论；M3 算力拓扑决策改变云端默认（D023）；guardian 改写语言结论（D003）；阶段 readiness 关闭时按实际执行修订对应计划并在本文件补记。

## D027 · M1 固定任务的实时仿真入口（2026-09-21）

**决策**：按用户要求补齐可亲自操作的实时入口。保留 `461ec85` 的离线证据浏览器；新增 `console/` 单页与仅绑定 `127.0.0.1` 的标准库 HTTP 桥，通过已有严格 SSH 身份访问云端，不开放云主机端口、不增加系统服务或依赖。入口为 `dev_stack.py console`。HTTP 桥验证 Host、Origin 和内存中的会话 nonce，不接收任意路径、命令或任务参数；仅接受固定 M1 巡检及种子 7/19/41。

云端 `scripts/remote_live.py` 管理有界、脱离 SSH 会话存活的单次仿真任务；进程继承持有的 `stack.lock`，与部署及既有 M1 批次互斥。运行 ID 与操作 ID 幂等；状态、传感器帧、事件和结果按运行隔离。操作经 executive 既有本地通道，不触达适配器；补齐过期/错任务/错步骤/不合法生命周期的拒绝记录。页面关闭不取消任务，失联与后台异常不显示成功、不启动替代运行。

实时显示使用机载观测及相机帧；页面只呈现状态。运行结束继续调用独立裁判与 MCAP 回放；`interactive` 是固定仿真任务的人工操作模式，不加入既有 22 场景的 M1 发布矩阵。裁判按账本中的实际取消或暂停超时确定预期中止，不能由页面把异常改成通过。完整证据沿用 `fetch` 与 `eval.viewer`，不复制离线提取器。

**范围**：这是 M1 人工体验补遗，不关闭 M2 WP-M2-15，也不提前承诺 hri.v0、自然语言规划、任务审批、签名上行、A2A 或真机入口。D026 的完整 M2 服务拓扑与三项决策待办保持有效。

**验收**：本地 HTTP 鉴权、启动/操作幂等、并发互斥、过期与错目标拒绝、断开/刷新不重复启动；云端正常巡检、人工暂停/恢复与取消，覆盖多个种子；受影响的 M1 取消/暂停场景回归。每份证据绑定准确 SHA、控制面哈希、裁判和回放结果，不转借历史 66/66。

**重估触发器**：需要从其他设备直连、多人权限、任意任务输入、真机或完整 M2 服务时，重新决策身份/签名/部署；不能把本机桥直接绑定公网。

## D028 · Tailscale 私网常驻仿真控制台（2026-09-22）

**决策**：用户确认参照 car-agent 的 Tailscale 私网方式，现阶段不新增应用账号/登录体系，并授权实施。将 D027 页面与证据生成常驻到云端；只经独立的 Tailscale Serve HTTPS 端口 `8447` 访问，后端宿主端口仅绑定 `127.0.0.1:8768`，不启用 Funnel，不修改既有 car-agent 映射、tailnet ACL、安全组或 `.env`。能通过现有 tailnet 规则到达此入口的设备被视为本阶段可信操作者；这不是多人角色权限或真机准入。

**进程边界**：网页服务使用 Uvicorn 的单进程 ASGI 入口，在受限、只读根文件系统的容器中运行，复用已验证的源码镜像，不持 Docker socket、SSH 密钥或飞控连接。只读挂载本项目记录及版本目录，证据页面写入独立缓存目录。机内任务代理 `scripts/console_broker.py` 以原工作区属主运行，由本项目专用 systemd unit 常驻；只在私有 Unix socket 接受同 UID 的固定 `live_status/live_start/live_operate` 请求，调用原有 `remote_live.py`。网页 HTTP 输入不能指定 shell、路径、部署、任意场景或控制意图。

代理的 systemd unit 使用 `KillMode=process`：重启入口不能杀死已经独立运行、持有 `stack.lock` 的仿真任务。飞行、任务准入、恢复策略、操作有效期和幂等语义保持 D027；浏览器和入口进程不是机载心跳。网页和代理具有独立资源与请求上限。

**访问保护**：固定配置外部 HTTPS Origin，继续校验 Host、Origin、会话 nonce、JSON 类型与大小；不因位于 tailnet 而去掉防跨站保护。公网监听与 Funnel 不是回退方案。升级/失败回退只处理本项目服务和新 Serve 端口；禁止 `tailscale serve reset` 或覆盖整个 Serve 配置。

**部署**：`dev_stack.py console-cloud` 只读生成部署计划，`--apply` 激活本项目专用代理 unit、网页容器及独立 Serve 映射；状态与证据绑定准确应用 SHA、镜像、控制面及配置摘要。升级前后核对其余容器及 Serve 配置，检测非本项目配置变化时不能声称隔离通过。依赖只进入项目锁文件/镜像，不安装全局 Python 包。

**验收**：HTTP 边界、私有 socket 方法/身份限制、路径隔离、运行幂等、入口重启不重跑/中断任务；经真实 Tailnet HTTPS 运行正常巡检、暂停恢复、取消并核对裁判/回放；验证 loopback 发布、Funnel 关闭、旧映射不变。完整 M1 66 组、M2 或浏览器视觉验收分别记录，不转借历史结果。

**重估触发器**：访问者中出现只允许观看的成员、开启公网/Funnel、引入任意任务或真机时，重新决策应用身份与授权。依据：[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)、[Uvicorn](https://uvicorn.dev/)。

**实施补充（2026-09-22）**：首次激活时，网页容器和机内代理均健康，但 Docker 对 `internal: true` 网络返回实际端口映射 `8768/tcp: null`，宿主回环入口不可达，激活已回退。网页入口改用独立的 `console_ingress` 桥接网络，宿主仍仅绑定回环；不连接仿真网络，SITL/guardian/executive 的内部网络保持原样。本阶段的网页网络边界是“独立入口网络 + 仅回环发布 + 私有 Serve”，不宣称网页容器完全禁止出站。部署同时检查声明端口与 Docker 的实际发布结果，不能只看 Compose 配置。

**构建缓存补充（2026-09-22）**：首次经常驻代理启动任务时，Buildx 向用户的 `.docker/buildx/activity` 写缓存，被 `ProtectHome=read-only` 拒绝，尚未起飞。代理显式设置 `DOCKER_CONFIG` 到本项目 `console/docker-client/`，只允许本项目范围内的缓存写入；不放宽用户目录保护、不复制其他项目或用户的 Docker 凭据。

## D029 · M2 Planner 默认 MiniMax-M3，沿用 car-agent 的 Provider 配置（2026-09-23 用户更正）

**日期**：2026-09-23 · **状态**：生效

**决策**：按用户更正，M2 Planner / Provider 的默认模型由 `claude-opus-5` 改为 MiniMax `MiniMax-M3`，配置沿用 car-agent `llm-gateway` 已在真栈使用的形态：OpenAI 兼容 `chat/completions`（默认 `https://api.minimaxi.com/v1/chat/completions`，`MINIMAX_BASE_URL` 可覆盖）、Bearer 鉴权、`max_completion_tokens`、结构化规划恒关思考（`thinking: {type: disabled}`），头部 `<think>` 内联段一律剥离。变量名只参照 car-agent `.env.example`（`MINIMAX_API_KEY`、`MINIMAX_LLM_MODEL`）；值只来自进程环境或部署挂载的密钥文件，不读取、不复制 car-agent `.env`。

移植范围：从 embodied-agent `providers/{llm,runtime,ratelimit,health,cache,guarded}.py`（其 LLM 段本身移植自 car-agent `llm-gateway`）复制改造到 `src/drone_agent/providers/`。保留 `OpenAICompatibleProvider`（厂商差异由 token 参数、思考开关、鉴权三项覆盖）、厂商注册表（默认 minimax；mimo / deepseek / qwen 只在有 key 时注册；独立视觉档 `qwen-vl`）、限流 / 健康 / 缓存。去掉 Redis 持久化、全局热切换控制面、embedding 与 `AnthropicProvider`：它们不属于 M2 规划链路，需要时另立条目。新增录制回放 provider。

结构化输出：MiniMax 不保证遵守强制工具调用（car-agent 实测 MiniMax-M3 走成工具调用约 45–48%），因此沿用 car-agent M1a 的双通道——强制函数调用 `submit_mission_draft`，未走工具时从正文抢救 JSON——之后在客户端做 JSON Schema 与 Pydantic 校验。无效时附校验错误重试，总调用不超过 3 次（1 + 2 次重试），仍无效即失败；每次记录走的通道（toolcall / salvage）。

拒答：模型在草案中选择 `decline`，或服务商以内容过滤结束（`finish_reason=content_filter`、MiniMax `base_resp` 敏感内容码），均映射为 `refused`，不重试、不换厂商。规划请求不做跨厂商自动回退：同一基线必须固定模型，换模型只能显式配置并写入 `Provenance.model_id`（与 car-agent「pin 请求恒不跨」一致）。

视觉：Evidence Verifier 的 VLM 业务判断沿用 car-agent 的独立视觉档（`qwen-vl`，DashScope key）；无 key 时如实记录未运行，不影响确定性判定。

测试默认走录制回放或桩；实调需显式 `DRONE_LIVE_LLM=1` 且有 key，结果单独记录，不计入门禁（D026 不变）。

**理由**：用户要求与 car-agent 共用同一供应商与配置；该配置在 car-agent 有真栈探针、A/B 数据和弱工具调用的抢救经验。本项目的安全保证来自 Compiler / Admission / 机载复核，不依赖某个模型是否服从，换厂商不改变边界。

**替代方案**：保留 `claude-opus-5` 为默认（否：用户更正）；为 MiniMax 改用 `response_format=json_schema`（否：car-agent 未验证该能力，工具调用 + 抢救是已测路径）；规划请求跨厂商自动降级（否：破坏基线的固定模型前提）。

**重估触发器**：MiniMax 模型或工具调用率变化；准入率基线显示一次通过率不足；需要第二厂商对照评测时另立条目。

## D030 · 任务包签名、机载验签与机器人身份（M2 决策待办①）

**日期**：2026-09-23 · **状态**：生效

**决策**：

- 签名对象：mission-service 对 `ApprovalRecord` 的规范陈述签名。算法 Ed25519；消息为域分隔前缀 `drone-agent/approval-statement/v1\n` 加规范 JSON（去掉签名字段、时间统一为 UTC、键排序、UTF-8）。陈述含 `package_hash`、任务 ID / 版本、审批人、审批时间、有效期与机器人集合，因此一个签名同时绑定可执行内容与授权。`ApprovalRecord` 兼容追加 `signature`（base64）与 `signer_key_id`，二者不进入 `package_hash`。`MissionPackage` 不另加签名字段：其可执行内容已经以 `package_hash` 进入被签陈述，第二个签名不增加任何安全性质。
- 密钥保管：签发私钥由部署脚本在云主机本项目 `secrets/` 下生成（0600），只读挂载给 mission-service，不进镜像、仓库、日志或源码快照。机器人只持公钥：`trust.json`（key id 为 `ed25519:` 加公钥 SHA-256 前 16 位十六进制）在配置阶段只读挂载，进程启动时加载一次，空中不轮换。轮换即停飞后换发公钥并重启机载进程；已移除的旧钥签发的包一律拒绝。
- 机载验签（纵深）：uplink 收包时、executive 与 guardian 构造时各自独立验签，任一失败即 fail closed；此外继续执行 M1 的哈希、审批窗口、机器人范围与登记表复核。同一 `mission_id` 已接受 v 版本后，机载拒绝 ≤ v 的包。控制权代次水位按机器人持久化，跨任务版本单调。
- 链路身份：部署脚本为每次部署生成私有 CA（ECDSA P-256），签发 mission-service 服务端证书与机器人客户端证书（`CN=uav_01`、URI SAN `drone-agent://robot/uav_01`）。gRPC 双向 TLS；服务端以证书身份判定 `robot_id`，请求中的机器人与证书不符即拒绝。证书指纹写入部署回执；私钥只挂载到各自容器。
- M1 本地信任模式保留：未给 `--trust` 的 guardian / executive 仍按 M1 读取本地可信任务包（固定 M1 回归、D027/D028 入口）。M2 编排恒给 `--trust`，uplink 不写入未签名的包。

**理由**：签名解决传输中的篡改与伪造审批；它不替代机载复核，机载仍按本机能力描述与登记表重新校验。Ed25519 签名短、实现确定；TLS 证书用 P-256 以保证 gRPC（BoringSSL）兼容。

**替代方案**：只做 mTLS 不签名（否：链路身份不能证明审批内容）；HMAC 共享密钥（否：机载持有即可伪造）；任务包与审批各签一次（否：无额外性质）。

**重估触发器**：M4-A 真机需要硬件密钥存储；出现多个签发方或第二台机器人（M4-B）。

## D031 · 机载 uplink 进程与 executive 无网络边界（M2 决策待办②）

**日期**：2026-09-23 · **状态**：生效

**决策**：

- 机载新增 `uplink` 进程（`runtime/uplink.py`），是机载唯一的对外网络端点：只接入到 mission-service 的受限上行网络，由机器人主动拨出 mTLS 连接；不监听端口、不发布宿主端口、不接入仿真网络、不挂载 guardian 的 IPC 目录。guardian 只接仿真网络（MAVLink），executive 保持 `network_mode: none`。
- 下行交接：uplink 验签后把任务包原样（含签名）原子写入机载私有卷的 `inbox/`；操作请求（暂停 / 恢复 / 取消）经身份、绑定与有效期检查后写入 M1 已验证的操作者信箱，同一时刻只有一个待处理请求。executive 与 guardian 仍各自验签和复核。uplink 物理上够不到 guardian 控制套接字，因此不能提交控制意图。用私有卷而非另一个 UDS 服务：两者都不跨网络命名空间；文件交接复用 M1 已验证的信箱语义，uplink 重启不丢状态，且迫使消费者独立重验。
- 上行：uplink 只读地读取机载账本与证据，经 `FleetTransport` 发布（至少一次；事件 ID 取账本行哈希，服务端去重）；证据影像经新增的 `PublishMedia` 上传；能力与约 1 Hz 状态同样上报。
- 失联语义：服务或 uplink 不可用时，机载按已授权任务包继续或按恢复策略收尾；M2 不因服务掉线触发恢复（包默认允许继续，与 M1 `authorized_to_continue` 相同）。uplink 崩溃不影响 guardian 与 executive。
- 仿真中的机载监管者：M2 编排脚本在 inbox 出现已验签的包后才启动 guardian 与 executive，进程生命周期与 M1 相同；真机常驻监管者在 M4-A 定义。

**理由**：只有一个进程持网络，攻击面与故障面都集中在它身上；机器人主动拨出与真实蜂窝链路常见的 NAT 形态一致；控制出口仍只在 guardian，uplink 无法越过 executive 的操作通道。

**替代方案**：executive 直接联网（否：打破 M1 边界）；mission-service 推送到机载监听端口（否：机载需开放入站端口）；uplink 经 UDS 直连 guardian（否：给 uplink 触达控制出口的路径）。

**重估触发器**：M3 引入 Zenoh 传输；M4-A 定义真机常驻监管者；需要在空中接收新任务版本。

## D032 · 审批策略与有界重规划（M2 决策待办③）

**日期**：2026-09-23 · **状态**：生效

**决策**：

- 人工审批是默认：控制台审批人（D033 的会话身份）对确定版本的 `package_hash` 签署 `ApprovalRecord`。
- `configs/approval_policy.yaml` 定义可自动批准的重规划，M2 只允许一类改动：对最近一次结果为失败、未证实或未知的内容节点原样重试（技能、版本与参数不变）。同时必须满足：同一空间体积、同一恢复策略、能源预算不增加、不新增 `allow_unverified_from`、机器人集合不变、时间窗不超出原审批、每任务重规划不超过 2 次。其余改动（新目标、新技能、改参数、删除证据要求、换体积）一律回控制台人工审批。自动批准的 `approver` 为 `policy:<policy_id>@<version>`；业务账本记录基础版本哈希、改动分类、策略版本与触发事件。
- 触发：`skill_failed`，效果 `unverified` / `refuted` / `unknown`，能力变化。VLM 异常候选在 M2 只入账和进入报告，交地面复核属于 M4-B。
- 只重生成受影响子图：默认提议器是确定性原样重试；规划模型也可作为提议器，但其输出同样经过 Compiler、Admission 与审批策略，扩范围或新增 `allow_unverified_from` 一律拒绝。
- 机载版本切换只在安全点：落地、上锁并记录 `recovered_to(landed_disarmed)` 或任务结果之后，新版本使用更高的控制权代次。空中切换版本需要重新设计空中授权并单独验证，不在 M2 实施。

**理由**：M1 已验证的规则禁止在空中授予新代次（`new_authority_requires_grounded_reconciliation`）；放宽它需要新的安全论证与 SITL 场景。落地后切换同时满足「安全点 + 新代次」，不改变已验证的控制权语义。原样重试的风险不超过已批准的原节点，适合自动批准。

**替代方案**：空中悬停等待新版本（否：需放宽已验证规则，另立条目）；所有重规划人工审批（否：原样重试也要人工，成本高且无额外安全性）；允许模型自行批准（否）。

**重估触发器**：M3 需要空中改航或自主层要求在线替换子图；自动批准分类出现误判。

## D033 · M2 控制台与 A2A 的身份与权限（D028 重估）

**日期**：2026-09-23 · **状态**：生效

**决策**：M2 起控制台可以提交任意自然语言任务，命中 D028 的重估触发器，身份与授权重定为：

- 控制台（Tailnet）身份取 Tailscale Serve 注入的 `Tailscale-User-Login`（Serve 为 tailnet 用户注入并剥除客户端伪造的同名头；后端仍只绑定回环）。没有该头（如 tagged 设备）只能查看，不能提交、审批或操作。本机桥模式身份为 `local:<操作系统用户>`。仍不新增应用账号体系、不启用 Funnel。
- 角色：tailnet 操作者可提交、审批与暂停 / 恢复 / 取消；审批绑定 `package_hash`、会话身份与时间，审批后的任何改动都使签名失效。
- A2A：`/a2a` JSON-RPC 2.0（`message/send`、`tasks/get`）与 `/.well-known/agent-card.json`。调用方以 Bearer token 认证（服务端只存 token 的 SHA-256），信任级别为第三方，只授予 `mission.submit` / `mission.read`；载荷出现控制级键、`flight.*` scope、审批或操作请求一律拒绝并记 Issue。A2A 提交的任务只进入待审批，返回的只有状态与报告。语音与声纹不构成授权。
- 权限判定与 Issue 码表共用 `runtime/permission.py` 与 `runtime/issues.py`。
- 控制台 hri.v0 使用 WebSocket；实现为既有 Uvicorn ASGI 入口加纯 Python `wsproto`，依赖锁定在 `uv.lock`，不引入前端框架。

**理由**：Serve 身份头由 Tailscale 注入并防伪造，是现有私网准入之上最小的可审计身份。A2A 调用方不是人，不能审批。

**替代方案**：新增登录系统（否：当前阶段不需要，保持 D028 原则）；A2A 直接创建已批准任务（否：绕过人工审批）。

**重估触发器**：出现只读成员或多角色；开启公网访问；真机；外部 agent 需要写权限。依据：[Tailscale Serve identity headers](https://tailscale.com/kb/1312/serve)。

## D034 · M2 落位补充：规划检索、MCP 子集、场景 v2、巡检技能相位与能耗估计

**日期**：2026-09-23 · **状态**：生效

**决策**：

- Planner 的上下文由引擎经 MCP 只读工具检索，按请求范围（体积、资产、历史、天气、空域状态）以数据块注入提示；模型只拿到输出函数 `submit_mission_draft`，不直接调用工具。对弱工具调用厂商，单次调用更可复现，回放哈希也覆盖全部工具返回。
- MCP 为本项目内最小子集：JSON-RPC 2.0 over stdio，`initialize`、`tools/list`、`tools/call`、`ping`，协议修订 `2025-06-18`；工具带 `readOnlyHint`，服务器进程没有写路径。不引入第三方 MCP 包。
- M2 场景登记表 `configs/scenarios/m2_campus_v2.yaml`（`map_version=campus_v2`）：在 M1 场景基础上增加蓝色资产 `asset_blue`、每个资产的预验证观察航线，以及未报备的 `campus_rooftop` 体积（真实空域模式，准入必须 fail closed）。M1 场景文件与哈希不变；仿真世界增加对应静态标记。
- `skill.inspect.asset` 在一个技能内分两个意图相位：`approach`（上传登记的观察航线）与 `capture`（拍照）。`SkillManifest` 兼容追加 `intent_phases`（每相位的前置条件）；guardian 只接受载荷 `{skill_id, params, phase}`，相位须在清单内且顺序合法；补拍上限来自参数，用尽仍不合格即 `unverified`。
- 能耗估计：从 M1 完整验收运行的遥测按技能统计，写入技能清单的 `energy_estimate`（`basis: sim_only`、来源运行、样本数、均值与上界），`estimated_energy_fraction` 取保守上界；只用于仿真准入。
- 规划、准入、审批与报告等服务侧模型（`MissionRequest`、`AdmissionResult`、`Issue`、报告）不进入 `drone.contracts.v1`。跨到机器人的新增对象（`OperatorRequest`、投递、媒体）按只增原则追加；`ApprovalRecord` 与 `Evidence` 只追加字段。

**理由**：规划检索是 M2 计划已写明的设计边界；小型 MCP 子集足以承载只读工具，且不给机载镜像带入额外依赖；新地图版本避免改动 M1 已留证的登记表；把相位写进清单，guardian 才能按声明而不是按技能名绑定。

**替代方案**：模型自主调用工具的多轮循环（否：MiniMax 工具调用率约一半，且更难复现）；把接近与补拍拆成两个技能（否：与架构中 `inspect_asset` 的内部状态机不一致）。

**重估触发器**：需要模型按需检索大规模地图；MCP 规范出现不兼容修订；M3 引入 Offboard 接近路径。

## D035 · M2 任务台常驻 Tailnet 入口：常驻任务服务与按需仿真飞行（2026-09-23）

**日期**：2026-09-23 · **状态**：生效

**决策**：按用户要求并获部署授权，把 M2 任务台（hri.v0 与 A2A 网关）常驻到云端，经既有 Tailscale Serve 的独立 HTTPS 端口 `8448` 访问，后端只发布 `127.0.0.1:8769`。D028 的 M1 固定入口 `8447` 保持原样；两个入口各自部署、回退与验收。身份与权限沿用 D033：写操作只认 Serve 注入的单个 `Tailscale-User-Login`，没有该头的会话只读；不新增登录、不启用 Funnel、不改 ACL 与其他 Serve 映射。本阶段不配置 A2A 调用方（`/a2a` 一律 401），配置调用方另行记录。

**进程与数据**（compose 项目仍为 `drone-agent-cloud`；服务名统一 `desk` 前缀，不与端到端编排的服务重名）：

- 常驻 `desk`：`console/mission.py` 的 ASGI 页面，Uvicorn 单进程；只读根文件系统、工作区属主 UID、去除全部 capabilities；独立 `desk_ingress` 桥接网络；只读挂载任务服务 API 套接字目录与监管者公开状态目录；不持 Docker socket、SSH 密钥、签名私钥、模型 key 或飞控连接。
- 常驻 `desk-service`：`fleet/main.py`，业务账本、媒体与录制持久在 `~/drone-agent/desk/service/`；规划模式 `auto`：项目 secrets 中有模型 key 即实调 MiniMax-M3（D029），否则使用带标注的脚本回答（只回答 M2 场景集原文，页面与规划记录标明），绝不以 mock 冒充；只接入内部 `desk_uplink` 网络。
- 常驻 `desk-uplink`：D031 的机载 uplink；机器人目录 `~/drone-agent/desk/robot/`（inbox、信箱、uplink 状态、代次水位、各版本飞行产物）跨任务持久。
- 信任根独立：`secrets/desk/` 由 `fleet/provision.py` 生成（签名密钥、trust、CA、服务与机器人证书），与端到端用例的 `secrets/m2/` 分开；模型 key 共用 `secrets/m2-model/`。
- 按需飞行：`desk-sitl`、`desk-collector`、`desk-guardian`、`desk-executive`、`desk-judge` 属于 compose profile `flight`，只由监管者启停，网络与挂载同端到端用例；任务台不做故障注入。

**仿真机载监管者**（D031「仿真中的机载监管者」的常驻形态）：`scripts/desk_supervisor.py` 以工作区属主运行在本项目专用 systemd unit 中（`KillMode=process`、用户目录只读、项目内 Docker 客户端目录）。它只读取 uplink 验签后写入 inbox 的任务包，不接收网页或网络输入。某任务的最新已接受版本还没有飞行记录时：

1. 检查审批有效期与共享主机内存，再以非阻塞方式取得项目 `stack.lock`（与部署、M1 / M2 批次、D027 / D028 运行互斥）；忙时等待，并在公开状态中写明原因。审批在起飞前过期则记录「审批过期、未起飞」，不飞。
2. 停下 M0 空闲 SITL，启动全新的 `desk-sitl` 与真值采集，等 PX4 设置 home 后再等 5 秒。每个版本都从飞控断电重启开始，即一次地勤换电，与端到端验收相同。
3. 清除信箱中不属于本版本的残留操作请求；代次取机器人代次水位 + 1；依次启动 guardian 与 executive，二者照常各自验签；有界监视到落地上锁并写出结果，上限 600 秒。guardian 放弃控制权（`abort`）后，或超出上限时，由「仿真操作员」经 PX4 控制台发出降落，等同人工接管收尾，记录在案，不计为成功。
4. 停止 executive、guardian、采集与 SITL，恢复 M0 空闲 SITL，释放项目锁。等服务镜像追平；任务到达终态后，把该任务各版本的飞行产物、真值、已接受任务包、服务签名密钥 ID 与服务导出视图复制到独立用例目录，在线与回放各运行一次 `judge_m2`。期望分类为 `any`：不预设完成与否，但出现任何问题、错误成功或回放不一致都不通过。结果写入公开状态，页面显示。

监管者重启时先接管正在进行的飞行（容器不随监管者退出），不重飞、不启动替代飞行。页面只展示监管者与裁判记录，不产生判定。

**操作员取消不触发重规划**：任务台让 tailnet 操作者能在飞行中取消。D032 的重规划触发只列失败类结果，但实现按「未完成的内容节点」判断：在起飞阶段取消时，巡检节点从未运行，被当作 `not_run` 自动批准重试，任务会违背操作者意图再次起飞。修正为：一个版本中任何步骤的执行状态为 `cancelled`（只有操作者取消会产生这一状态），该版本结束后不重规划，任务记为未完成并记录 `replan.cancelled_by_operator`。需要再飞时由操作者重新提交。

**理由**：任务台必须让 tailnet 操作者真正完成「提交 → 审批 → 飞行 → 报告」。飞行仍在仿真中，按与端到端验收相同的编排语义执行，没有新增控制路径：网页只经服务 API，审批变成签名任务包，操作变成机载复核的操作请求，飞行只在 inbox 出现已验签任务包时才开始。独立端口使 D028 已验收的入口不受影响；SITL 按需启动，避免在共享主机上长期占用 1.5 CPU。

**替代方案**：复用 `8447` 并替换 M1 入口（否：使 D028 已验收入口失效，且两者身份模型不同）；网页或服务直接启动飞行（否：绕过签名与 uplink）；SITL 常驻（否：长期占用共享主机，且每个版本仍需断电重启）；与端到端用例共用 `secrets/m2/`（否：信任根与验收证据互相牵连）。

**验收**：经真实 Tailnet HTTPS 完成自然语言提交 → 规划（有 key 时实调）→ 审批 → 飞行 → 三列报告与证据，独立裁判在线 / 回放一致、错误成功 0；暂停 / 恢复与取消经服务 → uplink → 信箱 → executive；无身份会话只读，客户端伪造的身份头不被采信，A2A 无 token 拒绝；飞行中重启网页与监管者不中断、不重飞；激活前后其他容器与 Serve 映射（含 `8447`）不变。证据绑定准确 SHA，不转借 M2 端到端或 D028 的历史结果。

**重估触发器**：多名操作者需要角色区分或只读成员；需要并发飞行或多机器人；开启 A2A 调用方或公网；真机。

## D036 · 任务服务经允许列表代理访问模型端点（2026-09-23）

**日期**：2026-09-23 · **状态**：生效

**背景**：常驻任务台（D035）写入模型 key 后，首个实调请求在 0.4 秒内以 `planner.technical_failure` 失败，原因是任务服务只接入内部网络，容器内连域名解析都不可用。M2 端到端编排（`sim/compose.m2.yaml`）中的任务服务同样只在内部上行网络，所以 `m2 --planner live` 从未真正实调过。此前的端到端只用脚本回答，这一缺口没有暴露。

**决策**：

- 任务服务继续只接入内部网络，不获得通用出站。新增模型出站代理 `fleet/model_proxy.py`（任务台服务名 `desk-model-proxy`，端到端 `model-proxy`）。它是标准库实现的 HTTP CONNECT 隧道，只接受允许列表中精确的「主机:端口」，当前只有规划端点 `api.minimaxi.com:443`；配置视觉档时再显式加入 DashScope。其他目标、非 CONNECT 请求与超长请求头一律拒绝，并记录目标与决定，不记录任何内容。TLS 在任务服务与模型端点之间端到端，代理看不到请求、回答或 key。
- 代理接入一个与任务服务共享的内部 `*_model` 网络，以及一个可出站的 `*_egress` 桥接网络；不发布宿主端口，只读根、无 capabilities。任务服务以 `HTTPS_PROXY` 指向它。页面、uplink、guardian、executive 与仿真容器都不接入这两个网络。
- 允许列表写在 compose 命令中，改动即部署变更。`MINIMAX_BASE_URL` 指向其他主机时必须同时修改允许列表，否则规划如实失败。

**理由**：模型调用是 D029 规定的唯一出站需求。签名私钥与模型 key 都在任务服务进程中，出站只开到模型端点，能缩小进程被利用时的外泄面。标准库实现不新增镜像、依赖或配置语言。

**替代方案**：任务服务直接接入可出站网络（否：签名私钥所在进程获得任意出站）；引入 squid、tinyproxy 等外部镜像（否：新增需锁定的外部镜像与配置面，当前只需要 CONNECT 允许列表）；在任务服务进程内按主机过滤（否：进程被利用时过滤同样会被绕过，隔离必须在进程之外）。

**验收**：单元测试覆盖放行、拒绝目标、非 CONNECT 与超长请求头；云端核对任务服务容器仍不能直接解析或连接外部主机、经代理的非允许目标被拒；任务台实调规划成功，并完成飞行与独立裁判。

**重估触发器**：接入第二个模型厂商或视觉档；模型端点需要随环境变化；出现需要出站的其他服务组件。

## D037 · M2 证据闭环复核与统一云端飞行台（2026-09-24）

**决策**：本轮经用户授权评审、修复、合并并部署。以 D035 的 `8448` 为统一入口：`/` 为自然语言任务，`/fixed/` 为 M1 固定巡检；同一来源提供导航与资源，复用原有两条执行链和项目锁。M1 的历史 `8447` 继续作为兼容入口，不删除历史证据、不改其他 Serve 映射。

统一网页进程只增加访问 D028 私有代理 socket、只读运行/源码目录和独立证据页面缓存；M1 写操作同样要求 D033 的已识别操作者，并保留 Host / Origin / nonce 检查。M2 仍经任务服务签名、uplink、executive、guardian；M1 仍只接受固定任务，不能提交任意包。统一入口健康检查同时核对 M1 运行版本与 M2 服务版本。激活前先将 D028 代理更新至同一部署。

**证据语义**：服务收到机载成功事件但尚未取得对应影像并复核时，目标保持“不确定”，任务显示“证据同步中”；缺口或损坏的账本不能用于完成、操作绑定或自动重规划。迟到证据补齐后重新推导报告；重复证据上传不得清除已持久化的媒体引用。自动批准的原样重试必须同时保留技能绑定、超时和证据要求。

**验收**：新增回归用例先复现问题；本机 lint / 全量测试、同版本云端检查、M2 端到端场景和受影响的 M1 场景、统一入口真实 HTTPS 与浏览器验收。分别记录准确 SHA、脚本/实调规划、裁判与回放，不继承历史全量门禁。

**重估触发器**：希望把 M1 固定任务也改成 M2 签名模板，或需要并发飞行、跨任务队列取消、真机时另立决策；本轮只合并使用入口，不改机载控制权与模型边界。

## D038 · M3 算力与拓扑：SITL 线留在共享云主机，Jetson-in-the-loop 线待硬件（M3 决策待办②）

**日期**：2026-09-24 · **状态**：生效

**实测背景**：共享云主机 4 vCPU（Xeon Platinum 8255C，含 AVX-512）/ 7.7 GiB（开工时约 4.8 GiB 可用）/ 38 GiB 可用磁盘，无 GPU，同机常驻约 30 个其他项目容器，平时负载 1–2.5；binfmt 只注册了 `python3.12`，没有 qemu，不能原地构建 arm64。固定基础镜像 `px4io/px4-dev-ros2@sha256:558f…` 已带 ROS 2 Jazzy `ros-base`、`/usr/local/bin/MicroXRCEAgent` 与 colcon；PX4 v1.17.0 SITL 已编进 `uxrce_dds_client`，启动脚本默认连 `127.0.0.1:8888`。云主机能访问 `codeload.github.com`、`hf-mirror.com`、清华 PyPI / Ubuntu / ROS 镜像，不能访问 `packages.ros.org` 与 `huggingface.co`。本机为 Intel Core Ultra 5 125H / 32 GiB / Intel Arc 核显，没有 NVIDIA GPU。项目内没有 Jetson。

**决策**：

- M3 验收拆成两条线，分别留证：**M3-SITL**（云端 amd64：第二控制路径、自主层、CPU 推理、四类场景、监督周期与意图延迟测量）与 **M3-JIL**（Jetson-in-the-loop：同一 Dockerfile 的 arm64 镜像在 Jetson 上运行机载侧进程）。两条线都通过才关闭 M3。M3-SITL 通过后可作为 M4-B 联合仿真的前置；M4-A 真机仍要求 M3-JIL 通过。
- D023 的云端默认不变：M3-SITL 的仿真、ROS 2 自主层与推理都在现有共享云主机上按容器配额运行，不开 GPU 云实例。控制路径不含推理；事件检测在 CPU 上用 ONNX Runtime 运行，按 D040 的预算实测，不达标才重估 GPU。
- 容器配额是上限而不是预留：SITL 1.5 CPU / 2 GiB、真值采集 0.3、guardian 0.6、executive 0.4、XRCE agent 0.25、传感器转接 0.25、出口节点 0.25、自主层 0.6、边缘推理 0.5 CPU。深度相机 64×48 @ 5 Hz、下视 RGB 保持 160×120 @ 5 Hz、局部规划 5 Hz、事件检测 ≤ 1 Hz。计算过载注入只在被注入容器自己的配额内施加，不向共享主机外溢；构建限 `-j2`。回归沿用「小批 + 安静窗口」。
- M3-JIL 拓扑（待硬件）：推荐 Jetson Orin NX 16 GB 开发套件，备选 Orin Nano Super 8 GB，JetPack 6.x（L4T 36.x）。SITL + Gazebo 放在与 Jetson 同一局域网的工作站，机载侧（XRCE agent、guardian、executive、出口节点、自主层、边缘推理）在 Jetson 上，飞控链路走独立网段以模拟真机串口 / 以太网。这需要在工作站启动真栈，会在 JIL 线上偏离 D023，届时单独确认并在本条补记；采购、接入网络与本机真栈都需要用户操作。
- arm64：机载镜像用构建参数选择基础镜像，amd64 用现有固定基础，arm64 用 L4T 上的 ROS 2 Jazzy 基础。在 Jetson 上原生构建验证；或经用户批准，在云主机注册 qemu binfmt 后交叉构建（系统配置变更，按红线另批）。未验证前只算「构建定义就绪」，不写成已构建。

**理由**：M3-SITL 的判据（单一出口、CBF、四类场景、周期 p99）不需要 GPU，也不需要 arm64；把硬件依赖集中到 JIL 线，能让可验证的部分先完成且证据不混淆。「arm64 镜像能跑」不等于真机就绪，「SITL 通过」也不等于 JIL 通过。

**替代方案**：GPU 云实例（否：M3-SITL 用不上，还要新费用与新凭据）；把 M3-SITL 搬到本机 WSL2（否：违背 D023 的用户要求，也脱离既有部署、隔离与回执流程）；等 Jetson 到位再开始整个 M3（否：大部分工作与硬件无关）。

**重估触发器**：M3-SITL 在配额内实测监督周期或推理延迟不达标；共享主机负载使批次反复作废；Jetson 到货；需要机载运行生成式 VLM（≥ 1B）。

## D039 · 外部模式出口节点是 guardian 出口的延伸；`drone.autonomy.v1` 本地自主层协议（M3 决策待办①）

**日期**：2026-09-24 · **状态**：生效

**PX4 v1.17.0 源码核实**（`src/modules/commander`）：外部模式经 `register_ext_component_request` 注册后获得 `NAVIGATION_STATE_EXTERNAL1..8`（custom mode：main 4 / sub 11–18）。PX4 每 300 ms 发一次 `arming_check_request`；模式连续漏答超过 3 次即标为 unresponsive，置 `mode_req_other` → 该模式不能运行 → failsafe 回退（`checkModeFallback` 最终落到 RTL）。注销正在使用的模式时，用户意图改为 `AUTO_LOITER`（Hold）。多旋翼 `GotoControl` 的 goto 设定值 500 ms 超时，超时后位置控制器改用失效保护设定值（水平速度归零并以降落速度下降）。因此「停止发布」本身不会让 PX4 退出外部模式，只会在 500 ms 后进入下降；出口节点必须主动退出，不能靠重发旧设定值维持回路。

**决策**：

- 出口节点 `ros2_ws/src/da_egress_ext`（C++，基于 `px4_ros2_cpp` release/1.17 固定提交）只注册一个外部模式 `DroneAgent Local`：不替换任何内部模式、`preventArming(true)`、不注册模式执行器、从不调用 `deferFailsafes`（源码扫描契约测试 + 裁判读取 ULog 的 `config_overrides`）。它没有任务语义，只做两件事：有新鲜授权就把设定值转发给 PX4；授权过期就停。
- 设定值只接受来自 guardian 的 `AuthorizedSetpoint`：带机器人、任务版本、步骤、`lease_epoch`、`command_seq`、`issued_at` 与 `ttl_ms`（guardian 取 300 ms，节点硬上限 500 ms）。低于已见最高代次的一律拒绝；同代次序号必须递增；按节点单调时钟计算的剩余有效期与按墙钟计算的年龄都要在 TTL 内；数值必须有限且在速度上限内。设定值类型固定为多旋翼 goto（NED 位置 + 水平 / 垂直限速），不暴露速度、姿态、推力或执行器接口。
- 只在模式被 PX4 激活时发布（`updateSetpoint`，20 Hz），并且只发布仍在 TTL 内的最新授权。授权在模式激活期间失效时，当周期立即停止发布，并发出唯一允许的一条命令 `DO_SET_MODE → AUTO_LOITER`（PX4 Hold），记录 `watchdog_exit`；1 s 后仍未离开外部模式，就在 arming check 中报告不可运行，交给 PX4 原生失效保护。节点不能解锁、起飞、降落或切换到其他模式。节点崩溃或 DDS 断开时，由 PX4 自己按 unresponsive 回退。
- 模式切换由 guardian 决定：guardian 先下发「原地悬停」授权，再经 MAVLink `DO_SET_MODE`（custom mode 取节点上报且经范围校验的 nav_state）进入外部模式，并以 `CURRENT_MODE` 确认；外部模式期间的期望模式集合是 `{EXTERNALk, HOLD}`。正常退出与任何恢复都是先停止授权，再经 MAVLink 下发 hold / rtl / 降落。
- 两条链路互斥：MAVLink 的航线、起飞、降落等飞行写入只在外部模式之外；DDS 设定值只在外部模式激活且授权新鲜时发布；两者的交接点只有上述模式切换。guardian 记录每条 MAVLink 写入与每条授权，节点记录每次发布、拒绝与看门狗动作；裁判用 ULog 的 `vehicle_status` 与 `vehicle_command` 核对互斥与模式迁移。
- 能力协商：只有节点已注册、最近 500 ms 内有状态、PX4 消息兼容性检查通过时，适配器才在实际能力中声明 `external_mode`；否则缺席，编译器改用航线版本或拒绝。
- `drone.autonomy.v1`（`proto/drone/autonomy/v1/autonomy.proto`）只用于本机进程之间，不进入车队协议：`LocalTask`（guardian → 规划节点：目标、范围、限速、截止时间）、`TrajectorySegment`（规划节点 → guardian：候选轨迹片段、来源与实现阶段、`valid_until`、状态）、`ObstacleSet`（局部地图 → guardian：邻近障碍点与不确定性）、`LocalizationReport`（定位健康节点 → guardian）、`AuthorizedSetpoint`（guardian → 出口节点）、`EgressStatus`（出口节点 → guardian）、`BeliefFact`（感知 / 边缘推理 → executive）。传输为私有 Unix 流套接字上 4 字节大端长度前缀的 `AutonomyFrame`，单帧上限 64 KiB；按角色分目录（出口、自主层、信念各一个套接字，只挂载给对应容器），连接时检查 `SO_PEERCRED` 的 UID。ROS 2 一侧用系统 protoc 3.21 生成的绑定（与系统 protobuf 4.21 同版本），本包用 grpcio-tools 生成的绑定；线路格式一致。M3-SITL 验收前为草案，通过后写入 wire 锁。
- 自主层节点与学习型插件只提交候选；影子阶段的输出只进记录，不连 guardian 的套接字。真值不进入自主层：Gazebo 传输限于仿真网络命名空间的回环，只有固定清单内的相机话题被转成 ROS 2 图像；自主层容器不挂载真值目录。

**理由**：PX4 的外部模式本身有注册、健康检查与原生回退，比直接发 Offboard 设定值更符合「飞控优先」；用 PX4 自己的 Hold 作为授权过期后的唯一动作，既不重发旧设定值，也不在注销 / 重新注册上引入空窗。按角色拆分套接字，使规划节点无法冒充出口节点提交设定值。

**替代方案**：Offboard 话题直发（否：没有模式注册与健康检查，失效行为依赖 `COM_OBL_RC_ACT`）；授权过期只停止发布（否：500 ms 后 PX4 进入下降，且外部模式不退出）；授权过期即注销模式（否：注销后要重新注册，而阻塞式注册不能在回调里完成，能力会出现空窗）；guardian 直接加入 ROS 2（否：违背 D014）；gRPC over UDS（否：C++ 与系统 Python 两侧都要引入 gRPC，长度前缀帧足够）。

**重估触发器**：px4_ros2 或 PX4 升级改变注册 / 健康检查语义；需要速度或姿态级接口；真机串口 / 以太网链路（M4-A）；第二台机器人。

## D040 · 安全监督周期与意图延迟预算；测量方法（M3 决策待办③，数值部分）

**日期**：2026-09-24 · **状态**：生效（guardian 语言结论在测量完成后补记于本条）

**决策**：

- guardian 监督周期 10 Hz。每个 M3 场景在其自身负载下报告周期分布：p99 ≤ 120 ms、最大 ≤ 200 ms（M1 空载参照 p99 102.9 ms）。guardian 自检：最近 2 s 窗口内超过 20% 的周期大于 150 ms，或任一周期大于 250 ms，即视为自身健康退化，触发 `compute_overloaded`。
- 意图延迟：规划片段生成（`created_at`）到出口节点发布对应设定值，p99 ≤ 250 ms；guardian 转发的授权 TTL 300 ms；规划片段 `valid_until` 为生成后 400 ms；局部地图 `ObstacleSet` 有效期 500 ms；定位健康报告有效期 500 ms；出口节点状态 5 Hz、500 ms 视为失联。
- 事件检测：单帧推理 p99 ≤ 1 s，非阻塞，控制路径不依赖其输出。
- 测量方法：SITL 上三档负载（空载 = M1 同款任务；感知满载 = 深度 + 局部规划 + 定位健康；推理满载 = 再加事件检测连续推理）× 两种隔离（guardian 独立 cgroup 配额；guardian 与自主层同一 cgroup 共享配额）。每档 3 个种子，报告周期、意图延迟、推理延迟分布与 CPU / 内存占用。计算过载场景把 CPU 竞争进程放在自主层容器内，验证 guardian 周期不因之越过预算。JIL 线用同一方法在 Jetson 上重测。
- guardian 语言结论（D003 触发器）只依据上述测量：SITL 与 JIL 都满足预算则维持 Python；任一线不满足，先看 CPU 隔离能否恢复预算，仍不满足才按 D003 用 C++/Rust 重写（`drone.control.v1` 接口不变，先冻结接口测试）。M3-SITL 的结论先补记；JIL 数据到位后再补记一次。

**理由**：预算要在最坏负载下测，否则 10 Hz 只是空载性质；把「guardian 自身退化」定义为可观测的周期统计，才能成为恢复策略的触发条件。

**替代方案**：只测空载（否：不能回答 D003）；用平均周期（否：尾延迟才是安全相关量）。

**重估触发器**：任一场景越过预算；JIL 数据与 SITL 差异显著；guardian 增加新的周期性工作。

## D041 · 仿真传感器与感知 / 事件检测模型（M3 决策待办④）

**日期**：2026-09-24 · **状态**：生效

**决策**：

- 机体：M3 仿真用 PX4 的 `x500_vision` 机体（外部视觉里程计由 Gazebo 里程计插件加噪声模拟），在其上保留 M1 的下视 RGB `cam_0`，另加前视深度相机 `depth_0`（64×48、5 Hz、水平视场 1.5 rad、量程 0.3–12 m）。PX4 v1.17 的 `4005_gz_x500_vision` 不开启视觉融合（`EKF2_EV_CTRL` 默认 0），M3 仿真镜像另设机架文件，只额外设置 `EKF2_EV_CTRL`（水平位置 + 三维速度）与仿真专用的 `SYS_FAILURE_EN`（供 PX4 自带 `failure gps off` 注入 GNSS 失效），不改动任何失效保护参数。仿真视觉里程计与仿真 GNSS 属同一类传感器模型；agent 只看到 PX4 估计器的输出与相机图像，看不到 Gazebo 位姿。
- 世界：M3 场景 `m3_campus_v3` 在 M2 世界上增加登记的静态障碍（墙体与立柱）、北侧资产 `asset_green` 与备用降落点 `site_b`；航线版本保留绕障的预验证航线，供外部模式不可用时回退。
- 局部几何：深度 → 占据栅格 → ESDF（`scipy.ndimage` 欧氏距离变换）→ 规划评分与给 guardian 的邻近障碍点，全部确定性，不含学习模型。
- 资产检测：仿真世界里的资产是颜色标记，COCO 类检测器在这里没有信号。M3-SITL 用确定性的颜色特征 + 深度测距检测，输出带协方差（由深度噪声、位姿不确定性与时间对齐误差合成）、置信度与有效期的 `WorldFact(belief)`；缺协方差即拒绝入图。YOLO 级学习检测器在 M4-A 有真实相机数据后选型，不在 SITL 里用假信号代替。
- 事件检测（WP-M3-15）：CLIP ViT-B/32 级对比式视觉语言模型的视觉编码器 ONNX（量化版），ONNX Runtime CPU；文本提示的嵌入在构建时预先计算，运行时只跑视觉编码器，对固定提示集做零样本「事件相关性」分类，输出 `WorldFact(candidate, source=model)`，带模型文件 SHA-256。模型文件经 `hf-mirror.com` 按固定哈希下载。它不是生成式 VLM；≤ 8B 生成式 VLM 的机载评估放在 M3-JIL（TensorRT）。

**理由**：感知在 M3 的安全相关用途是局部几何（避障与 CBF），它必须确定、可复算；学习模型的价值与负载由事件检测承担，并且只产生候选事实。用真实但不相关的检测器制造输出，只会产生无法评测的数字。

**替代方案**：在 SITL 中跑 COCO YOLO（否：没有对应目标，输出无意义）；引入 `ros_gz_bridge` 及 Gazebo vendor 包（否：镜像多出数百 MB，只为转两路图像；改用系统 Python 已有的 gz-transport 与 rclpy 写一个固定清单的转接节点）；生成式 VLM 在云 CPU 上运行（否：达不到 1 s 预算）。

**重估触发器**：M4-A 真实相机数据到位；事件检测延迟或准确率不达标；需要双目或激光雷达。

## D042 · M3 安全语义增量：恢复策略 v2、CBF 约束过滤、能源可达性、任务卡住与计算过载

**日期**：2026-09-24 · **状态**：生效

**决策**：

- 策略按任务包的 `recovery_policy_ref` 加载：M1 / M2 场景仍是 `multirotor_m1@v1`（`mission_upload` 模式的生产策略，上下文计算方式不变）；M3 场景为 `multirotor_m3@v2`（`configs/recovery_policies/multirotor_m3_v2.yaml`）。v2 在 v1 的基础上新增上下文键 `control_mode`、`visual_localization_ok`、`nearest_site_reachable`，并新增触发条件 `trajectory_stale`（外部模式下没有有效规划片段）、`progress_stalled`、`compute_overloaded`、`autonomy_unavailable`。
- v2 的新边：外部模式下观测过期或轨迹过期 → hold，10 s 后 rtl；`progress_stalled` → hold，10 s 后 rtl；`compute_overloaded` → hold，10 s 后 rtl；`autonomy_unavailable` → hold，10 s 后 rtl；GNSS 失效但视觉定位可用 → hold，10 s 后原地降落（没有全局位置时 PX4 不能执行 RTL）；低电且返航不可达、备用降落点可达 → `land_at(site)`。继承自 v1 的边内容不变；`mission_upload` 上下文下 v2 与 v1 的选边结果相同（单元测试钉住）。
- 恢复动作先于一切：任何干预先停止出口授权，再下发 MAVLink 恢复命令。
- 观测新鲜（外部模式）：飞控位姿之外，还要求局部地图 `ObstacleSet` 新鲜；地图过期视同观测过期。轨迹新鲜：最新规划片段未过 `valid_until`，且属于当前任务、代次与坐标系。
- 任务卡住：guardian 独立判定，不依赖规划节点自报——外部模式下 10 s 内到目标的直线距离减少不足 0.5 m（已在目标容差内除外），或规划节点连续 3 s 报告无路可走。新鲜但不前进的片段因此不能掩盖卡住（MAVSDK 自动重发陷阱在自主层的对应物）。
- 计算过载：guardian 自身周期统计越限（D040），或规划片段自报周期越限（连续两个周期超过预算 1.25 倍）。只有收到带越限标记的新鲜片段才算过载；完全收不到片段是轨迹过期，两者不混淆。
- 自主层不可用：外部模式下出口节点状态或定位健康报告失联，或出口节点报告 PX4 链路丢失。
- CBF 约束过滤（`guardian/constraint_filter.py`）：对每条授权前的目标，以离散时间控制屏障函数把期望速度投影到安全集合：约束包括登记围栏的六个面、登记障碍（按半径膨胀）与新鲜局部地图的邻近障碍点，条件 `n·u ≥ -α·h`、`|u| ≤ v_max`；不可行（已在不安全集合内或约束冲突）即拒绝并触发 `trajectory_stale` 的恢复路径，不发布任何设定值。M1 的包络硬检查与围栏前瞻保留为下界。过滤耗时计入监督周期。
- 能源可达性（`guardian/energy.py`，仅 v2）：按本次飞行实测的放电率（滑动窗口拟合，缺样本时用技能清单的保守上界）、到 home 与各降落点的距离、限速、风（未知即按场景登记的阵风上限）与余量，给出 `rtl_reachable` / `nearest_site_reachable` 及其区间；任一输入未知，对应项为假（保守边）。

**理由**：外部模式带来了 v1 不存在的失效方式——设定值链路本身卡住、自主层算力不足、局部地图过期——它们必须有各自可注入、可复现的触发条件；M1 已验证的 `mission_upload` 语义不能被新模型悄悄改变，所以按策略版本分开计算上下文。

**替代方案**：把新触发条件并入 v1（否：改变已验证 M1 边的上下文）；由规划节点自报是否卡住（否：被卡住的一方不能证明自己没卡住）；CBF 只用登记障碍（否：看不到未登记障碍）；CBF 只用局部地图（否：地图过期时没有下界）。

**验收**：v2 每条新边至少一个注入场景 × 3 个种子，并生成绑定边内容哈希、场景、软件版本与证据的记录；继承边的记录来自同一版本上的 M1 完整回归；`unverified_edges()` 为空才能称 v2 已验证。
