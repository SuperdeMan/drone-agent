# P1 实现与验收记录

[路线图](roadmap.md) · [P1 方案](p1-implementation.md) · [存储设计](p1-storage-design.md) · [运营架构 §11](architecture/09-operations.md) · [发布门禁](../scripts/verify_p1_release.py)

**P1 已完成（2026-09-26，软件 / SITL 范围）。** 候选 `b49701b` 的九项门禁全部通过：3 站 / 3 逻辑机场 / 3 台逻辑 UAV 的 S0 故障矩阵、单机 PX4 SITL 经虚拟机场的正式链路、M2 同版本回归，以及带 P1 目录与已迁移账本的常驻任务台。S0 与 S1 分开计数：S0 的逻辑飞行不是物理仿真，S1 只有一台 PX4 SITL；虚拟机场不是机场硬件或厂商协议。P2–P5 尚未实现；原 M3 仍因 JIL 缺席而未关闭。

## 版本与门禁

| 项目 | 准确记录 |
|---|---|
| 被测 / 部署源码 | `b49701b6386ee7ea88dc05221879786f717d1947` |
| 云端部署 | `20260926T004655Z-4719c375`；[部署回执](verification/p1-2026-09-26/deployment.json) |
| 控制面摘要 | `145fce668763dfe5901c40605e54a11445c9a7505779179956b7051b7f4a53cc` |
| P1 总门禁 | [release.json](verification/p1-2026-09-26/release.json)：`status=passed`，九项均 passed；含能力清单和 10 份输入摘要 |
| 常驻入口 | [控制台](verification/p1-2026-09-26/resident-console-activation.json)与[任务台](verification/p1-2026-09-26/resident-desk-activation.json)均为同一候选；入口已脱敏 |
| 入口验证 | [HTTPS / WebSocket 检查](verification/p1-2026-09-26/desk/http-probe.json)与[资源入口探针](verification/p1-2026-09-26/desk/resources-probe.json) |

后续只增加证据和文档的提交不改变被测源码；不能把这份结果改称其他代码 SHA 的完整验收。

## 已实现范围

- **资源契约与目录**（WP-P1-01）：`fleet/resources.py` 定义项目 / 站点 / 机场 / 机器人绑定、六维机场状态、可派遣判定与预约；拒绝未知字段、真实或厂商后端与断链引用。目录在 `configs/sites/`，测试成员在仓库，常驻入口的真实成员只在云端 secrets。
- **存储**（WP-P1-02/03/06，D056 已批准）：同一账本纯增量升到 schema v2（12 张 `op_` 表），迁移前在线备份并逐表核对；每项资源只有一个有效持有、同活动键幂等、CAS 恰好一次释放。
- **机场入账与模拟**（WP-P1-04）：绑定后端、boot / seq、3 s 新鲜度、1 s 未来偏差、2 份报告对账、粘滞维护锁；动作 ACK 与物理结果分离。模拟器在 `eval/`，服务不读取其真值或故障配置。
- **判定、领取与对账**（WP-P1-05/06/07）：预览 / 准备 / 领取同一纯函数；uplink 拉取时在同一事务重查取消、审批、后端、判定与预约；只凭权威终态 + 地面状态 + 机场新鲜在位证据释放；任务级取消意图屏障阻止领取与自动重飞。
- **项目授权与资源入口**（WP-P1-09/10）：角色 ∩ 信任上限，所有任务 / 资源 / 媒体 / 订阅调用校验项目，不可读一律 `service.not_found`；`dock:` 身份只能报告与领取动作。任务台显示资源、年龄、来源与原因，提供项目绑定提交、领取前取消与管理员解锁，没有注入或飞控接口。
- **验证底座**（WP-P1-08/11/12）：S0 世界（真实机载 uplink / guardian / executive + 逻辑飞行 + 逻辑机场）、S1 云端用例、S0 / S1 独立裁判（含反向验证）与门禁。

