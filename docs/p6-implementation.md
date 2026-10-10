# P6 实施方案：主动补拍与目标区域质量（WP-P6-02）

[路线图](roadmap.md) · [实施任务 §10](operations-implementation.md) · [评审记录](research/external-review-2026-10-10.md) · [P4 记录](p4-readiness.md) · [运营架构](architecture/09-operations.md)

**状态：2026-10-10 用户批准（D078），实施中。** 评审的四个问题都按设计稿的建议定下：
1. 首版只做同位重采，每次巡检至多补拍 1 次；
2. 可补拍原因按 §3.2，`model.timeout` 不算（模型问题重试分析，不重飞）；
3. 缺几何信息直接拒判，不退回整图检查；
4. 补拍任务仍逐次人工审批（D032 不变）。

读代码后对评审稿做了五处细化，实施中又补充一处，见 §6。本方案不改变任何已有门禁的状态。

## 1. 要解决的问题

现在的闭环是「拍照 → 分析 → 看不清 → 拒判，等人处理」。拒判原因已经存在（`quality.blurry`、`quality.exposure`、`analysis.undeterminable` 等），但只能让分析节点失败、运行以失败结束，系统不会利用这些原因补救。

另一个问题更隐蔽：`quality-v1` 只看整张图。目标上的强反光或遮挡只占画面一小块，整图检查照样通过；接下来颜色特征分析器会把被遮住的标记读成「疑似损伤」，模型分析器也可能误判。

评审建议：把失败信息转成有边界的下一步动作。补拍只在已登记拍摄位重采；次数与资源有预算；每次飞行仍要审批；补拍失败不产生结论；模型只能给出原因，不能生成飞行指令。验收目标是在遮挡、模糊与位置偏差场景中减少拒判与人工接管，且错误判定不增加。

## 2. 现状核对

| 事实 | 位置 | 对设计的约束 |
|---|---|---|
| `quality-v1` 只看整张图的分辨率、亮度、截断比例与梯度清晰度 | `fleet/analysis.py::check_quality` | 背景清楚不代表目标清楚 |
| 证据带拍摄位置与协方差，**不带姿态**；PX4 适配器、逻辑飞行器与厂商网关都不填 `orientation` | `contracts/events.py`、`adapters/px4_mavsdk.py` | 航向未知；补航向要改机载适配器与 wire，本工作包不做 |
| 采集只有在服务复核为 verified 时才进入分析：标记颜色占比须在登记范围内，拍摄点距资产不超过 `above_asset_m` | `fleet/verifier.py`、`fleet/workflow.py::_await` | 完全遮住标记的采集在复核层就是 unverified，运行以未知结束，不会到分析层；补拍只处理已证实采集上的分析拒判 |
| P4 分析作业被拒时，节点以带原因的失败结束；运行只要有失败节点就是 `failed` | `business.py::poll_analysis`、`workflow.py::outcome` | 被补拍弥补的首次拒判不能再让运行失败 |
| 条件只有 `suspected` / `decision` 两种输出，谓词只读**已完成**节点的输出 | `workflow_models.py::Predicate`、`predicate_holds` | 失败节点的原因不能作条件，需要内核扩展 |
| 模板是无环 DAG，节点数有上限 | `WorkflowSpec` 校验 | 补拍只能按固定次数展开，不能循环 |
| 每次飞行都要人工审批（D032），领取闸门重查可派遣性与预约 | `service.py`、`dispatch.py` | 补拍是一次新的任务，沿用全部审批与派遣规则 |

## 3. 设计

### 3.1 目标区域质量（`quality-v2`）

质量画像新格式 `drone.quality-profile/v2`：包含 v1 的整图限值，另加相机模型与目标限值。相机模型随画像固定，因为画像就是「某种采集配置下的质量限值」。站点地图与能力描述都已被任务包与 wire 固定，往里加字段会改变它们的摘要。

