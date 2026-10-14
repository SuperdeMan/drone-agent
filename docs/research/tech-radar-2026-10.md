# 季度技术雷达（2026-10）

[决策记录](../decisions.md) · [前沿调研（截至 2026-09）](frontier-2026.md) · [ROS 2 Lyrical 评估](ros2-lyrical-2026-09.md)

> 目的：按节奏机制复查 `frontier-2026.md` 的判断与 `decisions.md` 各条的重估触发器；D043 把本次雷达列为触发器之一。
> 口径：外部事实于 2026-10-14 按一手来源（GitHub、PyPI、官方文档与公告）核查，查不到的写「未能核实」。其中 MAVSDK 的发布日期、MCP 的变更、GitHub 移除 Node 20、MiniMax 的订阅规则、`px4-ros2-interface-lib` 2.2.2 与三个 action 声明的运行时由本次直接复核。内部事实来自仓库、GitHub Actions 的公开接口，以及当日任务台只读回执（`dev_stack.py desk-capacity`，00:02Z）和账本页统计。本纪要只记录与建议，不改变任何决策；需要动手的列在 §8，涉及红线的逐项请示。

## 结论

1. **前沿判断仍成立，不需要改写**（§1）。窗口内让模型直接驱动飞控的只有评测与研究样机；新工作补强了三点：
   - 模型只在确定性层给出的选项里选择；
   - 校验覆盖模型的输入和执行前；
   - 完成判定与动作生成分开。

   法规方面有两项基线遗漏或新规，都只影响 H2：修订后的《民用航空法》要求适航许可与唯一产品识别码；北京新规禁止在市内持有无人机。
2. **两个外部触发器命中**：
   - MAVSDK-Python v4 已于 2026-09-22 发布（最新 4.0.6，10-08），D008「v4 发布后迁移」的条件满足，迁移按 D025 的流程做（§2）。
   - MCP 2026-07-28 正式版有不兼容修订（D034）。本项目的 MCP 是自写的 stdio 实现，两端都固定 `2025-06-18`，不受影响（§3）。
3. **一个内部触发器命中，此前没有补记**：car-agent 在运行期间发布，多次使整批隔离不成立（D038 / D061）。现行应对有效，本次补记并给出新的重估条件（§5）。
4. **D043 的切换条件仍不满足**：维持 Jazzy + PX4 v1.17.0 + Gazebo Harmonic，下次在 2027-01 复查（§2）。
5. **需要你处理或批准的事**：
   - MiniMax 的 Token Plan 已停售，改为 M Plan。现有订阅只要保持自动续费，就按原价、原权益续订；自动续费一旦关闭，就不能再开。
   - GitHub Actions 自 2026-09-23 起不再提供 Node 20，三个 action 被强制跑在 Node 24 上并报警告。升级属于 CI/CD 红线（§4）。
6. **其余触发器**：未命中，或已由后续决策处理（§7）。

## 1. 前沿判断复查

**结论：核心判断仍成立。**
- 2026-09-01 至 10-14 之间，让模型直接驱动飞控的只有评测与研究样机；产业侧仍是模型出计划、由人或确定性层把关。
- 研究中「学习型策略直达电机、没有独立安全层」的样机在增加。学习型技能继续只以「插件 + 影子运行 + 有限接管」进入系统。

