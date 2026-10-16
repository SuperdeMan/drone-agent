# CLAUDE.md — drone-agent

建设无人机巡检运营平台，以安全约束任务运行时为执行底座：业务目标 → 持久工作流 → 资源调度 → 受约束任务 → 本地执行与证据 → 复核 / 工单 / 复检。P0–P5 已完成软件 / SITL 范围验证；P6 仍在推进，硬件验证另行开展。架构入口 `docs/architecture/00-overview.md`，运营设计 `09-operations.md`；决策记录只增不删，阶段状态见 `docs/roadmap.md`。复用 `../embodied-agent` / `../car-agent` 前读指定复用清单，不建立运行时依赖。

## 当前阶段

- M0–M2 已完成，M3-SITL 已通过；原 M3 未关闭，Jetson-in-the-loop 由 H1 承接。P0–P5 已完成软件 / SITL 范围验证，厂商机场只验证到协议模拟器。准确提交、负记录、门禁与历史成绩见 [路线图](docs/roadmap.md)及各 readiness；不得把历史成绩转借给新提交。
- P6 的 WP-P6-02–07 已按各自范围准出；航段走廊与解释核对 v2 按预登记规则不采用。WP-P6-01 已完成素材、标注与 S2 工具准备；正式业务清单和真实评测尚未完成，P6 未整体准出；接手见 [P6 素材交接](docs/p6-materials-handoff-2026-10-16.md)。X1 按已批准方案（D088）推进，具体状态见路线图。
- 常驻任务台配置以 `desk-cloud --status` 和实际回执为准；最近已验证的工作流 / 业务目录为 `p6_desk_v1`，运营 / 调度目录为 `p5_desk_v1`，视觉画像 change-v3、规划提示 planner-v3、解释核对 v1。统一入口为 8448（`/` 任务台，`/fixed/` M1）；激活按 console-cloud 再 desk-cloud 并核对两条版本。M3 外部模式尚未接入常驻任务台。
- 账本的运营 schema v2 与 workflow / scheduling / business / capacity / asset 子版本各自独立；详细表与迁移见 [运营架构](docs/architecture/09-operations.md)、[运维指南](docs/operations-guide.md)和相应设计。任何新增持久化表仍属于 schema 变更，须先设计并获批准。
- 执行 / 安全语义变更先改契约与安全文档，再改代码。工作流不直接控制飞行，模板审批不替代逐次飞行批准；取消阻断后续派遣，失联占用先对账，模型业务发现不升级飞行判定（D050–D053）。

## 目录结构

完整模块与部署定义见 [架构文档地图](docs/architecture/00-overview.md)及 [部署架构](docs/architecture/07-deployment.md)。模块随里程碑创建，不预建空模块；新增顶层目录或架构变更先改文档并追加决策。

- `docs/`：`architecture/`（00–09）、`decisions.md`、`roadmap.md`、复用清单、验收记录与 `research/`；决策只增不删。
- `src/drone_agent/`：单包多模块，领域模型在 `contracts`，机载执行在 `mission` / `guardian` / `adapters` / `runtime`，规划在 `providers` / `planner` / `admission`，运营在 `fleet`，页面在 `console`，局部自主协议在 `autonomy`，评测在 `eval`。机载模块及 `uplink` 不得传递导入规划或模型模块，契约测试钉住边界。
- `ros2_ws/`：ROS 2 Jazzy 的定位、导航、感知、事件检测与 C++ 外部模式出口；节点不导入 `drone_agent`，只经 `proto/drone/autonomy/v1` 本地套接字帧交互，不触真值，出口只转发。
- `proto/`：进程契约与共享消息；包名 `drone.<service>.v1`，禁止旧字段重编号。字段清单与共享 proto 由 `scripts/generate_contract_fields.py` 导出。
- `configs/`：版本与能力锁定在 `platforms/`，技能在 `skills/`，恢复策略在 `recovery_policies/`，场景在 `scenarios/`，画像在 `perception/` / `analysis/`；运营 / 工作流 / 调度 / 厂商 / 长稳分别在 `sites/` / `workflows/` / `scheduling/` / `vendors/` / `soak/`。真实成员只放云端 secrets；`planner_tools.yaml` 只读白名单。草案场景名不代表已验证。
- `tests/`：镜像源码；`contracts/` 验证边界，`fault_injection/` 验证恢复，规划对抗语料在 `eval/adversarial/`。
- `eval/`：版本化评测任务与对抗语料；`s2/` 保存冻结清单、协议、署名及开发候选。图片不进 Git，默认数据根为 `D:/drone-agent-data/`。`BASELINES.md` 只增不改，负结果照记。
- `sim/`：仿真，Windows 用 `scripts/stage_sim.py` 暂存到 ASCII 路径；`scripts/`：复用脚本；`experiments/`：一次性实验，禁止被 `src/` 依赖，30 天未引用可按全局删除红线申请清理。
- `.github/workflows/`：D077 持续检查在 main 推送与 PR 上按 `uv.lock` 跑 Ruff、契约字段、全量 pytest 与客户端测试；只读、无密钥、不触云端，不替代发布门禁。不开分支保护，改工作流仍须先获批准。

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
- 人工核对：`dev_stack.py runs` 列运行，`fetch --run <run> --cases <a-7,b-19> --apply` 拉取后生成 viewer；本地用 `python -m drone_agent.eval.viewer <目录>`。页面仅展示和复算，不替代独立裁判或门禁（[评测架构](docs/architecture/08-evaluation.md) §7）。
- 同一提交的准出只看对应 `scripts/verify_*.py` 与证据清单；门禁要求的 S0 当场重跑，云端检查、S1、回归、任务台回执须绑定候选。脚本模型只验证链路，未验证的硬件能力不能宣称。

