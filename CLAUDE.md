# CLAUDE.md — drone-agent

面向空地异构机器人的安全约束任务运行时，以无人机为首要本体：自然语言目标 → 类型化任务 → 本地约束内执行 → 按证据确认结果 → 空地任务交接。架构单一事实来源是 `docs/architecture/`（入口 `00-overview.md`），决策记录见 `docs/decisions.md`（只增不删），阶段与退出标准见 `docs/roadmap.md`。姊妹项目：`../embodied-agent`（机械臂，本仓库的模板与主要复用来源）、`../car-agent`（座舱，语音 / 权限 / 账本参考）。

## 当前阶段

**M1 已完成（2026-09-20，SITL 范围）**。原里程碑版本、66 组完整矩阵与 400 项测试证据以 `docs/m1-readiness.md` 为准；D027 实时仿真入口补遗的当前部署、测试与版本边界见 `docs/live-console-readiness.md`。下一阶段按 `docs/roadmap.md` 的 M2 推进，M2–M4 的批次、工作包与验收判据见 `docs/m2-implementation.md`、`docs/m3-implementation.md`、`docs/m4-implementation.md`（D026）；每份计划的「决策待办」未补进 `docs/decisions.md` 前不写对应代码。任何执行 / 安全语义的改动先改 `docs/architecture/02-contracts.md` 与 `03-safety.md`，再改代码。

## 目录结构

完整定义与理由见 `docs/architecture/07-deployment.md` 与各文档；本节只列约定：

- `docs/` — `architecture/`（00–08 分主题，`00-overview.md` 是入口与文档地图）、`decisions.md`、`roadmap.md`、`reuse-from-embodied-agent.md`、`research/`（前沿调研、GPT-6 Pro 评估原文与摘要）。架构级变更**先改文档 + `decisions.md` 增条目，再动代码**。
- `src/drone_agent/` — 单包多子模块。**模块随里程碑创建，不预建空模块。** 已有 `contracts`(M0)、`runtime`(日志/IPC/MCAP)、`guardian`(监督/恢复/出口)、`adapters`(PX4)、`mission`(执行/登记表/验证)、`eval`(裁判/场景/注入)、`console`(D027 的 M1 固定仿真入口，M2 扩展规划/审批)。后续 `providers`/`planner`/`admission`(M2)、`fleet`(M2 起：业务账本、目录、传输、直通协调器、A2A；M4：协调器四项能力与交接)、`autonomy`(M3；ROS 2 节点在 `ros2_ws/`，不进本包)。
- `proto/` — 进程间契约（executive ↔ guardian；车队协议）与共享消息；M0 骨架，M1 冻结，包名 `drone.<service>.v1`。字段清单与共享 proto 由 `scripts/generate_contract_fields.py` 导出；禁止旧字段重编号。
- `configs/` — `platforms/`（版本锁定与目标能力描述）、`skills/`（技能草案）、`recovery_policies/`（恢复策略图）、`scenarios/`（注入矩阵与后续评测场景）、`planner_tools.yaml`（规划层工具白名单，只读）。草案中的场景名不代表已验证。
- `tests/` — 镜像 `src/`；`tests/contracts/` 是契约测试，`tests/fault_injection/`（M1）是故障注入测试；`tests/admission/` 与 `tests/planner/`（M2）承载对抗性规划测试，语料版本化在 `eval/adversarial/`。
- `eval/` — 版本化评测任务、`adversarial/`（M2 起的对抗性规划语料）与 `BASELINES.md`（只增不改，负结果照记）。
- `sim/` — M0 开发 compose 与只读冒烟；M1 扩展完整仿真。Windows 先用 `scripts/stage_sim.py` 暂存到 ASCII 目录，版本与 digest 固定，见 `sim/README.md`。
- `scripts/` — 一次性可复用脚本；`experiments/` — 一次性实验，禁止被 `src/` 依赖，30 天未引用可清理（删除仍先按全局红线问）。
- 新增顶层目录属于架构变更，走文档先行流程。

## 命名

