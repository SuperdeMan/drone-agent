# 云端开发与联调

按 2026-09-19 的用户要求，后续 Linux 构建、PX4/Gazebo 仿真和服务联调默认使用现有云服务器。本机负责代码编辑和快速单测；`scripts/dev_stack.py` 只操作 cloud，连接失败不会启动本地 Docker。

初始 M0 部署见 [2026-09-19 历史验收](cloud-readiness-2026-09-19.md)。当前应用 SHA、部署 ID 和状态以 `status` 及本次部署回执为准；历史测试数字不能转借到新版本。

M1 已通过 [2026-09-20 完整验收](m1-readiness.md)，原里程碑运行版本为 `eefe76e`。D027 实时入口补遗的部署版本和新增验证以 [实时入口验收](live-console-readiness.md) 为准；本机页面、云端运行与后续文档归档提交分开记录。

## 当前部署范围

M0 未解锁冒烟继续作为部署前置；M1 另有 sim/aircraft/ground 三镜像、executive/guardian 双进程和独立裁判。M2 在同一工作区增加 `sim2` 仿真镜像（多一个蓝色资产）、ground 镜像中的 mission-service、aircraft 镜像中的 uplink，以及项目 `secrets/` 下的签名密钥与 mTLS 证书；端到端用例按需启停，不常驻。真机的 guardian、控制出口与飞控连接仍在设备侧，云端只验证模拟飞控。

工作区使用 SSH 用户的 `~/drone-agent/`，不放入 car-agent 目录。Docker project 为 `drone-agent-cloud`；SITL 限 1.5 CPU / 2 GiB，契约测试限 1 CPU / 1 GiB。仿真网络为内部桥接，测试容器无网络，二者不发布宿主端口。SSH 用于部署与管理。

D027 增加固定 M1 任务的 [实时浏览器入口](live-simulation.md)：`dev_stack.py console` 在本机回环地址提供页面，经 SSH 读取云端并提交有界操作。云端不增加公开服务端口；它与后续 M2 的 mission-service/审批控制台分开验收。

D028 将网页常驻到云端，使用仅绑定宿主 `127.0.0.1:8768` 的受限容器和专用任务代理，由独立 Tailscale Serve HTTPS `8447` 入口提供私网访问。日常体验不再需要本机桥；`dev_stack.py console-cloud --status` 返回地址，配置与准确版本见 [Tailnet 指南](tailnet-console.md) 和 [验收记录](tailnet-console-readiness.md)。它不启用 Funnel、不改变既有其他映射，也不新增应用登录体系。

配置依据：[Compose 资源属性](https://docs.docker.com/reference/compose-file/services/#cpus)、[内部网络](https://docs.docker.com/reference/compose-file/networks/#internal)。验收还会读取实际容器设置，确认配置已经生效。

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

密钥与证书由 `fleet/provision.py` 在 ground 镜像中以工作区用户身份生成到 `~/drone-agent/secrets/m2/`，重复运行保留签名密钥；它们不进入快照、Compose 文件、镜像或日志。

产物位于 `artifacts/<deployment_id>/m2-<run_id>/<scenario>-<seed>/`：`input/`（场景、脚本回答、故障文件）、`service/`（业务账本、媒体、`ready.json`、实调录制）、`inbox/`、`mailbox/`、`uplink/`、`robot/`、`aircraft/<mission_id>/v<n>/`（每个版本一次飞行）、`truth/`、`ulog/`、`service-export/`、`judge/`。拉取与人工核对沿用 `fetch`；证据浏览器对 M2 用例按任务版本各生成一条记录，并显示规划事件带与规划 / 审批 / 报告 / 复核 / 问题表。

准入率基线只在本机、有密钥时运行，会产生模型费用：

```powershell
uv run python scripts/m2_baseline.py --output docs/verification/m2-baseline-<日期>.json --price-input <元/百万> --price-output <元/百万> --price-source "<厂商价格页与日期>"
```

没有给出价格来源时费用记为 unpriced。

本机任务台 `uv run python -m drone_agent.console.mission --local` 在 <http://127.0.0.1:8769> 提供 hri.v0 控制台和进程内任务服务：提交、规划、准入、审批与签名都在本机完成，没有机器人连接，不起飞（D023）；有 `MINIMAX_API_KEY` 时实调规划，否则只回答 M2 场景集中的请求原文，页面标明所用规划器。M2 是否关闭只看 `scripts/verify_m2_release.py`：在同一 commit 上核对云端检查、对抗语料、E2E、M1 完整回归与实调基线，缺证据的判据记为 missing。

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
└─ artifacts/<run_id>/       # 构建日志、冒烟 JSON、JUnit、部署回执；m1-*/ 与 m2-*/ 为飞行批次
```

同一项目的变更使用独立 `flock` 串行化，不占用 car-agent 发布锁。重任务开始前检查共享服务器余量；当前部署要求至少 3 GiB 可用内存、8 GiB 可用磁盘。归档、旧部署、停止的测试容器和证据不自动清理；不改系统设置、安全组、Tailscale、systemd、CI/CD 或数据库 schema。

应用测试结果只属于回执中的 `source_sha`；本地新增云端工具测试不计入该应用 commit 的云端测试数字。M0 历史证据保持原样，云端验证单独记录。

验证镜像使用 pip 支持的 [独立 Python 环境管理](https://pip.pypa.io/en/stable/topics/python-option/)，不为测试而改变 ROS 的依赖版本。
