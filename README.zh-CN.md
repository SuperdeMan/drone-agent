# drone-agent

[English](README.md) | **中文**

面向空地异构机器人的安全约束任务运行时，以无人机为首要本体。

它把自然语言目标编译为可验证的类型化任务（`MissionSpec`），在机载本地约束内持续执行，按真实证据确认结果，并支持任务在无人机与地面机器人之间交接。大模型只表达目标与判断证据；确定性运行时负责执行、恢复与安全；飞控原生失效保护与人工接管始终保留。

## 阅读入口

| 想了解 | 看 |
|---|---|
| 项目规则、当前阶段、红线 | [CLAUDE.md](CLAUDE.md)（AI 编码代理入口 [AGENTS.md](AGENTS.md)） |
| 架构总览与文档地图 | [docs/architecture/00-overview.md](docs/architecture/00-overview.md) |
| 六类契约与三元状态 | [docs/architecture/02-contracts.md](docs/architecture/02-contracts.md) |
| 安全体系（运行时保障、恢复策略图、停止语义） | [docs/architecture/03-safety.md](docs/architecture/03-safety.md) |
| 空地协同 | [docs/architecture/04-air-ground.md](docs/architecture/04-air-ground.md) |
| 路线图 M0–M6 | [docs/roadmap.md](docs/roadmap.md) |
| 技术决策记录 | [docs/decisions.md](docs/decisions.md) |
| 前沿调研与 GPT-6 Pro 评估 | [docs/research/](docs/research/) |
| 从姊妹项目复用什么 | [docs/reuse-from-embodied-agent.md](docs/reuse-from-embodied-agent.md) |

设计文档为中文；代码标识符为英文，代码注释中英双语（英文在前、中文在后）。

## 一图看架构

```text
L0 操作者入口（控制台、经 A2A 接入的 cockpit-agent 语音、API）
L1 任务规划与协同（LLM/VLM 规划器、只读 MCP 工具、车队协调器）
L2 确定性编译与准入（能力、空间、时间、能源、空域、资源；审批绑定任务版本）
L3 机载任务执行（任务 DAG、长时间技能、本地权威状态、证据）
L4 局部自主（感知、定位、局部地图、确定性规划；学习型策略以影子运行插件接入）
L5 安全监督与控制出口（guardian 进程：租约与序号、新鲜度、包络、Simplex 决策）
L6 平台适配器（PX4 经 MAVSDK / px4_ros2、DJI Cloud API、ArduPilot、Nav2）→ 飞控失效保护、RC 接管
```

五条原则：模型表达目标、运行时执行目标；单一控制出口；证据先于成功（`UNKNOWN` 永远不是成功）；契约先于模块；能力协商而非假统一接口。

## 当前状态

M1 已于 2026-09-20 完成，范围为 PX4 SITL / Gazebo：executive/guardian 双进程、持久化命令对账、五技能、认证本地 IPC、真实相机证据与独立裁判。版本 `eefe76e` 通过 400 项测试和全部 66 组多种子飞行/故障场景，错误成功报告为 0，离线回放一致。见 [版本限定的验收证据与边界](docs/m1-readiness.md)。

M2（受约束 Agent）已于 2026-09-23 关闭：版本 `f362b9e` 通过发布门禁，最后一项判据是 MiniMax-M3 实调准入率基线——20 条可规划请求全部一次通过准入，8 条应拒答请求全部拒答，4 条应拦截请求无一被准入。自然语言请求默认由 MiniMax-M3（D029）规划为窄草案，经 fail-closed 编译与准入，按确切任务包哈希审批、Ed25519 签名，由机载 uplink 经 mTLS 拉取；executive 与 guardian 起飞前再次验签并执行新的两相位巡检技能，任务服务复核证据并生成三列报告。版本 `f362b9e` 上 18/18 组多种子端到端用例（三种措辞的标称请求、影像降级后的重试、服务停机、拒答、被骗规划器）通过，错误成功报告 0、回放一致；M1 完整矩阵仍全部通过（66/66，在共享主机的静默窗口分批运行）；两套对抗语料全部拦截。这些运行使用带标注的脚本规划回答；准入率基线是录制下来的实调运行。见 [M2 验收记录](docs/m2-readiness.md)。

