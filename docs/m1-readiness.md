# M1 实现与验收记录

**M1 已完成（2026-09-20，PX4 SITL / Gazebo 范围）。** 同一候选的完整 66 组场景通过，发布门禁、独立裁判、离线回放和原始文件哈希复核均通过。历史或部分结果未计入本次基线。

## 候选与入口

| 项目 | 候选信息 |
|---|---|
| 运行源码 | `eefe76e7c579200b998f9af0a0e29bc8358a1af7` |
| 云端部署 | `20260919T170015Z-8b7fc06e` |
| 完整运行 | `m1-20260919T171218Z-db465377` |
| 控制面哈希 | `e426158f8aac852f038bba6cc1c1ac41070f3bc17c9c74d5427bdc7fee38aba1` |
| 平台 | PX4 `1.17.0` / Gazebo Harmonic `8.15.0` / MAVSDK `3.17.4` |
| 本地与云端测试 | 候选均为 400 项通过；云端 0 failed / error / skipped |
| 线路协议 | `drone.*.v1` 已冻结；领域载荷 `schema_version=0.1.0` |
| 验收规模 | 22 个场景 × 7/19/41 三个种子，66/66 通过 |
| 三分类结果 | 18 组任务完成、48 组合理安全中止、0 组不安全或错误行为 |
| 错误成功报告 / 回放 | 0 / 66 组一致 |
| 恢复策略 | 14 条边，每条均有三个种子的绑定记录 |
| 证据完整性 | 1,347 个文件逐项复算 SHA-256，0 不一致 |
| 监督周期 | 29,477 个样本；最差单场景 p99 102.9 ms，最大周期 112.8 ms |
| 实测仿真倍率 | 请求 1×；各场景平均实测 0.879×–0.975× |
| 共享环境隔离 | 前后其他 30 个容器身份摘要一致 |
| 验收后状态 | 恢复空闲 SITL；未解锁冒烟通过，探针发送控制命令数 0 |

代码、交付和运行方法分别见 [实施计划](m1-implementation.md)、[技能清单](m1-skill-catalog.md) 与 [云端开发指南](cloud-development.md)。`scripts/verify_m1_release.py` 检查完整矩阵、统一 SHA、实际恢复边、零错误成功报告、回放一致性及原始证据，缺项或跨版本均拒绝关闭。

机器可读证据：[最终门禁](verification/m1-2026-09-20.json)、[完整场景回执](verification/m1-2026-09-20-receipt.json)、[文件哈希与周期审计](verification/m1-2026-09-20-artifact-audit.json)、[恢复边绑定记录](verification/m1-2026-09-20-recovery.json)、[部署与测试](verification/m1-2026-09-20-deployment.json)、[验收后未解锁冒烟](verification/m1-2026-09-20-post-smoke.json)。所有运行结果仅属于上表的应用 SHA；后续文档归档提交不另称为已跑过整套飞行矩阵。

## 已实现的运行时

| 部分 | 实现与边界 |
|---|---|
| 权威状态 | 哈希链 JSONL，控制意图先 fsync 再写入；持久化代次、水位与回执；重启不重放未决命令，并恢复未结束工作的监督 |
| IPC | protobuf / gRPC local credentials / 私有 UDS；任务包、审批哈希、机器人、租约 holder 与资源共同校验；无公网控制端口 |
| guardian | 10 Hz 独立监督；观测、业务心跳、租约、空间前瞻、能源及模式仲裁；恢复写入与迟到 resume 回执不能越过新的控制权 |
| executive | 确定性 DAG 调度、串行资源调度、长技能生命周期；unknown 阻断后继；取消请求与已安全取消分开 |
| 五技能 | takeoff、登记航线 fly_route、实际 RGB capture_image、返航等待点 return_home、land；完成由新鲜遥测和真实媒体判定 |
| 飞控适配 | MAVSDK mission_upload；`CURRENT_MODE` 观察实际/期望模式；飞控/人工优先；没有 Offboard 执行和空中断电接口 |
| 证据 | 真值独立进程，agent 只接收估计观测与相机帧；JSONL、MCAP、ULog、原始 RGB、媒体哈希、位姿协方差与时间窗留存 |
| 回放 | 从 MCAP 重建事件、校验哈希链并重判；在线与离线结果必须一致 |

M1 使用本地可信的预批准任务包。它没有把普通哈希当作签名；远程签发、证书、Planner 与控制台属于后续阶段。相机与恢复能力按实际适配器发布，M0 目标能力表不直接当作运行事实。

## 故障覆盖的含义

- 命令重复、旧 epoch、意图过期和未支持控制模式通过真实本地 RPC 注入；检查无额外物理派发。
- 命令超时在适配器回执边界注入；真实 PX4 已可能执行，executive 必须先对账，不得把 unknown 变成成功。
- executive 心跳丢失通过停止实际 executive 进程注入，guardian 独立恢复。
- 取消、暂停、恢复、暂停超时通过操作者通道注入；降落中取消维持降落，确认地面/上锁稳定后才记 cancelled。
- 外部模式接管使用模拟 GCS 的 LAND，并从 ULog 核对 ACK 与实际模式迁移。POSCTL 请求被 PX4 暂时拒绝的旧实验保留为失败，不算接管覆盖。
- 观测过期、租约失效、低电、预测越界、上行断链、定位退化及飞控失效保护标志属于明确标注的运行时输入注入；不宣称真实电池放电、GNSS 硬件故障或实物 RC 接管已验证。

生产准入仍要求恢复策略有内容绑定的验证记录；实验入口必须显式标注 `--simulation` 才能运行策略与故障注入。M0 的 15 条草案边保留；M1 使用独立的 mission-upload 策略，14 条边均已通过本次场景验证。验证记录单独归档，不把有限 SITL 记录嵌入配置后自动开放为真机生产策略。

## 时钟与资源

本次完整验收使用 1× 请求速度。共享主机的仿真上限为 1.5 CPU / 2 GiB，相机为 160×120、5 Hz；各个进程的资源约束见云端指南。`--speed-factor 2` 保留显式加速入口，报告同时记录请求倍率与实测倍率；现有配额下不承诺达到 2× 吞吐。

2× 请求下曾发生大于 500 ms 的真实遥测间隙，guardian 按原阈值安全中止；这些不是指定故障场景的通过证据。没有为凑通过率放宽观测、租约或心跳阈值。

## 复现与审计

```powershell
uv run ruff check .
uv run pytest -q
uv run python scripts/generate_contract_fields.py --check
uv run python scripts/freeze_wire.py --check
uv run python scripts/dev_stack.py m1 --scenario all --seeds 7,19,41 --speed-factor 1
```

云端原始目录：`~/drone-agent/artifacts/<deployment_id>/<run_id>/<scenario>-<seed>/`。其中 `aircraft/` 为权威日志、MCAP、能力与进程身份，`truth/` 为 Gazebo 真值，`ulog/` 为飞控原始日志，`judge/` 为 ULog 提取、在线裁判与回放结果。失败试验与历史镜像保留用于审计，负结果记入 [基线账本](../eval/BASELINES.md)。

共享主机上的其他 Compose 项目曾在开发验证期间独立更新，相关批次的全局环境快照因此不一致，未被计为本次完整验收。本次完整矩阵前后均为 30 个其他容器，身份摘要同为 `b8189ee60407b62dcaa9b4272477e659b10a0e5e90f0a9b9bf8f58bbf1e3e88a`。