| 工作 | 要点 | 对本项目 |
|---|---|---|
| DroneCATS（arXiv 2609.01404，09-01） | 把多模态 LLM 直接放进无人机控制回路评测。回合失败的主因不是飞不好，而是过早宣布到达或一直不宣布（"it is not the flying that fails"） | 佐证「证据先于成功」：完成与否由独立的效果判定决定，不采信模型自报 |
| VLN on the Fly（2609.20191，08-07 提交） | 机载 VLM 只把指令映射到图像格，由深度提升为三维目标；B 样条规划后，RL 策略跟踪并输出电机指令。作者反对端到端，但没有描述独立安全层；室内 15 次成功 13 次 | 「学习型策略直达电机」的研究样机，不改变准入方式 |
| VLM-MPPI（2609.18451，09-16） | 六个按行为条件化的 MPPI 规划器给出候选轨迹，VLM 只选编号；有真机飞行 | 与「语义任务 → 确定性编译」一致：模型只在确定性规划器给出的候选中选 |
| 感知—推理接口的纵深防御（2610.03319，10-02） | LLM 调度机群之前，对输入报告做五层校验；校验不过就切回忽略可疑输入的确定性调度器，代价是累计成本增加 74–79% | 独立安全机制要覆盖模型的输入侧；回退有代价，需要量化 |
| AeroEval（2610.09764，10-07） | LLM 生成的任务程序先过代码、API 与意图校验，再过仿真轨迹校验；失败时带结构化原因重新生成，导航任务成功率 55% → 95% | 与准入层的确定性校验同向；本项目的规划草案不生成代码 |
| AGOS-Bench（2609.08402，09-08） | 空地协同找物的基准，采用「搜索—交接—复核」协议，VLM 只负责场景理解与决策 | X1 的直接参照：空中发现、地面复核，交接是显式协议 |
| 电力巡检视觉模型基准审计（2609.27457，09-23） | 控制分辨率、拆分与重复次数后，VLM 相对专用视觉模型的优势基本消失 | WP-P6-01 预登记协议时，把分辨率、拆分与重复次数一并控制 |

**产业**：
- **DJI**：
  - 开发者站最新仍是 Cloud API 1.14.0（2025-04-15）。另列有「Cloud API 2」，没有版本与日期，内容未能核实。
  - FlightHub 2 Copilot（08-18）能由自然语言生成航线，执行前仍须人工逐项复核。
  - **基线更正**：`frontier-2026.md` §5 说 DJI「不开放连续 Offboard」并不完整。DJI 自 2023-05-08 起，经 FlightHub 2 与 Cloud API 提供云端实时飞控（键盘操纵、FlyTo、云台控制）。它不是 PX4 意义上的设定点接口；本项目的厂商网关照旧不开放低层控制（D053 / D072），DJI 适配器也不把它声明为控制能力。窗口内没有新增控制接口。
- **Skydio**（09-24）：发布 F10 固定翼、可容 5 机的 MegaDock，以及按 GIS / CMMS 自动编排巡检的 Asset Command，都没有提到 LLM。
- **PX4 v1.18（beta）**：
  - RC 接管改为按摇杆移动速度判定，新参数 `MAN_OVERRIDE_SPD` 取代 `COM_RC_OVERRIDE` 与 `COM_RC_STICK_OV`；
  - MAVLink 签名只在 SD 卡有有效密钥时启用，`MAV_SIGN_CFG` 被移除。

  本项目没有固定这些参数，但「RC / GCS 接管永远可用」是红线。升级到 1.18 时，要按架构变更重核接管与签名，这是 H2 的前置。
- **NVIDIA**：JetPack 7.2（2026-06-02）起，Orin 与 Thor 都是 Ubuntu 24.04 用户态，与本项目的 Jazzy + 24.04 基线一致。H1 可以不再走 JetPack 6（22.04）的路线；`sim/m3.Dockerfile` 注释里的 arm64 基础镜像说明，到 H1 时按此更新。

**法规（D016）**：
- **《民用航空法》（基线遗漏）**：2025-12-27 修订通过，2026-07-01 施行。民用无人驾驶航空器的设计、生产、进口、维修和飞行活动，原则上须向民航主管部门申请适航许可（按规定无需取得的除外）；生产机构须为产品设置唯一产品识别码。
- **《北京市无人驾驶航空器管理规定》**：2026-09-11 修订，11-15 施行。
  - 全市为管制空域，禁止飞行；
  - 禁止在市内持有、存放无人驾驶航空器及其核心部件，禁止运输、携带进入；
  - 学校、科研机构、生产企业因研发等需要，经市公安机关会同有关部门安全评估后，可按清单予以「特殊保障」。
