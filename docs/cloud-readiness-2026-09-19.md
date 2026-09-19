# 云端联调环境验收（2026-09-19）

本页是首次 M0 部署的历史快照；当前运行时与操作方式见 [云端开发指南](cloud-development.md)，实际版本由 `scripts/dev_stack.py status` 查询。

现有服务器上的 drone-agent 仿真与契约验证工作区已部署并通过验收。后续需要 Linux 构建、SITL 或服务联调时默认使用云端；操作见 [开发指南](cloud-development.md)。这不是 M1 飞行闭环或 M2 任务服务的完成声明。

## 当前部署

| 项目 | 结果 |
|---|---|
| 应用源码 | `f1fdc3e0391c11ca01ee57c930a08392c1f20862`，由 git archive 导出 |
| 控制面哈希 | `bfd290133bfb5e95ea4222cdb9b9b7e9c9a7db26e5619ec9d20380837f378936` |
| 部署 ID | `20260919T124946Z-90d6912a` |
| 验收时间 | 2026-09-19 20:50:49（Asia/Shanghai） |
| 工作区 | SSH 用户的 `~/drone-agent/`；独立 project `drone-agent-cloud` |
| 状态 | SITL running / healthy；无新宿主端口；内部网络；非 privileged |
| 资源上限 | 实际 HostConfig：1.5 CPU、2 GiB 内存、2 GiB 内存加 swap 总额、512 PID |
| 采样占用 | 约 0.91 CPU、196.5 MiB 内存（单次快照，不作为性能基线） |
| 仿真版本 | PX4 1.17.0 / commit `d6f12ad1c4f70ad3230afd7d86e971421e02fef4`；Gazebo Harmonic 8.15.0 |
| 云端应用测试 | 258 passed，0 failed / error / skipped；默认 importlib 模式 |
| 本地云工具检查 | ruff 与 286 项测试通过；对应验证时的工作区，工具文件哈希已记录，不转借给云端应用 SHA |

机器可读证据见 [cloud-2026-09-19.json](verification/cloud-2026-09-19.json)，其中包含镜像 ID、二进制与探针哈希、实际资源设置和其他容器前后身份摘要。SSH 地址、私钥与 token 不进入证据。

## 实际验证

- 部署过程在云端构建契约验证镜像，依赖按 f1fdc3e 的 uv.lock 带哈希安装到独立 venv；`pip check` 通过，与 ROS Python 包隔离。
- 云端接收到 2 个未解锁 PX4 心跳、30 个递增位置样本；Gazebo 迭代 1855 → 2524，`x500_0` 可见；探针发送控制命令数为 0。
- 部署后独立运行 `scripts/dev_stack.py verify` 与 `test`，均返回实际应用 SHA、控制面哈希和部署 ID；测试再次得到 258 / 0 / 0 / 0。
- 原有 30 个容器的 ID、镜像、启动时间与重启次数摘要前后一致。car-agent 运行版本仍为 `ca4bf3705a8e73d19e56fb713c36c3773af7a5c9`，前后均为 5 个健康入口、零告警。
- 未修改宿主机 `.env`、Tailscale、安全组、systemd、CI/CD 或数据库 schema。

## 后续入口与边界

M1 新会话先按 [AGENTS.md](../AGENTS.md) 的必读顺序了解规则、架构与契约，再读 [M0 核对记录](m0-readiness.md)、本页和 [云端开发指南](cloud-development.md)。从 [路线图的 M1 任务](roadmap.md#m1--无大模型的单机安全闭环) 继续；当前仅完成联调前置，六项运行时任务和飞行闭环退出标准仍待实现。开始联调前确认云端状态与冒烟结果：

```powershell
uv run python scripts/dev_stack.py status
uv run python scripts/dev_stack.py verify
uv run python scripts/dev_stack.py test
```

需要测试新应用代码时，先形成已提交 SHA，再执行 `deploy --sha <SHA>` 查看计划，随后 `--apply`。工具不自动 commit 或 push。首次 1.23 GB 引导归档已上传，后续通常复用云端镜像，只传输源码与控制文件。慢链路中断使用 `--resume`，不跳过最终整体哈希检查。

当前云端没有 Planner、任务服务、业务控制台或实际 guardian/executive；这些模块按里程碑实现后再部署。真机控制链留在设备侧，不通过公网连续下发飞控动作。测试容器、历史部署与归档保留用于审计，不自动清理。
