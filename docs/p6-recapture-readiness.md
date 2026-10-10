# P6 主动补拍与目标区域质量（WP-P6-02）验收记录

[路线图](roadmap.md) · [P6 实施方案](p6-implementation.md) · [决策 D078](decisions.md) · [运营架构](architecture/09-operations.md) · [运维手册](operations-guide.md) · [发布门禁](../scripts/verify_p6_recapture.py)

**WP-P6-02 已准出（2026-10-10，候选 `ecee452`），范围为软件与 SITL。** 门禁十五项全部通过：
- **S0**：P6-F01–F10 × 3 个种子，30/30，在线与回放一致，计数全 0。
  - 21 个运行的首次拒判由补拍接替，运行完成。
  - 两次都拒判（F04）与拒绝补拍（F06）的 6 个运行以失败结束，没有结论。
  - 对照场景 F09 中，整图画像（`quality-v1`）的对照模板把阴影读成疑似 3 次，P6 模板 0 次。
- **S1**：一架 PX4 SITL 飞行器，5/5，9 次飞行。
  - 标记中心的发光圆片使首次采集以 `quality.target_exposure` 拒判：目标亮像素占比 0.997–1.0，限值 0.5。
  - 圆片移除后，补拍结果正常。
  - 圆片不移除时，两次拒判，运行失败，没有发现。
  - 只有损伤贴片时，质量层放行，分析为疑似。
- **位姿模型**：9 次拍摄与 Gazebo 真值对照：
  - 真实航向与登记航线末段方向相差 1.9°–12.7°（声明 ±20°）；
  - 上报位置与真值相差 0.02–0.09 m（声明 0.35 m）；
  - 拍摄点停在标记中心前 0.68–0.92 m。
- **常驻任务台**：激活到候选，工作流与业务目录为 `p6_desk_v1`。经真实 Tailnet 入口完成一次补拍会话：
  - 首次采集因反光被拒判；
  - 补拍等待审批时先清除反光，再批准；
  - 补拍分析正常，运行完成；
  - 两次飞行都由本人批准，并经独立裁判。
- **回归**：P1–P5 的 S0 矩阵与授权矩阵（任务台的 P6 目录）当场重跑，全部通过；P2 S1 与 P4 S1 各 5/5。

计数与证据边界：
- S0 的反光、阴影、模糊与位置偏差是逻辑相机上的合成效果；S1 的反光是 Gazebo 中的发光圆片。两者都不代表真实相机、真实反光或任何识别精度。
- **航向是声明的，不是测量的**：证据位姿不带姿态。S1 的位姿模型只用 Gazebo 真值核对过。上真机前，要么在机载证据中加入姿态（改机载适配器与 wire，属架构变更），要么在实际平台上重新标定。
- 补拍 v1 只在同一登记拍摄位重采，每次巡检至多一次，只用于巡检模板；每次补拍飞行仍要人工审批（D032）。
- 不新增表或列。机载 executive、guardian、适配器、uplink、wire、技能与恢复策略都未改，按 scope 判据不需要 M1 回归。
- 常驻任务台的模型分析器仍固定 change-v3（D078 用户决定）；本记录不涉及任何模型精度。

## 版本与门禁

| 项目 | 准确记录 |
|---|---|
| 被测 / 部署源码 | `ecee4520c3a8280b1f09199c6519f6ff35410986` |
| 云端部署 | `20261010T105646Z-ff1fd44a`；[部署回执](verification/p6-recapture-2026-10-10/deployment-ecee452.json) |
| 控制面摘要 | `c781e1fe253be825d387cfefd5116e6a7aefa2e58f641ef076100102793c3a19` |
| 门禁 | [release.json](verification/p6-recapture-2026-10-10/release.json)：`status=passed`，十五项全部 passed；运行时 HEAD 即候选，绑定 14 份输入摘要与能力清单 |
| 持续检查 | GitHub Actions run `38046643577`（`ecee452`）success；不计入门禁（D077） |
| 常驻入口 | [控制台](verification/p6-recapture-2026-10-10/desk/console-activation.json)与[任务台](verification/p6-recapture-2026-10-10/desk/desk-activation.json)都激活在同一候选上；入口已脱敏 |

