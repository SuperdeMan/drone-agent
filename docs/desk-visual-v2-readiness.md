# 运营台视觉 v2 核验（D074，2026-10-09）

[唯一实施交接](desk-visual-v2-implementation.md) · [决策 D074](decisions.md) · [D068 验收](desk-workspaces-readiness.md) · [运维手册](operations-guide.md)

**已推送并部署，常驻任务台运行 `fb77a9f00efff6fd3e1e8a101abc41c37a8d8436`。** 该版本合并 P5 准出记录，包含经 Figma MCP 对照的视觉 v2 与浏览器图标。部署前已确认 P5 长稳实际结束、最终裁判与仓库记录一致，并获用户明确批准推送与部署；本页末尾记录本次同 SHA 的本机、云端与线上浏览器核验。没有新增服务字段，没有修改 CSP、hri.v0、逐次审批、执行、机载或裁判；`live.js` 保持原字节。P4 的 S2 保持开放；P5 准出仍绑定 `1716843`，不转记为视觉版本的门禁结果。

## 准确版本

| 项目 | 版本 / 范围 |
|---|---|
| 接手基线 | `04bdd38`，工作区干净 |
| 决策先行 | `62c9062`：增加 D074 并记录实施前数据核对 |
| tokens 独立提交 | `05fe285`：同步两页冷中性主题与墨色主操作 |
| 初版本机实现 | `b621c93`：按交接文字规范完成；当时本会话没有 Figma MCP，不能将该批回执称为 Figma 对照 |
| MCP 对照实施 | `959fe43`：取得真实设计上下文、截图与原始资产后，修正布局、组件、阴影和响应式；统一入口的旧应用标识检查发现一项失败 |
| 主体核验版本 | `6132927a25fd01fd63cb5022ca2f43ad1456dac6`：在应用元信息中保留 `FLIGHT DESK`，原检查不改动；主体核验回执均绑定此版本 |
| 浏览器图标补丁 | `aaf36c78792f344a49b125a802ad81be9fefa9d8`：两页补上随主题切换的浏览器图标；单独回执见浏览器图标补记 |
| 已部署版本 | `fb77a9f00efff6fd3e1e8a101abc41c37a8d8436`：合并 P5 准出记录；部署 `20261009T033701Z-6dc6ca93`，两个入口版本一致、健康正常 |

## Figma MCP 对照

