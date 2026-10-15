# P6 固定提案定位对照包

承接 v9 的六个目标、33 个固定提案，提供带 ID 的全图 / 局部对照、原始分辨率 / 实际模型输入 / 道路提示切换、缩放和平移、12 项优先筛选。它是只读开发工具，没有新模型调用、源照片或人工标注，原 v9 分组不改写。

- 本机入口：`D:/drone-agent-data/p6-proposal-viewer-20261015/viewer-v4/index.html`，可直接双击或在浏览器打开；默认定位 `12892-p01`。
- [操作与重建说明](../../../docs/p6-proposal-viewer-guide.md)、[构建器](../../../scripts/p6_proposal_viewer.py)、[页面源码](../../../scripts/p6_proposal_viewer/)。
- [bundle.json](bundle.json)绑定 5 张不同原图、实际模型图和道路提示，共 16 份不同图像字节。图像及截图保存在仓库外，没有重编码素材；局部用 SVG 视窗显示。
- [包回执](package-receipt.json)与[实际看图记录](visual-review.json)保存准确摘要。当前包 SHA-256 为 `431c75f523194b721cd1572bd6d4e53c24c73e140c533ed05b4f51a013e93cac`。

## 验证与历史

Ruff、12 项新增 Python 检查、7 项前端逻辑检查通过。[最终浏览器回执](browser-qa/final.json) **110 / 110**，错误 0；使用隔离 Edge 会话，检查全部 33 项坐标、双视图选择、三种图片、缩放 / 拖动、筛选空态、四种屏宽、200% 放大、实际视口内选中项、文件打开与深链接。检查不产生人工记录。

历史记录全部保留：[检查脚本被 CSP 拒绝](browser-qa/attempt-a.json)、[早期 100 项通过](browser-qa/before-layout.json)、[新增选中项可见性检查失败](browser-qa/layout-regression.json)、[隐藏 SVG 零宽度错误](browser-qa/hidden-svg-failure.json)、[中间 106 项检查](browser-qa/intermediate-106.json)、[加入实际视口交集后的 200% 检查失败](browser-qa/viewport-intersection-failure.json)。最终版修正清单可用高度、缩放坐标及隐藏图像处理，并用实际视口截图核验。早期“通过”不作为最终布局验收。

源码冻结后的全量回归另行记录；此处初次归档时仍待完成。P6 未准出，33 项均未获独立人工确认。v6 道路 / 实体登记工具保持原状，本页不提供登记或自动采用结论。
