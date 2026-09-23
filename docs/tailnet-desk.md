# M2 任务台 · Tailnet 常驻入口

D035 把 M2 任务台常驻到云端。在已接入 tailnet 的设备上打开入口，用自然语言提交巡检任务，查看规划、准入与任务包，批准后由云端仿真飞行，最后得到三列报告、机载相机证据与独立裁判结果。飞行只在仿真中进行；任何一步都不直达飞控。

D028 的 M1 固定任务入口（`8447`）保持不变，见 [Tailnet 控制台指南](tailnet-console.md)。

## 访问

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
uv run python scripts/dev_stack.py desk-cloud --status
```

返回的 `origin` 形如 `https://<已有节点名>.<tailnet>.ts.net:8448`，就是浏览器入口。真实 tailnet 名称只由命令返回，不写入仓库。

身份沿用 D033：Tailscale Serve 为 tailnet 用户注入的 `Tailscale-User-Login` 就是操作者身份，可提交、审批、驳回、暂停、恢复与取消；没有该头的会话（如 tagged 设备）只读。不新增登录页，不启用 Funnel。A2A 端点 `/a2a` 当前没有配置调用方，一律返回 401。

## 使用

1. **提交**：写下目标（例如「检查起飞点东侧的红色设备标记，带回一张清晰照片」），选择飞行体积，可勾选资产。页面标明规划器：配置了 MiniMax key 时是 `MiniMax-M3` 实调；否则是带标注的脚本回答，只认 M2 场景集中的请求原文，其他文本不会得到规划。
2. **审批**：通过准入的版本显示任务包节点与哈希。批准即对该哈希签名；审批后任何改动都会让签名失效，机载会拒收。准入拒绝或模型拒答的请求不会出现审批按钮。
3. **飞行**：右侧「云端飞行」显示仿真监管者的状态。批准后任务包经 mTLS 下发到机载 uplink，验签后进入 inbox；监管者取得项目锁后，先让飞控断电重启并预热（地勤换电），再启动 guardian 与 executive。其他仿真批次或部署占用云端时显示「等待云端工作区」。审批的有效期不够跑完一整架次时不会起飞，页面显示「未起飞」。
4. **操作**：飞行中的暂停、恢复、取消经任务服务 → uplink → 操作者信箱 → executive，由 executive 与 guardian 复核任务、版本、代次、步骤与时效；页面按钮不直达飞控。被取消的任务不会自动重规划（D035）；需要再飞时重新提交。
5. **结果**：报告的「已完成」只接受「执行成功 ∧ 效果已证实」，且服务侧复核一致。证据可点开查看机载相机图像。任务到达终态后，监管者把本任务的飞行产物、真值、任务包与服务视图复制到独立用例，在线与回放各跑一次独立裁判，页面显示分类、错误成功报告数与回放一致性。页面只展示，不产生判定。

影像降级等导致巡检未证实时，服务按 D032 自动批准一次原样重试（新版本、新代次，落地后才切换），每任务最多 2 次。

## 部署

先按既有流程部署并验证一个已提交版本，再激活任务台：

```powershell
uv run python scripts/dev_stack.py deploy --sha HEAD --apply --artifacts D:/drone-agent-cloud
uv run python scripts/dev_stack.py m2-key --apply      # 可选：把本机环境的 MINIMAX_API_KEY 写入云端 secrets
uv run python scripts/dev_stack.py desk-cloud           # 只读计划
uv run python scripts/dev_stack.py desk-cloud --apply
uv run python scripts/dev_stack.py desk-cloud --status
```

激活时构建该版本的 M2 镜像，在 `secrets/desk/` 生成任务台自己的签名密钥与证书（重复激活保留签名密钥），启动常驻容器，安装 `drone-agent-desk-supervisor.service`，最后新增 Serve `8448` 映射。它核对页面、服务与监管者都运行该版本，核对容器的实际端口、网络、挂载与用户，并确认其他容器与 Serve 条目（含 `8447` 与 car-agent 的入口）没有变化；任何一步失败都只回退本项目的这些组件。写入或更换模型 key 后，重新 `--apply` 一次，规划器才会切换。激活与飞行使用同一个项目锁，飞行中激活会被拒绝。

## 进程与边界

| 组件 | 运行方式 | 边界 |
|---|---|---|
| `desk` 页面 | Uvicorn 单进程 ASGI，0.4 CPU / 512 MiB | 只读根、属主 UID、无 capabilities；只发布 `127.0.0.1:8769`，独立 `desk_ingress` 网络；只读挂载 API 套接字目录与监管者公开状态；没有 Docker socket、SSH 密钥、签名私钥、模型 key 或飞控连接 |
| `desk-service` 任务服务 | `fleet.main --planner auto`，0.5 CPU / 512 MiB | 只接入内部 `desk_uplink` 与 `desk_model` 网络，没有直接出站；签名私钥、服务证书与模型 key 只读挂载；账本、媒体与录制在 `~/drone-agent/desk/service/` |
| `desk-model-proxy` 模型出站代理 | `fleet.model_proxy`，0.2 CPU / 128 MiB | D036：唯一接入可出站 `desk_egress` 网络的常驻容器；只为 `api.minimaxi.com:443` 建立 CONNECT 隧道，TLS 端到端，看不到内容与 key；其他目标一律拒绝并记录；不挂载任何目录 |
| `desk-uplink` 机载 uplink | 0.3 CPU / 256 MiB | 只接入 `desk_uplink`，主动拨出 mTLS；只读挂载飞行产物；够不到 guardian 套接字 |
| 仿真监管者 | systemd unit，工作区属主，`KillMode=process` | 只读 uplink 写入的已验签任务包，不接收网页或网络输入；飞行时持有项目锁；重启时接管进行中的飞行，不重飞 |
| 飞行容器 | `desk-sitl` / `desk-collector` / `desk-guardian` / `desk-executive` / `desk-judge`，profile `flight` | 与端到端用例相同的网络与挂载：executive 无网络，guardian 只在仿真网络；不做故障注入 |

机器人目录 `~/drone-agent/desk/robot/`（inbox、信箱、代次水位、各版本飞行产物）跨任务持久，代次单调递增。每架次的仿真侧记录（真值、传感器、ULog、PX4 控制台）在 `~/drone-agent/desk/flights/<任务>-v<版本>/`，裁判用例在 `~/drone-agent/desk/judge/`。

## 核对

验收经真实 Tailnet HTTPS 进行，使用 `scripts/desk_probe.py`（在 tailnet 设备上运行）：

```powershell
uv run python scripts/desk_probe.py http --origin <origin>
uv run python scripts/desk_probe.py spoof --origin <origin>
uv run python scripts/desk_probe.py session --origin <origin> --text "<请求>" --asset asset_red --approve
uv run python scripts/desk_probe.py session --origin <origin> --text "<请求>" --asset asset_red --approve --pause-step return_home
uv run python scripts/desk_probe.py session --origin <origin> --text "<请求>" --asset asset_red --approve --cancel-step inspect_asset_red
```

探针按页面相同的 hri.v0 协议驱动任务并记录服务、监管者与裁判报告的内容，回执对 tailnet 主机名与操作者登录名脱敏；它本身不做判定。`watch --mission <任务ID>` 可接上已有任务直到裁判结果公布。当前版本与验收结果见 [任务台验收记录](tailnet-desk-readiness.md)。

如果以后出现只读成员、多角色、A2A 调用方、公网访问、并发飞行或真机，需要按 D035 的重估触发器重新决定身份、授权与部署。
