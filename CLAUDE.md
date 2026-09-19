# CLAUDE.md — drone-agent

面向空地异构机器人的安全约束任务运行时，以无人机为首要本体：自然语言目标 → 类型化任务 → 本地约束内执行 → 按证据确认结果 → 空地任务交接。架构单一事实来源是 `docs/architecture/`（入口 `00-overview.md`），决策记录见 `docs/decisions.md`（只增不删），阶段与退出标准见 `docs/roadmap.md`。姊妹项目：`../embodied-agent`（机械臂，本仓库的模板与主要复用来源）、`../car-agent`（座舱，语音 / 权限 / 账本参考）。

## 当前阶段

**M0 进行中（2026-09-19 起）**：规范与架构文档、六类契约（Pydantic）与契约测试、复用清单。M0 退出标准见 `docs/roadmap.md`。下一步 M1：无大模型的 PX4 SITL 单机安全闭环（executive + guardian + PX4 适配器 + 故障注入 + 独立裁判）。任何执行 / 安全语义的改动先改 `docs/architecture/02-contracts.md` 与 `03-safety.md`，再改代码。

## 目录结构

完整定义与理由见 `docs/architecture/07-deployment.md` 与各文档；本节只列约定：

- `docs/` — `architecture/`（00–08 分主题，`00-overview.md` 是入口与文档地图）、`decisions.md`、`roadmap.md`、`reuse-from-embodied-agent.md`、`research/`（前沿调研、GPT-6 Pro 评估原文与摘要）。架构级变更**先改文档 + `decisions.md` 增条目，再动代码**。
- `src/drone_agent/` — 单包多子模块。**模块随里程碑创建，不预建空模块。** M0 只有 `contracts/`。规划中的子模块与里程碑：`contracts`(M0) · `runtime`(M1：obs、ledger、ipc) · `guardian`(M1：安全监督、约束过滤、恢复策略、控制出口) · `adapters`(M1：`px4_mavsdk`、`sim`) · `mission`(M1：executive、skills) · `eval`(M1：裁判、场景、故障注入) · `providers`(M2，移植) · `planner`(M2) · `admission`(M2) · `fleet`(M4) · `autonomy`(M3，ROS 2 节点放 `ros2_ws/`，不进本包)。
- `proto/` — 进程间契约（executive ↔ guardian；车队协议），proto 先行；M1 建立，包名 `drone.<service>.v1`。
- `configs/` — `platforms/`（版本锁定与能力描述）、`recovery_policies/`（恢复策略图）、`scenarios/`（评测场景）、`planner_tools.yaml`（规划层工具白名单，只读）。
- `tests/` — 镜像 `src/`；`tests/contracts/` 是契约测试，`tests/fault_injection/`（M1）是故障注入测试。
- `eval/` — 版本化评测任务与 `BASELINES.md`（只增不改，负结果照记）。
- `sim/` — docker compose、Gazebo 世界与机体（M1）。
- `scripts/` — 一次性可复用脚本；`experiments/` — 一次性实验，禁止被 `src/` 依赖，30 天未引用可清理（删除仍先按全局红线问）。
- 新增顶层目录属于架构变更，走文档先行流程。

## 命名

- 目录与 Python 模块 `snake_case`；文档 `kebab-case.md`；技能 `skill.<domain>.<action>`（如 `skill.flight.takeoff`、`skill.inspect.asset`、`skill.ground.recheck`）；资源 `<robot_id>.<resource>`（如 `uav_01.motion`）；proto 包 `drone.<service>.v1`；所有契约对象带 `schema_version`。
- 从姊妹项目移植的文件，文件头必须标注：`# Ported from embodied-agent <path> @ <commit>, changes: <summary>`（car-agent 同理）。

## 语言与风格