| 工作 | 入口与约束 |
|---|---|
| M1 / M2 | `dev_stack.py console` 只操作云端固定 M1；`dev_stack.py m2 --scenario all --seeds 7,19,41` 跑 M2，`--planner live` 才是实调。本机 `python -m drone_agent.console.mission --local` 只规划与签名、不飞。门禁 `verify_m2_release.py`；见 [M2 记录](docs/m2-readiness.md)。 |
| 常驻入口 | `console-cloud` / `desk-cloud` 默认只读计划，`--apply` 激活本项目容器 / unit / Serve，`--status` 查询；key 写入或替换后重新激活仍按全局批准边界。只用既有 Tailscale，不启用 Funnel，不改其他应用映射或 ACL。`desk_probe.py` 经实际 HTTPS 核对；见 [任务台指南](docs/tailnet-desk.md)。 |
| M3-SITL | `dev_stack.py m3 --scenario <id|all|class:名称> --seeds 7,19,41`，裁判 `eval/judge_m3.py`，门禁 `verify_m3_release.py`。guardian 等出口与 PX4 连通且定位有结论后才开放控制（D048）；核对真值净距、两链互斥、出口看门狗、D040 预算及绑定的 v2 恢复边。只接受隔离成立且整批通过的回执；见 [M3 记录](docs/m3-readiness.md)。 |
| P0–P4 | 本机 `python -m drone_agent.eval.p{1,2,3,4}_world --output <目录>`，P3 另有 `--part ladder`；云端 `dev_stack.py p1|p2|p3|p4`。门禁 `verify_p{0,1,2,3,4}_release.py`；P4 的 S2 复验用 `verify_p4_s2_release.py`。`--workflows` / `--scheduling` 必须带 `--catalog`，`--business` 必须带 `--workflows`；激活先在副本演练迁移。 |
| S2 数据与画像 | `dev_stack.py p4-data` 导入，`p4-s2 --split … --mode live|replay|scripted|retrieval` 评测。先校准、冻结画像、测试只跑一次；VisA 测试拆分已披露使用两次，此后不得再作测试（D065、D066、D076）。公开道路开发样本不算独立留出或正式真值。 |
| P5 | 本机 `p5_world` 与 `p5_authz`，云端 `dev_stack.py p5`；门禁 `verify_p5_release.py`。升级先 `desk-members --next --apply` 暂存再 `desk-cloud --apply`；长稳用 `desk-soak`，备份与恢复演练见 [运维指南](docs/operations-guide.md)。长稳期间不手工操作三个项目、不激活新版本。 |
| P6 补拍 | 本机 `p6_world`，云端 `dev_stack.py p6`；任务台 `desk-world --section s1 --asset asset_red --state glare --apply` 后用 `desk_probe.py workflow --start --workflow recapture_watch --clear-before-recapture asset_red`（须指定 origin）。门禁 `verify_p6_recapture.py`；相机航向只核对过仿真真值。 |
| 模型对抗与规划 | 本机 `python -m drone_agent.eval.p6_adversarial run --corpus … --mode scripted|replay`；实调只用云端 `dev_stack.py p6-adv`。每语料 / 画像只录一次；429 无回答，按限速续跑；规划请求不得含时刻。改提示即新版本，冻结后由未见提示的独立作者写留出，预登记提示 / 语料 / 计分代码摘要和顺序；冻结提交不泄露机制。门禁 `verify_p6_adversarial.py`、`verify_planner_choice.py --decision D080|D081`。 |
| 指代 / 解释 | 改站点别名即受信配置变更，重新跑 `reference_check`（误拒为 0）；门禁 `verify_reference_check.py`。解释语料、核对规则或提示变更须新版本和新留出 / 实调；采用只由预登记规则判定。见 [解释记录](docs/explain-readiness.md)、[v2 负结果](docs/explain-check-v2-readiness.md)；门禁 `verify_explain.py`、`verify_explain_check.py`。 |
| 资产档案 | `python -m drone_agent.eval.p6_assets --output <目录>`，独立裁判 `eval/judge_assets.py`；任务台 `desk_probe.py assets`；门禁 `verify_asset_archive.py`。档案只追加，fact / decision / belief 分栏；比较只用前两者、不下归因结论，容差在 `configs/analysis/compare_v1.yaml`。 |
| 空域与容量 | `p3_world --part ladder|s0 --footprint box-v1|corridor-v1`；容量用 `python -m drone_agent.eval.capacity_growth template|generate|check|measure|judge`，数据在 `D:/drone-agent-data/capacity-ladder/`（保留 template 输入）。门禁 `verify_fine_reservations.py` / `verify_capacity.py --evidence <绝对路径>`。候选采用按预登记规则，不由单次时延拍板。 |
| 时间敏感测量 | Windows 后台节流会影响 S0 / 阶梯；`PYTHONPATH` 加载 `scripts/high_qos/sitecustomize.py`，仅作用于当前进程。容量每次测量独立进程，旁跑 `eval/host_power.py`；全程接交流电、不休眠、期间不改被导入的源码。约 2 小时的测量先确认电源与不合盖。 |
| 页面 | 改 `console/` 后另跑 `node --test tests/console/test_mission_client.cjs tests/console/test_live_client.cjs`。浏览器核验 `scripts/desk_browser.py`；`desk_preview.py --seed` 只作 S0 预览。页面脚本不得含控制 / 注入帧名，两页设计变量保持一致（D068）。 |