机载 executive、guardian、适配器、wire、技能与恢复策略均未改动，门禁的 scope 判据据此确认不需要重跑 M1 飞行矩阵。

## 检查结果

| 判据 | 实际结果 |
|---|---|
| scope | 相对 P0 候选 `de597d0` 变更 98 个文件，全部在 P1 路径与文档内；机载 executive / guardian / 适配器、wire、技能、平台与恢复策略未改，M1 回归按规则不需要 |
| checks | Linux **1102 项通过，0 失败 / 错误 / 跳过**；契约字段与 v1 wire 冻结复核通过；部署前后其他 30 个容器身份一致 |
| adversarial | 确定性 42/42、脚本自然语言 32/32，授权包 0；真实模型对抗录制 0 条、32 条未录制，不冒充实调对抗结果 |
| s0_matrix | 门禁当场运行 **42/42**（14 类 × 3 种子），在线 / 回放一致；错误派遣、重复派遣、错误释放、项目越权与错误成功全 0；没有作废尝试 |
| s1 | **5/5**，4 次 PX4 SITL 飞行（领取前维护一例按设计不飞）；五项计数全 0，在线 / 回放一致，其他容器不变 |
| m2_regression | **18/18**（12 完成、6 合理不飞），错误成功 0，回放一致；脚本规划，三批隔离均成立 |
| desk | 目录 `p1_s1_v1`，迁移 `migrated`，副本演练 `passed`；页面、脚本与健康为同一候选；资源入口显示项目角色、机场年龄 / 来源 / 会话与判定原因，5 类注入 / 控制帧被拒，页面无控制帧；机场后端无网络、无 Docker 权限 |
| desk_session | MiniMax-M3 实调任务 `completed`，1 次飞行；经 `campus_s1` 绑定、领取闸门与对账释放；监管者裁判在线 / 回放一致，权威 flight.json 与任务、版本、epoch、时间和 SHA 一致 |
| historical | 原 M3-SITL passed、总状态 not_passed、JIL missing；P0 passed；两份历史回执字节不变，不作为本候选成绩 |

S0 的 14 类故障与期望（每类 × 种子 7 / 19 / 41，全部由独立裁判在线与按录制各判一次、结论一致）：

| ID | 注入 / 操作 | 实际判定 |
|---|---|---|
| F01 | 正常设备、能源与审批 | 单次飞行完成，释放经对账 |
| F02 | 充电中 / 冷却中 / 返程能源不足 | 3 个任务均不派遣，原因分别为 `energy.charging` / `energy.cooling` / `energy.insufficient` |
| F03 | 维护锁后迟到正常遥测、操作者解锁 | 锁不被遥测解开，操作者解锁被拒；admin 解锁后才起飞 |
| F04 | 机场离线、未绑定后端的报告 | 3 s 后 `dock.status_stale`、不领取；错后端报告拒收；恢复报告后完成 |
| F05 | 未来时间、倒序 seq、已替代 boot | 三种报告均拒收且不延长有效期；重启的机场新会话对账后才计入 |
| F06 | 开盖 ACK 到达但舱盖卡滞 | `dock.lid_jammed`，0 次飞行；开盖动作以 failed 结束，不以 ACK 当完成 |
| F07 | 飞行后充电 ACK 成功但电量不增长 | 首个任务完成；机场保持 `energy.charging`，不按计时变 ready，下一任务不被领取并取消 |
| F08 | 两任务争用同一机场 | 同时只有一个持有者，第二个审批在第一个对账前被拒；重复提交返回同一任务；两次飞行先后完成 |
| F09 | 落地后账本行丢失 | 持有转 `uncertain`，服务重启后仍保留并阻止另一审批；行到达后恰好释放一次 |
| F10 | 机场称在位而机体称在空中 | `presence.conflict` / `robot.airborne` 阻断并公开冲突，不挑选任一方 |
| F11 | 领取前维护 / 审批过期 / 取消；领取后取消 | 前三者都不交付；领取后的取消在首个运行步骤由服务转发送达，任务 `incomplete` 且不再起飞 |
| F12 | 飞行中服务与机场重启、重复与迟到 ACK | 收敛为一次飞行；新会话对账；2 次多余 ACK 被忽略；持有恰好释放一次 |
| F13 | 跨项目读取 / 媒体 / 审批 / 操作 / 提交 / 资源，viewer 审批、裁剪角色、A2A 提交 | 全部拒绝且不泄漏数据，不签发任务包 |
| F14 | 声称真实来源、其他后端、未知机场、人员调用后端方法、客户端指定后端、真实设备目录 | 全部拒绝；飞行保持逻辑标签 |

