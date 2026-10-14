# P6 道路开发输入与诊断

2026-10-13 按用户“仅公开数据，继续推进开发评测”的指示建立。见[开发诊断报告](../../../docs/p6-materials-development.md)。

## 内容

- [input-index.json](input-index.json)：25 张图对应 26 条开发条目；18 个中性目标支持域、8 个无目标候选。路径相对仓库外的数据根目录解析。
- [development-proposals.json](development-proposals.json)：源标签与 AI 开发建议，和模型输入分开；人工标签为空。
- [capture-group-proposals.json](capture-group-proposals.json)：19 个保守分组建议，不是独立地点证明。
- [diagnostic/config.json](diagnostic/config.json) / [precall.json](diagnostic/precall.json)：首次实调前固定的实验提示、参数与输入摘要。
- [诊断摘要](diagnostic/summary.json)：MiniMax-M3.1-Flash-Preview 的 10 条实调、10 条结构有效、10 条本地严格回放；6 条源标签分歧，不换算准确率。
- [运行记录](diagnostic/run.json)、[原始结果](diagnostic/results.json)、[录制](diagnostic/recordings/)与[隔离回执](diagnostic/execution.json)：精确来源与实际输出。
- [verification.json](verification.json)：数据、输入隔离、记录回放与工具检查的回执；2026-10-14 已补完浏览器验收。
- [浏览器回执](browser-qa/20261013T161344Z.json)：60 项实际 Edge 检查通过，含 HTTP 与离线文件模式；[图标 404 负记录](browser-qa/20261013T155951Z.json)保留。
- [工具源码快照](tool-source/README.md)：20 份原始源码与摘要，涵盖三轮数据处理、复核页和诊断工具；图片仍在仓库外。

## 本地目录

`D:/drone-agent-data/p6-materials-20261013-round3/`：

- `review.html`、`review-icon.svg`、`review-app.js`、`review-core.js`、`data.js`：已在 Edge 验证的离线开发复核工具；需保留相对目录。
- `targets/`：18 张只含 0 / 255 的中性支持域 PNG。
- `assets/`：复核预览和源标签叠加图。源红／绿标签不进入模型输入。
- `diagnostic/inputs/`：实际发送的 20 张 JPEG（10 条 × 原图／青色轮廓）与固定清单。
- `diagnostic/review/`：模型框与中性目标的对照预览。
- `diagnostic/cloud/`：下载的云端回执与原始记录。

运行时代码为云端镜像 `6249129596b7cfe88c7322f8b425c7b432dba24b`，一次性诊断脚本另以 SHA-256 绑定。没有部署当前工作树或更新任务台，不能转借本次结果为 HEAD 发布证据。

## 复现检查

```powershell
node --test D:/drone-agent-data/p6-materials-20261013-round3/test_review_core.cjs
.venv/Scripts/python.exe D:/drone-agent-data/p6-materials-20261013-round3/summarize_diagnostic.py
```

第二条只在本机重建请求并严格回放，不调用模型。`run_cloud_diagnostic.py` 会产生新的实际模型调用，重跑将属于新的开发批次，不能覆盖本次结论。

2026-10-14 已用已安装 Edge 的隔离测试会话完成真实浏览器操作与截图检查，60/60 通过；首轮图标 404 已修复。测试导出全部标记为 QA 夹具，不是真人标注。数据和模型输出仍缺独立业务真值；本包的 `formal_release_eligible` 始终为 false。