设计来源：[bcdFxnQTIoDUhBoaxB2JJI](https://www.figma.com/design/bcdFxnQTIoDUhBoaxB2JJI)。[读取与资产回执](verification/desk-visual-v2-figma-2026-10-09/figma-read.json)记录节点、截图摘要、资产摘要、原始尺寸与实际渲染尺寸；不保存短期资产下载 URL。

- **15 个页面节点均实际调用 `get_design_context` 和 `get_screenshot`**：浅色总览、待审批、已完成、工作流、调度、机队、发现、审计、M1；四个深色页面；两张 390 px 手机稿。节点 ID 与唯一交接 §1 一致。
- **9 个组件 / 规范节点**：SourceChip `3:84`、EnvTag `3:99`、Dialog `7:138`、排版与层级 `25:418`、HashBlock `3:117`、DecisionPanel `7:135`、Badge `3:57`、NavItem `5:115`、Step `6:86`。
- **21 份 SVG 原资产**内嵌为 data URI，保持原始路径与尺寸；两页不依赖临时 Figma URL。单色资产以遮罩绑定主题色。MCP 普通下载的深色品牌 SVG 仍含浅色填充，改用只读 `use_figma` 导出深色实例，品牌标保持 28×28。未修改 Figma 原稿。
- 当前蓝色步骤复用原生圆形组件；通过 Plugin API 核对 `6:46` 为 18×18、内描边 2 px、居中 6 px 圆点及 surface / info 变量绑定。导航图标 18×18；侧栏 232 px、列表栏 320 px；事实栏 296 px。

对照修正包括：总览卡片组织与 KPI 顺序、紧凑导航、顶栏与胶囊环境标识、来源图形、准确的三档阴影、横向事实行、蓝色复核 / 维修反馈区、完整分组哈希、编号任务包、已完成页前置结果区、工作流右栏、机场维度与动作记录、证据主图 / 缩略、审计六列及筛选、手机页头与参数 / 事实折叠。

## 数据与行为边界

| 项目 | 实施取舍 |
|---|---|
| 任务级安全判定 | 服务未提供，显示「服务未提供」与 UNKNOWN 中性虚线；不从机载事件聚合出成功 |
| 执行 / 效果 | 现有报告是逐步骤记录，因此按步骤展示；保留服务三列目标报告，不推导新的任务级值 |
| guardian 干预次数、已证实步骤 x / y | 任务视图没有对应计数，省略；不复制设计示例 |
| 机位坐标 | 用户明确选择本轮不加字段。只列机器人 / 机场名称并注明坐标缺失；持有单元按已有 cell 坐标绘制，原点在视图外时注明，并提供本地全景切换 |
| 候选排序与 ETA | 只用已有 `decisions[].order`、`candidates[].eta_s / usage / reasons`；站点 / 机场明确来自当前资源目录。没有判定记录时不补设计样例数值 |
| 审计 | 计数只统计当前加载页；没有单独的动作执行结果字段，列名为「记录状态」，只显示已记录 / 被拒，不能把记录当作执行成功 |
| 环境与来源 | 从当前项目资源及 hello 机器人清单读取；缺失或项目不一致时标记未记录；任一真实设备优先显示「真机 · 实飞」；混合后端列表只读 |
| 关单条件 | 只列本轮反馈、采集、分析来源、复核与服务结算；缺项标明，不自行重算是否通过 |
| 独立裁判 | 服务报告完成也不能遮住裁判失败；失败徽标保持在顶部结果区，不能折叠到右栏后消失 |
| 字体、手机计数与 M1 | 按交接继续用系统字体。手机待处理计数使用琥珀色，避免稿中青柠色与品牌色使用规则冲突。M1 地图保留 `live.js` 的动态投影与现场数据，没有把设计示意图替换为运行画面 |
| 输入与权限 | 沿用角色校验、逐版本哈希审批、原有原因必填性、取消对话框不发送的行为；不因画面示例而改变操作定义 |

页面中的时间、ID、证据数量、规划来源与结论以服务为准。因此这里核对的是布局、组件、资产和色彩语义，不宣称运行画面与含演示数据的 Figma 截图逐像素相同。

## 主体版本的本机核验

| 检查 | `6132927` 的结果 / 回执 |
|---|---|
| 工作区 Ruff / pytest | Ruff 通过；**1326 passed、1 skipped、0 failed / errors**，623.937 s；[工作区回执](verification/desk-visual-v2-figma-2026-10-09/worktree-checks.json) |
| `git archive` 副本 | 仓库之外的 ASCII 路径、独立虚拟环境、无 `.git`（向上发现也返回 128）；Ruff 通过；**1326 passed、1 skipped、0 failed / errors**，629.463 s；[副本回执](verification/desk-visual-v2-figma-2026-10-09/archive-checks.json) |
| Node 两组客户端测试 | **32/32**；既有语义断言保留，图形断言仅随装饰标记更新；新增完成报告不能遮住独立裁判失败的检查；[客户端回执](verification/desk-visual-v2-figma-2026-10-09/local-checks.json) |
| 标准浏览器巡检 | 浅 / 深 × 1440×900 / 390×844；七工作区共 48 项，44 个非空视图正常渲染，4 项为默认项目没有任务单的空详情；M1 另在两主题渲染；溢出 0、控制台错误 0；[回执](verification/desk-visual-v2-figma-2026-10-09/browser.json) |
| 补充浏览器检查 | 四种组合共 **28 个视图**：待审批、已完成、工作流、发现、工单、`fleet_s0` 调度详情、M1 桌面 / 手机；溢出 0、脚本错误 0；[回执](verification/desk-visual-v2-figma-2026-10-09/supplemental.json) |
| 资产、尺寸与对比度 | 21 份原 SVG 字节一致；浅 / 深两页品牌、导航图标及栏宽核对通过。实际可见文字检查未发现低于 4.5:1 的项（排除禁用态、装饰 SVG 与分隔箭头）；不替代完整无障碍审核 |
| 本机 HTTP | 两页 200、保留 CSP；`/mission.js` 与 `/fixed/live.js` 和候选 Git blob 按字节相同；[回执](verification/desk-visual-v2-figma-2026-10-09/preview-http.json) |

唯一 pytest 跳过项为原有 `test_api_socket_roundtrip`：Unix socket 的 Windows 条件跳过。没有跳过或放宽契约测试，本轮未运行 Linux 云端检查。

补充检查实际复制完整哈希并与服务值比对；展开 / 收起手机参数；检查折叠事实区；通过本机页面确认候选建单，再取消维修对话框，工单保持 open；调度的四种组合均绘制 9/9 个记录单元，聚焦 / 全景切换不丢失单元。这些使用 `desk_preview.py --seed` 的隔离 S0 世界、脚本规划和分析夹具，M1 是空闲替身；不作为发布门禁证据。

初次 `959fe43` 全量检查的一项失败为旧应用标识缺失；由 `6132927` 修复，原测试保持不变，见[负记录](verification/desk-visual-v2-figma-2026-10-09/initial-negative.json)。不转借前一版本的结果。

本机核验使用 `http://127.0.0.1:8771`。Figma 导出与渲染截图留在 `outputs/desk-visual-v2-figma-2026-10-08/`，截图含身份，不进仓库。当前预览启动状态见下方图标补记。

## 浏览器图标补记（2026-10-09）

按用户要求，`aaf36c7` 为运营台和 M1 固定巡检页增加相同的 `rel="icon"`。再次通过 Figma MCP 的 `get_design_context` / `get_screenshot` 核对品牌节点 `7:151`，复用页头已下载的浅 / 深两份原 SVG；图标内部的主题规则只切换可见资产，不修改原始图形。图标采用 data URI，保持现有 CSP，无额外资源接口或依赖；两页脚本均与 `6132927` 相同。

本机 Edge 已核对两页 × 浅 / 深四种组合：图标声明与源码一致、SVG 正常解码、原尺寸 28×28、两份原资产字节保持一致；实际绘制的背景色分别为 `#15202A` / `#E6EBEF`，没有 CSP、页面脚本错误或横向溢出。该检查验证图标资源与主题绘制，不读取浏览器原生标签栏。

`aaf36c7` 的[独立核验回执](verification/desk-favicon-2026-10-09.json)：工作区及无 `.git` 的 archive 副本均为 **1326 passed、1 项原有 Unix socket 平台跳过、0 failed / errors**，两处 Ruff 通过，客户端 **32/32**。工作区 / 副本中的两页字节与该提交相同。首次 archive 检查随会话中断停在 75%，没有完成回执；本次已完整重跑，不把中断进度计作通过。

后续会话中断关闭了本机预览；2026-10-09 继续工作时尝试在新的 S0 用例目录重启，自动审批审核返回 `blocked by policy`，未给出更具体的原因，命令未启动。该本机预览未恢复，也未重试绕过拦截；之后用户明确批准云端部署，当前体验入口改为下方已激活的常驻任务台。

## 部署等待记录（历史）

[主体核验时保存的只读回执](verification/desk-visual-v2-figma-2026-10-09/pre-deploy-soak-status.json)为 **running**；图标补丁未刷新云端状态，不能据此声称长稳目前仍在运行或已经结束：

- 长稳 `soak-20261005T192959Z-8ca0dffb`；候选 `17168434ca825f9ec07917c1426d81f7815c73df`。
- 部署 `20261005T190214Z-5b857985`；计划结束为北京时间 **2026-10-09 03:30:01**。

视觉与图标实现阶段未激活新版本、未在常驻台执行写操作、未 push。计划结束不等于实际结束或已判定；本机核验不能替代部署前的实际结束与最终裁判核对，也不能替代部署后的真实入口验证。下方为完成这些步骤后的发布记录。

## 常驻部署（2026-10-09）

用户明确批准推送与部署后，先合并远端 `32dcb70` 的 P5 准出文档，再将 `fb77a9f` 推送到 `origin/main`。P5 长稳 `soak-20261005T192959Z-8ca0dffb` 的实时状态为 finished、编排 unit 已停止；云端最终裁判与已提交结果的规范化摘要相同，机器回执的八个分项全部 passed，P5 总门禁二十二项全部 passed。见[长稳状态](verification/desk-visual-v2-deploy-2026-10-09/pre-deploy-soak-status.json)、[裁判核对](verification/desk-visual-v2-deploy-2026-10-09/pre-deploy-judge-check.json)及 [P5 门禁](verification/p5-2026-10-06/release.json)。

[发布回执](verification/desk-visual-v2-deploy-2026-10-09/release.json)绑定 `fb77a9f`、部署 `20261009T033701Z-6dc6ca93` 与控制面摘要 `fdd528289e8d5071b41027909cb0c9ea235ddf83bd625b6ff73007a3a67e0f62`。相对 P5 候选，运行代码只改 `mission.html`、`mission.js`、`live.html`。

| 核对 | 同一部署版本的结果 |
|---|---|
| 本机与 archive | Ruff 通过；工作区和无 `.git` 副本各 1326 passed、1 项原有 Unix socket 平台跳过；客户端 32/32；[本机回执](verification/desk-visual-v2-deploy-2026-10-09/local-checks.json) |
| Linux 云端 | 1327 项，0 失败 / 错误 / 跳过；未解锁冒烟 passed；[部署回执](verification/desk-visual-v2-deploy-2026-10-09/deployment.json) |
| 激活 | 先 [M1 控制台](verification/desk-visual-v2-deploy-2026-10-09/console-activation.json)，再 [M2 运营台](verification/desk-visual-v2-deploy-2026-10-09/desk-activation.json)；四份迁移副本演练与目录切换通过，四个机场会话 active；同机其他 30 个容器与其他 Serve 路由均未改变 |
| 线上 HTTP 与身份边界 | 两页和健康接口 200；console / service / fixed 均为 `fb77a9f`；`mission.js` 与 `/fixed/live.js` 和部署 SHA 的 archive 按字节相同；无效 A2A token 返回 401，跨源 WebSocket 返回 403，伪造身份未采用；[HTTP](verification/desk-visual-v2-deploy-2026-10-09/http.json)、[身份探针](verification/desk-visual-v2-deploy-2026-10-09/spoof.json) |
| 线上浏览器 | 浅 / 深 × 1440×900 / 390×844，七工作区共 48 个视图均渲染；另有两项 M1 桌面检查，横向溢出与脚本错误均为 0；[浏览器回执](verification/desk-visual-v2-deploy-2026-10-09/browser.json) |
| 图标与 M1 手机 | 两页 × 浅 / 深共四项 390×844 补查：图标与源码一致、主题颜色正确、无 CSP 错误、脚本错误或横向溢出；[补充回执](verification/desk-visual-v2-deploy-2026-10-09/favicon-and-fixed-mobile.json) |

当前入口仍由 `uv run python scripts/dev_stack.py desk-cloud --status` 返回，仓库不保存真实 tailnet 名称。此次线上核验没有新发起任务或飞行，浏览器展示的历史任务与 M1 结果不计作该版本新跑的飞行验收。截图留在 `outputs/desk-visual-v2-deploy-2026-10-09/`，不进仓库。

## 历史清理状态

上一批用户批准清理的 10 个中间副本（约 628 MiB）被自动安全审核拒绝，工具只返回 `blocked by policy`，命令未启动，仍保留；[清理状态](verification/desk-visual-v2-2026-10-08/cleanup.json)。本轮没有重新尝试删除。
