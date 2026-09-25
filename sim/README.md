# PX4 / Gazebo 仿真与验证入口

[项目首页](../README.zh-CN.md) · [部署架构](../docs/architecture/07-deployment.md) · [云端开发](../docs/cloud-development.md)

当前目录包含 M0 冒烟、M1 飞行 / 故障验证、M2 任务闭环、M3-SITL 局部自主和两类常驻入口。日常构建与联调默认使用云端 `scripts/dev_stack.py`（D023），每个结果绑定对应源码和控制面哈希。

| 入口 | 配置 | 用途与证据 |
|---|---|---|
| M0 本地冒烟 | [compose.yaml](compose.yaml) | 单服务、未解锁；[M0 记录](../docs/m0-readiness.md) |
| 云端基础工作区 | [compose.cloud.yaml](compose.cloud.yaml) | 空闲 SITL 与检查；[云端指南](../docs/cloud-development.md) |
| M1 飞行 / 故障批次 | [compose.m1.yaml](compose.m1.yaml) | `dev_stack.py m1`；[M1 记录](../docs/m1-readiness.md) |
| M2 任务闭环批次 | [compose.m2.yaml](compose.m2.yaml) | `dev_stack.py m2`；[M2 记录](../docs/m2-readiness.md) |
| M3 局部自主批次 | [compose.m3.yaml](compose.m3.yaml) | `dev_stack.py m3`；[M3 记录](../docs/m3-readiness.md) |
| M1 固定任务控制台 | [compose.console.yaml](compose.console.yaml) | `console-cloud`；[使用指南](../docs/tailnet-console.md) |
| M2 常驻任务台 | [compose.desk.yaml](compose.desk.yaml) | `desk-cloud`；[使用指南](../docs/tailnet-desk.md) |

M1/M2 飞行镜像由 [m1.Dockerfile](m1.Dockerfile) 构建；M3 使用 [m3.Dockerfile](m3.Dockerfile)；`m1_setup.py` / `m2_setup.py` 准备相机与场景，`collect.py` 隔离传感器帧和裁判真值。M2 默认脚本规划只验证链路，真实模型行为单独留证；本机任务台不运行这些飞行容器。M0 镜像和历史冒烟证据保持独立。

## M0 显式本地冒烟

下文保留 M0 已验证的本地操作方法；没有明确本地需求时不启动本机 Compose。

M0 单服务入口已通过未解锁的真实环境冒烟。基线为 Ubuntu 24.04、Gazebo Harmonic、PX4 v1.17.0；工具链镜像 digest 和源码 commit 固定在 [Dockerfile](Dockerfile) 与 [平台锁](../configs/platforms/px4_sitl_multirotor.yaml)。官方 1.17 预编译 Gazebo 标签缺失时，从稳定源码构建，见 [D022](../docs/decisions.md#d022--用固定官方开发镜像构建-px4-v1170)。

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

## 新运营路线的仿真边界（设计）

P1 的机场 / UAV 逻辑模拟属于 S0；本目录已验证的 PX4/Gazebo 属于 S1；授权影像回放与模型分析属于 S2；P5 厂商协议模拟属于 S3。P1 三逻辑设备不代表三机物理仿真，P3 至少两机同世界另建证据。新模拟后端必须走正式任务、审批与证据契约，来源可追溯；具体任务见 [P1 计划](../docs/p1-implementation.md)。H1/JIL 与真实设备不由软件仿真结果追认。
