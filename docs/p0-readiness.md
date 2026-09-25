# P0 实现与验收记录

[路线图](roadmap.md) · [工作包](operations-implementation.md) · [来源实现](../src/drone_agent/fleet/provenance.py) · [发布门禁](../scripts/verify_p0_release.py)

**P0 已完成（2026-09-25，软件 / SITL 范围）。** 候选 `de597d0` 的七项门禁全部通过；运行来源贯穿任务版本、采集、分析记录、报告、任务台与离线证据页。P1–P5 尚未实现；原 M3 仍因 JIL 缺席而未关闭。

## 版本与门禁

| 项目 | 准确记录 |
|---|---|
| 被测 / 部署源码 | `de597d0eb7430260285e0965dc07f7f904ab5e70` |
| 云端部署 | `20260925T151620Z-a8010bf5`；[部署回执](verification/p0-2026-09-25-r2/deployment.json) |
| 控制面摘要 | `4cbadb08c989aebfec297340f0e302753b22e2ddd348a46712975a8b3d65996a` |
| P0 总门禁 | [release.json](verification/p0-2026-09-25-r2/release.json)：`status=passed`，七项均 passed；含能力清单和 26 份输入摘要 |
| 常驻入口 | [控制台](verification/p0-2026-09-25-r2/resident-console-activation.json)与[任务台](verification/p0-2026-09-25-r2/resident-desk-activation.json)均为同一候选；入口已脱敏 |
| 入口验证 | [HTTPS / WebSocket 检查](verification/p0-2026-09-25-r2/http-probe.json)：页面与脚本匹配、三条健康版本一致、A2A 无效身份 401、跨源连接 403 |

后续只增加证据和文档的提交不改变被测源码；不能把这份结果改称其他代码 SHA 的完整验收。

## 已实现工作包

- **WP-P0-01/02**：能力与来源核对、新架构 / 路线、原 41 个 M3/M4 工作包的去向、D049–D054。
- **WP-P0-03**：受信入口构造 SourceContext；每版本固定 RunHeader 与 ModelUse；每份采集有 EvidenceOrigin，每次分析有 AnalysisOrigin；RunProvenance 输出可复算的冻结投影。
- **WP-P0-04**：独立 P0 门禁、能力清单、当前候选与历史 / 硬件结果分离、来源 / 版本 / 输入哈希检查。

来源保存在现有 `versions.decision.run_provenance`，SQLite schema v1 与机载 wire 均不改。记录只能追加，驳回和重启不覆盖旧来源；历史缺项显示 `legacy_unknown`。客户端不能选择执行后端或伪造来源；生产 CLI 当前只提供 none 与 px4_sitl，逻辑后端仅用于隔离测试。

规划来源按 Provider 实现与调用记录区分脚本、录制 / 缓存、实调、确定性与未运行，不按模型名称猜测。影像逐份绑定采集身份和媒体摘要，分析来源与规划独立；来源不参与审批、飞控或三元结果判定。

## 检查结果

| 判据 | 实际结果 |
|---|---|
| scope | 相对审阅基线的变更属于 P0；guardian、executive、适配器、wire 与恢复配置未改 |
| checks | Linux **1000 项通过，0 失败 / 错误 / 跳过**；部署前后其他 30 个容器身份一致 |
| adversarial | 确定性 42/42、脚本自然语言 32/32，全拦或拒答，授权包 0；真实模型对抗录制 0 条，32 条未录制，不冒充实调对抗结果 |
| e2e | **6 场景 × 3 种子 = 18/18**；12 完成、6 合理拒绝且不飞；错误成功 0，在线 / 回放一致 |
| source_views | 18 份视图、21 个任务版本、21 份采集；文件摘要与独立裁判一致；来源投影、配置、报告与版本全部匹配 |
| live_sources | MiniMax-M3 两次实调：正常任务单版本完成；取消任务单版本安全收尾；均重规划 0、错误成功 0、裁判 / 回放一致；权威 flight.json 与任务、版本、epoch、状态、起止时间和 SHA 一致 |
| historical_boundaries | 原 M3-SITL passed、总状态 not_passed、JIL missing；历史回执字节不变，不作为本候选成绩 |

Windows 本地完整检查为 1000 项：999 通过，1 项按既有规则跳过（Unix socket，由 Linux 全量覆盖）。ruff、任务台 JS 语法、字段清单与 wire 冻结检查通过；字段清单仍为 54 个模型 / 371 个字段 / 16 个枚举。新增 16 项来源 / 门禁测试覆盖重启、冻结、脚本冒充、录制 / 缓存、混合媒体、来源篡改、缺项、错版本与实际飞行回执形状。