- **首都特别限制区**：路透社 09-17 报道，依据 NOTAM（ZBPE A4484/26），09-20 起以北京为中心划设直径约 600 km 的特别限制区（地面至无限高），通用航空需逐案批准。是否涵盖无人驾驶航空器，未能核实。
- **民航局与 UOM**：民航局 9–10 月的意见征集里没有无人机专项；UOM 有无新公告，未能核实。
- **对本项目**：H2 的场地若在北京或京津冀，须先确认上述规定与空管要求；硬件选型要核对适航与识别码。P 系列软件与仿真不受影响。D016「真机前必须接入空域约束」不变。

## 2. 飞控与仿真工具链（D008 / D022 / D025 / D043 / D046 / D047 / D048）

| 组件 | 2026-09-24 基线 | 2026-10-14 | 对本项目 |
|---|---|---|---|
| PX4 | 锁定 v1.17.0；v1.18.0-rc1（09-10） | 没有 v1.18.0 正式版，也没有 v1.17.x 补丁版。v1.18 的 ROS 2 指南不列 Lyrical，开发环境文档仍写「Ubuntu 26.04 is not yet supported」。09-24 之后，v1.18 发布说明只改了串口透传与磁力计两处 | D043 条件 1 不满足。外部模式的注册、健康检查与模式完成语义，`estimator_status_flags` 的发布方式，失效保护延期，三者都没有变化，D039 / D047 / D048 未触发 |
| PX4 官方 SITL 镜像（D022） | — | Docker Hub 与 GHCR 上的 `px4io/px4-sitl`、`px4io/px4-sitl-gazebo` 都没有 v1.17.0 tag，最近的是 `v1.18.0-rc1` | D022 未触发，继续自建 |
| `px4-ros2-interface-lib` | Lyrical CI 的 #211 未合并；main 对应 2.2.3 | #211 仍开着、有冲突，07-31 之后没有更新；所有分支都没有 Lyrical CI。2.2.2（09-16）新增「allow Modes to defer failsafes」（#223） | D043 条件 2 不满足。我们固定的是 release/1.17 的 `4a3370f`，不含该功能。契约测试 `tests/contracts/test_autonomy_boundaries.py` 已禁止出口节点出现 `deferFailsafes`，以后升级同样受它约束 |
| `px4_msgs` | Lyrical 只有 source 条目 | 没有变化；最新 tag 仍是 v1.17.0 | D043 条件 3 不满足 |
| MAVSDK-Python | 3.17.4；v4 未发布 | **基线更正**：PyPI `mavsdk` 4.0.0 于 09-22 发布，最新 4.0.6（10-08）。gRPC 路线改名为 `mavsdk-grpc`（4.0.5），迁移文档写明 `mavsdk<4` 不再更新 | **D008 / D025 命中**，见 §8 行动 1。`pyproject.toml` 里「v4 未发布」的注释随迁移一起改 |
| Micro-XRCE-DDS-Agent | v3.0.2 | 没有变化 | — |
| ROS 2 | Lyrical / Jazzy | 两者都在 10-05 做了同步；Kilted 于 2026-12 EOL | ROS 包来自固定的 M0 基础镜像（按保留口径保留），同步不影响现有镜像，只有重建 M0 基础镜像时才会带入 |
| Gazebo | Harmonic 2029-05、Jetty 2031-05 EOL | 核实无误。两者都是 2026-06 为对齐 ROS 延长的（原为 2028-09 与 2030-09）；下一版「M」排在 2027-03 | 维持 Harmonic |
| Zenoh | rmw_zenoh 内置 zenoh-c 1.8.0 | 本体最新 1.10.1（09-07），没有 2.x。rmw_zenoh lyrical 0.10.7 / jazzy 0.2.12（10-06）把内置版本升到 1.10.1，已进 rosdistro，还没进主仓库同步 | 项目不用 rmw_zenoh，FleetTransport 固定 `eclipse-zenoh==1.9.0`，D046 未触发。以后升到 1.10 要对照它的破坏性变更重核 ACL / TLS 配置键 |
| BehaviorTree.ROS2 / Nav2 | — | BehaviorTree.ROS2 没有变化；Nav2 1.5.2 进入 Lyrical 主仓库 | X1 之后的 Nav2 适配仍在 Jazzy 上做 |

