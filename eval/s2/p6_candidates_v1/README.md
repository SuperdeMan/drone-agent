# P6 公开素材候选包

这是 2026-10-13 的**开发素材适用性审查**，不是冻结的 S2 测试集。项目报告见 [P6 素材审查](../../../docs/p6-materials-review.md)。

## 已取得内容

- UAVVaste 原始标注：772 张 / 3718 个垃圾标注，全部完成元数据检查。
- 已取得并验证 37 张原图，186,809,471 字节；覆盖 17 个文件名分组，但分组不等于独立地点。
- 37 张 AI 初审：4 张铺装地面异物候选、3 张环境上下文、30 张不符合当前目标场景。均不是人工确认的道路业务真值。
- 完整 ZIP 未取得；不宣称整包 MD5 已通过。原图逐条目长度和 CRC32 与官方 ZIP 目录一致，另有本地 SHA-256。
- 未调用外部模型评测 API，未改视觉画像，未创建正式测试拆分。

## 文件

| 文件 | 内容 |
|---|---|
| [sources.json](sources.json) | 来源选择、许可、精确版本、下载范围、字节数与限制 |
| [source-snapshots.json](source-snapshots.json) | 仓库外来源快照的相对路径、长度与 SHA-256 |
| [metadata-summary.json](metadata-summary.json) | 全部 772 张的元数据、标注与原拆分检查 |
| [summary.json](summary.json) | 已取得 37 张的解码、EXIF 和相似性诊断；计数不外推 |
| [image-manifest.json](image-manifest.json) | 37 张原图的 SHA-256、大小、源名称与开发用途标记 |
| [visual-review.json](visual-review.json) | AI 对每张图的适用性初审、疑似同地点线索；`human_verified=false` |
| [sample-selection.json](sample-selection.json) | 每个文件名分组一张的补齐规则及 17 项清单；其余 20 张来自已恢复条目 |
| [sample-download-receipt.json](sample-download-receipt.json) | 17 项补齐清单的条目验证回执 |
| [annotation-member-receipt.json](annotation-member-receipt.json) | ZIP 标注条目与固定仓库文件的对照 |

## 本地查看与复现

数据根目录：`D:/drone-agent-data/p6-materials-20261013/`。

- `README.md`：本地入口及署名。
- `raw/uavvaste-images/`：37 张未经修改的原图。
- `audit/sheets/representative-1.jpg`：按文件名分组的概览，保留原构图与源标注框。
- `audit/sheets/all-01.jpg`、`all-02.jpg`：全部已取得图像的预览。
- `sources/`：官方元数据、作者文档、ZIP 目录原始尾部、固定版本的 COCO 标注与拆分。
- `acquire_samples.py`：续传所选条目，按 ZIP 目录校验，不替用户接受其他数据源条款。
- `audit_metadata.py`、`audit_materials.py`：重算元数据与图像审计。
- `publish_inventory.py`：发布本批清单与已经完成的 AI 初审记录；更换样本须先重新审查，不能套用旧记录。
- `acquire_archive.py`：整包续传工具；只有全部下载并通过官方 MD5 才改名为 `.zip` 并生成完整归档回执。

在项目根目录使用已有虚拟环境，无需安装全局依赖：

```powershell
.venv/Scripts/python.exe D:/drone-agent-data/p6-materials-20261013/audit_metadata.py
.venv/Scripts/python.exe D:/drone-agent-data/p6-materials-20261013/audit_materials.py
.venv/Scripts/python.exe D:/drone-agent-data/p6-materials-20261013/publish_inventory.py
```

这些命令用于本批采集目录；它们不是正式准出门禁。下载与审计过程中未改本项目运行时代码或云端服务。

## 数据署名

UAVVaste，Marek Kraft、Mateusz Piechocki、Bartosz Ptak、Krzysztof Walas，数据 DOI [10.5281/zenodo.8214061](https://doi.org/10.5281/zenodo.8214061)，论文 DOI [10.3390/rs13050965](https://doi.org/10.3390/rs13050965)。数据许可按官方 Zenodo 元数据为 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。

原图未修改；本地预览进行了缩小、拼版与原标注框绘制，署名与源链接保留。作者代码仓库的 Apache 许可不用于替代数据许可。
