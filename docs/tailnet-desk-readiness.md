# M2 任务台 Tailnet 常驻入口验收（2026-09-23）

D035 把 M2 任务台常驻到云端，经既有 Tailscale Serve 的独立 HTTPS 端口 `8448` 访问。使用方法见 [任务台指南](tailnet-desk.md)。下列结论全部来自同一版本 `b9cf00c`，经真实 Tailnet HTTPS 从本机取得。本轮规划器是带标注的脚本回答：云端尚未写入 MiniMax key，任务台内的实调规划还没有验证。

## 准确版本与组件

| 项目 | 版本 / 边界 |
|---|---|
| 应用、页面、任务服务、uplink、监管者 | `b9cf00cccc99411d6676643fe4fa70a6b6d25042` |
| 部署 ID | `20260923T113254Z-bb8c2a70`；控制面 SHA-256 `0475da8dad21a8916f3cb41975a96029c08910ae76ef69652d55843f7087160d` |
| 云端测试 | 805 项，失败 / 错误 / 跳过均为 0（Linux 检查镜像）；部署后未解锁冒烟通过 |
| 镜像 | `drone-agent-m2-sim` / `drone-agent-m1-aircraft` / `drone-agent-m1-ground`，标签 `b9cf00c…-0475da8dad21` |
| 私网入口 | 现有 Tailscale 节点的 HTTPS `8448`（`desk-cloud --status` 返回完整地址）→ 宿主 `127.0.0.1:8769` |
| 常驻进程 | `desk`（页面）、`desk-service`（任务服务）、`desk-uplink`（机载 uplink），均以工作区属主 UID 运行，只读根、无 capabilities；`drone-agent-desk-supervisor.service`（`KillMode=process`） |
| 信任根 | `secrets/desk/`：审批签名密钥 `ed25519:6563c47cfafa2a22`；CA / 服务 / 机器人证书指纹见激活回执 |
| 规划器 | `scripted`（`drone.planner.scripted/v1`，只认 M2 场景集原文，页面与规划记录都标明）；未写入模型 key |
| 访问控制 | 既有 tailnet 策略；写操作只认 Serve 注入的登录身份；不改 ACL，不启用 Funnel；A2A 未配置调用方 |

部署回执见 [deployment.json](verification/tailnet-desk-2026-09-23-deployment.json)，激活、容器实际边界与隔离核对见 [activation.json](verification/tailnet-desk-2026-09-23-activation.json)。归档中的 tailnet 主机名与操作者登录名已脱敏。

## 激活与隔离

- 激活核对页面与服务的 `/health` 都报告 `b9cf00c`，规划器标签与计划一致（`scripted`），监管者 unit 为 active。
- 三个常驻容器的实际发布与网络：只有 `desk` 发布 `127.0.0.1:8769`，只接入独立的 `desk_ingress`；`desk-service` 与 `desk-uplink` 不发布端口，只接入内部 `desk_uplink`。页面只读挂载 API 套接字目录与监管者公开状态。三者均无 Docker socket 或 SSH 路径。
- 激活前后其他 30 个容器的身份摘要相同；Serve 中除 `8448` 外的全部条目（含 D028 的 `8447` 与 car-agent 的入口）摘要相同，全部验收结束后再核对一次仍相同。Funnel 关闭。

## 入口边界

经真实 Tailnet HTTPS（系统默认 TLS 校验），见 [http.json](verification/tailnet-desk-2026-09-23-http.json) 与 [spoof.json](verification/tailnet-desk-2026-09-23-spoof.json)：

- 页面 200，内容安全策略显式列出本入口的 `wss://` 源；`/mission.js` 与本地检出逐字节一致；`/health` 报告服务就绪、页面与服务版本均为 `b9cf00c`、监管者空闲。
- Agent Card 只公布 `mission.submit` 与 `mission.read`；`/a2a` 无 token 与未知 token 都返回 401（`auth.token_invalid`）。
- 跨源 WebSocket 握手被拒（403）。
- 客户端伪造 `Tailscale-User-Login` 时，会话身份仍是 Serve 注入的真实 tailnet 登录名，伪造值没有被采信。

## 任务验收

五个任务均由探针按页面相同的 hri.v0 协议驱动，身份为 tailnet 操作者；「裁判」指监管者在任务终态后运行的 `judge_m2`（在线与回放）。

| 任务 | 请求 | 过程 | 结果 |
|---|---|---|---|
| `m-5c9319d2718a` | 红色标记（中文原文） | 批准 → 预热后飞行（代次 4）→ 巡检证据机载判定 verified、服务复核一致 | 已完成；裁判「完成」、错误成功 0、回放一致；全程 137 s |
| `m-e8607755fe6c` | 蓝色标记；飞行开始 8 s 后同时重启页面容器与监管者 | v1 起飞 2.4 s 时 guardian 以 `geofence_predicted_breach` 悬停，随后返航降落上锁，起飞结果 `unknown / unverified`；巡检未运行，服务按 D032 自动批准原样重试；v2 换电重启后以代次 6 完成 | 已完成（重规划 1 次）；v1 记录为「重启后接管」，没有重飞；探针重连 1 次；裁判「完成」、错误成功 0、回放一致 |
| `m-49720861d286` | 红色标记；巡检中取消 | 取消经服务 → uplink → 信箱送达，guardian 正常收尾落地（代次 7），巡检记为 `cancelled / unknown` | 未完成；记录 `replan.cancelled_by_operator`，没有自动重飞；裁判「未完成」、错误成功 0、回放一致 |
| `m-b34bec793283` | 飞到邻居后院拍窗户 | 规划器拒答（`planner.refused`） | 不出现审批，没有投递与飞行 |
| `m-a4a3298e0f62` | 「顺便拍所有标记」，范围只有红色资产 | 准入拦截：`scope.asset_not_requested` ×2、`energy.budget_exceeded` | 不出现审批，没有投递与飞行 |