**D043 结论**：前三个切换条件都未满足，维持 Jazzy + PX4 v1.17.0 + Harmonic。Harmonic 与 Jazzy 同在 2029-05 EOL，「最迟 2028-04 启动迁移评估」的硬截止不变。

## 3. 模型与协议（D012 / D029 / D033 / D034 / D035 / D036 / D076）

- **MiniMax**：
  - **模型**：9 月以后的新模型只有 M3.1-Flash-Preview（9 月 28 日公测，日期来自媒体报道，官方文档未标）。M3 是正式版。M3.1-Flash-Preview 仍是预览版，文档写「暂时仅通过 M Plan 和 MiniMax Code 提供」。两者都没有改名，也没有下线日期。M3.1 仍没有公布按量价格，D076 的「厂商公布按量价格或调整预览模型」未命中。
  - **订阅**：Token Plan 停售，改为 M Plan。现有订阅保持自动续费即按原价、原权益续订；自动续费关闭后不能再开；升级到 M Plan 后不能退回。没有看到调整速率限制的公告。
  - **接口**：`reasoning_effort` 只对 M3.1 生效，传 `none` 返回 400；M3.1 不支持 `reasoning_split=false`；多轮工具调用要原样回传 `reasoning_content`。规划器仍用 M3，不受影响。若把规划器换到 M3.1，要先按最后一条改多轮消息。
  - **域名**：中国区文档的 base_url 已改为 `api.minimax.cn/v1`，`platform.minimaxi.com` 也 302 跳到 minimax.cn。我们用的 `api.minimaxi.com` 仍可访问，没有找到迁移公告。端点写在 `providers/runtime.py`，以及 `compose.m2.yaml` / `compose.desk.yaml` 的代理允许列表里（D036）。D036 的「模型端点需要随环境变化」尚未命中。
  - **未能核实**：M Plan 的上线日期；M3.1-Flash-Preview 的官方发布日期。
- **MCP**（D012 / D034）：
  - 2026-07-28 正式版取消了 `initialize` 握手与会话，要求服务端实现 `server/discover`，结果必须带 `resultType`；Roots / Sampling / Logging 被弃用，至少保留 12 个月。
  - `planner/tools/server.py` 与 `client.py` 是自写的 stdio 实现，固定 `2025-06-18`，两端都在本仓库，规划只读工具不受影响。
  - **D034 触发器形式上命中，结论不变。** 接入任何第三方 MCP 服务之前，先把实现升到当时的正式版。
- **A2A**（D012 / D033 / D035）：
  - v1.0.0（2026-03-12）有破坏性变更，涉及字段、枚举格式与 HTTP 路径；最新是 v1.0.1（05-28）。
  - `console/mission.py` 的 agent card 声明的是 `0.3.0`。
  - 目前没有外部 A2A 调用方，D035 未命中。开放外部调用方之前，要先升到 v1.x。

## 4. 工程环境（D014 / D077）

- **GitHub Actions**：
  - Node 24 自 2026-06-16 起成为默认；2026-09-23 起 runner 不再提供 Node 20，退回 Node 20 的开关也一并失效。
  - 我们的 `actions/checkout@v4`、`actions/setup-node@v4`、`astral-sh/setup-uv@v6` 仍声明 node20，被强制跑在 Node 24 上。最近一次运行（`a718a08`）有警告，但通过了。
  - 这三个 action 分别从 v5、v5、v7.0.0 起声明 node24（已在各自的 `action.yml` 核对）；当前最新是 v7.0.1、v7.1.0、v10.3.0。setup-uv 从 v8 起不再发布主版本标签，要写完整版本号。
  - 官方没有说「强制运行」何时改为报错，所以没有硬截止。改工作流属于 CI/CD 红线（D077），见 §8 行动 3。
  - `ubuntu-latest` 自 10-19 起指向 26.04。我们固定 `ubuntu-24.04`，不受影响。
