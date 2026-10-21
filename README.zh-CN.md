# drone-agent

**自然语言描述任务，确定性运行时执行，以证据确认结果。**

[English](README.md) | **中文**

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![M2 complete in simulation](https://img.shields.io/badge/Milestone-M2%20%7C%20simulation-0F766E)](docs/m2-readiness.md)
[![M3 simulation line passed](https://img.shields.io/badge/M3--SITL-passed-0F766E)](docs/m3-readiness.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

无人机巡检运营平台，以安全约束任务运行时为底座，提供类型化任务、准入与审批、本地执行、持久工作流、资源调度，以及基于证据的复核、工单与复检。

> **当前范围：**P0–P5 已通过软件 / SITL 门禁；M3 局部自主已通过仿真验证，Jetson 与现场验证走 H 主线。P6 的 02–07 工作包已按各自范围准出；航段走廊和解释核对 v2 未采用。**WP-P6-01 仍未完成：**公开无人机材料收集及用户授权的独立 AI 初标与核验继续推进；真人候选计时移至后续运营验证。独立资产覆盖、真实跨次配对与正式评测仍待补齐，P6 未整体准出。当前数量、剩余工作与证据边界见 [P6 交接](docs/p6-road-corpus-handoff-2026-10-22.md)和[路线图](docs/roadmap.md)。

[本机体验](#本机体验) · [设计](#设计) · [验证记录](#验证记录) · [文档导航](#文档导航) · [路线图](#路线图)

## 现有能力

- **用自然语言规划巡检任务。** MiniMax-M3 生成受约束草案；规划器通过只读工具，基于登记的资产、航线和飞行体积构建上下文。
- **执行前检查任务。** 确定性编译与准入检查能力、参数、空间、时间、能源、空域和资源；信息未知或无效时拒绝放行。
- **将审批绑定到任务。** Ed25519 签名覆盖已批准版本和任务包哈希；任务包经 mTLS 传输，机载再次独立核验。
- **在本地监督下执行。** mission executive 调度技能；独立的 `guardian` 进程持有唯一飞控连接并执行恢复策略，飞控原生失效保护与人工接管始终保留。
- **按证据报告，保留不确定性。** 影像与遥测检查支撑“已完成 / 未完成 / 不确定”报告；MCAP、ULog 和事件记录用于独立裁判与离线回放。
- **经受约束的第二控制路径做局部自主（M3，仿真）。** ROS 2 Jazzy 节点按深度地图规划短时域片段；`guardian` 用控制屏障函数过滤每个目标，只授权几百毫秒；PX4 外部模式节点只转发这些授权，授权结束即停。机载事件检测只产生候选事实。
- **只派遣站点能支撑的任务（P1，仿真）。** 项目绑定站点、机场与飞行器；机场报告通信、舱盖、机位在位、补能、环境与运维状态，同一个确定性可派遣判定加独占预约，约束预览、开盖和机载拉取任务包的那一刻。只有终态结果加新鲜的落地证据才释放占用，从不按超时释放。机场是逻辑模拟器，不是厂商硬件。
- **运行持久巡检工作流（P2，仿真）。** 版本化模板只含白名单活动，可人工、按每日 / 间隔排班或由绑定的事件触发。每个运行固定模板版本，经租约、fencing 与 outbox 在服务重启后继续；同一活动不会重复派飞，取消之后不再开始新动作。每次飞行仍需单独人工审批。分析只有带标注的脚本夹具或确定性颜色特征，不代表识别质量；reviewer 确认后生成模拟工单，维修反馈启动复检运行。
- **跨站点、跨飞行器调度任务（P3，仿真）。** 操作者提交巡检任务单；可重放的纯判定逐候选给出排除原因，其余按预计到场、近期使用与机器人 ID 排序。同一事务完成分配、编译任务并持有机器人、机场与航线经过的空域单元；失联飞行器的包络持续增长，直到对账。领取前被阻断过久的分配以新代次撤回给另一台，飞行明确失败则任务接力。每次飞行仍需单独人工审批。
- **以证据闭合业务链（P4，仿真）。** 确定性质量层与持久分析作业把已证实的采集变为带来源标注（模型实调、录制、脚本或确定性）的候选发现。只有项目 reviewer 能确认发现；每个已确认发现一张工单，维修反馈启动复检，只有反馈之后的新的已证实采集、允许来源的未疑似分析与 reviewer 对本轮的确认同时成立才关单。模型回答只是候选，从不改变飞行判定。
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

想在本机查看全部运营工作区（机队与机场、工作流、调度、发现与工单），运行 `uv run python scripts/desk_preview.py --seed` 并打开 <http://127.0.0.1:8770>。它用 S0 逻辑世界配上常驻任务台同款目录提供页面；飞行是逻辑的并如实标注，预览中的任何内容都不是验证证据。

### 在仿真中运行飞行闭环

联调使用已配置的 Linux 云端工作区。按[配置指南](docs/cloud-development.md)完成准备后，检查目标并获取常驻任务台地址：

```bash
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py desk-cloud --status
```

统一云端飞行台在同一入口提供**运营台**（任务与审批、工作流、调度、机队与机场、发现与工单等工作区，D068）与 **M1 固定巡检**。M2 审批后在 PX4/Gazebo 中飞行并生成证据报告；M1 保留实时轨迹、相机、暂停 / 恢复与取消。两种模式依次飞行，切换页面不取消任务。部署与访问见[飞行台指南](docs/tailnet-desk.md)，本轮评审与验证见 [M2 评审记录](docs/m2-review-2026-09-24.md)。

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

各记录保存表中提交的完整门禁、失败项与适用边界；历史结果不验证后续提交。P6 公开素材只作为[开发证据](docs/p6-materials-development.md)，不构成发布门禁。

| 证据 | 版本 |
|---|---|
| [运营解释助手（WP-P6-05，D083）](docs/explain-readiness.md) | `76e7403` |
| [精细空域预约（WP-P6-06，D084）](docs/fine-reservations-readiness.md) | `6544a9a` |
| [容量观测与数据保留（WP-P6-07，D085）](docs/capacity-readiness.md) | `a63f3ac` |
| [解释核对 v2（D086）](docs/explain-check-v2-readiness.md) | `7f7b97b` |
| [资产档案（WP-P6-04，D087）](docs/asset-archive-readiness.md) | `6249129` |
| [跨站点指代检查（D082）](docs/reference-check-readiness.md) | `4398938` |
| [绑定身份（D081）](docs/planner-binding-readiness.md) | `b4a17a3` |
| [规划澄清（D080）](docs/planner-clarification-readiness.md) | `02694be` |
| [P6 对抗录制（D079）](docs/p6-adversarial-readiness.md) | `8d69ae1` |
| [P6 主动补拍（D078）](docs/p6-recapture-readiness.md) | `ecee452` |
| [外部评审加固（D075、D076）](docs/research/external-review-2026-10-10.md) | `45ba863` |
| [运营台视觉 v2（D074）](docs/desk-visual-v2-readiness.md) | `0094238` |
| [M1 运行时](docs/m1-readiness.md) | `eefe76e` |
| [M2 发布门禁](docs/m2-readiness.md) | `f362b9e` |
| [规划器实调基线](docs/verification/m2-baseline-2026-09-23.json) | `f362b9e` |
| [常驻任务台实调](docs/tailnet-desk-readiness.md) | `74984f9` |
| [M2 评审与统一入口](docs/m2-review-2026-09-24.md) | `e8edf28` |
| [M3-SITL 发布门禁](docs/m3-readiness.md) | `75382dc` |
| [P0 发布门禁](docs/p0-readiness.md) | `de597d0` |
| [P1 发布门禁](docs/p1-readiness.md) | `b49701b` |
| [P3 发布门禁](docs/p3-readiness.md) | `73fcf23` |
| [P4 发布门禁](docs/p4-readiness.md) | `8f41112` |
| [P4 S2 复验](docs/p4-readiness.md) | `f4b1b99` |
| [运营台工作区（D068）](docs/desk-workspaces-readiness.md) | `a0d12ff` |
| [P2 发布门禁](docs/p2-readiness.md) | `3bdbd50` |

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
| 运营层与下一批工作 | [运营架构](docs/architecture/09-operations.md) · [实施任务](docs/operations-implementation.md) · [P4 验收](docs/p4-readiness.md) · [P5 验收](docs/p5-readiness.md) · [P6 补拍验收](docs/p6-recapture-readiness.md) · [P6 对抗录制验收](docs/p6-adversarial-readiness.md) · [P5 方案](docs/p5-implementation.md) · [运维手册](docs/operations-guide.md) |
| 设计理由与复用来源 | [决策记录](docs/decisions.md) · [姊妹项目复用](docs/reuse-from-embodied-agent.md) |
| P6 公开素材与剩余工作 | [交接](docs/p6-road-corpus-handoff-2026-10-22.md) · [来源审查](docs/p6-materials-review.md) · [完整道路标注](docs/p6-road-labeling-guide.md) · [开发诊断](docs/p6-materials-development.md) |

## 路线图

| 阶段 | 范围 | 状态 |
|---|---|---|
| M0–M2 | 契约、单机运行时、受约束 Agent 与证据闭环 | 已完成仿真验证 |
| M3 | 局部自主、感知、定位与降级处理 | 仿真线已通过；Jetson-in-the-loop 待硬件 |
| P0 | 能力盘点、运行来源与产品门禁 | 软件 / SITL 范围已完成 |
| P1 | 项目、站点、虚拟机场与派遣领取闸门 | 软件 / SITL 范围已完成 |
| P2 | 持久业务工作流 | 软件 / SITL 范围已完成 |
| P3 | 多站多机调度 | 软件 / SITL 范围已完成（同一世界两架 PX4 SITL） |
| P4 | 多模态业务闭环 | 软件 / SITL 范围已完成；S2 以 MiniMax-M3.1-Flash-Preview 复验（D076） |
| P5 | 平台 v0.1 | 软件 / SITL 范围已完成（厂商机场只验证到协议模拟器） |
| P6 | 证据驱动运营 | WP-P6-02–07 按已记录范围准出；WP-P6-01 正在推进公开数据开发，正式评测集未冻结；P6 未整体准出 |
| H1–H3 | JIL、台架与受限真机、现场运营 | 独立硬件验证线 |
| X1–X3 | 空地协同、厂商真实接入、模型与内核研究 | 条件满足后扩展 |

详见[完整路线图与退出标准](docs/roadmap.md)。局部自主只在仿真中验证；跨机器人交接及 DJI / ArduPilot 适配尚未实现。空域准入已有接口定义与录制的 UOM 后端，真实模式没有报备的任务会被拒绝；能耗估计仅用于仿真。

## 许可证

[Apache License 2.0](LICENSE)。
