# M0 Linux / PX4 / Gazebo 冒烟

日常构建与联调默认走 [云端开发入口](../docs/cloud-development.md)（D023）。下文是 M0 已验证的显式本地操作方法；没有明确本地需求时不启动本机 Compose。

M1 入口是 [compose.m1.yaml](compose.m1.yaml) 和 [m1.Dockerfile](m1.Dockerfile)，通过云端 `dev_stack.py m1` 执行。`m1_setup.py` 增加实际下视 RGB 相机与巡检目标，`collect.py` 隔离传感器帧与裁判真值；M0 的原镜像与只读冒烟保持独立。M1 的飞行、记录和故障结果以对应候选 SHA 的验收回执为准。

本目录是单服务开发仿真入口，已通过未解锁的真实环境冒烟。基线为 Ubuntu 24.04、Gazebo Harmonic、PX4 v1.17.0；工具链镜像 digest 和源码 commit 固定在 [Dockerfile](Dockerfile) 与 [平台锁](../configs/platforms/px4_sitl_multirotor.yaml)。官方 1.17 预编译 Gazebo 标签缺失时，从稳定源码构建，见 [D022](../docs/decisions.md#d022--用固定官方开发镜像构建-px4-v1170)。

Windows 需要已安装、可运行 Linux 容器的 Docker Desktop。首次构建拉取工具链并编译 PX4；不修改主机 WSL/系统/全局依赖。以 PowerShell 在仓库根运行：

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
uv run python scripts/stage_sim.py D:/drone-agent-m0
docker compose -f D:/drone-agent-m0/compose.yaml config --quiet
docker compose -f D:/drone-agent-m0/compose.yaml build sitl
docker compose -f D:/drone-agent-m0/compose.yaml up -d sitl
docker compose -f D:/drone-agent-m0/compose.yaml exec -T sitl python3 /opt/drone-sim/smoke.py --timeout 30 --output /artifacts/smoke.json
docker compose -f D:/drone-agent-m0/compose.yaml logs --no-color sitl
docker compose -f D:/drone-agent-m0/compose.yaml stop sitl
```

Linux 使用同一命令，将暂存目录改为 `/tmp/drone-agent-m0` 等 ASCII 路径。暂存脚本只复制四个指定文件，拒绝覆盖未标记的非空目录；不会复制源仓库、`.env` 或其他项目。产物保存于暂存目录的 `artifacts/`，容器保留为停止状态，方便检查日志。

如主机已有本地代理，构建可临时加 `--build-arg http_proxy=http://host.docker.internal:10808 --build-arg https_proxy=http://host.docker.internal:10808`（10808 是本次开发机端口，其他机器按已有端口替换）。这些是单次构建参数，不写入主机配置或镜像 ENV。容器内 Ubuntu 包从清华镜像直连下载，仍校验 Ubuntu 签名；Gazebo 从 OSRF 源获取。镜像拉取使用 Docker 后端自己的网络入口，与 RUN 中的代理参数是两条路径；不得为方便下载改用未锁定的镜像版本。

冒烟检查：固定源码、PX4 进程、CRC 校验后的连续未解锁心跳、时间递增且有限的本地位置遥测、Gazebo 世界迭代与 x500 实体。探针只接收 UDP，不发送飞控命令；无主机端口、特权、真实设备映射。它不验证起飞、任务执行、相机、guardian/executive 或恢复策略。

Compose 健康检查仅表示 PX4 进程存活；完整冒烟必须显式运行上述 `smoke.py`。实际完成状态与证据见 [M0 核对记录](../docs/m0-readiness.md)。

构建使用 `make px4_sitl_default`；`gz_x500` 是运行目标，留到入口脚本调用。源码采用稀疏检出，排除文档媒体；仅拉取 SITL 所需子模块，并保留固定 commit。实际通过版本为 Gazebo 8.15.0，后续包版本变化仍需重跑冒烟并保留新证据。

依据：[PX4 Gazebo 仿真](https://docs.px4.io/v1.17/en/sim_gazebo_gz/)、[官方容器与开发工具链](https://docs.px4.io/main/en/simulation/px4_sitl_prebuilt_packages)。
