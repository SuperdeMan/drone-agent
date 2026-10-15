# PX4 / Gazebo 仿真与验证入口

[项目首页](../README.zh-CN.md) · [部署架构](../docs/architecture/07-deployment.md) · [云端开发](../docs/cloud-development.md)

当前目录包含 M0 冒烟、M1 飞行 / 故障验证、M2 任务闭环、M3-SITL 局部自主、P1–P6 的 S1 批次、X1 空地联合仿真和两类常驻入口。日常构建与联调默认使用云端 `scripts/dev_stack.py`（D023），每个结果绑定对应源码和控制面哈希。

| 入口 | 配置 | 用途与证据 |
|---|---|---|
| M0 本地冒烟 | [compose.yaml](compose.yaml) | 单服务、未解锁；[M0 记录](../docs/m0-readiness.md) |
| 云端基础工作区 | [compose.cloud.yaml](compose.cloud.yaml)、[checks.Dockerfile](checks.Dockerfile) | 空闲 SITL 与云端检查（`dev_stack.py test`）；[云端指南](../docs/cloud-development.md) |
| M1 飞行 / 故障批次 | [compose.m1.yaml](compose.m1.yaml) | `dev_stack.py m1`；[M1 记录](../docs/m1-readiness.md) |
| M2 任务闭环批次 | [compose.m2.yaml](compose.m2.yaml) | `dev_stack.py m2`；[M2 记录](../docs/m2-readiness.md) |
| M3 局部自主批次 | [compose.m3.yaml](compose.m3.yaml) | `dev_stack.py m3`；[M3 记录](../docs/m3-readiness.md) |
| P1 虚拟机场 | M2 + [compose.p1.yaml](compose.p1.yaml) | `dev_stack.py p1`：一架 PX4 SITL 经虚拟机场的领取闸门飞行；[P1 记录](../docs/p1-readiness.md) |
| P2 持久工作流 | M2 + P1 + [compose.p2.yaml](compose.p2.yaml) | `dev_stack.py p2`；[P2 记录](../docs/p2-readiness.md) |
| P3 两机同世界 | [compose.p3.yaml](compose.p3.yaml) | `dev_stack.py p3`：一个 Gazebo 世界中的两架 PX4 SITL；[P3 记录](../docs/p3-readiness.md) |
| P4 业务闭环 | M2 + P1 + [compose.p4.yaml](compose.p4.yaml) | `dev_stack.py p4`：经 Gazebo 世界服务放置 / 移除损伤贴片；[P4 记录](../docs/p4-readiness.md) |
| P5 平台 | M2 + P1 + P4 + [compose.p5.yaml](compose.p5.yaml) | `dev_stack.py p5`：P5 仿真镜像，放置 / 移除道路障碍物；[P5 记录](../docs/p5-readiness.md) |
| P6 主动补拍 | M2 + P1 + P4 + [compose.p6.yaml](compose.p6.yaml) | `dev_stack.py p6`：在标记上放置 / 移除发光圆片；[P6 补拍记录](../docs/p6-recapture-readiness.md) |
| P6 对抗录制 | M2 + [compose.adversarial.yaml](compose.adversarial.yaml) | `dev_stack.py p6-adv`：一份语料一个容器，只经白名单代理实调；[P6 对抗录制记录](../docs/p6-adversarial-readiness.md) |
| X1 空地联合 | [compose.x1.yaml](compose.x1.yaml) | `dev_stack.py x1`：同一 Gazebo 世界中的 x500 与差速地面车；[X1 记录](../docs/x1-readiness.md) |
| M1 固定任务控制台 | [compose.console.yaml](compose.console.yaml) | `console-cloud`；[使用指南](../docs/tailnet-console.md) |
| 常驻任务台（平台 v0.1） | [compose.desk.yaml](compose.desk.yaml) | `desk-cloud`；[使用指南](../docs/tailnet-desk.md)、[运维手册](../docs/operations-guide.md) |

M1/M2 飞行镜像由 [m1.Dockerfile](m1.Dockerfile) 构建，P1、P2、P4 与 P6 的 S1 沿用它；M3 使用 [m3.Dockerfile](m3.Dockerfile)；P3 的两机世界、P5 的道路世界与 X1 的空地世界分别由 [p3.Dockerfile](p3.Dockerfile)、[p5.Dockerfile](p5.Dockerfile) 与 [x1.Dockerfile](x1.Dockerfile) 构建。`m1_setup.py` / `m2_setup.py` 准备相机与场景，`collect.py` 隔离传感器帧和裁判真值。M2 默认脚本规划只验证链路，真实模型行为单独留证；本机任务台不运行这些飞行容器。M0 镜像和历史冒烟证据保持独立。

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

## 运营路线的仿真边界

P 系列与 X1 的逻辑机场、逻辑飞行器与逻辑地面车属于 S0；本目录的 PX4/Gazebo 入口属于 S1；授权影像回放与模型分析属于 S2；P5 厂商协议模拟属于 S3。四层分别计数、互不替代：P1 的三台逻辑设备不代表三机物理仿真，P3 的两机同世界与 X1 的一机一车各有独立证据。新模拟后端必须走正式任务、审批与证据契约，来源可追溯。H1/JIL 与真实设备不由软件仿真结果追认。