- 文档中文；代码、标识符、注释、commit message 英文；commit 用 Conventional Commits（`feat:`/`fix:`/`docs:`/`refactor:`/`test:`/`chore:`）。
- **例外**：移植文件保留原有中文注释（承载事故复盘与设计动机）；移植文件中新增 / 改写的注释与全部新写代码一律英文。
- 术语纪律（契约测试 `test_no_forbidden_terms` 扫描 `src/`）：机械臂语义 `qpos`/`qvel`/`gripper`/`ee_pose`/`joint_targets` 与座舱语义 `cockpit`/`cabin`/`座舱` 不得出现在本仓库代码中。`vehicle` 不禁（PX4 / MAVLink 用它指飞行器本身，如 `VehicleStatus`），但不用它表达座舱语义。本仓库的领域词：`aerial`/`ground`/`robot`/`platform`/`mission`/`skill`/`lease`/`evidence`/`guardian`/`executive`。

## 验证

- 代码改动后必须绿：`uv run ruff check .` + `uv run pytest -q`。
- 契约测试（`tests/contracts/`）不许跳过、注释或放宽断言；它们是「模型不能触达控制出口」「UNKNOWN 不是成功」「旧代次命令被拒」等红线的可执行形式。
- 故障注入（M1 起）：恢复策略图的每条边至少对应一个注入场景；未经验证的边不能进入 `configs/recovery_policies/` 的生产配置。
- 评测驱动（M1 起）：任何执行 / 安全 / 策略变更以固定场景集 + 多随机种子的三分类结果说话；基线只能被数据推翻。
- proto 变更后运行生成脚本确认编译通过；生成物在 `gen/`，不进 git。
- 文档改动后自查相对链接有效、`roadmap.md` 阶段状态与实际一致。

## 已知环境约束

- **国内网络**：uv 拉包用每次命令的环境变量走镜像，不写入任何全局配置：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`。
- **仓库路径含中文**（`产品/`）：原生库（MAVSDK 原生绑定、Gazebo 资产、ONNX Runtime）加载前需复制到 ASCII 路径；Linux 侧（WSL2 / Docker）把仓库放在 ASCII 路径下再挂载。
- **开发机是 Windows**：PX4 SITL、Gazebo、ROS 2 只在 Linux 容器或 WSL2 中运行；契约、规划、准入等纯 Python 代码在 Windows 直接测试。
- **ROS 2 节点用系统 Python 3.12**（rclpy 绑定），不进 uv 虚拟环境；本包与 ROS 2 节点只通过 proto / IPC 交互，`src/drone_agent/` 不 import `rclpy`。

## 工程红线（继承全局 CLAUDE.md 与 embodied-agent D009，另加飞行条款；只加严不放松）

- **模型边界**：LLM/VLM 只产出 `MissionSpec` 草案与带置信度的业务判断；永不触达控制出口、不生成代码、不决定恢复动作。MCP 只接只读工具；A2A 只传任务请求与进度。
- **单一控制出口**：只有 `guardian` 进程持有飞控连接；任何控制写入携带 `lease_epoch` 与 `command_seq`；旧代次一律拒绝；超时后先对账再重发。
- **证据先于成功**：`execution_status` / `effect_verdict` / `safety_verdict` 三元分离；`UNKNOWN` 永远不是成功；依赖步骤只在 `effect_verdict = verified` 后放行；报告中「已完成」只接受 `succeeded ∧ verified`。
- **飞控优先**：不禁用飞控原生失效保护；不使用 PX4 失效保护延期；executive / guardian 不提供空中断电（kill）接口；RC / GCS 接管永远可用。
- **真机纪律（M4 起）**：RC 接管在手、围栏与返航点已验证、首飞降速、空域已按 UOM 报备、无关人员不在场；任何上真机的代码先过 SITL 故障注入与 guardian 覆盖，并通过共因故障清单（伴飞计算机断电、串口拔出、RC 接管、飞控失效保护）。
- **密钥**：API key 不进代码 / commit / 日志；**严禁复制 car-agent 的 `.env`**，只参考 `.env.example` 变量名。
- **依赖**：PX4 / ROS 2 / MAVSDK / Gazebo / Nav2 的大版本升级属于架构级变更，先在 `decisions.md` 记录再升级；版本锁定在 `configs/platforms/`。

## 节奏机制

- 季度技术雷达（1/4/7/10 月）：复查 `docs/research/frontier-2026.md` 的判断与 `decisions.md` 各条的重估触发器，纪要存入 `docs/research/`。
- 评测驱动（M1 起）：策略、模型与安全配置变更以版本化评测集数字说话。
