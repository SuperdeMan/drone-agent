# 运营台视觉 v2 实施交接

[决策记录](decisions.md) · [D068 工作区验收](desk-workspaces-readiness.md) · [任务台指南](tailnet-desk.md) · [运营层 §8](architecture/09-operations.md) · [路线图](roadmap.md)

本页是运营台视觉 v2 从 Figma 设计稿落到代码的唯一交接入口。设计已于 2026-10-08 经用户确认，**已按 D074 完成 Figma MCP 对照与浏览器图标，并在确认 P5 长稳结束、最终判定完成及用户批准后，以 `fb77a9f` 推送并部署到常驻任务台**；准确版本、数据边界与回执见[视觉 v2 核验记录](desk-visual-v2-readiness.md)。它只改页面的视觉与组件结构，不改变任何门禁的状态，也不改变执行、准入、审批、机载与裁判的语义。

## 1. 设计来源

- Figma 文件：<https://www.figma.com/design/bcdFxnQTIoDUhBoaxB2JJI>（需 Figma MCP；`fileKey = bcdFxnQTIoDUhBoaxB2JJI`）。
- 本轮按用户明确要求，必须用 Figma MCP 的 `get_design_context` / `get_screenshot` 按下表节点对照。工具不可用时可按第 4–6 节做准备，但不能据此宣称 Figma 对照已完成。
- 设计稿中的字体 Noto Sans SC / JetBrains Mono 只是替身；代码继续用现有系统字体栈（`--font` / `--display` / `--mono`）。
- 设计稿中的数据来自 `desk_preview.py --seed` 的本机预览，另有少量为演示编写的记录（审计里的 `viewer-01`、机场动作回执、候选的预计到场与近期使用数值、证据小图）。页面只展示服务给出的记录，不得把这些示例写进代码。

| 页面 | 节点 | 内容 |
|---|---|---|
| 00 现状对照 | `2:2` | 当前页面截图与逐项问题，对应 v2 的处理 |
| 01 Foundations | `2:3` | 颜色（浅 `25:2` / 深 `25:210`）、字体、间距圆角、层级、色彩语义规则（`25:418`） |
| 02 Components | `2:4` | 22 个组件与 13 个图标，见第 5 节 |
| 03 Screens · Light | `2:5` | 总览 `9:2`、任务待审批 `11:275`、任务已完成 `13:572`、工作流运行 `14:838`、调度 `16:1119`、机队与机场 `18:1227`、发现详情 `19:1400`、运行审计 `21:1550`、M1 固定巡检 `22:1660` |
| 04 Dark & Mobile | `2:6` | 深色：总览 `23:1719`、任务待审批 `23:1776`、任务已完成 `23:2020`、机队 `23:2160`；手机 390：总览 `24:1014`、任务详情 `24:1178` |
| 05 实现映射 | `2:7` | 变量新旧对照、组件与类名、约束、落地顺序、已定的三项决定 |

## 2. 已确认的决定

1. **方向 A「运营精度」**：信息架构沿用 D068 的工作区与 hash 路由；重做视觉层级、配色语义与核心组件。
2. **主操作改为墨色**（`--primary`）。绿色只表示结果（已完成、通过、可派遣），不再用于按钮、选中态、焦点环与品牌。
3. **详情页顶部为决策区**，只在有待办时出现（审批、复核、维修反馈）。审批面板内嵌完整哈希，分 8 组显示。取消任务退到标题旁，作为次要操作。
4. **新增组件**：三元判定条、来源标签（方角加图标，与状态徽标区分）、顶栏运行环境标识、分组哈希块；UNKNOWN 用中性色加虚线加空心点或问号。
5. **机场维度**：正常的保持中性，只有异常的着色。
6. **时间线**：先显示中文事件名，原始代码作为次要信息。中文名由页面维护显示用词表，与 `STATUS` / `REASON` 同类；词表缺项时直接显示原始代码，不猜测含义。
7. **混合执行后端**：顶栏显示一个「仿真 · 混合后端」标签，悬停列出各后端下的机器人（只读，不是选择器）。只有一种后端时显示该后端；出现真实设备时一律显示红色实心的「真机 · 实飞」。
8. **深色品牌标反相**：`--brand` 深色取 `#E6EBEF`，旋翼描边用 `--primary-ink`，青柠核心不变。
9. **不引入前端框架与构建**，理由同 D068：CSP、`desk_probe` 按字节核对单个 `/mission.js`、云端检查镜像没有 Node。