- 目录与 Python 模块 `snake_case`；文档 `kebab-case.md`；技能 `skill.<domain>.<action>`（如 `skill.flight.takeoff`、`skill.inspect.asset`、`skill.ground.recheck`）；资源 `<robot_id>.<resource>`（如 `uav_01.motion`）；proto 包 `drone.<service>.v1`；所有契约对象带 `schema_version`。
- 从姊妹项目移植的文件，文件头必须标注：`# Ported from embodied-agent <path> @ <commit>, changes: <summary>`（car-agent 同理）。

## 语言与风格

- 文档中文；README 中英双版：`README.md` 英文（GitHub 默认入口）、`README.zh-CN.md` 中文，顶部互链，内容同步更新。
- 代码标识符与 commit message 英文；commit 用 Conventional Commits（`feat:`/`fix:`/`docs:`/`refactor:`/`test:`/`chore:`）。
- **注释与 docstring 中英双语**（D019）：docstring 英文段在前、中文段在后，同一 docstring 内以空行分隔；行内注释写成 `# English / 中文`；Pydantic `Field(description=...)` 同样 `"English / 中文"`。两种语言表达同一含义，不允许只写一种；改注释时两种语言一起改。
- **例外**：移植文件保留原有中文注释（承载事故复盘与设计动机），并补上英文；移植文件中新增 / 改写的注释同样双语。
- 术语纪律（契约测试 `test_no_forbidden_terms` 扫描 `src/`）：机械臂语义 `qpos`/`qvel`/`gripper`/`ee_pose`/`joint_targets` 与座舱语义 `cockpit`/`cabin`/`座舱` 不得出现在本仓库代码中。`vehicle` 不禁（PX4 / MAVLink 用它指飞行器本身，如 `VehicleStatus`），但不用它表达座舱语义。本仓库的领域词：`aerial`/`ground`/`robot`/`platform`/`mission`/`skill`/`lease`/`evidence`/`guardian`/`executive`。

## 验证

- 代码改动后必须绿：`uv run ruff check .` + `uv run pytest -q`。
- 契约测试（`tests/contracts/`）不许跳过、注释或放宽断言；它们是「模型不能触达控制出口」「UNKNOWN 不是成功」「旧代次命令被拒」等红线的可执行形式。
- 故障注入（M1 起）：恢复策略图的每条边至少对应一个注入场景；未经验证的边不能进入 `configs/recovery_policies/` 的生产配置。
- 评测驱动（M1 起）：任何执行 / 安全 / 策略变更以固定场景集 + 多随机种子的三分类结果说话；基线只能被数据推翻。
- proto 变更后运行 `uv run python scripts/generate_contract_fields.py --check` 与 `uv run python scripts/generate_proto.py` 确认一致性和编译；stub 在 `gen/`，不进 git。
- 文档改动后自查相对链接有效、`roadmap.md` 阶段状态与实际一致。
- 人工核对（M1 补遗）：`uv run python scripts/dev_stack.py runs` 列出云端运行，`fetch --run <run> --cases <a-7,b-19> --apply` 拉取并核对摘要后自动生成 `viewer.html`；本地目录用 `uv run python -m drone_agent.eval.viewer <目录>`。页面只展示与复算，不产生判定，不能替代 `judge/` 与发布门禁；扩展只加提取器，见 `docs/architecture/08-evaluation.md` §7。
- 实时体验（D027）：`uv run python scripts/dev_stack.py console`，浏览器访问 `http://127.0.0.1:8768`，操作云端固定 M1 仿真；只允许开始、暂停、恢复、取消，走 executive 通道，不能直达飞控。见 [实时指南](docs/live-simulation.md)。这不是完整 M2 控制台；新提交验证不能转借历史 M1 的 66/66。

## 已知环境约束

- **默认云端联调**（D023，用户明确要求）：Linux 构建、PX4/Gazebo 与服务联调走 `uv run python scripts/dev_stack.py`；本机用于编辑和快速单测，不自动启动本地真栈。连接失败应明确报错；云端操作指南见 `docs/cloud-development.md`。当前只部署仿真/验证工作区，真机 guardian 与飞控连接仍在设备侧。
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