```yaml
camera: {camera_id: cam_0, width: 160, height: 120, hfov_rad: 1.4, mount_below_m: 0.15}  # 下视针孔
target: {min_visible_fraction: 0.95, min_side_px: 24, min_core_px: 150,
         dark_level: 40, max_dark_fraction: 0.9, bright_level: 245, max_bright_fraction: 0.6,
         min_sharpness: 10}
```

**几何（航向未知）**：
- 相机高于资产 `Z = 位置.z − mount_below_m − 资产.z`，焦距 `f = (width / 2) / tan(hfov / 2)`。
- 资产按登记的位置与 `size_m` 视为与地图轴对齐的正方形。
- 可见比例取所有航向中的最小值（按 1° 采样，多边形与画面裁剪）。
- 目标尺度：投影边长 `f · size / Z`。
- **确定目标像素**：只有相机正下方点落在目标正方形内、留有余量时，画面中心周围半径 `r = f · (size / 2 − 切比雪夫偏移) / Z` 的圆盘在任何航向下都属于目标。用它测曝光。
- **目标边界环**：从中心算起 `[r − 2, 外接半径 + 偏移 + 2]` 像素的圆环，总包含目标边缘，不含目标内部。在这里测清晰度，因为平整的标记内部没有纹理。

**检查顺序**（第一个不合格即拒判，测量值全部记录）：

| 顺序 | 原因 | 条件 | 可补拍（§3.2） |
|---|---|---|---|
| 1 | `quality.target_unknown` | 缺位姿、资产几何，相机编号或画面尺寸与画像不符，或相机不在资产上方（Z < 0.3 m） | 否 |
| 2 | `quality.resolution` / `quality.exposure` / `quality.blurry` | 与 v1 相同的整图检查 | 曝光、模糊可以；分辨率不可以 |
| 3 | `quality.target_out_of_frame` | 最坏航向下可见比例 < `min_visible_fraction` | 是 |
| 4 | `quality.target_too_small` | 投影边长 < `min_side_px` | 否：同位重采尺度不变 |
| 5 | `quality.target_off_center` | 确定目标像素 < `min_core_px`（航向未知时，相机不在目标上方就无法确定目标像素） | 是 |
| 6 | `quality.target_exposure` | 确定目标像素中暗像素（max(R,G,B) < `dark_level`）或亮像素（min(R,G,B) > `bright_level`）比例超限 | 是 |
| 7 | `quality.target_blurry` | 边界环上亮度梯度幅值的 99.5 分位 < `min_sharpness` | 是 |

- 限值只在 S0 逻辑帧与 S1 Gazebo 渲染上标定并写进画像，改任何限值即新版本。
- `quality-v1` 不变，已固定它的运行继续有效。
- 画像的来源：模型分析器本来就固定自己的质量画像；确定性分析器也可以固定一个（`quality` 字段，未设置时用业务目录的默认画像，规范形式省略该字段）。常驻任务台的各站点相机不同（PX4 SITL、逻辑机队、厂商模拟器），所以任务台的补拍分析器自带 S1 画像，其余分析器保持原样。
- 曝光的暗 / 亮规则是仿真世界中的启发式，不是遮挡检测器。真实世界的遮挡要靠模型分析器的 `unusable_reason`（→ `analysis.undeterminable`，可补拍）。

### 3.2 补拍策略（`recapture-v1`，业务目录的可选字段）

```yaml
recapture: {version: recapture-v1, reasons: [quality.blurry, quality.exposure, quality.target_out_of_frame, quality.target_off_center, quality.target_exposure, quality.target_blurry, analysis.undeterminable]}
```

- 目录加载时校验：原因只能取自上表标「是」的项与 `analysis.undeterminable`。
- 下列原因写进目录即加载失败：
  - `target.mismatch`：拍错对象，同位重采无益；
  - `model.*`：模型问题不是采集问题，含用户明确排除的 `model.timeout`；
  - `analysis.no_reference`、`quality.media_*`、`quality.capture_time_unknown`、`quality.resolution`、`quality.target_too_small`、`quality.target_unknown`：重拍不会改变。
