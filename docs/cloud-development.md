# 云端开发与联调

按 2026-09-19 的用户要求，后续 Linux 构建、PX4/Gazebo 仿真和服务联调默认使用现有云服务器。本机负责代码编辑和快速单测；`scripts/dev_stack.py` 只操作 cloud，连接失败不会启动本地 Docker。

当前部署已通过 [2026-09-19 验收](cloud-readiness-2026-09-19.md)，应用源码为 `f1fdc3e`，SITL 保持运行，云端契约测试 258 项通过。

## 当前部署范围

当前可运行的是 M0 契约验证与未解锁的 PX4/Gazebo 环境。Planner、mission-service、控制台及机载 executive/guardian 尚未实现；这些服务随 M1/M2 实现后接入本工作区。真机的 guardian、控制出口与飞控连接仍在设备侧，云端发送任务和边界。

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