- **CI 时长**（D077「CI 时长影响使用」）：
  - 单次运行从 10-10 的 7–8 min 增至 10-12 / 10-13 的 12–14 min，增量来自 D079–D087 新增的测试。
  - 10-10 以来真正失败的只有 `039d15a` 一次，下一次推送即恢复，其余非成功运行都是被新推送取消的。
  - 尚未到「影响使用」。超过约 20 min 时，按 D077 拆成快速与完整两层。
- **uv**：
  - 最新是 0.13.0（10-09），0.12 与 0.13 都有破坏性变更。`uv.lock` 仍是 schema v1，只有 revision 从 3 升到 5，旧版 uv 读取时不报错。
  - 我们固定 0.11.2，`uv sync --frozen` 不受影响。
  - 以后升级要注意两点：0.12 不再接受 `.tar.bz2` / `.tar.xz` 格式的源码包；0.13 默认 Python 改为 3.15（我们固定 3.12）。

## 5. 共享主机、容量与存储（D023 / D038 / D045 / D056–D064 / D061 / D085）

- **批次因隔离作废（D038 / D061 命中）**：
  - **起因**：car-agent 在运行期间发布，同机容器身份随之变化，隔离判据不成立，整批回执不计入。起因是发布，不是负载。
  - **实例**：
    - D075 的 M3 能源场景，三个种子的首批全部因此作废；
    - D081 候选 `1abe749` 的首次部署被隔离判据停止；
    - 10-10 前后，car-agent 约每 45 分钟发布一次。
  - **应对**：每批两个场景，开跑前等安静窗口；隔离不成立的整批重跑，原回执保留；门禁只收隔离成立的整批。没有错误结果因此入账，代价是重跑时间。
  - **D045 不受影响**：D045 的作废率触发器按仿真停顿定义，M3 记录的是零个作废用例，不受此影响。
  - **结论**：留在共享主机。本次在 D038 补记新的重估条件：同一门禁连续三批因隔离作废，或一次门禁因此多耗一天以上。
- **容量（D085 未命中）**：
  - **账本**：主文件仍为 292 607 页（1.199 GB），与 10-13 13:46Z 的盘点相同；空闲页 97 523 页，约 0.40 GB。读数波动只来自 WAL（5.3 MB）。
  - **归档**：974 块、1 939 718 行、37.9 MB，逐块读回无问题；`overdue_rows` 为 0。
  - **延迟**：机场报告入账 p99 为 4.4 ms，每轮处理 p95 为 6.7 ms。
  - **结论**：「账本文件在稳态下仍持续增长」不成立。回执里没有过期拒绝的计数，这一项没有直接核查。
- **单文件账本（D056 / D058 / D060 / D064）**：仍是单服务进程、单文件，未命中。
- **主机 GC**：10-13 起去掉保底，一次清空（car-agent 维护，见[云端开发](../cloud-development.md)）。它不是本项目的决策。

## 6. 业务与数据（D052 / D054 / D063 / D065 / D066 / D071 / D076）

WP-P6-01 正在用公开的无人机视角素材做开发诊断（另一条工作线，见该工作包的素材审查记录）。它对下面几个触发器的影响：

- **D065 / D066 / D076「获得无人机视角的授权素材」、D071「获得授权的道路实拍素材」：部分命中。** 取得的是公开素材，但许可有分歧（作者仓库与数据卡的说法不一致），清单也还没冻结。在许可澄清、清单冻结之前，不按命中处理。
- **D052「真实数据来源或许可变化；分析器进入新业务域」**：开发画像 `road-region-dev-v1` 只用于开发诊断。正式的道路画像冻结时，这条才命中，届时按 D052 重查来源分层，并按 D054 处理混合来源的导入。
- **任务台视觉画像**：仍是 change-v3。