- 没有 `recapture` 字段的业务目录：任何拒判都不补拍；规范形式省略该字段，已有目录的摘要不变。
- 判定只在分析节点失败的那一刻用当时加载的目录做一次，结果连同策略版本写进节点的 `detail`，之后重启或重放都读同一记录。

### 3.3 工作流内核的最小扩展

1. **谓词输出 `recapture`**：只对 P4 分析节点（`findings: true`、`purpose: inspection`）有效。
   - 节点失败且 `detail.recapture` 为真时取值 `true`；失败但不可补拍时为 `false`。
   - 节点完成或被跳过时没有这项输出，谓词为假。谓词读取失败节点记录的判定，但从不把失败当成完成。
   - 这项输出只能出现在指向该节点的补拍提交节点的条件里。
2. **补拍提交节点**：`submit_mission` 新增参数 `recapture_of: <分析节点>`，未使用时不进入规范形式，已有模板摘要不变。校验要求：
   - `requires: done`，`after` 含该分析节点，条件恰为 `<分析节点>.recapture == true`；
   - 机器人、体积与资产与原巡检节点完全相同，且必须是固定机器人（同位重采：编译出同一条登记观测航线）；
   - 原巡检不是补拍（不能链式补拍），每个分析节点至多一个补拍节点；
   - 补拍分析的分析器、`findings` 与 `purpose` 与原分析相同。
3. **选择节点** `select_analysis`（确定性，无副作用）：参数 `analyses: [analyze, analyze_r]`，须 `requires: done` 且 `after` 含全部分析节点。各分析须为同一分析器的巡检用途 P4 分析。
   - 取第一个完成的分析，输出其 `AnalysisOutput` 加 `selected`；
   - 没有完成的：任一输入结果未知时以 `upstream_unknown` 跳过，任一失败时以 `analysis.no_usable_capture` 失败（`detail` 列出各次拒判），全部合法跳过时以 `upstream_skipped` 跳过；
   - 复核与工单可以引用选择节点，读取的发现、工单与复核语义不变。
4. **运行结论**：失败的分析节点若 `detail.recapture` 为真，且其补拍提交节点已经开始（未被跳过），视为「已由补拍接替」，不计入失败。补拍分支本身的成败照常计入。
5. **补拍任务的请求**：投递时附带原分析作业与原因码，请求文本写成 `Workflow <模板>: recapture <资产> in <体积> after <原因> (analysis <作业>)`，审批页因此能看到补拍原因。

```text
inspect → await → analyze ──(requires: done, when analyze.recapture == true)──→ inspect_r → await_r → analyze_r
select_analysis(analyze, analyze_r) → human_review(when select.suspected) → create_work_order(when confirmed) → report
```

不加循环、不加表达式；DAG 上限、审批、派遣与取消规则都不变。

### 3.4 证据、来源与结算

- 补拍证据是新的采集，按 D037 / D063 复核后才进入作业；补拍任务的请求记录原分析作业与原因。
- 两次都拒判：选择节点失败，运行以 `failed` 结束，两次拒判原因都保留。资产状态仍未知，不产生发现，也不给任何缺省结论。
- 补拍成功的分析与普通分析一样只是候选。
- v1 只用于巡检模板。复检模板不变：复检分析被拒时，本轮按 `reinspection-v1` 记为 unknown，工单等待新的维修反馈。

### 3.5 持久化与入口

- 不新增表、不新增列：策略在业务目录，判定写在既有的 `wf_nodes.detail`，选择结果写在既有的 `wf_nodes.result`。
- 入口不增加补拍按钮：补拍只由模板与策略触发，人只做逐次审批。页面增加选择节点与新原因码的中文名，补拍任务在请求文本中带原因。

## 4. 验证计划

**S0**（`eval/p6_world.py`，P6-F01–F10 × 种子 7 / 19 / 41）：在 P4 世界上加入逻辑相机的反光 / 遮挡、整图模糊、目标局部模糊与拍摄位置偏差，以及一个不带补拍的对照模板。

