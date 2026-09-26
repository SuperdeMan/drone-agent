# 云端开发与联调

按 2026-09-19 的用户要求，后续 Linux 构建、PX4/Gazebo 仿真和服务联调默认使用现有云服务器。本机负责代码编辑和快速单测；`scripts/dev_stack.py` 只操作 cloud，连接失败不会启动本地 Docker。

初始 M0 部署见 [2026-09-19 历史验收](cloud-readiness-2026-09-19.md)。当前应用 SHA、部署 ID 和状态以 `status` 及本次部署回执为准；历史测试数字不能转借到新版本。

M1 已通过 [2026-09-20 完整验收](m1-readiness.md)，原里程碑运行版本为 `eefe76e`。D027 实时入口补遗的部署版本和新增验证以 [实时入口验收](live-console-readiness.md) 为准；本机页面、云端运行与后续文档归档提交分开记录。

## 当前部署范围

M0 未解锁冒烟继续作为部署前置；M1 另有 sim/aircraft/ground 三镜像、executive/guardian 双进程和独立裁判。M2 在同一工作区增加 `sim2` 仿真镜像（多一个蓝色资产）、ground 镜像中的 mission-service、aircraft 镜像中的 uplink，以及项目 `secrets/` 下的签名密钥与 mTLS 证书；端到端用例按需启停，不常驻。M3（D038）再增加 `m3-aircraft`（ROS 2 Jazzy、外部模式出口节点、事件检测）与 `m3-sim`（x500_vision、深度相机、M3 世界）两个镜像，用例按需启停；M3 SITL 上限 2.0 CPU / 2 GiB（D045），自主层 0.6、出口节点 0.25、事件检测 0.5 CPU。真机的 guardian、控制出口与飞控连接仍在设备侧，云端只验证模拟飞控。

工作区使用 SSH 用户的 `~/drone-agent/`，不放入 car-agent 目录。Docker project 为 `drone-agent-cloud`；SITL 限 1.5 CPU / 2 GiB，契约测试限 1 CPU / 1 GiB。仿真网络为内部桥接，测试容器无网络，二者不发布宿主端口。SSH 用于部署与管理。

D027 增加固定 M1 任务的 [实时浏览器入口](live-simulation.md)：`dev_stack.py console` 在本机回环地址提供页面，经 SSH 读取云端并提交有界操作。云端不增加公开服务端口；它与后续 M2 的 mission-service/审批控制台分开验收。

D028 将网页常驻到云端，使用仅绑定宿主 `127.0.0.1:8768` 的受限容器和专用任务代理，由独立 Tailscale Serve HTTPS `8447` 入口提供私网访问。日常体验不再需要本机桥；`dev_stack.py console-cloud --status` 返回地址，配置与准确版本见 [Tailnet 指南](tailnet-console.md) 和 [验收记录](tailnet-console-readiness.md)。它不启用 Funnel、不改变既有其他映射，也不新增应用登录体系。