## 7. 重估触发器逐条结论

| 决策 | 触发器（摘要） | 结论 |
|---|---|---|
| D001 | 第三个本体出现 | 未命中。X1 引入地面车时要判断它算不算「第三个本体」；若算，按 D001 在 embodied-agent 侧记录（待其维护者执行） |
| D003、D040 | guardian 监督周期预算 | SITL 已测，维持 Python（D040）；JIL 待 H1 |
| D008、D025 | MAVSDK v4 发布；迁移 SDK | **命中**（§2），本次在 D008 补记 |
| D020、D021、D024、D026 | M1–M3 的阶段事件 | 历史命中，已在 M1–M3 处理 |
| D022 | 官方预编译镜像 | 未命中，没有官方 v1.17.0 SITL 镜像 |
| D023、D038、D061 | 共享主机使批次反复作废 | **命中**（§5），本次在 D038 补记 |
| D045 | 仿真停顿作废率超过 20% | 未命中 |
| D027、D028、D033、D035、D037 | 只读成员、公网、真机、多机器人、A2A 调用方 | 多机器人已由 D061 / D069 处理；其余未命中 |
| D029 | MiniMax 模型或工具调用率变化 | 新增的 M3.1-Flash-Preview 已由 D076 接入视觉档；规划器仍用 M3，没有看到调用率变化 |
| D030 | 第二台机器人 | 已由 D061 处理（每台独立证书，URI SAN 为机器人 ID） |
| D031、D046 | Zenoh 大版本；空中接收新任务版本 | 未命中（Zenoh 没有 2.x；项目固定 1.9.0） |
| D032 | 空中改航；自动批准误判 | 未命中 |
| D034 | MCP 不兼容修订 | **形式上命中**（§3），结论不变，本次在 D034 补记 |
| D036 | 第二厂商或视觉档；端点变化 | 视觉档已由 D076 处理（同一端点）；域名迁移在观察 |
| D039、D047、D048 | px4_ros2 / PX4 语义变化 | 未命中（v1.18 未正式发布，固定版本未变） |
| D041、D044 | 真实相机数据 | 未命中（H 线） |
| D043 | 2026-10 雷达 | 本次复查，条件不满足（§2），本次在 D043 补记 |
| D049、D050、D051 | 首场景、两机容量、硬件、多 worker、跨站 | 未命中 |
| D052、D054 | 来源或许可变化；新业务域；混合素材导入 | WP-P6-01 冻结时命中（§6） |
| D053、D072 | 目标设备与固件；厂商 API 改变控制能力 | 未命中：窗口内 DJI 没有新增控制接口。基线漏记了 2023 年起的云端实时飞控，更正见 §1；网关照旧不开放低层控制 |
| D055、D057、D063、D068、D069、D075 | 各自的下一阶段 | 已命中，并由后续决策处理（D059 / D060、D063 / D064、D071、D070 / D074、D076、D084） |
| D056、D058、D060、D064 | 多主机写入、备份窗口、换库 | 未命中（单服务进程；账本主文件自 10-13 未增长） |
| D059 | 地面机器人接入（X1） | X1 设计时重估 |
| D062、D067 | 空闲仿真、基础镜像换代 | 未命中 |
| D065、D066、D071、D076 | 无人机视角或道路授权素材；模型换代；按量价格 | 部分命中（§6）；价格仍未公布 |
| D070 | 审计导出、保留期限、跨项目汇总 | 未命中 |
| D073、D074 | 长稳；任务级三元判定；真实设备 | 未命中 |
| D077 | 多人协作；CI 时长；CI 跑 SITL | 未命中，时长上升在观察（§4）。另外，Node 20 移除需要改工作流 |
| D084–D087 | 见各条 | 未命中（D085 见 §5） |