| 场景 | 内容 |
|---|---|
| F01 | 首拍目标反光 → 补拍 → 复核确认损伤 → 一张工单 |
| F02 | 整图模糊 → 补拍正常，不复核 |
| F03 | 位置偏差 1.2 m → 补拍正常 |
| F04 | 两次都反光 → 选择节点失败、运行失败，无发现 |
| F05 | 拒判原因不可补拍（`model.timeout`、`model.error`、`target.mismatch`）→ 不补拍 |
| F06 | 补拍审批被拒 → 运行失败，没有第二次飞行 |
| F07 | 补拍任务等待审批时取消运行 → 无派遣、无分析 |
| F08 | 补拍节点投递期间服务重启 → 恰好一个补拍任务 |
| F09 | 对照：同样的反光，`quality-v1` 模板把遮挡读成疑似、拒判需要人处理，P6 模板补拍得出正常 |
| F10 | 越权：viewer、他项目 operator 与匿名调用不能启动、审批或复核补拍 |

**独立裁判**（`eval/judge_p6.py`，含 P4 全部不变量）另外核对：
- 补拍只发生在策略允许的拒判之后，每次巡检至多一次，机器人、资产与航线与原巡检相同；
- 每次补拍飞行都有人工审批；
- 拒判的采集从不产生发现或结论；
- 选择节点取的确实是第一个完成的分析；
- 按画像与位姿复算每个 `quality.target_*` 拒判。

**S1**（PX4 SITL，`scripts/remote_p6.py`）：Gazebo 世界服务在标记中心放置发光圆片（强反光）。
- 反光在补拍前移除：补拍得出正常；
- 反光不移除：两次拒判，运行失败；
- 只有损伤贴片：`quality-v2` 不拦截，得出疑似并开单。

限值先用一次不计入的标定运行取渲染测量，冻结后再跑计入的用例。

**指标**：
- 对照模板与 P6 模板的拒判结束率、需要人工处理的运行数，以及错误疑似数；
- 补拍成功率、额外飞行次数与时长，分开报告。

**回归**：
- P1–P5 S0 矩阵与授权矩阵；
- P2 / P4 S1；
- 常驻任务台激活到 P6 目录，并做一次补拍会话；
- 门禁 `scripts/verify_p6_recapture.py`。

## 5. 工作包

| 工作包 | 内容 |
|---|---|
| WP-P6-02a | `quality-v2` 画像格式、几何与检查，S0 / S1 画像 |
| WP-P6-02b | 内核：`recapture` 输出、补拍提交节点、`select_analysis`、运行结论 |
| WP-P6-02c | 业务目录 `recapture-v1`、补拍请求文本、页面中文名、P6 目录（S0 / S1 / 任务台） |
| WP-P6-02d | S0 世界与矩阵、独立裁判、S1 编排与标定 |
| WP-P6-02e | 任务台激活、会话探针与门禁 |

## 6. 与评审稿的差异

1. **相机模型随质量画像固定**：评审稿写的是「登记在站点地图」。站点地图与能力描述的摘要已被任务包与 wire 固定，加字段会改变已固定的摘要，所以相机模型改放在画像里。
2. **航向未知时用保守几何**：证据位姿没有姿态，补上要改机载适配器与 wire。为此新增一个可补拍原因 `quality.target_off_center`：相机不在目标上方时，目标像素无法确定。
3. **`quality.target_too_small` 不可补拍**：同位重采的尺度不变，补拍无益；更近的登记拍摄位留到 v2。
4. **两次都拒判时运行以 `failed` 结束**：评审稿写的是「未知收尾」。现有内核把拒判记为失败而不是未知，这里与 P4 保持一致；两种写法在业务上都表示资产状态未知、没有结论。
5. **v1 只用于巡检模板**：复检模板的补拍需要改结算规则读取选择节点，留到 v2。
6. **确定性分析器可自带质量画像**（实施中补充）：任务台一个部署内各站点相机不同，业务目录的单一默认画像不能同时适用。
