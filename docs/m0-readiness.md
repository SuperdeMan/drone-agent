# M0 交付核对（2026-09-19）

M0 剩余待办已全部完成，包含真实 PX4/Gazebo 的未解锁环境冒烟。M1 飞行任务闭环尚未实现；环境启动证据不替代技能执行或恢复策略故障注入验证。

## 交付与证据

| 项目 | 实物与验证 | 状态 |
|---|---|---|
| 六类契约 | `src/drone_agent/contracts/`；扩展撤销代次、续期、嵌套参数、审批范围与任务边界哈希、UTC 时间规范化、编译 DAG 回归 | 完成 |
| proto 骨架 | [字段清单](../proto/contract-fields.json)、共享/control/fleet 三份 proto；48 个模型、316 个字段、15 个枚举；grpcio-tools 编译通过 | 完成 |
| 五技能草案 | [技能目录](../configs/skills/)；参数/资源/取消/暂停/证据/失败模式齐备，与目标平台一致 | 完成 |
| 恢复注入矩阵 | [矩阵](../configs/scenarios/m0_fault_matrix.yaml)；15 条边一一匹配，另有 8 类运行时故障；种子 7/19/41 | 静态覆盖完成，运行待 M1/M3 |
| D006 重估 | [embodied-agent D016](https://github.com/SuperdeMan/embodied-agent/blob/main/docs/decisions.md#d016--d006-重估第三个消费者-drone-agent-出现公共内核仍以双场景验证为前提)；保留复制改造，不提前抽库 | 已追加 |
| Linux / ASCII | Docker Desktop Linux Engine 29.6.1 / Compose 5.1.4；`D:/drone-agent-m0` 暂存标记与四文件哈希；compose config 通过 | 完成 |
| PX4 / Gazebo | D022 固定工具链与源码构建通过；Ubuntu 24.04.4 / PX4 1.17.0 / Gazebo Harmonic 8.15.0；未解锁遥测与世界时钟通过 | 完成 |

## 已执行检查

在 Windows 开发环境，对本批工作区执行：

```text
uv run ruff check .                                     -> All checks passed
uv run python scripts/generate_contract_fields.py --check -> 48 models, 316 fields, 15 enums
uv run python scripts/generate_proto.py                 -> gen/contracts.pb
uv run pytest -q --junitxml=...                          -> 258 passed, 0 failures, 0 errors, 0 skipped
docker compose -f D:/drone-agent-m0/compose.yaml config --quiet -> exit 0
docker compose -f D:/drone-agent-m0/compose.yaml build ... sitl -> exit 0
docker compose ... exec -T sitl python3 /opt/drone-sim/smoke.py ... -> passed
```

验证时基于本仓初始提交 `e9e9586` 的未提交工作区，具体输入由下方代码指纹锁定。初始基线为 212 项测试；最终使用项目默认 `--import-mode=importlib`，未跳过或放宽契约红线。`gen/` 是可再生产物，不进版本控制。

机器可读证据：[m0-2026-09-19.json](verification/m0-2026-09-19.json)，包含完整 HEAD、代码文件哈希、依赖版本、测试计数、镜像 ID 与原始探针结果。代码指纹为 `ee5b551e66b5cb35bc6e1b8ce6f145a460b7d238da02fe57ac35a89c1235c718`。

环境冒烟时间为 **2026-09-19 18:29:03（Asia/Shanghai）**：2 个未解锁 PX4 心跳、30 个位置样本（boot time 5604 → 6572 ms）、Gazebo 迭代 1690 → 2380、实体 `x500_0`，探针发送控制命令数为 0。镜像 ID：`sha256:1d0c3261d16ae715ac62955cae73a9094e3244c381c2cbd93784e17dbb3976b9`；PX4 二进制 SHA-256：`1bd27da3d6fb44d584c4a8c476bc117d6be170749e0da6ff8e26d61c6dbbd5a7`。

原始日志与 JUnit 保存在 `D:/drone-agent-m0/artifacts/`。启动日志含 SDF `gz_frame_id` 扩展提示及未连接 GCS 的预飞检查提示；本轮未验证解锁或起飞，也未为通过检查修改飞控失效保护。取证后已停止仿真容器，保留可复用镜像与产物。

用户已明确授权清理本次临时资源。四个已退出、无挂载的探针容器 `drone-agent-m0-{proxycheck,toolchain-check,mirrorcheck,osrfcheck}` 已删除；`D:/drone-agent-m0/tools`（约 1 GB）的删除在授权后仍被自动审批审查以 `blocked by policy` 拒绝，目录继续保留，需用户手动删除。这些临时资源不影响 M0 验证结果。

embodied-agent 修改前 HEAD 为 `16ceae82a309fc511d6de9fd73b8fd32b281f1c5`，本批只追加文档重估记录，没有运行或改变其代码/仿真基线。

## M1 接手边界

- 实现 executive/guardian、PX4 适配器、鉴权与签名验证、持久化代次/对账、领域 wire 编解码与意图载荷白名单、本地日志与独立裁判；骨架 RPC 不能直接用于执行。
- 所有 15 条恢复边均缺真实通过证据，`require_verified()` 会拒绝生产使用。匹配 `fi.*` 名称不算已验证；未来通过记录必须绑定当前边哈希、软件版本与证据。
- 当前策略缺少一些完整运行分支：mission_upload 下观测过期、上下文缺失时的确定性兜底、可继续上行断链后的有界重入。矩阵已记录这些缺口，M1 必须先设计和验证再启用。
- Offboard 与视觉定位成功降级场景的实测属于 M3；M1 只验证缺能力拒绝，不借这些规划场景声明已具备能力。
- `gz_x500` 环境冒烟不包含相机链路；五技能的谓词与 `m1_campus_v1.*` 阈值尚待实现。目标 capability 不能作为运行时真实能力直接发布。
