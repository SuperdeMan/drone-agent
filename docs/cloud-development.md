# 云端开发与联调

按 2026-09-19 的用户要求，后续 Linux 构建、PX4/Gazebo 仿真和服务联调默认使用现有云服务器。本机负责代码编辑和快速单测；`scripts/dev_stack.py` 只操作 cloud，连接失败不会启动本地 Docker。

初始 M0 部署见 [2026-09-19 历史验收](cloud-readiness-2026-09-19.md)。当前应用 SHA、部署 ID 和状态以 `status` 及本次部署回执为准；历史测试数字不能转借到新版本。

## 当前部署范围

M0 未解锁冒烟继续作为部署前置；M1 另有 sim/aircraft/ground 三镜像、executive/guardian 双进程和独立裁判。Planner、mission-service 与控制台属于 M2。真机的 guardian、控制出口与飞控连接仍在设备侧，云端只验证模拟飞控。

工作区使用 SSH 用户的 `~/drone-agent/`，不放入 car-agent 目录。Docker project 为 `drone-agent-cloud`；SITL 限 1.5 CPU / 2 GiB，契约测试限 1 CPU / 1 GiB。仿真网络为内部桥接，测试容器无网络，不发布宿主端口。SSH 是当前管理与验证入口。

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

镜像从已校验的检查镜像构建并按 SHA 命名；`bc` 仅安装在仿真镜像内，供 PX4 标准加速时钟启动脚本使用。MAVSDK 固定 `3.17.4`，其电量单位为 0–100，在适配器边界转换为契约的 0–1。航线上传后等待 PX4 异步检查，再确认实际进入 MISSION；依据为 [PX4 官方 MAVSDK 集成测试](https://github.com/PX4/PX4-Autopilot/blob/v1.17.0/test/mavsdk_tests/autopilot_tester.cpp)。

同一时刻仅运行一个场景。SITL 1.5 CPU/2 GiB，guardian 0.6 CPU/512 MiB，executive 0.4 CPU/512 MiB，采集器 0.3 CPU/256 MiB，离线裁判 0.5 CPU/512 MiB；无主机端口。只有 guardian 与模拟飞控共享网络；executive 无网络，只有私有 UDS。真值目录不挂载给机载进程。

产物位于 `artifacts/<deployment_id>/m1-<run_id>/<scenario>-<seed>/`：`input/` 固定任务与注入、`aircraft/` JSONL/MCAP/实际影像/能力快照/进程身份、`truth/` Gazebo 真值、`ulog/` 飞控原始日志、`judge/` 裁判结果。`progress.json` 可查看长批次进度，`build-*.log` 实时写入构建输出。实验结束恢复本项目空闲 SITL；历史记录和旧镜像不自动删除。

离线重判使用同一版本 ground 镜像执行 `python3 -m drone_agent.eval.judge <run目录> --root /workspace --output <结果文件>`。裁判复算影像、真值轨迹、前驱门控与 MCAP 事件一致性。新规则重判必须保留旧结果并标注新的软件 SHA。

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
└─ artifacts/<run_id>/       # 构建日志、冒烟 JSON、JUnit、部署回执
```

同一项目的变更使用独立 `flock` 串行化，不占用 car-agent 发布锁。重任务开始前检查共享服务器余量；当前部署要求至少 3 GiB 可用内存、8 GiB 可用磁盘。归档、旧部署、停止的测试容器和证据不自动清理；不改系统设置、安全组、Tailscale、systemd、CI/CD 或数据库 schema。

应用测试结果只属于回执中的 `source_sha`；本地新增云端工具测试不计入该应用 commit 的云端测试数字。M0 历史证据保持原样，云端验证单独记录。

验证镜像使用 pip 支持的 [独立 Python 环境管理](https://pip.pypa.io/en/stable/topics/python-option/)，不为测试而改变 ROS 的依赖版本。
