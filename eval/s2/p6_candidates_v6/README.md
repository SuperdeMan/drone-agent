# P6 离线道路边界复核包

本包承接 [v5 AI 草稿](../p6_candidates_v5/README.md)，将 6 项道路与 13 项实体整理为可以圈绘、登记、导出和离线校验的工作流。它是开发工具，不是新模型评测；没有新增来源照片、模型调用或独立人工业务标签。

操作步骤、重新构建与导出校验命令见[使用说明](../../../docs/p6-boundary-review-guide.md)。页面源码在 [scripts/p6_boundary_review/](../../../scripts/p6_boundary_review/)，来源校验在 [p6_boundary_review.py](../../../scripts/p6_boundary_review.py)。原图、支持域、生成包、浏览器截图与 QA 夹具始终留在仓库外。

开发记录按新的独立格式导出；道路版本变化会使关联实体需要重新复核。浏览器只完成结构核对，完整来源及栅格几何以离线回执为准。用户填写的记录仍不自动成为正式真值；模拟记录明确标记为 QA 夹具，不计入人工证据。

## 本批入口与来源

- 本机页面：`D:/drone-agent-data/p6-boundary-review-20261015/review-v2/index.html`，可直接用浏览器打开。
- [bundle.json](bundle.json)：6 道路 / 13 实体、全部 18 个原始候选框、5 张不同原图与 6 张支持域的绑定。图片均为既有素材。
- [package-receipt.json](package-receipt.json)：本机包位置、源摘要、QA 导出 / 截图 / 离线回执摘要。包内没有预填的人工记录，初始进度为 0 / 19。
- [source-refresh.json](source-refresh.json)：2026-10-15 再查 RescueNet 的版本与许可声明，差异仍未消除；ROAD 下载 / 许可仍未开放。没有新增原图或联系作者。
- 包 SHA-256：`88f71dffed65a68b168c7dc203cbea0f2d282008da6e4bc558f93573f5544c57`。
- 工具冻结提交：`d754dc105d45a5758abd79c3dbcba92a9e162f18`，在初版 `cd4e482` 上修正放大布局。包身份同时绑定页面源码、构建器及既有几何工具，旧 v3–v5 记录不改写。

## 开发工具检查

冻结代码 `d754dc1`：Ruff 通过；Python 定向 56 项（新增复核测试 30 + 既有几何测试 26）、前端逻辑 10 项通过；Windows 全量 1664 通过、1 项既有 Unix socket 跳过、0 失败 / 错误。全量耗时 837.829 秒，源码在检查期间未变；后续提交只归档文档与证据。

[最终浏览器回执](browser-qa/final.json)为 50 / 50，通过 Edge 的 HTTP 临时回环服务和本地文件打开两条路径；覆盖全部 19 项原图载入、桌面 / 窄屏 / 200% 缩放、圈绘、道路版本失效、导入导出及负向导入。所有填写均来自隔离的 `QA_FIXTURE_NOT_HUMAN` 会话，真实人工业务复核为 0。

[实际看图记录](visual-review.json)覆盖最终桌面实体叠图、390 px 页面和 200% 放大页面；检查者为 Codex AI，只检查工具界面，不产生人工业务标注。其余截图只记作自动采集，不扩大视觉检查范围。

初次浏览器检查在第 30 个通过项后因检查脚本漏加 await 表达式括号中断，保留[负记录](browser-qa/attempt-1.json)；修正检查脚本后的[开发包复验](browser-qa/development.json)及[布局修正前检查](browser-qa/before-layout.json)各通过 49 项。随后实际看图发现：200% 放大虽无横向溢出，图片区域仍被侧栏挤得过窄。因此增加画面宽度断言；旧包在[新增断言下失败](browser-qa/layout-regression.json)，新版 50 项通过。历史包摘要保存在 [history/](history/)，不把早期 49 项结果当作最终视觉通过。

最新版路径为 `review-v2/`，此前的 `review/` 留存。若已经用旧包填写记录，继续用旧包及其历史 SHA 校验，不将旧记录直接移植到新包身份，也不覆盖旧缓存。

[空导出](qa-empty-audit.json)和[包含圈绘 / 采用草稿的 QA 导出](qa-drawn-audit.json)均通过离线来源与几何校验。QA 记录仍让 6 项道路、13 项实体全部保留在操作员待办中。完整检查与源版本绑定见[验证回执](../../../docs/verification/p6-boundary-review-2026-10-15.json)。阶段状态仍以[当前交接](../../../docs/p6-materials-handoff-2026-10-15.md)为准，WP-P6-01 与 P6 整体未准出。