候选之后只增加证据与文档的提交不改变被测源码。不能把这份结果改称其他代码 SHA 的验收。

## 已实现范围

- **目标区域质量 `quality-v2`**（WP-P6-02a）：画像固定相机模型、位姿模型与目标限值。
  - 几何：按采集位置与资产登记的位置、尺寸，把目标正方形投影到画面。可见比例取位姿容差内的最小值；核心像素是容差内任何航向与位置下都属于目标的像素，边界环是可能落在目标边缘的像素。
  - 检查顺序：几何缺失 → 整图检查 → 出画 → 过小 → 偏离中心 → 目标曝光（核心像素）→ 目标清晰度（边界环）。
  - 位姿模型：航向为 `unknown`（S0，取全部航向）或 `approach_route`（S1，登记观测航线末段方向 ± 容差），另加位置容差。
  - 位置、相机或尺寸缺失，或画面尺寸与相机不符，一律 `quality.target_unknown`，不退回整图检查。
- **补拍策略 `recapture-v1`**（WP-P6-02c）：业务目录列出可补拍原因，即整图模糊 / 曝光、目标出画 / 偏离中心 / 曝光 / 模糊，以及 `analysis.undeterminable`。`model.timeout`、`quality.target_too_small` 等不可补拍。没有策略的目录摘要不变。
- **工作流内核**（WP-P6-02b）：
  - 失败的分析节点提供谓词输出 `recapture`，取自节点详情中业务层写下的判定；
  - 补拍提交节点 `recapture_of` 与原巡检使用同一机器人、体积与资产，不允许链式补拍；
  - 无副作用的 `select_analysis` 选第一个完成的分析；
  - 被补拍接替的拒判不计入运行失败，运行结论另记 `recaptured`。
- **服务与页面**：补拍请求文本带原因与原分析作业。页面加入「选择分析结果」节点与新原因码的中文名。入口不加补拍按钮。
- **确定性分析器可固定质量画像**：任务台各站点相机不同，所以由分析器各自固定画像；未设置时行为与摘要不变。
- **目录**：S0（`p6_campus_v1`）、S1（`p6_s1_v1`）、任务台（`p6_desk_v1`，逐字节保留全部 P5 模板与分析器，在 PX4 SITL 站点上加 `recapture_watch` / `recapture_reinspection`）。
- **验证**：
  - S0 世界 `eval/p6_world.py`：逻辑相机的反光、阴影、模糊与拍摄偏差；
  - 独立裁判 `eval/judge_p6.py`：自行复算几何与质量，S1 用 Gazebo 真值核对位姿模型；
  - S1 编排 `scripts/remote_p6.py`：Gazebo 世界服务放置 / 移除发光圆片；
  - 任务台 `desk-world` 增加 `glare` 状态，`desk_probe.py workflow --clear-before-recapture` 驱动补拍会话；
  - 门禁 `scripts/verify_p6_recapture.py`。
- **存储**：不新增表或列。策略在业务目录，判定在既有的 `wf_nodes.detail`，选择结果在既有的 `wf_nodes.result`。

## 检查结果