## 3. 不变的边界

- CSP（`application.py` 的 `CSP` 与任务台同款）不改：只用系统字体，样式内联，单个同源脚本，图片只有 `data:`。
- hri.v0 不新增帧类型；页面不做任何判定，只展示服务记录、发送由人选择的具名帧。
- 审批仍按任务包哈希逐个进行，没有批量操作；按钮仍由服务按角色校验。
- 状态始终同时有文字与色调；UNKNOWN 不借用任何结果色；页面不汇总或推断判定。
- 页面脚本不得出现控制或注入帧名（`test_the_page_script_names_no_control_or_injection_frames`）。
- 两页的设计变量保持一致（`test_both_pages_share_one_set_of_design_tokens`，区段 `/* tokens:start */ … /* tokens:end */`）。
- M1 固定页只换样式，`live.js` 的行为不改。
- 只要需要新增服务字段，哪怕只读，都属于协议面变化：先停下来问用户，获批后写进本次决策条目（先例：D068 的两处只读字段）。

## 4. 设计变量（替换两页 tokens 区段）

| CSS 变量 | Figma 变量 | 浅色 | 深色 | 说明 |
|---|---|---|---|---|
| `--bg` | bg/canvas | `#F2F4F5` | `#0B0F12` | 原米黄改冷中性 |
| `--surface` | bg/surface | `#FFFFFF` | `#12171B` | |
| `--surface-2` | bg/surface-2 | `#F7F8F9` | `#161C21` | |
| `--surface-3` | bg/surface-3 | `#ECEFF1` | `#1D252B` | |
| `--line` | border/default | `#E1E5E8` | `#232C33` | |
| `--line-strong` | border/strong | `#C9CFD4` | `#33404A` | |
| `--ink` | text/primary | `#12181D` | `#E6EBEF` | |
| `--ink-2` | text/secondary | `#3C4750` | `#B9C3CB` | |
| `--muted` | text/muted | `#66727C` | `#8A96A0` | |
| `--faint` | text/faint | `#8E99A2` | `#66727C` | 浅色 2.9:1，只用于分隔点与禁用态 |
| `--primary`（新） | action/primary | `#15202A` | `#E6EBEF` | 替代按钮上的 `--brand` |
| `--primary-hover`（新） | action/primary-hover | `#26333E` | `#FFFFFF` | 替代 `filter:brightness` |
| `--primary-ink`（替代 `--brand-ink`） | action/on-primary | `#FFFFFF` | `#0B0F12` | |
| `--brand`（改义） | brand/mark | `#15202A` | `#E6EBEF` | 只用于品牌标底色 |
| `--accent` | brand/accent | `#C6E58A` | `#C6E58A` | 只用于品牌标与深色地图区 |
| `--accent-ink`（新） | brand/on-accent | `#1A2A10` | `#1A2A10` | |
| `--focus`（新） | focus/ring | `#12181D` | `#E6EBEF` | 焦点环 |
| `--ok` / `--ok-bg` / `--ok-line` | status/ok/* | `#1F7A45` / `#E6F4EC` / `#B5DCC4` | `#6FCF97` / `#0F2419` / `#1F4A33` | |
| `--warn` / `-bg` / `-line` | status/warn/* | `#9A5B0B` / `#FCF1DF` / `#EBCB94` | `#F0B45B` / `#2A1E0C` / `#533B17` | |
| `--bad` / `-bg` / `-line` | status/bad/* | `#B3261E` / `#FCE9E7` / `#F0B9B3` | `#F28B82` / `#2D1412` / `#5A2722` | |
| `--info` / `-bg` / `-line` | status/info/* | `#1F5F9E` / `#E7F0FA` / `#B7CFEA` | `#7DB3F0` / `#0F1F33` / `#21406A` | |
| `--neutral` / `-bg` / `-line` | status/neutral/* | `#55616B` / `#EEF1F3` / `#D3D9DE` | `#A3AEB7` / `#192026` / `#2E3840` | |
| `--sim` / `-bg` / `-line`（新） | source/sim/* | `#5B43B0` / `#F1EDFB` / `#D3C8F2` | `#B39DF2` / `#1E1733` / `#3D2F66` | 只表示仿真执行后端 |
| `--field` / `-2` / `-ink` / `-muted` / `-line` | field/* | `#0E1A16` / `#14241E` / `#E3EBE6` / `#8FA39A` / `#23362E` | `#0A1411` / `#10201A` / `#E3EBE6` / `#8FA39A` / `#203229` | 地图区两种主题都深色 |
| `--scrim`（新） | overlay/scrim | `#0B0F12` | `#000000` | 对话框遮罩（按需加透明度） |
| `--radius-sm` / `-md` / `-lg` | radius/* | 6 / 8 / 12 | 同左 | 标签 / 控件 / 卡片 |
| 间距 | space/* | 2 4 6 8 12 16 20 24 32 40 | 同左 | 新写的样式取这组刻度 |

- **移除**：`--brand-text`（链接改用 `--ink` 加下划线）、`--brand-soft`（选中态改用 `--surface-2` 加 1.5 px `--primary` 描边）。
- **对比度（WCAG）**：除 `--faint` 外，所有文字与底色组合在两种主题下都 ≥ 4.5:1。
- 阴影改为三级：卡片、决策区（Raised）、对话框，取值见 Figma 的 Elevation 样式。

## 5. 组件映射

| Figma 组件（节点） | 页面类名 | 渲染位置 | 要点 |
|---|---|---|---|
| Button `3:36` | `.btn` `.primary` `.danger` `.ghost` `.small` | 全局 | primary 用 `--primary`；禁用态透明度 0.42 |
| Badge `3:57` | `.badge.ok/warn/bad/info` + `.unknown`（新） | `badge()` | unknown：虚线描边、空心点 |
| SourceChip `3:84` | `.src`（新） | 用到 `SOURCE[…]` 的各处 | 仿真紫（菱形）、真实设备实心墨、模型描边、脚本虚线、未记录琥珀虚线 |
| EnvTag `3:99` + EnvPopover `33:173` | `.env`（新） | `renderShell()` | 由当前项目机器人的 `execution_backend` 推出；混合时用 Mixed 变体加悬停列表 |
| HashBlock `3:117` | `.hash`（新） | `approvalCard()` | 分 8 组显示；复制与比较仍用完整字符串；窄屏折行 |
| NavItem `5:115` / Rail `7:189` | `.rail a[aria-current=page]` | 侧栏 | 当前项：`--surface` 底加 3 px `--primary` 指示条；计数只表示等人处理的条目 |
| ListItem `5:138` | `.item[aria-current=true]` | 各列表 | 标题用请求原文；选中态用墨色描边 |
| AttentionRow `5:191` | `.attn a` | `attention()` | 标题与元信息单行截断 |
| KPI `5:218` | `.kpi` | `kpi()` | 只有等人处理的数字着琥珀 |
| FactRow `5:233` | `.facts`（由 `.kv` 演进） | 详情右栏 | 右侧事实栏宽 296 px，桌面端吸顶 |
| TimelineRow `5:257` | `.timeline li` + `.code` | `eventsList()` 等 | 中文事件名（用词表）加原始代码 |
| Step `6:86` / Stepper `6:87` | `.stepper li` | `stepper()` | `STAGE` 映射不变，只换图形：完成为实心勾、当前为蓝环、等人为琥珀环、终止为实心叉 |
| FlowNode `6:197` | `.flow li` | `renderRun()` | 与步骤条同一套节点语言 |
| DimTile `6:230` | `.dim` | `dims()` | 正常维度中性加小绿点，只有异常维度着底色 |
| VerdictTriad `6:329` | `.triad`（新） | `reportCard()` | 见第 7 节第 1 项的数据前提 |
| DecisionPanel `7:135` | `.decision`（新） | 审批、复核、维修反馈 | 左侧色条加标题、说明、操作；取消改为标题旁的次要按钮 |
| Dialog `7:138` | `dialog.ask` | `ask()` | 行为不变：取消即不发送 |
| TopBar `7:159` / BrandMark `7:151` | `.top` / `.brand` | 两页 | 模式切换、项目、运行环境、连接、身份 |

## 6. 工作区落位

- **总览**：KPI 一行；左侧主列为待处理，「资源提示」另列且不计入待处理数；右侧为最近任务和机队摘要。
- **任务详情**：标题区（eyebrow、请求原文引用、徽标、来源标签、取消）→ 步骤条 → 决策区 → 双栏（主栏：任务包、报告与证据、机载事件；右栏：规划与准入、派遣、运行来源）。尚无内容的部分收进一张「飞行之后」卡；任务包参数显示为键值标签，不再是 JSON。
- **工作流**：复核上移到决策区；节点列表与时间线遵循第 5 节。
- **调度**：候选表（排序、机器人、站点 / 机场、预计到场、近期使用、判定、原因），数据来自已有的 `candidates[].eta_s` / `usage` / `reasons`；空域网格放大，加机位、原点、比例与图例。
- **机队与机场**：8 个维度两行；飞行器判定与原因块；机场动作按 `requested → acked → completed` 显示。
- **发现与工单**：决策区；主图加缩略；分析逐条显示（分析器、来源、结论、分数、原因）；右栏列出关单条件的服务状态。
- **运行审计**：筛选标签（被拒的尝试单独着色），表格中被拒的行用浅红底。
- **M1 固定页**：迁移到新变量与按钮；顶栏与运营台一致；`live.js` 不改。
- **响应式**：1240 px 以下侧栏收为图标；760 px 以下侧栏改为横向滚动标签条，详情改为单栏；哈希折行。

## 7. 动手前必须核对的数据前提

1. **三元判定的「安全」格**：页面目前拿到的报告只有 `execution_status` / `effect_verdict`，以及汇总的三列；`safety_verdict` 只在服务的机载事件里（`fleet/events.py`）出现，任务视图没有任务级的安全判定。页面不得自行汇总出一个安全结论。可选做法：
   - 安全格显示「服务未提供」，按 UNKNOWN 样式呈现；
   - 或给视图加一个只读字段——这需要用户先批准（见第 3 节）。
   默认先按第一种做，并向用户提出第二种。
2. 任务已完成页的「guardian 干预次数」「已证实步骤 x / y」：只有服务视图里有对应字段时才显示，否则省略。
3. **运行环境标识**：确认当前项目每台机器人的 `execution_backend` 在页面数据中可得（资源视图已有）；拿不到时显示「来源未记录」样式，不猜测。
4. **审计筛选计数**：只统计已加载页的记录，并注明这一点；不另请求全量。

## 8. 实施步骤

1. 先在 `decisions.md` 增加决策条目「运营台视觉系统 v2」，编号取当时的下一个空号（本页写作时最新为 D073），引用本页与 Figma 文件；如果第 7 节第 1 项获批加字段，也写进这一条。
2. 替换两页的 tokens 区段（同步修改 `mission.html` 与 `live.html`），单独提交一次，便于对照。
3. 新增 `.src` `.env` `.hash` `.triad` `.decision` `.unknown`，调整第 5 节其余类；改写对应的渲染函数。
4. 在 `mission.js` 加事件中文名用词表（只用于显示）。
5. 本机核验：
   - `uv run ruff check .`
   - `uv run pytest -q`
   - `node --test tests/console/test_mission_client.cjs tests/console/test_live_client.cjs`
   - `uv run python scripts/desk_preview.py --seed`，逐个工作区与 Figma 对照，浅色 / 深色，1440 / 390 宽
   - 在 `git archive` 副本中跑全量测试（云端检查镜像没有 `.git`）
6. **部署时间**：常驻任务台在 P5 长稳期间不得激活新版本（[运维手册](operations-guide.md)）。先确认长稳已结束并完成判定；不确定时问用户。部署后：
   - 从部署 SHA 的检出运行 `desk_probe.py`，按字节比对 `/mission.js` 与 `/fixed/live.js`；
   - 运行 `desk_browser.py`，浅色 / 深色 × 1440×900 / 390×844，确认横向溢出与控制台报错为 0；
   - 按需加 `--mission` 在页面中走一次任务。
7. **收尾**：
   - 新增 `desk-visual-v2-readiness.md` 记录准确版本与回执；
   - 在决策条目里补实施补记；
   - 在 `README.md` / `README.zh-CN.md` 的验证表各加一行（中英同步）；
   - 在 `architecture/00-overview.md` 文档地图中登记。

## 9. 验收判据

- 第 3 节的边界全部保持，对应测试原有断言不放宽。
- 两组 Node 客户端测试保留全部语义断言，新增覆盖：哈希分组显示与完整复制、UNKNOWN 徽标、环境标识的单一 / 混合 / 真实设备三种情况、事件用词表缺项时回落到原始代码。
- 每个工作区在浅色 / 深色 × 1440 / 390 下与 Figma 对应画面一致：层级、组件、色彩语义一致；数据以服务为准。
- `desk_browser.py` 全部视图横向溢出为 0、控制台报错为 0。
- 不改变任何门禁状态；P4 的 S2 保持开放，P5 准出仍绑定 `1716843` 的记录，不转记为视觉版本的门禁结果。