配置依据：[Compose 资源属性](https://docs.docker.com/reference/compose-file/services/#cpus)、[内部网络](https://docs.docker.com/reference/compose-file/networks/#internal)。验收还会读取实际容器设置，确认配置已经生效。

D037 将常驻使用入口统一到 `8448`：主页为 M2，`/fixed/` 为 M1。升级顺序是 `deploy --apply` → `console-cloud --apply` → `desk-cloud --apply`，最后用 `desk-cloud --status` 核对同版本健康；使用与新增挂载见 [飞行台指南](tailnet-desk.md)，评审证据见 [2026-09-24 记录](m2-review-2026-09-24.md)。

## 连接参数

连接参数来自进程环境，不写入仓库、部署清单或 `.env`：

| 参数 | 含义 |
|---|---|
| `DRONE_AGENT_DEPLOY_HOST` | 云主机地址或 SSH 主机名 |
| `DRONE_AGENT_DEPLOY_USER` | 用户名，默认 ubuntu |
| `DRONE_AGENT_SSH_IDENTITY` | 已有 SSH 私钥路径，只供本机 SSH 使用 |

没有设置 `DRONE_AGENT_DEPLOY_HOST` 时，复用已配置的 `CAR_AGENT_DEPLOY_HOST` / `CAR_AGENT_DEPLOY_USER` / `CAR_AGENT_SSH_IDENTITY`。一旦指定 DRONE 主机，就必须完整提供它的身份，不能混用另一主机的密钥。脚本不读取 car-agent `.env`，不上传私钥。SSH 使用 `BatchMode` 与严格 known-host 校验；原始 SSH banner 不进入日志。

## 日常命令

在仓库根目录执行：

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
uv run python scripts/dev_stack.py target
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py verify
uv run python scripts/dev_stack.py test
uv run python scripts/dev_stack.py logs
uv run python scripts/dev_stack.py stop
uv run python scripts/dev_stack.py start
```

`verify` 在云端容器内接收遥测并检查 Gazebo 时钟，不发控制命令。`test` 在云端对当前部署的应用 commit 执行完整 pytest；默认 importlib 模式保持不变，失败、错误或跳过都会使本轮验证不通过。测试容器退出后保留，便于审计，不自动删除。

`status` 只读检查目标、容量、镜像与当前项目容器；它不创建工作区、不部署、不启动容器。`stop/start` 仅作用于本项目 SITL。

## M1 飞行与故障验证

先部署已提交版本，再显式运行：

```powershell
uv run python scripts/dev_stack.py m1 --scenario nominal --seeds 7
uv run python scripts/dev_stack.py m1 --scenario all --seeds 7,19,41
```

这两个命令会在云端仿真中解锁/起飞。场景清单为 `configs/scenarios/m1_suite.yaml`；指定部署的不可变源码定义实际任务、种子随机化、注入与裁判。默认 `m1` 跑正常任务的三个种子。任一失败即停止本批扩展并保留产物，不能只按进程退出零判断通过，必须检查回执 `status` 与各项 `passed`。

共享主机的验收默认 `--speed-factor 1`；显式 `--speed-factor 2` 用于单独的加速实验，报告中的 `measured_sim_speed` 才是实际倍率。`--scenario faults` 选择故障子集，也可用逗号列出若干场景；这些子集回执不能单独关闭 M1。完整准出由 `scripts/verify_m1_release.py` 按指定 SHA 中的场景与种子清单核对。

镜像从已校验的检查镜像构建并按 SHA 命名；`bc` 仅安装在仿真镜像内，供 PX4 标准加速时钟启动脚本使用。MAVSDK 固定 `3.17.4`，其电量单位为 0–100，在适配器边界转换为契约的 0–1。航线上传后等待 PX4 异步检查，再确认实际进入 MISSION；依据为 [PX4 官方 MAVSDK 集成测试](https://github.com/PX4/PX4-Autopilot/blob/v1.17.0/test/mavsdk_tests/autopilot_tester.cpp)。

同一时刻仅运行一个场景。SITL 1.5 CPU/2 GiB，guardian 0.6 CPU/512 MiB，executive 0.4 CPU/512 MiB，采集器 0.3 CPU/256 MiB，离线裁判 0.5 CPU/512 MiB；无主机端口。只有 guardian 与模拟飞控共享网络；executive 无网络，只有私有 UDS。真值目录不挂载给机载进程。

产物位于 `artifacts/<deployment_id>/m1-<run_id>/<scenario>-<seed>/`：`input/` 固定任务与注入、`aircraft/` JSONL/MCAP/实际影像/能力快照/进程身份、`truth/` Gazebo 真值、`ulog/` 飞控原始日志、`judge/` 裁判结果。`progress.json` 可查看长批次进度，`build-*.log` 实时写入构建输出。实验结束恢复本项目空闲 SITL；历史记录和旧镜像不自动删除。

离线重判使用同一版本 ground 镜像执行 `python3 -m drone_agent.eval.judge <run目录> --root /workspace --output <结果文件>`。裁判复算影像、真值轨迹、前驱门控与 MCAP 事件一致性。新规则重判必须保留旧结果并标注新的软件 SHA。

## M2 端到端验证

部署已提交版本后运行（会在云端仿真中解锁 / 起飞）：

```powershell
uv run python scripts/dev_stack.py m2 --scenario nl_inspect_red --seeds 7
uv run python scripts/dev_stack.py m2 --scenario all --seeds 7,19,41
```

场景集为 `configs/scenarios/m2_suite.yaml`：红 / 蓝资产的自然语言请求（种子选择三种措辞）、第一版影像被抹平后的策略批准重试、飞行全程任务服务停机、应拒答请求、被骗规划器扩大范围。每个用例依次：生成输入 → 启动 SITL 与真值采集 → 启动 mission-service 与 uplink → 以 `harness:m2-<run>-<case>` 身份经服务 API 提交请求 → 按确切 `package_hash` 审批 → 等 uplink 验签并写入 inbox → 每个任务版本用新 guardian / executive 与新代次飞行 → 等服务镜像追平两本账本 → 导出服务视图 → 在线与 MCAP 回放各跑一次 `eval/judge_m2.py`。回执 `suite.json` 记录签名密钥 ID、证书指纹与其他容器身份。

`--planner scripted`（默认）使用 `m2_prepare` 生成的带标注脚本回答，只证明服务、签名、上行、飞行与报告链路，不能当作模型行为或准入率基线。`--planner live` 需要先把本机进程环境里的 `MINIMAX_API_KEY` 写入云端项目 secrets：

```powershell
uv run python scripts/dev_stack.py m2-key            # 只返回计划
uv run python scripts/dev_stack.py m2-key --apply    # 写入 secrets/m2-model/minimax.key（0600），不回显
```

本机的 key 只放在进程环境里，本项目不读 `.env`（D029）。需要推送或更换云端 key、重跑准入率基线，或者让本机任务台实调时，在自己的 PowerShell 窗口临时设为用户环境变量；输入不回显，也不进命令历史：

```powershell
$k = Read-Host "MINIMAX_API_KEY" -AsSecureString
[Environment]::SetEnvironmentVariable("MINIMAX_API_KEY", [Net.NetworkCredential]::new("", $k).Password, "User")
# 用完删除，云端副本不受影响：
[Environment]::SetEnvironmentVariable("MINIMAX_API_KEY", $null, "User")
```

已经打开的终端和 Claude Code 会话不会自动继承新设的用户变量，需要在命令里读取 `[Environment]::GetEnvironmentVariable("MINIMAX_API_KEY", "User")`，或者重开会话。本机环境若设有 SOCKS 的 `ALL_PROXY`，httpx 会因缺少 `socksio` 在发请求前失败。MiniMax 是国内端点，只在该命令的进程里移除 `ALL_PROXY`，并把 `api.minimaxi.com` 加入 `NO_PROXY` 直连即可。

密钥与证书由 `fleet/provision.py` 在 ground 镜像中以工作区用户身份生成到 `~/drone-agent/secrets/m2/`，重复运行保留签名密钥；它们不进入快照、Compose 文件、镜像或日志。

产物位于 `artifacts/<deployment_id>/m2-<run_id>/<scenario>-<seed>/`：`input/`（场景、脚本回答、故障文件）、`service/`（业务账本、媒体、`ready.json`、实调录制）、`inbox/`、`mailbox/`、`uplink/`、`robot/`、`aircraft/<mission_id>/v<n>/`（每个版本一次飞行）、`truth/`、`ulog/`、`service-export/`、`judge/`。拉取与人工核对沿用 `fetch`；证据浏览器对 M2 用例按任务版本各生成一条记录，并显示规划事件带与规划 / 审批 / 报告 / 复核 / 问题表。

准入率基线只在本机、有密钥时运行，会产生模型费用：

```powershell
uv run python scripts/m2_baseline.py --output docs/verification/m2-baseline-<日期>.json --price-input <元/百万> --price-output <元/百万> --price-source "<厂商价格页与日期>"
```

没有给出价格来源时费用记为 unpriced。基线以 `HEAD` 记录软件版本，门禁要求所有判据来自同一 commit；要给已验证过的候选补基线，在该候选的独立工作树里运行（步骤见 [M2 验收记录](m2-readiness.md#复现与审计)）。门禁输入哈希按字节计算、仓库按 LF 存储，门禁拒收 CRLF 输入。

常驻任务台（D035）：`dev_stack.py desk-cloud` 只读计划，`--apply` 构建当前部署版本的 M2 镜像、在 `secrets/desk/` 生成任务台自己的信任根、启动 `desk` / `desk-service` / `desk-uplink` 并安装仿真监管者 unit，新增 Serve `8448` 映射；`--status` 返回入口与监管者状态。批准的任务由监管者持项目锁飞行，所以任务台飞行与部署、M1 / M2 批次及 D028 运行互斥，排队时页面显示「等待云端工作区」。使用与边界见 [任务台指南](tailnet-desk.md)。

本机任务台 `uv run python -m drone_agent.console.mission --local` 在 <http://127.0.0.1:8769> 提供 hri.v0 控制台和进程内任务服务：提交、规划、准入、审批与签名都在本机完成，没有机器人连接，不起飞（D023）；有 `MINIMAX_API_KEY` 时实调规划，否则只回答 M2 场景集中的请求原文，页面标明所用规划器。M2 是否关闭只看 `scripts/verify_m2_release.py`：在同一 commit 上核对云端检查、对抗语料、E2E、M1 完整回归与实调基线，缺证据的判据记为 missing。

## M3-SITL 验证

部署已提交版本后运行（会在云端仿真中解锁 / 起飞，并启动外部模式出口节点与自主层）：

```powershell
uv run python scripts/dev_stack.py m3 --scenario ext_inspect --seeds 7
uv run python scripts/dev_stack.py m3 --scenario ext_inspect,ext_goto --seeds 7,19,41 --keep-going
uv run python scripts/dev_stack.py m3 --scenario ext_inspect,route_fallback --seeds 7,19,41 --edge off --keep-going
uv run python scripts/dev_stack.py m3 --scenario ext_inspect --seeds 7,19,41 --edge-period 0 --isolation shared
```

场景集为 `configs/scenarios/m3_suite.yaml`（16 个场景，覆盖观测过期、任务卡住、网络中断、计算过载四类与定位、能源、事件检测的辅助场景），逐场景期望见 `m3_expectations.yaml`。首次运行按版本构建四个镜像：`drone-agent-m1-ground`、`drone-agent-m2-sim`、`drone-agent-m3-aircraft`（ROS 2 Jazzy、`px4_msgs` / `px4_ros2_cpp` 固定版本、出口节点、事件检测的 ONNX Runtime 与 CLIP 视觉编码器）与 `drone-agent-m3-sim`（x500_vision、前视深度相机、M3 世界）；ROS 2 基础层、事件检测运行时与提示嵌入层只依赖固定输入，跨版本复用。每个用例依次：等主机安静窗口（CPU 压力与负载降下来，最多 10 分钟，等待情况写入回执）→ 启动 SITL 与真值采集 → 相机转接与 XRCE agent → 出口节点、自主层与事件检测 → guardian（外部模式任务先等出口节点与 PX4 连通、自主层给出有结论的定位报告，最多 90 s，结果写入账本 `external_ready`，D048）→ executive；飞行中按场景在声明的边界注入故障；结束后在线与 MCAP 回放各跑一次 `eval/judge_m3.py`，外部模式用例再生成单独归档的影子报告 `judge/shadow.json`。

- `--keep-going`：失败后继续跑完所选用例（诊断与测量批次）；默认首个失败即停。
- `--edge off` / `--edge-period 0` / `--isolation shared`：D040 的测量档位（关闭事件检测；连续推理；guardian 与自主层、事件检测绑定到同一核心）。档位写入每个用例的 `measure.json` 与回执。
- 回执 `suite.json` / `progress.json` 的每个用例另记：资源采样汇总（各容器 CPU、节流、内存与主机 CPU 压力，原始数据在 `resources.jsonl`）、仿真停顿列表、事件检测延迟与真值一致率、外部模式计数、安静窗口等待与 Mesa 着色器缓存条目数。
- 仿真停顿：共享主机上 llvmpipe 首次编译着色器或 CPU 争用会让 Gazebo 整体冻结；项目工作区 `cache/mesa-shaders-m3/` 持久化着色器缓存，冷缓存运行可能出现一次性停顿。由停顿引发的新鲜度恢复被裁判判为 `void_simulator_stall`，必须同版本同种子重跑，不计通过也不计系统失败（D045）。

产物位于 `artifacts/<deployment_id>/m3-<run_id>/<scenario>-<seed>/`：`input/`（任务包、场景、注入文件）、`aircraft/`（guardian / executive 账本与 MCAP、影像、状态与监督统计）、`egress/`（出口节点证据）、`autonomy/`（规划、感知证据）、`edge/`（事件检测逐帧记录）、`truth/`、`ulog/`、`judge/`（裁判结果、回放结果、影子报告、飞控事件）、`resources.jsonl`、`measure.json`、`quiet-gate.json`。M3-SITL 是否通过只看 `scripts/verify_m3_release.py`：同一 commit 上的云端检查、对抗语料、M3 场景集、M1 与 M2 回归、M2 的 Zenoh 子集、D040 测量与 v2 恢复边绑定；M3-JIL 缺硬件时记为 missing，M3 本身不能关闭。门禁与绑定都在只增加记录与文档的后代提交上运行，只接受隔离成立、整批通过的回执；其余回执另存，不作为输入：

```powershell
uv run python scripts/bind_recovery_edges.py --sha <被测版本> --m3 <M3 场景集回执> --m1 <M1 回执> --apply
uv run python scripts/verify_m3_release.py --sha <被测版本> --deployment <部署回执> --m3 <场景集与测量回执> --m1 <M1 回执> --m2 <gRPC 回执> --m2-zenoh <Zenoh 回执> --output <门禁结果>
```

2026-09-25 的候选证据按这一布局归档在 `docs/verification/m3-2026-09-25/`（计入的回执按类别分目录，`not-counted/` 与先前候选的 `diagnostics/` 分开保存），见 [M3 验收记录](m3-readiness.md)。

M2 的 Zenoh 传输（D046）：`dev_stack.py m2 --scenario nl_inspect_red,service_outage --seeds 7,19,41 --transport zenoh` 在同一 M2 编排与裁判下让服务与 uplink 走 Zenoh（TLS + mTLS + 按证书名的访问控制）；默认 `--transport grpc` 不变。

## 查看与人工核对结果

裁判结论是机器产出；人工核对用证据浏览器。它读取的正是裁判读取的产物，只展示与复算摘要，不产生也不修改判定。

```powershell
uv run python scripts/dev_stack.py runs
uv run python scripts/dev_stack.py fetch --run m1-<run_id> --cases nominal-7,cancel_cruise-7
uv run python scripts/dev_stack.py fetch --run m1-<run_id> --cases nominal-7,cancel_cruise-7 --apply --artifacts D:/drone-agent-cloud
uv run python -m drone_agent.eval.viewer D:/drone-agent-cloud/runs/<deployment_id>/m1-<run_id>
```

`runs` 只读列出云端 `artifacts/` 下的运行及其回执摘要，不占用变更锁。`fetch` 默认只返回计划（用例、文件数、字节数）；`--apply` 经 SFTP 拉取到本地 ASCII 目录 `runs/<deployment_id>/<run>/`，逐文件核对远端清单摘要，对裁判散列过的文件再与回执比对；任何差异都报错、写入 `fetch.json` 并且不生成页面。`--receipt` 指定归档回执（如 `docs/verification/m1-2026-09-20-receipt.json`）时，另报告云端回执是否与归档一致。`--exclude ulog,sensor` 跳过大文件；`--cases all` 拉整批。远端只有 `runs` / `inspect` 两个只读动作，拉取不改变服务器状态。

拉取成功后自动生成 `viewer.html`（单文件、离线、只读）。页面内容：裁判结论与期望、错误成功数、离线重判一致性；ENU 俯视轨迹（Gazebo 真值与 guardian 观测估计叠加，批准体积、登记航线、资产、降落点）；高度曲线、时间轴与事件带（任务 / 控制权 / 安全 / 操作者 / 注入）；任务步骤三元判定与状态迁移；影像证据（原始 RGB 无损转 PNG，媒体哈希、采集位姿、真值距资产距离）；监督周期 p99 与终态；账本哈希链、MCAP 事件重放、文件 SHA-256 与裁判 / 回执摘要核对；飞控 ULog 模式迁移摘录；原始文件清单。ULog 本身用 PX4 Flight Review 或 PlotJuggler 打开。

页面不是证据：证据是拉取目录里的原始文件与它们的摘要。页面显示不一致或缺席时以原始文件和裁判结果为准。设计原则与后续扩展点见 [评测体系](architecture/08-evaluation.md) §7。

## 部署流程

应用源码固定为指定的已提交 commit，且必须从本地 main 可达。工具不会自动 commit 或 push，脏工作树中的应用改动不进入源码包。远端执行脚本、Compose、验证 Dockerfile 与锁定依赖另以 `control_sha256` 绑定；验收时必须同时报告应用 SHA 与控制面哈希。

```powershell
# 先查看计划，再执行同一来源的部署。
uv run python scripts/dev_stack.py deploy --sha HEAD
uv run python scripts/dev_stack.py deploy --sha HEAD --apply
```

首次引导增加 `--bootstrap-images`，复用已经验证的 M0 镜像，并用 `--artifacts D:/drone-agent-cloud` 指定 ASCII 传输目录。此步骤只导出现有镜像，不在本机重新构建或运行真栈。归档经 SSH 传输后，远端核对 SHA-256、允许的镜像标签、文件系统层和运行配置；出现不同的同名镜像时拒绝覆盖。

较慢或中断的上传可用 `deploy --resume <原 artifact_directory> --apply` 继续。SFTP 只续传不可变的大归档，小文件重新完整传输；续传前检查本地包哈希，传完后仍验证远端整体 SHA-256。控制脚本已变化时拒绝复用旧部署包。

部署在服务器上构建契约验证镜像，依赖从指定 commit 的 `uv.lock` 导出并带哈希安装到独立虚拟环境，与镜像内的 ROS Python 包隔离。随后启动受限 SITL，运行未解锁冒烟与完整契约测试，比较其他容器的 ID、镜像、启动时间与重启次数。全部通过后才更新 `current.json`。更新失败时恢复本项目上一部署；首次部署失败则停止本项目 SITL。现有 car-agent 的容器、数据、环境与入口不属于操作范围。

## 目录与证据

```text
~/drone-agent/
├─ owner.json / stack.lock / current.json
├─ incoming/<run_id>/        # 校验过的源码、控制文件和引导归档
├─ releases/<run_id>/
│  ├─ source/                # git archive 的应用 commit
│  ├─ control/               # Compose、验证 Dockerfile、锁定依赖
│  └─ manifest.json
├─ secrets/                  # 0700；m2/ 签名密钥、trust、CA 与 mTLS 证书，m2-model/ 实调 key（均 0600）
├─ cache/mesa-shaders-m3/    # M3 SITL 共享的 Mesa 着色器缓存（D045）；只影响渲染耗时，可随时删除重建
└─ artifacts/<run_id>/       # 构建日志、冒烟 JSON、JUnit、部署回执；m1-*/、m2-*/、m3-*/ 为飞行批次
```

同一项目的变更使用独立 `flock` 串行化，不占用 car-agent 发布锁。重任务开始前检查共享服务器余量；当前部署要求至少 3 GiB 可用内存、8 GiB 可用磁盘。归档、旧部署、停止的测试容器和证据不自动清理；不改系统设置、安全组、Tailscale、systemd、CI/CD 或数据库 schema。

应用测试结果只属于回执中的 `source_sha`；本地新增云端工具测试不计入该应用 commit 的云端测试数字。M0 历史证据保持原样，云端验证单独记录。

验证镜像使用 pip 支持的 [独立 Python 环境管理](https://pip.pypa.io/en/stable/topics/python-option/)，不为测试而改变 ROS 的依赖版本。

## P 系列（2026-09-26，D049–D061）

P0 的 `verify_p0_release.py` 用法与证据见 [P0 记录](p0-readiness.md)。P1 的资源、虚拟机场与领取闸门（D055）及账本 schema v2（D056）已实现；P2 的持久工作流（D057）与工作流扩展表（D058）已实现；P3 的多站多机调度（D059）、调度扩展表（D060）与两机同世界 SITL（D061）已实现，命令如下；P4–P5 的脚本名仍是计划。

| 用途 | 命令 | 说明 |
|---|---|---|
| S0 故障矩阵 | `uv run python -m drone_agent.eval.p1_world --output <目录>` | 本机即可运行；14 场景 × 3 种子；正式任务服务 + 逻辑机场 + 运行真实机载进程的逻辑飞行器；独立裁判在线与回放各判一次；主机停顿（如休眠）使该次尝试作废并重跑，保留记录 |
| S1 用例 | `uv run python scripts/dev_stack.py p1 --scenario all [--keep-going]` | 云端 `compose.m2.yaml` + `compose.p1.yaml`；`uav_01` 在 PX4 SITL 上经虚拟机场的领取闸门飞行、审批后维护阻断、飞行中服务与机场后端重启，共 5 例 |
| 拉取 S1 产物 | `uv run python scripts/dev_stack.py fetch --run p1-<运行ID> --cases p1_s1_nominal-7 --apply` | 摘要核对同 M2 |
| 常驻任务台成员 | `uv run python scripts/dev_stack.py desk-members --apply` | 经 tailnet 读取本机在任务台的身份，写入云端 `secrets/desk/members.yaml`（0600）；登录名不进仓库、不进回执 |
| 常驻任务台 | `desk-cloud --apply` | 激活前在当前账本副本上运行 D056 迁移演练（只留计数与摘要），通过后才切换；服务启动时带备份迁移 |
| 发布门禁 | `uv run python scripts/verify_p1_release.py ...` | 见 [P1 记录](p1-readiness.md) |
| P2 S0 矩阵 | `uv run python -m drone_agent.eval.p2_world --output <目录>` | 本机即可运行；P2-F01–F15 × 3 种子：P1 世界加工作流引擎，只经正式 API 驱动；独立裁判 `eval/judge_p2.py` 在线与回放各判一次，并带 P1 派遣 / 释放 / 飞行检查 |
| P2 S1 用例 | `uv run python scripts/dev_stack.py p2 --scenario all [--keep-going]` | 云端 `compose.m2.yaml` + `compose.p1.yaml` + `compose.p2.yaml`；服务加载 `p2_s1_v1` 工作流目录；编排按人的方式逐任务审批、复核、维修反馈；链路 × 3 种子（两次飞行含复检）、飞行中取消、飞行中重启服务，共 5 例；裁判逐任务跑 M2 飞行裁判再做 P1 / P2 检查 |
| 草案对抗语料 | `uv run python -m drone_agent.eval.workflow_adversarial` | 工作流草案注入语料（脚本替身）；生效 / 越权草案必须为 0 |
| 常驻任务台（P2） | `desk-members --apply` 后 `desk-cloud --apply` | 成员列表给本机身份加 reviewer；激活前在同一账本副本上先后运行 D056 与 D058 演练，服务启动时带备份加上工作流表 |
| P2 发布门禁 | `uv run python scripts/verify_p2_release.py ...` | 见 [P2 方案](p2-implementation.md) 与验收记录 |
| P3 S0 矩阵 | `uv run python -m drone_agent.eval.p3_world --output <目录>` | 本机即可运行；P3-F01–F15 × 3 种子：两组相邻站点 + 隔离项目的逻辑世界加调度器，只经正式 API 驱动；独立裁判 `eval/judge_p3.py` 在线与回放各判一次，并带 P1 派遣 / 释放 / 飞行检查 |
| P3 规模阶梯 | `uv run python -m drone_agent.eval.p3_world --part ladder --output <目录>` | 10 / 30 / 100 个逻辑节点（每节点一台逻辑 UAV + 一个逻辑机场，从不算飞行器）；闭环任务流，按档报告判定耗时、分配延迟、等待与拒绝分布 |
| P3 S1 用例 | `uv run python scripts/dev_stack.py p3 --scenario all [--seeds 7] [--keep-going]` | 云端 `compose.p3.yaml`：一个 Gazebo 世界、PX4 实例 0 / 1（`uav_01` / `uav_02`）各自的机载栈、证书与逻辑机场；执行器先停本项目空闲 SITL、选空闲私网段、等安静窗口，批次首例先做 60 s 空闲容量探针；经常驻 API 中继调用服务，两架飞行互不等待；每例记录各容器 CPU / 内存 / 节流与主机负载，按飞行窗口算实时因子并读 guardian 监督周期 p99（D061 限值不满足即不通过）；共 5 例 |
| 常驻任务台（P3） | `desk-cloud --apply` | 服务另带 `p3_desk_v1` 调度目录；激活前在同一账本副本上依次运行 D056、D058 与 D060 演练，服务启动时带备份加上调度表 |
| P3 发布门禁 | `uv run python scripts/verify_p3_release.py ...` | 见 [P3 方案](p3-implementation.md) 与验收记录 |

P1/P2 联调复用现有云端单机 mission_upload 与项目锁，不需要 Jetson。P3 的两机同世界拓扑与容量预算见 D061；S0 的 10/30/100 逻辑节点规模与 S1 物理实例数分别记录。保持 guardian 独立配额、项目隔离与按需 SITL，不自动改动其他项目或系统配置。

H1 承接 M3-JIL。原 `verify_m3_release.py` 的总门禁仍会因 JIL 缺席而 `not_passed`；P 系列未来门禁单列所需软件能力与硬件缺项，不覆盖历史结果。运营存储 schema / 迁移的具体方案先完成审查并按项目红线获批，再实施（D056、D058 均已获批）。
