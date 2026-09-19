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

M0（2026-09）：规范、架构文档、契约模型与契约测试。尚无可飞行的代码；M1 目标是无大模型的 PX4 SITL 单机安全闭环。

## 开发

```bash
# 国内网络走镜像（每次命令加，不写全局配置）
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --group dev
uv run ruff check .
uv run pytest -q
```

PX4 SITL / Gazebo / ROS 2 只在 Linux（WSL2 或 Docker）运行，仓库需放在 ASCII 路径下；契约与规划层代码是纯 Python，在 Windows 可直接测试。

## 姊妹项目

- `../embodied-agent`：桌面操作机械臂，本仓库的模板与主要复用来源。
- `../car-agent`：智能座舱，语音 / 权限 / 账本参考；可经 A2A 作为授权任务入口。

## License

Apache-2.0