具体参数与验收范围以 [架构文档地图](docs/architecture/00-overview.md)、[云端指南](docs/cloud-development.md)、[实施任务](docs/operations-implementation.md)和各 readiness 为准。

## 已知环境约束

- **默认云端联调**（D023，用户明确要求）：Linux 构建、PX4/Gazebo 与服务联调走 `uv run python scripts/dev_stack.py`；本机用于编辑和快速单测，不自动启动本地真栈。连接失败应明确报错；云端操作指南见 `docs/cloud-development.md`。当前只部署仿真/验证工作区，真机 guardian 与飞控连接仍在设备侧。
- **国内网络**：uv 拉包用每次命令的环境变量走镜像，不写入任何全局配置：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`。
- **仓库路径含中文**（`产品/`）：原生库（MAVSDK 原生绑定、Gazebo 资产、ONNX Runtime）加载前需复制到 ASCII 路径；Linux 侧（WSL2 / Docker）把仓库放在 ASCII 路径下再挂载。
- **开发机是 Windows**：PX4 SITL、Gazebo、ROS 2 只在 Linux 容器或 WSL2 中运行；契约、规划、准入等纯 Python 代码在 Windows 直接测试。
- **ROS 2 节点用系统 Python 3.12**（rclpy 绑定），不进 uv 虚拟环境；本包与 ROS 2 节点只通过 proto / IPC 交互，`src/drone_agent/` 不 import `rclpy`。

## 工程红线（继承全局 CLAUDE.md 与 embodied-agent D009，另加飞行条款；只加严不放松）

- **模型边界**：LLM/VLM 只产出 `MissionSpec` / 后续 `WorkflowSpec` 类型化草案与带置信度的业务判断；永不触达控制出口、不生成代码、不决定恢复动作。MCP 只接只读工具；A2A 只传任务请求与进度。
- **单一控制出口**：只有 `guardian` 进程持有飞控连接；任何控制写入携带 `lease_epoch` 与 `command_seq`；旧代次一律拒绝；超时后先对账再重发。
- **证据先于成功**：`execution_status` / `effect_verdict` / `safety_verdict` 三元分离；`UNKNOWN` 永远不是成功；依赖步骤只在 `effect_verdict = verified` 后放行；报告中「已完成」只接受 `succeeded ∧ verified`。
- **飞控优先**：不禁用飞控原生失效保护；不使用 PX4 失效保护延期；executive / guardian 不提供空中断电（kill）接口；RC / GCS 接管永远可用。
- **真机纪律（H2 起，承接原 M4-A）**：RC 接管在手、围栏与返航点已验证、首飞降速、空域已按 UOM 报备、无关人员不在场；任何上真机的代码先过 SITL 故障注入与 guardian 覆盖，并通过共因故障清单（伴飞计算机断电、串口拔出、RC 接管、飞控失效保护）。
- **密钥**：API key 不进代码 / commit / 日志；**严禁复制 car-agent 的 `.env`**，只参考 `.env.example` 变量名。
- **依赖**：PX4 / ROS 2 / MAVSDK / Gazebo / Nav2 的大版本升级属于架构级变更，先在 `decisions.md` 记录再升级；版本锁定在 `configs/platforms/`。

## 节奏机制

- 季度技术雷达（1/4/7/10 月）：复查 `docs/research/frontier-2026.md` 的判断与 `decisions.md` 各条的重估触发器，纪要存入 `docs/research/`。
- 评测驱动（M1 起）：策略、模型与安全配置变更以版本化评测集数字说话。