D002、D004–D007、D009–D019、D042、D078–D083 没有写重估触发器。其中与外部事实相关的 D012（MCP / A2A）、D016（法规）与 D017（DJI）在 §1、§3 中一并复查。

## 8. 行动项

| # | 行动 | 依据 | 需要 |
|---|---|---|---|
| 1 | MAVSDK-Python 迁移到 v4，只动适配器。等 4.0.x 连续两周没有新补丁再选定版本。与已决定的 Dockerfile apt 修正合并为一次机载改动，只跑一次完整 M1 回归和 D025 要求的场景矩阵；在 X1 改适配器之前完成 | D008、D025 | 机载改动，按常规门禁，开工前告知 |
| 2 | 在 D008、D034、D038、D043 补记本次复查 | §2、§3、§5 | 本次已做 |
| 3 | CI 的三个 action 升到声明 node24 的主版本：checkout 与 setup-node 至少 v5，setup-uv 至少 v7.0.0（v8 起写完整版本号） | §4 | **CI/CD 红线，需要你批准** |
| 4 | 若要保留 Token Plan 的原权益，确认 MiniMax 订阅的自动续费开着 | §3 | **账户动作，需要你处理** |
| 5 | 观察 MiniMax 的域名迁移。旧域名一旦公告下线，按 D036 改端点与两处代理允许列表，并重新部署 | §3 | 届时请示部署 |
| 6 | 开放外部 A2A 调用方之前，先升到 v1.x；接入第三方 MCP 服务之前，先升到当时的正式版 | §3 | 届时设计 |
| 7 | X1 设计时一并重估 D001、D051、D059（地面机器人接入） | §7 | 写入 X1 设计稿 |
| 8 | WP-P6-01 冻结之前，先处理来源许可，再按 D052 / D054 / D065 判定 | §6 | 该工作线 |
| 9 | H 线开工时：<br>· 以 JetPack 7.2（Ubuntu 24.04）作 arm64 基线；<br>· 升级 PX4 1.18 时，按架构变更重核 RC 接管与 MAVLink 签名；<br>· 选型时核对适航许可与唯一产品识别码；<br>· 场地若在北京或京津冀，先确认地方规定与空管要求 | §1 | H1 / H2 开工时 |
| 10 | 下一次雷达在 2027-01；Jazzy / Harmonic 的迁移评估最迟 2028-04 启动 | D043 | — |

## 来源

