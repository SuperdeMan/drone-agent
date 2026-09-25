# 运营平台路线图评审与来源核对（2026-09-25）

[架构入口](../architecture/00-overview.md) · [新路线图](../roadmap.md) · [实施任务](../operations-implementation.md)

## 1. 结论与审阅范围

采纳用户提供的路线修订：保留安全约束任务运行时，前移站点资源、持久工作流、多机任务调度和多模态业务闭环；软件产品线 P0–P5 与硬件验证线 H1–H3 解耦，空地协同作为 P3 之后的扩展 X1。近期产品场景固定为园区登记设备巡检、异常复核、模拟工单和复检。

本次代码与文档基准为本地 `HEAD=9223d74`。共享评审使用 `75382dc` 的快照，且其中部分阶段描述早于验收归档；当前状态以仓库实际实现及其版本限定证据为准。本次仅修订文档，没有运行云端飞行、修改数据库、部署或新增硬件验证。

## 2. 来源与可复核位置

| 来源 | 本次读取范围 | 使用边界 |
|---|---|---|
| [用户提供的共享评审](https://chatgpt.com/s/t_6ab66843ee988191b4dd302bee490ae6) | 完整共享回答：六项缺口、目标架构、S0–S3、P0–P5 / H1–H3、首场景 | 本次路线修订的直接输入；不是验收证据 |
| [大疆《低空经济基础设施发展白皮书（2026）》公开 PDF](https://pdf.dfcfw.com/pdf/H3_AP202605281822957956_1.pdf) | 159 页；核对第 2 章运营底座、第 3 章业务闭环及第 6 章技术演进的相关原文 | 原作者白皮书的公开转载；行业方案与愿景不直接转换为本项目已实现能力或产品指标 |
| [PX4 Gazebo 文档（v1.17）](https://docs.px4.io/v1.17/en/sim_gazebo_gz/index) | 仿真路线参考 | 项目仍锁定 v1.17.0 与现有镜像；多机需在本项目另验 |
| [Temporal Workflow 文档](https://docs.temporal.io/workflows) | 持久工作流、事件历史与回放语义 | 用于重估触发器；本轮不引入 Temporal |

PDF 本次读取字节的 SHA-256：`161a12070618bd1ec3804e90e3c96625f78ac8fc06bd270eab0146f85f25c1a9`。以下“PDF 页”从 1 开始，“印刷页”取页面内页码，避免把共享回答中的临时行号当页码。

| 白皮书位置 | 对本项目的启发 | 落位 |
|---|---|---|
| §2.1.2，PDF 29 / 印刷 025 | 机场、飞行器与数据应用有不同职责 | P1 的 Site / Dock 与设备目录 |
| §2.2.2，PDF 32 / 印刷 028 | 定时与事件流程跨越采集、处理、报告 | P2 的业务工作流，不延长机载任务 DAG |
| §2.3.3，PDF 36 / 印刷 032 | 数据需进入后续业务 | P4 的发现、复核、工单、复检 |
| §2.4.4，PDF 38 / 印刷 034 | 共享设备仍需权限与数据隔离 | P1 服务端项目边界，P3 资源调度 |
| §3.1.3–3.1.4，PDF 53–57 / 印刷 049–053 | 问题处置与反馈闭环 | 先接模拟工单，再按需求接外部系统 |
| §6.1.2，PDF 144–147 / 印刷 140–143 | 资源调度、任务编排与数据融合分层 | 扩展既有 L0–L2，保留 L3–L6 |

共享回答末尾的 `sandbox:/mnt/data/drone-agent-no-hardware-roadmap-2026-09-25.md` 是原会话附件；本次共享页未提供可用附件正文，未把它列为已读来源。共享正文足以确定路线，本项目的契约与工作包在本次重新编制。DJI 的模拟任务及 Demo 维护状态没有作为本次实施前提；P5 只承诺目标协议的模拟合同测试，真实设备兼容性留给 H 线。

## 3. 与当前实现逐项核对

| 能力 | 实际证据 / 代码入口 | 修订判断 |
|---|---|---|
| M1 / M2 单机执行闭环 | [M1 readiness](../m1-readiness.md)、[M2 readiness](../m2-readiness.md) | 保留原 SHA 与证据；不重开、不追认新版本 |
| M3 局部自主仿真 | [M3 readiness](../m3-readiness.md)、[门禁结果](../verification/m3-2026-09-25/release.json) | `75382dc` 的 `m3_sitl=passed`；`jil=missing`，总状态 `not_passed`。修正共享评审中的“尚无 M3 验收” |
| 统一任务台 | [入口](../../src/drone_agent/console/unified.py)、[常驻记录](../tailnet-desk-readiness.md) | 已有 M2 / M1 统一入口，但项目锁让飞行串行；没有多站运营能力 |
| 目录与协调 | [catalog.py](../../src/drone_agent/fleet/catalog.py)、[coordinator.py](../../src/drone_agent/fleet/coordinator.py) | 静态能力回退与动态状态已有；`PassthroughCoordinator` 固定单机，协作只记 `recorded_not_executed`；需要新增可派遣判定和资源预约 |
| 服务与持久化 | [service.py](../../src/drone_agent/fleet/service.py)、[ledger.py](../../src/drone_agent/fleet/ledger.py) | SQLite 保存任务 / 审批 / 投递 / 证据；没有 WorkflowRun、Dock 或工单；不把现有幂等能力当作跨服务恰好一次 |
| 身份与项目权限 | [api.py](../../src/drone_agent/fleet/api.py)、[permission.py](../../src/drone_agent/runtime/permission.py) | 有来源身份和 scope、第三方只读自己任务摘要；还没有通用 `project_id` 隔离与项目角色 |
| 证据与视觉 | [verifier.py](../../src/drone_agent/fleet/verifier.py)、[事件检测](../../ros2_ws/src/da_edge_inference/) | 色标与确定性质量复核、可选 VLM 备注、CLIP 候选事件已有；通用行业缺陷识别、业务工单尚未验收 |
| 空域 | [airspace.py](../../src/drone_agent/admission/airspace.py) | 已有接口和录制 UOM 后端；无 live UOM 客户端或真实报备验收 |

## 4. 采纳时增加的工程约束

- **按代码修正 M3 描述。** 局部几何为深度体素与邻近障碍点，巡检编译选择 `skill.inspect.asset_local`；当前文档不再把初稿 ESDF 设想或 `goto_local + capture_image` 描述成实际实现。
- **P0 不因文档完成而关闭。** 统一运行来源记录与门禁消费仍要实现；历史回执不补造缺失字段。
- **审批与工作流分开。** 模板批准不能授权未来所有飞行；P2 仍逐任务审批，D032 的原样重试边界不扩大。
- **资源选择与投递之间再检查。** 排队期间的充电、维护、过期状态和取消意图必须阻断新投递；换机器人重新编译、准入和审批。
- **软件所有权与物理占用分开。** 租约失效、ACK 丢失或逻辑机场恢复都不能证明飞行器停止或机位空闲。
- **分层来源不能混成一个 mock 标志。** 执行、影像、规划、分析分别记来源；S2 实调不证明 S1 飞行，S0 规模不证明同等规模物理仿真。
- **任务服务为现有接入点。** 扩展 `fleet/` 与 `console/`；飞行重试由任务服务负责，工作流恢复不得再开一套派飞重试器。

## 5. 文档影响审计

| 文件组 | 处理 |
|---|---|
| `README.md`、`README.zh-CN.md`、`CLAUDE.md`、`AGENTS.md` | 同步定位、当前证据边界、P/H/X 导航与模型输出规则 |
| `architecture/00–08` | 修正 M3 已落地却仍写“未实现”的段落；同步阶段映射、供应商责任、来源与门禁 |
| `architecture/09-operations.md` | 新增运营层设计与契约草案 |
| `roadmap.md`、`operations-implementation.md`、`p1-implementation.md` | 新阶段、旧任务映射、工作包、依赖、验收与近期实施顺序 |
| `m3-implementation.md`、`m4-implementation.md` | 保留工作包 ID；明确 H1 / H2 / X1 的承接与前置，不再用旧总里程碑阻塞软件主线 |
| `decisions.md` | 只追加 D049–D053，说明对旧阶段排序的替代关系 |
| `reuse-from-embodied-agent.md` | 消除旧迁移阶段 P0–P3 与新产品线编号冲突；默认模型按 D029 |
| `cloud-development.md`、`sim/README.md`、`proto/README.md`、`m1-skill-catalog.md` | 补当前 M3 与新阶段入口；区分规划接口和现有命令 |
| `m0-readiness.md`、`m1-readiness.md`、`m2-readiness.md`、`m3-readiness.md`、`cloud-readiness-2026-09-19.md`、`live-console-readiness.md`、`tailnet-console-readiness.md`、`tailnet-desk-readiness.md`、`m2-review-2026-09-24.md` | 作为版本限定证据保留，不重写历史结论 |
| `m1-implementation.md`、`m2-implementation.md` | 已完成阶段计划保留；新排序由 roadmap 的映射解释 |
| `live-simulation.md`、`tailnet-console.md`、`tailnet-desk.md`、`ros2_ws/README.md` | 现有操作协议与运行命令有效，本次不改 |
| `research/frontier-2026.md`、`gpt6pro-review-2026-09-19.md`、`gpt6pro-review-digest.md`、`ros2-lyrical-2026-09.md` | 保留有日期的调研 / 原文；本评审记录新的路线判断 |
| `eval/BASELINES.md`、`docs/verification/` | 未执行新运行，不追加测试成绩、不修改回执 |

