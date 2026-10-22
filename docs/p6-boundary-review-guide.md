# P6 道路边界复核操作说明

这个离线工具把 [v5 的待复核项](p6-road-boundaries-development.md)分成 **6 项道路范围 + 13 项实体**。它保存操作员填写的开发记录，核对来源和几何，不自动建立独立真值、计算模型准确率或授予 P6 准出资格。当前材料已经用于 AI 开发，隐藏提示也不会使它变成盲评留出集。

## 打开与操作

本批可直接打开的页面和准确摘要见 [v6 包清单](../eval/s2/p6_candidates_v6/README.md)。双击外部包中的 `index.html`，或在浏览器打开该本地文件；不需要登录、部署服务或联网。图像、草稿缓存和导出均留在本机。

1. 从左侧选择“道路范围”，填写复核人，点击“开始 / 继续复核”。通过原图缩放和拖动查看路缘。先处理各自道路，再处理 E02、E04–E09 七组边缘问题及 E12。
2. 自行圈绘时，选择“内域”或“外域”，点击“画新轮廓”，逐点单击，最后点击“闭合”或按 Enter；Escape 取消当前未完成轮廓。“移除末个轮廓”只移除当前层最后一块，可重新圈绘。每层可有多个不相连轮廓，外域须包含全部内域。源图不会改动。
3. 内域表示能确认属于道路 / 可见实体的区域；外域表示可能范围。道路范围无法确认时选择“无法确定”，并说明原因。源支持域是可能范围的补充，不是已登记资产；其孔洞也不是已知道路外。
4. 需要参考时展开“参考提示”。“采用 AI 几何”只复制草稿到表单，仍需自己核对、填写依据、登记。“定位区域”在未圈绘时使用 AI 外域，同样记录提示暴露。实体页显示已登记道路，若道路参考过 AI，也保守记录关联的 AI 暴露。
5. 在实体页区分可见实体、阴影、表面损坏、性质未知。可见实体需圈绘或核对几何，再选择目标内、范围外、边缘不确定或无法确定；阴影、表面损坏与性质未知只记录性质，关系选“无法确定”。通过“显示来源框”核对多个旧框是否可作为同一组处理，未确认分组时保持未解决。
6. 写明依据后“登记本项”。修改并重新登记道路会产生新版本，相关实体显示“道路已变更”，必须重新复核登记后才能导出。这个动作不自动改变实体判断。
7. 导出 JSON 留存并执行下述离线校验。仅登记项进入导出；未登记草稿仅在此浏览器缓存。导入使用当前包身份；与已有记录或未登记草稿冲突时明确拒绝，保留两份原始文件，在独立浏览器会话中比较，不静默覆盖。

计时只统计已经点击开始、页面前台、窗口聚焦的活动时间，最后一次操作后最多累计 60 秒。切换条目会暂停；刷新后不会自行继续。它是客户端自报告的开发计时，不是经过身份认证的真实人工效率指标，也不补写未实际计时的时长。

## 可重复构建与导出校验

工具源码在 [scripts/p6_boundary_review.py](../scripts/p6_boundary_review.py)，静态页面在 [scripts/p6_boundary_review/](../scripts/p6_boundary_review/)。本批代码位于隔离工作树 `D:/drone-agent-data/p6-road-views-20261014/repo`，尚未同步到原 `main` 工作树；以下命令在这份工作树根目录执行，输出必须是新的外部目录：

```powershell
uv run --frozen python scripts/p6_boundary_review.py build --drafts eval/s2/p6_candidates_v5/drafts.json --index eval/s2/p6_candidates_v3/input-index.json --previous-summary eval/s2/p6_candidates_v4/summary.json --data-root D:/drone-agent-data/p6-materials-20261013-round3 --output <新的外部目录>
```

构建先重跑 v5 的来源和 18 个候选绑定检查，再复制 5 张不同原图、6 张目标支持域及页面到新目录，源字节保持不变。`bundle.json` 绑定原图、支持域、v5 草稿、v4 摘要、构建器、几何工具与页面源码；命令打印可信包 SHA-256。图像和生成页面包不进入 Git。

```powershell
uv run --frozen python scripts/p6_boundary_review.py validate --package <复核包目录> --bundle-sha256 <清单中的包SHA256> --reviews <导出的JSON> --output <新的校验回执JSON>
```

校验器要求包摘要、原图 / 支持域字节、UI 源码及加载的数据一致；拒绝错目标、重复记录、未声明字段、失效道路版本、未披露的草稿采用、无效多边形、内域越界和时间不一致。数据来源不是靠文件名推断。几何检查使用源图分辨率和既有一像素接触误差带，没有加入业务最小尺寸门槛。

结构合法但“目标内 / 外”声明与几何冲突，或实体分组未确认时，回执保留冲突并标为 `unresolved`。不要仅凭命令返回成功就认定业务复核完成：检查 `pending_roads`、`pending_entities`、`unresolved_operator_entities` 及逐项 `conflicts`。部分导出可以校验，未填写项持续保留。

`candidate` 仅表示以当前操作员道路和实体范围为条件的开发候选，`outside_target` 只针对这一实体，`non_entity` 只针对这组来源框。任何一个都不等于整张图正常、道路可通行、需要清障或已完成清障。操作员身份始终未认证；QA 夹具不减少操作员待办数，不计入人工业务证据。

## 验证与兼容边界

```powershell
uv run --frozen ruff check .
uv run --frozen pytest -q
node --test tests/test_p6_boundary_review.cjs
uv run --offline scripts/p6_boundary_review_browser.py --package <复核包目录> --output <新的浏览器QA目录>
```

浏览器检查使用已安装的 Edge 和独立会话，不读取使用者的配置；只启动临时回环静态文件服务，另检查 `file://` 打开。它不启动项目真栈。所有模拟操作均署名 `QA_FIXTURE_NOT_HUMAN`，截图与夹具导出放在仓库外。最新准确验证回执见 [v6 包清单](../eval/s2/p6_candidates_v6/README.md)。

本工具的导出格式是 `drone.p6-boundary-operator-reviews/v1`，只用于当前道路 / 实体复核；不混入旧 v3 整图复核格式。v1–v5 原始证据不改写，在用视觉画像、业务服务、控制链、数据库及 CI 均无改动。下一步需要实际业务复核填写记录；后续新口径评测还需单独冻结提示、数据与计分规则。