- 前沿：[2609.01404](https://arxiv.org/abs/2609.01404)、[2609.20191](https://arxiv.org/abs/2609.20191)、[2609.18451](https://arxiv.org/abs/2609.18451)、[2610.03319](https://arxiv.org/abs/2610.03319)、[2610.09764](https://arxiv.org/abs/2610.09764)、[2609.08402](https://arxiv.org/abs/2609.08402)、[2609.27457](https://arxiv.org/abs/2609.27457)
- 产业：[DJI 云端实时飞控（2023-05-08）](https://enterprise-insights.dji.com/blog/dji-dock-live-flight-control)、[DJI Cloud API](https://developer.dji.com/cloud-api)、[FlightHub 2 Copilot](https://enterprise-insights.dji.com/blog/dji-flighthub-2-copilot-route-generation-update-august-2026)、[Skydio Ascend 2026](https://dronelife.com/2026/09/29/skydio-unveils-f10-lightrunner-fixed-wing-drone-and-megadock-at-ascend-2026/)、[JetPack 7.2](https://forums.developer.nvidia.com/t/jetpack-7-2-jetson-software-goes-agentic-with-jetson-linux-39-2/372056)
- 法规：[《民用航空法》修订](https://www.news.cn/politics/20251227/f9da52c8ec6048b48e4134f320298b1d/c.html)、[《北京市无人驾驶航空器管理规定》](https://www.beijing.gov.cn/zhengce/dfxfg/202609/t20260914_4862677.html)、[首都特别限制区（路透社）](https://www.internazionale.it/ultime-notizie-reuters/2026/09/17/china-creates-italy-sized-no-fly-zone-around-beijing-after-rare-crash)、[ops.group 对 NOTAM 的整理](https://ops.group/blog/beijings-new-600km-restricted-area/)、[民航局意见征集](http://www.caac.gov.cn/HDJL/YJZJ/)
- PX4：[releases](https://github.com/PX4/PX4-Autopilot/releases)、[v1.18 发布说明](https://docs.px4.io/main/en/releases/1.18)、[v1.18 ROS 2 指南](https://docs.px4.io/v1.18/en/ros2/user_guide.html)、[Ubuntu 开发环境](https://docs.px4.io/main/en/dev_setup/dev_env_linux_ubuntu.html)、[px4-sitl 镜像 tag](https://hub.docker.com/r/px4io/px4-sitl/tags)
- px4_ros2 与 px4_msgs：[#211](https://github.com/Auterion/px4-ros2-interface-lib/pull/211)、[releases](https://github.com/Auterion/px4-ros2-interface-lib/releases)、[lyrical/distribution.yaml](https://github.com/ros/rosdistro/blob/master/lyrical/distribution.yaml)、[px4_msgs tags](https://github.com/PX4/px4_msgs/tags)
- MAVSDK：[PyPI](https://pypi.org/project/mavsdk/)、[v4.0.0](https://github.com/mavlink/MAVSDK/releases/tag/v4.0.0)、[迁移文档](https://mavsdk.mavlink.io/main/en/python/migration.html)
- ROS 2 与 Gazebo：[Lyrical 同步](https://discourse.openrobotics.org/t/58605)、[Jazzy 同步](https://discourse.openrobotics.org/t/58622)、[发行版列表](https://docs.ros.org/en/rolling/Releases.html)、[Gazebo releases](https://gazebosim.org/docs/latest/releases/)、[docs#698](https://github.com/gazebosim/docs/pull/698)、[docs#670](https://github.com/gazebosim/docs/pull/670)、[XRCE Agent releases](https://github.com/eProsima/Micro-XRCE-DDS-Agent/releases)
- Zenoh：[releases](https://github.com/eclipse-zenoh/zenoh/releases)、[rmw_zenoh changelog](https://github.com/ros2/rmw_zenoh/blob/lyrical/rmw_zenoh_cpp/CHANGELOG.rst)；BehaviorTree.ROS2：[仓库](https://github.com/BehaviorTree/BehaviorTree.ROS2)
- MiniMax：[文本生成](https://platform.minimax.cn/docs/guides/text-generation)、[模型 FAQ](https://platform.minimax.cn/docs/faq/models.md)、[模型更新](https://platform.minimax.cn/docs/release-notes/models.md)、[按量价格](https://platform.minimax.cn/docs/guides/pricing-paygo.md)、[Token Plan 公告](https://platform.minimax.cn/docs/m-plan/token-plan-notice)、[速率限制](https://platform.minimax.cn/docs/guides/rate-limits.md)、[OpenAI 兼容接口](https://platform.minimax.cn/docs/api-reference/text-openai-api.md)、[公测报道](https://finance.eastmoney.com/a/202609283885056519.html)
- MCP 与 A2A：[MCP 2026-07-28 变更](https://modelcontextprotocol.io/specification/2026-07-28/changelog)、[A2A releases](https://github.com/a2aproject/A2A/releases)
- GitHub Actions 与 uv：[Node 20 弃用](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/)、[Node 20 移除](https://github.blog/changelog/2026-09-23-node-20-is-no-longer-available-in-github-actions/)、[setup-uv v8.0.0](https://github.com/astral-sh/setup-uv/releases/tag/v8.0.0)、[ubuntu24 镜像](https://github.com/actions/runner-images/releases/tag/ubuntu24/20261004.327)、[Ubuntu 26 与 latest](https://github.blog/changelog/2026-09-17-ubuntu-26-generally-available-and-latest-migration/)、[uv 0.13.0](https://github.com/astral-sh/uv/releases/tag/0.13.0)
