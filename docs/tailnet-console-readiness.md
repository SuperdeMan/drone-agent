# Tailnet 云端控制台验收（2026-09-22）

D028 将固定 M1 仿真入口常驻到云端，网络准入使用既有 Tailscale，不新增应用登录页。使用方式见 [Tailnet 控制台指南](tailnet-console.md)。本次没有改变飞行控制、恢复策略或 wire 契约。

## 准确版本与组件

| 项目 | 版本 / 边界 |
|---|---|
| 应用、网页、机内任务代理与运行时 | `f0caa9419bc97f6584ecbcf771cfe6c414470ec2` |
| 部署 ID | `20260921T171707Z-8ad8f602` |
| 控制面 SHA-256 | `2b77621661752e501b8d2127613a3a4efcceb879c97f81f3f49a0ab299b286a4` |
| 私网入口 | 现有 Tailscale 节点的 HTTPS `8447`，由 `console-cloud --status` 返回完整地址 |
| 宿主后端 | 仅 `127.0.0.1:8768` |
| 进程 | 受限网页容器 + `drone-agent-console-broker.service`，私有 Unix socket 通信 |
| 访问控制 | 既有 tailnet 策略；不修改 ACL，不启用 Funnel，不提供成员角色隔离 |

本地和云端均通过 481 项 Python 测试，云端失败/错误/跳过为 0；ruff 和 4 项前端状态测试通过。新增测试包含真实 ASGI HTTP、来源与 nonce 拒绝、代理方法/peer 身份限制、证据摘要、缓存恢复、真实端口发布与网络成员校验。

源代码部署回执见 [deployment.json](verification/tailnet-console-2026-09-22-deployment.json)，入口激活和实际容器隔离见 [activation.json](verification/tailnet-console-2026-09-22-activation.json)。真实 tailnet 地址仅由运行命令返回，归档中已脱敏。

## 已核对的入口边界

- 本机通过真实 Tailnet HTTPS 访问页面、状态与健康接口，使用系统默认 TLS 验证，没有跳过证书校验。
- 无 nonce、错误 Origin、额外启动字段分别被拒绝；没有用登录页替代这些操作保护。
- 从本机直接连接服务器公网管理 IPv4 的 `8768` 与 `8447` 均不可达；Docker 实际发布记录仅为回环地址，Serve 的 Funnel 标志为关闭。
- 其余五个 Serve 映射前后摘要相同，其他 30 个容器身份摘要相同。
- 原始记录及版本目录只读挂载；网页无 Docker socket、SSH 密钥或飞控连接，UID/GID 为工作区属主。独立入口桥接网络不连接仿真网络，不宣称禁止所有出站流量。

边界探针见 [boundary.json](verification/tailnet-console-2026-09-22-boundary.json)。专用代理处于 enabled/active，`KillMode=process`、用户目录只读及项目内 Docker 缓存设置均已读回，见 [service.json](verification/tailnet-console-2026-09-22-service.json)。

## 飞行验证

真实 Tailnet HTTPS 共 **9/9 轮通过**，全部绑定同一 `f0caa94`：

| 操作 | 种子 | 分类 |
|---|---|---|
| 正常巡检 | 7 / 19 / 41 | 3 次 `completed` |
| 暂停、显式恢复 | 19 / 41 | 2 次 `completed` |
| 暂停期间重启网页与代理，再显式恢复 | 7 | 1 次 `completed` |
| 巡航取消 | 7 / 19 / 41 | 3 次 `safe_abort` |

错误成功报告为 0，9 次在线裁判/离线回放一致。每轮都验证了遥测与相机更新、重复启动/操作的幂等返回、并发新任务拒绝、机载操作接受和落地上锁终态；9 个正式证据页均从私网入口生成。189 个裁判覆盖的原始文件在云端重新核对摘要，无缺失或差异。最差单场景监督周期 p99 为 103.7 ms、最大周期 105.8 ms，见 [runs.json](verification/tailnet-console-2026-09-22-runs.json)。

重启场景中原后台飞行进程身份和任务 ID 均保持不变，机载任务接受事件仅 1 次；旧页面 nonce 被拒，刷新后恢复并完成。重启前已生成的证据页链接也再次读回成功。

最后保留的正常巡检为 `m1-20260921T173858Z-388362ad`（种子 41），任务完成并保存照片。随后未解锁环境冒烟通过，控制命令发送数 0，见 [post-smoke.json](verification/tailnet-console-2026-09-22-post-smoke.json)。入口返回 200、提供的 JavaScript 与源码一致，见 [entry.json](verification/tailnet-console-2026-09-22-entry.json)；常驻服务最终状态见 [status.json](verification/tailnet-console-2026-09-22-status.json)。

## 开发负记录

| 版本 | 观察 | 处理与范围 |
|---|---|---|
| `0dcfd0b` | 容器内和代理健康，但 Docker 的 internal 网络未实际发布端口（`8768/tcp: null`），宿主就绪检查失败 | 激活回退，未新增 Serve 映射；网页使用独立入口桥接网络，新增实际端口校验；飞行网络未变 |
| `b195748` | 首次任务的 Buildx 尝试写用户 `.docker/buildx/activity`，被 `ProtectHome=read-only` 拒绝，未起飞 | Docker 客户端缓存转到本项目目录；用户目录仍只读；不复制原 Docker 配置或凭据 |

上述运行不作为最终版本的通过证据。部署管理 SSH/SFTP 有过短时连接失败，按已知部署包续传/核对后继续；运行期页面、状态和操作已走云主机本地 IPC，不使用逐请求 SSH。

负结果摘要见 [development.json](verification/tailnet-console-2026-09-22-development.json)。

## 验收边界

浏览器自动化服务无法启动 `codex app-server`，没有实际浏览器点击与截图级视觉复核；HTTPS 接口、前端状态逻辑和真实仿真分别验证。M1 原 66 组完整矩阵仍属于 `eefe76e`，D027 的验证仍属于 `cdd57ad`；不转借到本提交，也不据此关闭 M2 或真机能力。

总索引与归档文件摘要：[tailnet-console-2026-09-22.json](verification/tailnet-console-2026-09-22.json)。