S1 在 PX4 SITL 的 `uav_01` 上经逻辑机场 `dock_s1` 运行：正常 × 3 种子各飞 1 个版本并完成；领取前维护锁 0 次飞行、任务取消；机场重启后新会话对账再完成。计数与裁判规则同 S0，另加 M2 飞行裁判的在线 / 回放一致性。

## 常驻任务台迁移

常驻任务台账本是真实的历史 v1 数据（30 个任务、32 个版本、1084 个事件）。激活时先在副本上演练：备份校验、迁移到 v2、`integrity_check=ok`、12 张新表，v1 全表转储摘要迁移前后相同（`0759157984451dda…`）。演练通过后才对正式账本迁移，迁移前备份 `ledger-v1-20260926T010241096166Z.sqlite3`（sha256 `661e371c95051f38…`，逐表行数与迁移前一致）保留在云端状态目录。回退路径保持 D056：部署上一版本（v1 代码不读新表）或停服恢复该备份。

迁移后任务台以 `p1_s1_v1` 目录运行：历史任务归 `legacy_m2`（第一方只读），新任务经 `campus_s1` / `uav_01` / `dock_s1` 提交。实调会话 `m-321d8a1a50d3` 使用 MiniMax-M3（`planner-v1`）规划，预览 / 准备 / 领取三次判定均 eligible，uplink 经闸门领取，飞行完成后预约以 `reconciled` 释放（证据含终态、机体状态时间与机场 boot / seq），监管者裁判在线与回放一致、错误成功 0。

## 运行清单

| 批次 | 云端运行 | 结果 / 回执 |
|---|---|---|
| S0 矩阵（门禁当场运行） | 本机干净候选 | 42/42，没有作废尝试；摘要 `5ec8cb0b9846c240…`，[门禁记录](verification/p1-2026-09-26/release.json) |
| S1 | `p1-20260926T005031Z-536ca94e` | 5/5；[回执](verification/p1-2026-09-26/s1/p1-20260926T005031Z-536ca94e.json) |
| M2 红 / 蓝资产 | `m2-20260926T010544Z-57289f52` | 6/6；[回执](verification/p1-2026-09-26/e2e/m2-nominal.json) |
| M2 影像降质 / 服务中断 | `m2-20260926T011509Z-983d67c2` | 6/6；[回执](verification/p1-2026-09-26/e2e/m2-recovery-outage.json) |
| M2 拒答 / 越界范围 | `m2-20260926T012815Z-9d316d0d` | 6/6；[回执](verification/p1-2026-09-26/e2e/m2-refused-scope.json) |
| 任务台实调 | `m-321d8a1a50d3` | completed，v1，replans=0；[会话](verification/p1-2026-09-26/desk/session.json)，[flight.json](verification/p1-2026-09-26/desk/flights/m-321d8a1a50d3-v1.json) |

M2 回归用脚本规划回答，只证明服务改动后的执行链与边界；三批分开运行，每批其他容器身份前后一致。S0 大目录保存在本机 `outputs/`，门禁记录其摘要；S1 与 M2 的大文件留在云端 artifacts，回执内有逐文件摘要。

## 开发负记录与边界