## 开发

Linux 构建与联调现在默认使用云端工作区，见 [云端开发指南](docs/cloud-development.md)，复用已有 SSH 连接参数：

```bash
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py verify
uv run python scripts/dev_stack.py test
```

固定仿真控制台可常驻云端，通过 Tailscale Serve 私网访问，不另设应用登录页。运行 `uv run python scripts/dev_stack.py console-cloud --status` 获取私有 HTTPS 地址，部署和访问边界见 [Tailnet 控制台指南](docs/tailnet-console.md)。原本机桥仍可通过 `uv run python scripts/dev_stack.py console` 在 <http://127.0.0.1:8768> 使用。两个入口都展示机载遥测与相机画面，暂停/恢复/取消经过既有 executive 通道。这是 M1 人工入口。M2 任务台（hri.v0 控制台与 A2A 网关）可在本机运行：`uv run python -m drone_agent.console.mission --local`（<http://127.0.0.1:8769>），提交、规划、准入、审批与签名在进程内完成，飞行只在云端（D023）。它也可常驻云端，经独立的 Tailscale Serve 端口访问（D035）：`uv run python scripts/dev_stack.py desk-cloud --status` 返回私有地址；批准的任务由持有项目锁的监管者在云端仿真中飞行，每个结束的任务都用仿真真值独立裁判。见 [任务台指南](docs/tailnet-desk.md)。版本 `b9cf00c` 已经真实 Tailnet HTTPS 验收（带标注的脚本规划）：标称巡检、飞行中重启页面与监管者（被接管而非重飞，经策略自动重试后完成）、操作者取消（不自动重试）、拒答与越界请求，错误成功报告 0、回放一致；见 [任务台验收记录](docs/tailnet-desk-readiness.md)。任务台内的 MiniMax 实调规划仍待模型 key。

[版本限定的实时入口验收](docs/live-console-readiness.md) 记录云端运行版本、本机界面版本、8 轮 HTTP 实时验证和 18 组 M1 回归子集。原有 66 组 M1 基线仍只属于 `eefe76e`。

云端常驻入口另有 [Tailnet 验收记录](docs/tailnet-console-readiness.md)，记录私网 HTTPS 边界、服务重启行为和准确部署版本。

人工核对一次云端记录的运行：列出运行、拉取其中一次（每个文件都与远端摘要和裁判回执核对）、打开生成的离线 `viewer.html`：

```bash
uv run python scripts/dev_stack.py runs
uv run python scripts/dev_stack.py fetch --run m1-<run_id> --cases nominal-7 --apply --artifacts D:/drone-agent-cloud
uv run python -m drone_agent.eval.viewer <拉取到的运行目录>
```

浏览器只展示与复算摘要；所有判定来自裁判（见 [评测体系](docs/architecture/08-evaluation.md) §7）。

本机仍可编辑代码并运行快速确定性检查：

```bash
# 国内网络走镜像（每次命令加，不写全局配置）
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --group dev
uv run ruff check .
uv run pytest -q
uv run python scripts/generate_contract_fields.py --check
uv run python scripts/generate_proto.py
```

PX4 SITL / Gazebo / ROS 2 只在 Linux（WSL2 或 Docker）运行，仓库需放在 ASCII 路径下；契约与规划层代码是纯 Python，在 Windows 可直接测试。

[仿真指南](sim/README.md) 保留显式本地回退的操作方法；云端工具不会静默启动本地真栈。[proto 指南](proto/README.md) 说明 wire 边界；[技能清单](docs/m1-skill-catalog.md) 区分草案与真实已实现能力。恢复场景名称表示计划覆盖，不是已通过的注入运行。

## 姊妹项目

- `../embodied-agent`：桌面操作机械臂，本仓库的模板与主要复用来源。
- `../car-agent`：智能座舱，语音 / 权限 / 账本参考；可经 A2A 作为授权任务入口。

## License

Apache-2.0
