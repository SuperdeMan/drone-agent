# 2026-10-16 道路 S2 准备与当前门禁

**实际结论：`missing`，WP-P6-01 未关闭。** 用户已批准有人复核的道路识别 S2 范围；D089 全部数量、独立性、跨次、计时和数值目标保留，现场清障效果另列未验证。当前没有收到真人标注导出，正式清单、校准 / 测试及真人候选复核仍缺。

| 目录 / 文件 | 证据边界 |
|---|---|
| `source-acquisition/` | 415 张完整 FloodNet 原图的元数据及新增 297 张的取得 / 续传结果；另 193 条仅头部记录。含源码快照和分组摘要修正记录，不含原图 |
| `annotation/` | 两批 118 / 297 张真实素材包的清单与摘要；39 项浏览器检查、200% 放大负记录和 QA 导出校验。QA 不计真人标签 |
| `verification/software-final.json` | `420a5c1` 同候选：Ruff、99 项相关检查、1854 项全量通过 / 1 项既有跳过、10 项前端逻辑检查；前端资源与 39 项浏览器检查时字节一致 |
| `verification/full-negative.json` | `a44d27a` 全量失败原记录及外部日志摘要，执行期跨多次系统待机；未修改为通过 |
| `closure-evidence/` | 当前门禁的实际声明、协议、曝光登记及软件证明。未完成的业务文件没有填入假数据 |
| `readiness.json` | 实际执行最终门禁的结果：`wp_p6_01_closed=false`，已明确缺少的真实业务证据 |

软件修正将模型任务与被评分目标的图片 / 描述 / 轮廓直接绑定，阻止 EXIF 旋转造成显示坐标不一致。415 张当前原图已核对方向为 1。模型制备不包含标签、人工实体轮廓或判断依据；校准和测试必须通过实际准入，测试前重新核对校准录制，并防止跨目录重用测试影像 / 关联组。

跨次稳定性既可使用有可信时刻的有序采集，也可使用有源影像身份、完整原图凭据和真人确认的无顺序不同实拍；后者不补造 UTC、不证明隔日稳定性或清障效果。原图数量、GPS 连接组和感知相似对都不自动成为独立资产数。

实际数据与页面在 `D:/drone-agent-data/p6-business-closeout-20261015/`，目录沿用启动日期；最终核对于 2026-10-16。原图、压缩字节、截图与原始 JUnit / 日志均在仓库外。已交付页面是 `labeling-human-v1/index.html`（118 张）和 `labeling-human-v3/index.html`（297 张）；内部 v2 保留为被替代包，不覆盖已有操作员缓存。

在仓库根目录复算当前缺口时，输出使用新文件名：

```powershell
uv run --frozen python scripts/verify_p6_road_business.py --evidence D:/drone-agent-data/p6-business-closeout-20261015/closure-evidence/evidence.json --data-root D:/drone-agent-data --output D:/drone-agent-data/p6-business-closeout-20261015/readiness-next.json
```

退出码 2 表示仍缺证据或未通过，不是可忽略的成功。当前没有模型质量数字、真实候选复核耗时或 P6 整体准出结论。后续操作见[交接](../../../../docs/p6-materials-handoff-2026-10-16.md)及[标注说明](../../../../docs/p6-road-labeling-guide.md)。