| 判据 | 实际结果 |
|---|---|
| scope | 相对 P4 收尾记录 `2c6a5da` 改动 57 个文件，全部在 P6 路径与文档内；机载路径未改，M1 回归按规则不需要 |
| checks | Linux **1411 项通过，0 失败 / 错误 / 跳过**；部署前后其他容器身份一致 |
| s0 | 门禁当场运行 **30/30**（P6-F01–F10 × 3），在线 / 回放一致；二十二项计数全 0，含越权补拍、超额补拍、补拍不一致、拒判采集出结论、选择不一致与质量不一致；作废尝试 0 |
| p1 / p2 / p3 / p4_regression | 门禁当场运行 S0 矩阵 **42/42、45/45、45/45、45/45**，计数全 0，作废尝试均为 0 |
| p5_regression | S0 **24/24** 与 S3 **36/36**，计数全 0 |
| authz | 在任务台的 P6 目录上，49 个方法 × 11 类身份：API 539 行、帧 248 行；应拒的 517 次全部拒绝，越权尝试写入审计 120 条；逃逸 0 |
| s1 | **5/5**，9 次 PX4 SITL 飞行；三份回执隔离都成立，在线 / 回放一致，计数全 0 |
| p2_s1_regression | **5/5**，8 次飞行，三份回执 |
| p4_s1_regression | **5/5**，12 次飞行，三份回执；另有一份因仿真停顿不计入（见「开发负记录」） |
| desk | 工作流与业务目录 `p6_desk_v1`，运营与调度目录 `p5_desk_v1`；迁移 current，副本演练与目录切换演练 passed；三种执行后端，四个机场会话 active；七个常驻容器都没有 Docker 权限；页面、脚本与健康都在候选上；伪造身份头被忽略 |
| desk_session | 见「常驻任务台：补拍会话」 |
| historical | P5 发布记录与 P4 S2 复验记录与各自收尾提交字节一致 |

## S0 矩阵

`eval/p6_world.py`，每个场景 × 种子 7 / 19 / 41，独立裁判在线与回放各判一次。

| 场景 | 内容 | 结果 |
|---|---|---|
| F01 | 受损标记上有反光：首次采集因目标区域被拒，反光消失后获批的补拍看到损伤，发现被确认并开一张工单 | 3/3 |
| F02 | 同一 ID 的事件触发两次只启动一个运行；整图模糊的首次采集补拍一次，结果正常 | 3/3 |
| F03 | 首次采集偏离标记 1.2 m：航向未知时无法确定目标像素，以偏离中心拒判；位于标记上方的补拍正常 | 3/3 |
| F04 | 阴影一直在标记上：采集与唯一一次补拍都被拒，选择节点失败，运行失败，没有发现也没有缺省结论 | 3/3 |
| F05 | 缺参考图、模型超时、provider 错误与目标不符都不补拍；模型回答目标被遮挡（`analysis.undeterminable`）时补拍一次，结果正常 | 3/3 |
| F06 | 操作者在审批前取消补拍任务以示拒绝：不再飞行，运行失败 | 3/3 |
| F07 | 补拍等待审批时取消运行：补拍从不飞行，取消后不做任何分析 | 3/3 |
| F08 | 服务接受补拍但响应丢失、进程死亡；重启后恰好一个补拍任务并飞行 | 3/3 |
| F09 | 对照：两个站点上相同的阴影。对照模板的整图画像让模型读成损伤，需要人驳回；P6 模板拒判目标区域、补拍并得出正常 | 3/3 |
| F10 | viewer、他项目、admin、agent、机场与匿名身份审批补拍或越权启动模板全部被拒；只有本项目操作者能审批 | 3/3 |

指标（门禁汇总）：
- 补拍 30 次，其中 24 次起飞；F06 取消补拍任务、F07 取消运行，这 6 次没有飞。
- 21 个运行由补拍接替首次拒判并完成；6 个补拍后仍失败（F04 阴影一直在，F06 拒绝补拍）；3 个在补拍审批前取消（F07）。
- F09 中对照模板错误疑似 3 次，P6 模板 0 次。
- 这些比例由场景设计决定，不是现实环境中的补拍成功率。S0 是逻辑飞行，不报告时长；S1 中每次补拍是一次额外的完整飞行（共 4 次）。

## S1：PX4 SITL 上的反光与补拍

部署 `20261010T105646Z-ff1fd44a`，确定性颜色特征分析器 `signature_s1_v1` 位于 S1 画像 `quality_target_s1_v2` 之后。Gazebo 世界服务在红色标记中心放置半径 0.95 m 的自发光白色圆片。

