# drone-agent

**自然语言描述任务，确定性运行时执行，以证据确认结果。**

[English](README.md) | **中文**

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![M2 complete in simulation](https://img.shields.io/badge/Milestone-M2%20%7C%20simulation-0F766E)](docs/m2-readiness.md)
[![M3 simulation line passed](https://img.shields.io/badge/M3--SITL-passed-0F766E)](docs/m3-readiness.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

面向无人机、逐步扩展到空地异构机器人的安全约束任务运行时。它把巡检请求转成类型化任务，检查执行边界，将审批绑定到确切任务，并按证据报告实际结果。

> **当前范围：** M2 已于 **2026-09-23** 完成，验证对象是 **PX4 SITL + Gazebo**（软件在环仿真）中的单架无人机。**2026-09-25** M3 的仿真线（经 guardian 约束的 PX4 外部模式局部自主）通过发布门禁；M3 要在 Jetson-in-the-loop 验证后才关闭。真机飞行和空地任务交接属于[后续里程碑](docs/roadmap.md)。

[本机体验](#本机体验) · [设计](#设计) · [验证记录](#验证记录) · [文档导航](#文档导航) · [路线图](#路线图)

## 现有能力

- **用自然语言规划巡检任务。** MiniMax-M3 生成受约束草案；规划器通过只读工具，基于登记的资产、航线和飞行体积构建上下文。
- **执行前检查任务。** 确定性编译与准入检查能力、参数、空间、时间、能源、空域和资源；信息未知或无效时拒绝放行。
- **将审批绑定到任务。** Ed25519 签名覆盖已批准版本和任务包哈希；任务包经 mTLS 传输，机载再次独立核验。
- **在本地监督下执行。** mission executive 调度技能；独立的 `guardian` 进程持有唯一飞控连接并执行恢复策略，飞控原生失效保护与人工接管始终保留。
- **按证据报告，保留不确定性。** 影像与遥测检查支撑“已完成 / 未完成 / 不确定”报告；MCAP、ULog 和事件记录用于独立裁判与离线回放。
- **经受约束的第二控制路径做局部自主（M3，仿真）。** ROS 2 Jazzy 节点按深度地图规划短时域片段；`guardian` 用控制屏障函数过滤每个目标，只授权几百毫秒；PX4 外部模式节点只转发这些授权，授权结束即停。机载事件检测只产生候选事实。
- **提供人和 Agent 的任务入口。** Web 任务台支持规划与审批；A2A 网关接受任务提交与状态查询；常驻任务台可通过 Tailscale 私网访问。

已实现的技能覆盖起飞、登记航线飞行、拍照、资产巡检、返航和降落。M2 巡检使用[仿真场景](configs/scenarios/m2_campus_v2.yaml)中预定义的观察航线。

## 本机体验

需要 **Python 3.12+**、**uv** 和 **Git**。本机任务台支持 Windows 和 Linux，无需 PX4 或 Gazebo。

```bash
git clone https://github.com/SuperdeMan/drone-agent.git
cd drone-agent
uv sync --group dev
uv run python -m drone_agent.console.mission --local
```

打开 **<http://127.0.0.1:8769>**，选择 `campus_training` 和 `asset_red`，原样提交以下示例：

> 检查起飞点东侧的红色设备标记，带回一张清晰照片。

查看规划与准入结果，再批准任务包。**本机模式完成规划、准入与签名，没有连接机器人，不会起飞。**

未设置 `MINIMAX_API_KEY` 时，任务台对 [M2 场景集](configs/scenarios/m2_suite.yaml)中的请求原文使用带标注的脚本回答；进程环境中有 key 时，使用 MiniMax-M3 实调规划。页面会标明所用规划器，密钥配置方法见[云端开发指南](docs/cloud-development.md)。

### 在仿真中运行飞行闭环

联调使用已配置的 Linux 云端工作区。按[配置指南](docs/cloud-development.md)完成准备后，检查目标并获取常驻任务台地址：

```bash
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py desk-cloud --status
```

统一云端飞行台在同一入口提供 **M2 自然语言任务**与 **M1 固定巡检**。M2 审批后在 PX4/Gazebo 中飞行并生成证据报告；M1 保留实时轨迹、相机、暂停 / 恢复与取消。两种模式依次飞行，切换页面不取消任务。部署与访问见[飞行台指南](docs/tailnet-desk.md)，本轮评审与验证见 [M2 评审记录](docs/m2-review-2026-09-24.md)。

PX4 SITL 与 Gazebo 在 Linux 中运行，原生组件使用 ASCII 仓库路径。[仿真指南](sim/README.md)另有显式启用本地仿真的操作方法。

## 设计

**请求 → 受约束草案 → 编译与准入 → 审批签名 → 本地执行 → 证据报告。**

| 边界 | 职责 |
|---|---|
| 地面站 / 云端规划 | 规划器提出草案，确定性代码构建 `MissionSpec`；模型没有控制权限。 |
| 准入与审批 | 检查任务，将审批绑定到 `package_hash`，投递已签名的 `MissionPackage`。 |
| 机载执行 | `uplink` 接收任务包，`executive` 调度技能，`guardian` 在控制写入前检查权限、新鲜度和约束。 |
| 验证与回放 | 复核证据，分别记录 `execution_status`、`effect_verdict`、`safety_verdict`；`UNKNOWN` 永远不计为成功。 |

云端下发已授权任务及其边界，本地执行和恢复不依赖云端持续下发逐帧控制命令。详见[架构总览](docs/architecture/00-overview.md)、[契约](docs/architecture/02-contracts.md)和[安全体系](docs/architecture/03-safety.md)。

## 验证记录

以下已归档结果仅属于各自的**确切版本与验证范围**，不能作为所有后续提交的测试结果。

| 证据 | 版本 | 已记录结果 |
|---|---|---|
| [M1 运行时](docs/m1-readiness.md) | `eefe76e` | 400 项测试；22 个飞行 / 故障场景 × 3 个种子，66/66 通过；错误成功报告 0，回放一致。 |
| [M2 发布门禁](docs/m2-readiness.md) | `f362b9e` | 775 项测试；端到端 18/18，M1 回归 66/66；42 条确定性与 32 条脚本规划对抗用例全部拦截或拒答；错误成功报告 0，飞行回放一致。 |
| [规划器实调基线](docs/verification/m2-baseline-2026-09-23.json) | `f362b9e` | MiniMax-M3：20/20 可规划请求一次通过准入，8/8 应拒答请求正确拒答，4/4 应拦截请求未被准入。 |
| [常驻任务台实调](docs/tailnet-desk-readiness.md) | `74984f9` | 中英文请求完成规划、审批、飞行与独立验证；隐私越界请求被拒答。 |
| [M2 评审与统一入口](docs/m2-review-2026-09-24.md) | `e8edf28` | Linux 833 项测试、M2 E2E 18/18 通过；修复证据闭环、取消后自动重飞与裁判缺口；实调与入口交互范围单独列出。 |
| [M3-SITL 发布门禁](docs/m3-readiness.md) | `75382dc` | 983 项测试；16 个局部自主与故障场景 × 3 种子 = 48/48；M1 66/66、M2 18/18、Zenoh 下 6/6；错误成功报告 0，飞行回放一致；监督周期 p99 ≤ 106.6 ms。Jetson-in-the-loop 待硬件。 |

M2 端到端与自然语言对抗运行使用**带标注的脚本规划回答**，验证执行链路及其边界。真实模型行为单独记录在实调基线和常驻任务台验收中。[机器可读的发布结果](docs/verification/m2-2026-09-23-release.json)汇集了 M2 的验收依据。

## 开发

```bash
uv run ruff check .
uv run pytest -q
uv run python scripts/generate_contract_fields.py --check
uv run python scripts/generate_proto.py
```

通过项目镜像下载依赖时，为当前命令或终端会话设置 `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`。契约与规划检查可直接在 Windows 运行；Linux 构建与联调使用 `scripts/dev_stack.py`。

贡献前请阅读 [CLAUDE.md](CLAUDE.md)（AI 编码代理入口为 [AGENTS.md](AGENTS.md)）。架构与执行语义变更先修改对应设计文档。设计文档使用中文，README 和代码注释保持中英双语。

## 文档导航

| 想了解 | 阅读入口 |
|---|---|
| 系统边界与完整文档地图 | [架构总览](docs/architecture/00-overview.md) |
| 消息语义与运行时保障 | [契约](docs/architecture/02-contracts.md) · [安全体系](docs/architecture/03-safety.md) · [Wire 协议](proto/README.md) |
| 云端仿真与任务台 | [云端开发](docs/cloud-development.md) · [任务台](docs/tailnet-desk.md) |
| 查看飞行证据与回放 | [评测体系](docs/architecture/08-evaluation.md) · [拉取与查看运行](docs/cloud-development.md#查看与人工核对结果) |
| 设计理由与复用来源 | [决策记录](docs/decisions.md) · [姊妹项目复用](docs/reuse-from-embodied-agent.md) |

## 路线图

| 阶段 | 范围 | 状态 |
|---|---|---|
| M0–M2 | 契约、单机运行时、受约束 Agent 与证据闭环 | 已完成仿真验证 |
| M3 | 局部自主、感知、定位与降级处理 | 仿真线已通过；Jetson-in-the-loop 待硬件 |
| M4 | 受限真机验证、UAV 与 rover 联合仿真 | 规划中 |
| M5–M6 | 真实空地协同、更多平台与模型插件 | 规划中 |

详见[完整路线图与退出标准](docs/roadmap.md)。局部自主只在仿真中验证；跨机器人交接及 DJI / ArduPilot 适配尚未实现。空域准入已有真实接口与录制的 UOM 后端，真实模式没有报备的任务会被拒绝；能耗估计仅用于仿真。

## 许可证

[Apache License 2.0](LICENSE)。
