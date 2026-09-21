# Tailscale 私网云端控制台

D028 将 M1 固定巡检入口常驻到云端。浏览器通过已有 Tailscale 网络打开 HTTPS 入口，不再依赖本机 Python 桥，也不新增应用账号或登录页。可访问此 Serve 端口的 tailnet 成员在当前阶段被视为可信操作者；应用不提供成员之间的角色隔离。

## 访问与部署

先在设备上连接已有 Tailscale 网络，然后使用以下命令查看入口：

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
uv run python scripts/dev_stack.py console-cloud --status
```

返回的 `origin` 是浏览器入口，形如 `https://<已有节点名>.<tailnet>.ts.net:8447`。真实 tailnet 名称由服务器读取，不写入仓库。网页、API 和证据页使用同一入口。

部署已提交版本时，先用既有流程部署与验证源码，再激活网页服务：

```powershell
uv run python scripts/dev_stack.py deploy --sha HEAD --apply --artifacts D:/drone-agent-cloud
uv run python scripts/dev_stack.py console-cloud
uv run python scripts/dev_stack.py console-cloud --apply
uv run python scripts/dev_stack.py console-cloud --status
```

不带 `--apply` 的命令只读展示计划。激活只增加本项目的 `drone-agent-console-broker.service`、`console` 容器及 Serve `8447` 映射；不改变 car-agent 的 443/8443–8446 入口。端口已被非本项目使用或存在 Funnel 时拒绝占用，不重置整个 Serve 配置。应用升级期间与飞行使用同一项目锁。

## 使用

1. 点击「开始云端任务」，观察起飞、巡检、拍照、返航和降落。
2. 暂停与恢复仅在当前技能允许时可用；取消需等待落地上锁和最终裁判结果。
3. 点击「拉取并核对完整证据」，云端直接校验原始文件并生成证据页；不再经 SFTP 下载中转，包含 ULog、传感器文件在内的裁判摘要均须一致。
4. 刷新页面会重新读取现有任务。网页或任务代理重启不会隐含取消或重新启动飞行；重启后会话 nonce 更新，需要刷新页面后提交新操作。

原本机入口 `dev_stack.py console` 继续保留为开发回退。任务、操作、恢复和裁判语义沿用 [实时仿真指南](live-simulation.md)，此入口仍只用于固定 M1 仿真。

## 进程与访问边界

- Tailscale Serve：私网 HTTPS `8447` → 宿主 `127.0.0.1:8768`；Funnel 不启用。网络准入沿用 tailnet 策略，本轮不修改 ACL。
- 网页：Uvicorn 单进程 ASGI；0.4 CPU / 512 MiB、只读根文件系统、非 root、去除 Linux capabilities；使用独立的 `console_ingress` 入口桥接网络，不连接仿真网络。只有宿主回环端口可进入，没有 Docker socket、SSH 密钥或飞控连接；不宣称完全禁止容器出站。
- 任务代理：工作区属主运行的本机 Unix socket 服务，限制 peer UID 与 `live_status/live_start/live_operate` 方法；没有任意命令或文件接口。代理 unit 使用 `KillMode=process`，不杀死已独立运行的飞行后台进程。
- 数据：记录与历史源码只读挂载；生成页面放在 `~/drone-agent/console/pages/`，摘要核对后缓存，网页重启后可恢复链接。
- 浏览器请求：继续检查固定 Host、Origin、nonce、JSON 大小与操作身份；页面掉线时不能使用旧状态自动重发操作。

## 核对

部署回执记录应用 SHA、控制面摘要、网页镜像、容器实际隔离设置、代理 unit 摘要，以及其他容器和 Serve 映射的前后摘要。验收需经真实 Tailnet HTTPS 操作，验证源文件、裁判与回放；不能把本机 HTTP 测试或历史 M1 66/66 当作新入口的通过证据。

当前版本和验证范围见 [Tailnet 控制台验收](tailnet-console-readiness.md)。

如果以后增加只读访客、跨团队共享、Funnel/公网、自定义任务或真机，需要重新决定应用身份和授权。当前简化依赖于私网可信操作者的范围。