| 版本 / 运行 | 发现 | 处理与计数 |
|---|---|---|
| `c6ac4fa` 首轮 S1 | 机场模拟器以无能力 root 运行，无法写入他人拥有的日志目录，5 例全部等不到机场会话 | 由编排为该目录设定可写权限，不给容器加能力；[负记录](verification/p1-2026-09-26/diagnostics/c6ac4fa-s1-dock-log-permission.json)，不计最终门禁 |
| `5ed5fda` 云端检查 | 新门禁测试读取 Git 历史，云端检查镜像是没有 .git 的归档 | 只替换测试中的 Git 读取边界并先在本机归档副本全量验证；[负记录](verification/p1-2026-09-26/diagnostics/5ed5fda-cloud-checks.json) |
| 本机开发 S0 | 主机休眠约 2 h，重连后裁判把停顿期间的状态误计为释放异常 | 裁判只统计领取之后的释放；主机停顿使尝试作废并重跑（最多 3 次），作废记录保留，不计通过或失败 |
| 本机开发 S0 | 正常落地后结果同步有数百毫秒延迟，持有短暂转 `uncertain` | 增加 5 s 结果宽限（`result_grace_s`），超时仍转 `uncertain`；不放宽释放证据 |
| 本机开发 F05 | 倒序报告的 seq 参照了被拒收的未来报告，裁判配对也复用了 boot / seq | 场景先恢复正常报告再倒序；裁判按每次受理配对其之前最后一次发送 |
| 迁移演练 | `LIKE 'op_%'` 的下划线通配命中 v1 的 `operations` 表；备份副本带 WAL 旁路文件 | 改为精确正则筛选；备份副本切换为 DELETE 日志模式后再校验 |

以上开发期结果只用于定位，没有作为候选成绩；最终候选另跑本页全部准出验证。

## 已知限制

- 固定绑定、单候选：多候选排序、时空预约、跨站接力、异地着陆属于 P3。
- 失败或无结果的开盖动作不会自动重发：投递等待至审批过期后作废；已领取但一直未起飞的任务保持占用直到有证据，不按时间释放。
- 站点地图由 M2 园区派生，只改登记表 ID 与降落点归属；规划器仍用基础场景。
- 常驻任务台只有一个逻辑机场与一台 PX4 SITL；机场电量是逻辑模型，不是 PX4 电池。
- 入口覆盖 HTTPS / API / WebSocket 与资源帧；没有新增浏览器截图视觉验收。

## P2 接手物

资源与原因码（`p1-dispatch-v1`）、稳定项目身份与角色、预约与活动键、领取闸门与任务级取消意图屏障、`missions.submit` / 资源查询 / 对账入口、运行来源、单机场 S1 配置与完整回执。P2 的流程状态、inbox / outbox 同样是 schema 变更，须先设计并获批准。P1 完成不证明持久工作流、多机飞行、VLM 识别或机场硬件已就绪。

## 复现

在候选或只增加文档 / 证据的后代提交上运行；输入保持原字节（LF），S0 矩阵由门禁当场重跑，约 10 分钟：

```powershell
uv run python scripts/verify_p1_release.py --sha b49701b6386ee7ea88dc05221879786f717d1947 --deployment docs/verification/p1-2026-09-26/deployment.json --s1 docs/verification/p1-2026-09-26/s1/p1-20260926T005031Z-536ca94e.json --m2 docs/verification/p1-2026-09-26/e2e/m2-nominal.json docs/verification/p1-2026-09-26/e2e/m2-recovery-outage.json docs/verification/p1-2026-09-26/e2e/m2-refused-scope.json --desk docs/verification/p1-2026-09-26/resident-desk-activation.json --desk-http docs/verification/p1-2026-09-26/desk/http-probe.json --desk-resources docs/verification/p1-2026-09-26/desk/resources-probe.json --desk-session docs/verification/p1-2026-09-26/desk/session.json --desk-flights-dir docs/verification/p1-2026-09-26/desk/flights --output outputs/p1-release-recheck.json
```

门禁只有全部必需项通过才返回 passed。下一阶段按[近期批次](operations-implementation.md)进入 P2；P1 不替代 H1/JIL，也不授权真实设备或机场硬件。