| 用例 | 种子 | 回执 | 飞行 | 首次采集 | 补拍 |
|---|---|---|---|---|---|
| `p6_s1_glare_recapture` | 7 / 19 | [`p6-20261010T110715Z-3adeb0c9`](verification/p6-recapture-2026-10-10/s1/p6-20261010T110715Z-3adeb0c9.json) | 4 | `quality.target_exposure`，亮像素占比 0.997 / 1.0 | 正常（亮像素 0，目标清晰度 132.1） |
| `p6_s1_glare_recapture` | 41 | [`p6-20261010T111520Z-3086eba8`](verification/p6-recapture-2026-10-10/s1/p6-20261010T111520Z-3086eba8.json) | 2 | `quality.target_exposure`，1.0 | 正常 |
| `p6_s1_glare_persists` | 7 | [`p6-20261010T111903Z-9d8b3d64`](verification/p6-recapture-2026-10-10/s1/p6-20261010T111903Z-9d8b3d64.json) | 2 | `quality.target_exposure`，1.0 | 仍为 `quality.target_exposure`；选择节点失败，运行失败，无发现 |
| `p6_s1_damage_found` | 7 | 同上 | 1 | 放行（亮 / 暗像素都为 0，贴片为中灰）；分析为疑似，确认并开单 | 不补拍 |

9 次采集的核心像素 508–586（限值 150），可见比例 0.995–1.0（限值 0.95），目标边长约 49–51 像素（限值 24）。

**位姿模型核对**（真值取离拍摄时刻最近的 Gazebo 样本，相差都不超过 0.03 s）：

| 量 | 9 次采集的范围 | 画像声明 |
|---|---|---|
| 真实航向与观测航线末段方向之差 | 1.9°–12.7° | ±20° |
| 上报位置与真值的水平距离 | 0.02–0.09 m | 0.35 m |
| 拍摄点到标记中心（沿进近方向） | 0.68–0.92 m | —（几何按上报位置计算） |

## 常驻任务台：补拍会话

激活回执见上表。会话经真实 Tailnet HTTPS 入口进行，身份为 tailnet 用户，回执已脱敏：[recapture-session.json](verification/p6-recapture-2026-10-10/desk/recapture-session.json)。

1. 用 `desk-world --section s1 --asset asset_red --state glare` 在红色标记上放置反光（[回执](verification/p6-recapture-2026-10-10/desk/world-glare.json)；两次改变都写入主机 `desk/world/history.jsonl`）。
2. `desk_probe.py workflow --start --workflow recapture_watch --asset-input asset_red --clear-before-recapture asset_red`：
   - t = 0.9 s 本人批准首次巡检；约 26–85 s 飞行；
   - 分析以 `quality.target_exposure` 拒判，补拍任务等待审批；
   - t = 90.9 s 探针先以 `desk-world` 把 `asset_red` 设回正常（退出码 0），再批准补拍；
   - 约 112–185 s 补拍飞行，分析正常；选择节点取补拍分析，复核因不是疑似而跳过；运行 `completed`，结论 `recaptured: [analyze]`。
3. 两次飞行都由监管者独立裁判：completed，回放一致，错误成功 0。
4. 会话共 190.7 s。t = 65.9 s 有一次 `SSLEOFError`，探针重新连接后继续；运行与审批不受影响。

## 回归

| 回归 | 结果 | 回执 |
|---|---|---|
| P1–P5 S0、授权矩阵 | 门禁当场运行，见「检查结果」 | [release.json](verification/p6-recapture-2026-10-10/release.json) |
| P2 S1 | 5/5：`p2_s1_chain` × 3 种子、`p2_s1_cancel_in_flight` 与 `p2_s1_restart`（种子 7），8 次飞行 | [p2-s1/](verification/p6-recapture-2026-10-10/p2-s1/) |
| P4 S1 | 5/5：`p4_s1_close` × 3 种子、`p4_s1_not_repaired` 与 `p4_s1_dedup`（种子 7），12 次飞行 | [p4-s1/](verification/p6-recapture-2026-10-10/p4-s1/) |