各任务回执：[标称](verification/tailnet-desk-2026-09-23-session-red.json)、[重启与重规划](verification/tailnet-desk-2026-09-23-session-blue-restart.json)、[取消](verification/tailnet-desk-2026-09-23-session-red-cancel.json)、[拒答](verification/tailnet-desk-2026-09-23-session-refused.json)、[越界](verification/tailnet-desk-2026-09-23-session-scope.json)，重启时刻与进程号见 [restart.json](verification/tailnet-desk-2026-09-23-restart.json)。

- 每架次都从全新启动的仿真器开始：从批准到起飞约 50–75 s（仿真启动、PX4 设置 home 后再等 5 s），起飞到落地约 45 s，任务终态后约 15 s 公布裁判结果。飞行期间 M0 空闲仿真暂停，结束后恢复；验收后的未解锁冒烟通过。
- 机器人代次水位跨任务、跨激活单调递增：开发版本上的三架次用了 1–3，本版本依次为 4、5、6、7。
- 三个飞行任务的页面「允许操作」在每个步骤都只有取消：M2 巡检任务包的起飞、巡检、返航、降落在技能清单中都不可暂停，页面不再提供机载必然拒绝的暂停。

## 开发中发现并修正

| 版本 | 观察 | 处理 |
|---|---|---|
| 部署前 | uplink 按任务 ID 字典序取状态文件，跨任务时会报告旧的飞行阶段，并可能卡住重规划版本的投递；另外每 0.5 s 重读全部历史账本 | `0fc9ed3`：取最近写入的状态文件，缓存已绑定的飞行；新增跨任务状态测试 |
| 部署前 | 起飞阶段取消后，巡检节点被当作 `not_run` 自动重试，会违背操作者意图再次起飞 | `4cfe8fd`：有 `cancelled` 步骤的版本不重规划，记录 `replan.cancelled_by_operator`（D035） |
| `dd0b3ff` | 页面在返航步骤提供暂停，guardian 以技能不可暂停拒绝（`operator_rejected guardian_rejected`），飞行照常完成、裁判通过 | `5dffbd7`：服务按技能清单的 `pausable` 预判，与 guardian 同一规则，最终仍由机载决定 |
| `dd0b3ff` | 页面重启期间 Serve 拒绝握手，探针重连时崩溃（飞行本身不受影响，被接管后完成） | `b9cf00c`：握手被拒按断线重连；新增 `watch` 跟随既有任务 |

开发版本上的三次飞行（标称、重启、暂停被拒）均完成且裁判通过，摘要见 [development.json](verification/tailnet-desk-2026-09-23-development.json)，暂停被拒的完整回执见 [development-pause-rejected.json](verification/tailnet-desk-2026-09-23-development-pause-rejected.json)。它们不作为 `b9cf00c` 的验收证据。

## 观察记录

- **新启动仿真器的起飞中止**：`m-e8607755fe6c` v1 起飞时，PX4 控制台记录了 IMU 时间戳错误，guardian 以围栏前瞻越界悬停并返航。这是安全中止：报告没有把它记为完成，服务按策略原样重试后完成。它与端到端试跑中「新启动仿真的时序问题」同类，围栏与遥测阈值不变。
- **落地后的心跳丢失记录**：executive 写出任务结果后约 2 s 退出，guardian 随即记录 `executive_heartbeat_lost`（悬停）并转为 `landed_disarmed`。这发生在落地上锁之后，是与端到端编排相同的既有语义，裁判不计为问题；页面事件列表会显示这一条。

## 相关入口

D028 的 M1 固定入口在同一版本上重新激活，消除此前「页面 `f0caa94` / 运行时新版本」的状态不一致：激活核对通过，其他容器与 Serve 条目不变，状态 ready，见 [console-activation.json](verification/tailnet-desk-2026-09-23-console-activation.json)。这不是 D028 九轮飞行验收的重跑，D028 的飞行证据仍属于 `f0caa94`。

## 验收边界

- 本轮规划器是脚本回答；任务台内的 MiniMax-M3 实调规划需要写入模型 key 并重新激活后另行验收。M2 门禁的实调准入率基线同样待 key（见 [M2 验收记录](m2-readiness.md)）。
- 没有浏览器点击与截图级视觉复核：探针实现页面相同的 hri.v0 协议，页面脚本与检出一致，但页面渲染本身没有自动化检查。
- 暂停 / 恢复无法在 M2 巡检任务包上端到端演示（没有可暂停的步骤）；恢复路径由 M1 与单元测试覆盖。
- 多版本、换电重启与策略自动批准由 `m-e8607755fe6c` 覆盖；影像降级触发的重规划没有在任务台上复现（任务台不做故障注入），由 M2 端到端用例覆盖。
- 本轮不配置 A2A 调用方，不开放公网，不涉及真机。

总索引与归档文件摘要：[tailnet-desk-2026-09-23.json](verification/tailnet-desk-2026-09-23.json)。未解锁冒烟见 [post-smoke.json](verification/tailnet-desk-2026-09-23-post-smoke.json)，最终状态见 [status.json](verification/tailnet-desk-2026-09-23-status.json)。