本次未改控制执行路径，因此没有重跑完整 M1 66 组或 M3 48 组飞行矩阵；它们仍只属于各自历史 SHA。P0 的新软件 / 集成保证由上述当前候选证据支持。

## 运行清单

| 批次 | 云端运行 | 结果 / 回执 |
|---|---|---|
| 红 / 蓝资产 | `m2-20260925T152115Z-9127db45` | 6/6；[回执](verification/p0-2026-09-25-r2/e2e/m2-nominal.json) |
| 影像降质 / 服务中断 | `m2-20260925T153119Z-90af7703` | 6/6；[回执](verification/p0-2026-09-25-r2/e2e/m2-recovery-outage.json) |
| 拒答 / 越界范围 | `m2-20260925T154502Z-7682c6d0` | 6/6；[回执](verification/p0-2026-09-25-r2/e2e/m2-refused-scope.json) |
| 实调正常 | `m-bbf8917d0baa` | completed，v1，replans=0；[探针](verification/p0-2026-09-25-r2/live-nominal.json) |
| 实调取消 | `m-3e7f55c7552c` | incomplete，v1，replans=0；取消已转入机载，裁判确认安全收尾；[探针](verification/p0-2026-09-25-r2/live-cancel.json) |

实调模型为 `minimax/MiniMax-M3`，提示版本 `planner-v1`，提示摘要 `6ccc030a90bb6203e57220ae77f48de1f956e8e8335d28340b40422c783a2eef`。实调探针是来源与链路验收，不是新一轮大规模模型准入率基线。

原始视图保存在 [source-views](verification/p0-2026-09-25-r2/source-views/)，实际飞行版本回执保存在 [live-flights](verification/p0-2026-09-25-r2/live-flights/)。大文件仍在云端 artifacts 与本机 fetch 目录，摘要在批次回执；门禁可从仓库小型证据复算。

## 开发负记录与边界

| 版本 / 运行 | 发现 | 处理与计数 |
|---|---|---|
| `eb1ddce` 首轮云端检查 | 新测试假设存在 .git；归档镜像没有 Git 历史，995 项中 1 项失败 | 修正测试读取边界，保持判据；[负记录](verification/p0-2026-09-25/diagnostics/eb1ddce-cloud-checks.json)，不计最终门禁 |
| `271c5ac` 首轮红标记 | 457 ms 墙钟内仿真仅前进 4 ms，触发 observation_stale 与额外重试；裁判因版本数不符判失败，虚报 0 | 保留[诊断](verification/p0-2026-09-25/diagnostics/271c5ac-simulator-stall.json)，不改安全阈值；没有将该记录改判通过 |
| `271c5ac` 首次 live 门禁 | 正常 / 取消链和来源审计通过，但门禁错误要求公开飞行摘要提供 source_sha | 改读真实 flight.json 并补正反例；[诊断](verification/p0-2026-09-25/diagnostics/271c5ac-live-gate-shape.json)。旧候选全部结果仅作开发记录；最终候选另跑完整验证 |

P0 当前贯穿本机无设备模式和 M2 服务 / SITL 链；S0 用隔离夹具验证来源契约。可选 VLM 分析通路有独立来源测试，常驻本轮未启用 VLM 分析，记录为 not_run；不把规划实调当成视觉识别验收。S2 导入、S3 网关、资源 / 工作流及硬件仍是后续工作。入口覆盖 HTTPS/API/WebSocket 与 JS 语法，没有新增浏览器截图视觉验收。

## 复现

在候选或只增加文档 / 证据的后代提交上运行；输入保持原字节，尤其不要重排 source-views 与 flight.json：

```powershell
uv run python scripts/verify_p0_release.py --sha de597d0eb7430260285e0965dc07f7f904ab5e70 --deployment docs/verification/p0-2026-09-25-r2/deployment.json --e2e docs/verification/p0-2026-09-25-r2/e2e/m2-nominal.json docs/verification/p0-2026-09-25-r2/e2e/m2-recovery-outage.json docs/verification/p0-2026-09-25-r2/e2e/m2-refused-scope.json --views-dir docs/verification/p0-2026-09-25-r2/source-views --live-probe docs/verification/p0-2026-09-25-r2/live-nominal.json docs/verification/p0-2026-09-25-r2/live-cancel.json --live-flights-dir docs/verification/p0-2026-09-25-r2/live-flights --output outputs/p0-release-recheck.json
```

门禁只有全部必需项通过才返回 passed。下一阶段按 [P1 方案](p1-implementation.md)从资源契约与可审查的持久化设计开始；P0 不替代 H1/JIL，也不授权数据库迁移或真实设备。