## 开发负记录（不计入）

| 记录 | 现象 | 原因与处理 |
|---|---|---|
| S1 标定（`c660ff9`，部署 `20261010T095336Z-ee9c2dec`，[不计入](verification/p6-recapture-2026-10-10/not-counted/s1-calibration/)） | 暂定画像下 12 次采集全部以 `quality.target_off_center` 拒判，补拍也一样 | 拍摄点停在标记前约 0.8 m；航向未知的保守几何只剩半径 3–5 像素的确定像素。加入位姿模型，S1 画像升为第 2 版，新候选 `ecee452`；所有计入的运行都在新候选上重跑（D078 S1 标定补记） |
| P4 S1 回归 `p4_s1_close` 种子 41（`p4-20261010T114844Z-cc7ba6be`，[不计入](verification/p6-recapture-2026-10-10/not-counted/p4-s1/)） | 飞行次数 3 ≠ 2；安全计数全 0，飞行裁判通过，错误成功 0 | 巡检刚起飞时仿真停顿：0.503 s 墙钟内仿真时间只前进 0.032 s，guardian 以 `observation_stale` 保持并返航，服务重新规划后 v2 正常完成。与 P0（`271c5ac`）、P3（`cccb03c`）的停顿同类；不改阈值，不改判，在安静窗口重跑通过 |

## 接手项

- **WP-P6-01 目标业务评测集**：需要用户提供或选定授权的无人机视角素材，才能衡量目标业务的识别能力。
- **测量航向**：在证据中加入姿态，用实测航向替代声明航向。要改机载适配器与 wire，属架构变更，先写决策。
- **补拍 v2**：更近的登记拍摄位（解决 `quality.target_too_small`），以及复检模板的补拍（需要改结算规则）。
- **常驻任务台的视觉画像**：仍是 change-v3，等 WP-P6-01 再定。
- **代码整洁**：`scripts/desk_probe.py` 第 491 行与 `tests/fleet/test_target_quality.py` 第 181 行各有一处续行被合并成长空白（语义不变）。为不改动已验证的候选，留到下次代码改动时整理。

## 复现

```powershell
# S0 与授权矩阵（门禁当场运行）/ the S0 and authorization matrices (run by the gate)
uv run python -m drone_agent.eval.p6_world --output outputs/p6-s0
# S1（云端）/ S1 on the cloud host
uv run python scripts/dev_stack.py deploy --sha HEAD --apply --artifacts D:/drone-agent-cloud
uv run python scripts/dev_stack.py p6 --scenario all
uv run python scripts/dev_stack.py p2 --scenario all
uv run python scripts/dev_stack.py p4 --scenario all
# 常驻任务台与补拍会话 / the resident desk and the recapture session
uv run python scripts/dev_stack.py console-cloud --apply
uv run python scripts/dev_stack.py desk-cloud --apply
uv run python scripts/desk_probe.py http --origin <origin> --output <dir>/http.json
uv run python scripts/desk_probe.py spoof --origin <origin> --output <dir>/spoof.json
uv run python scripts/dev_stack.py desk-world --section s1 --asset asset_red --state glare --reason "<reason>" --apply
uv run python scripts/desk_probe.py workflow --origin <origin> --start --workflow recapture_watch --asset-input asset_red --clear-before-recapture asset_red --output <dir>/recapture-session.json
# 门禁 / the gate
uv run python scripts/verify_p6_recapture.py --sha <full sha> --deployment ... --s1 ... --p2-s1 ... --p4-s1 ... --desk ... --desk-http ... --desk-spoof ... --desk-session ... --output docs/verification/p6-recapture-2026-10-10/release.json
```

共享云主机上按安静窗口分批运行，门禁只收隔离成立、整批通过的回执。
