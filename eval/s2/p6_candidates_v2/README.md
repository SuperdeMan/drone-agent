# P6 第二批道路素材候选包

本包是 RescueNet **官方 Figshare 训练发布 v1** 的开发素材审查，报告见[第二批审查](../../../docs/p6-materials-round2.md)。不作为冻结测试集或 P6 准出证据。

## 已取得与检查

- 80 张原始像素标注：44 张仅含畅通道路、11 张仅含阻塞道路、1 张混合、24 张无道路标签。
- 其中 25 张配对原图：8 张仅畅通、8 张仅阻塞、1 张混合、8 张无道路标签。
- 25 张原图共 125,898,431 字节；全部为 4000 × 3000，与掩码同尺寸。逐条目长度、CRC32 和本地 SHA-256 均已核验，解码与像素计数复算通过。
- 25 张均有 EXIF 时间与 GPS，发现 7 对相机距离 ≤ 100 m 的候选关系，分组仍需复核。
- 未下载原作者的验证、测试图片，未运行外部模型评测；没有正式业务真值或清障前后配对。

## 证据文件

| 文件 | 内容 |
|---|---|
| [sources.json](sources.json) | 精确发布、来源许可差异、本地来源快照与 SHA-256 |
| [source-receipt.json](source-receipt.json) | 官方归档信息、ZIP 目录源字节摘要；整包 MD5 未核验 |
| [selection-plan.json](selection-plan.json) | 查看掩码前保存的筛选规则，不是正式评测协议 |
| [mask-selection.json](mask-selection.json) / [mask-screen.json](mask-screen.json) | 80 张源掩码清单、逐文件摘要与类别像素计数 |
| [image-selection.json](image-selection.json) / [pair-manifest.json](pair-manifest.json) | 25 对选取规则与原始文件摘要 |
| [summary.json](summary.json) | 配对完整性、源标签统计与 EXIF 检查 |
| [verification.json](verification.json) | 本批完整性与文档检查回执；不是准出门禁 |
| [capture-metadata.json](capture-metadata.json) / [proximity-candidates.json](proximity-candidates.json) | 相机 GPS、原始 EXIF 时刻与邻近候选；不冒充目标资产位置或独立地点 |
| [review-queue.json](review-queue.json) | 25 项业务复核队列；人员、结论、时间均待填写 |

## 语义边界

像素值 7 是 Road-Clear、8 是 Road-Blocked，映射来自官方 descriptor 文件，不能根据 README 列表顺序推算。7 / 8 标注的是道路区域，**不是障碍物实例轮廓**。源分组不能直接转成整图的 P6 正常／异常／拒判真值。

本批采用作者关联 Figshare v1 的 CC0 发布源，同时保留 GitHub README 的 CC BY-NC-ND 声明差异；未宣称许可差异已解决或数据已经正式准入。项目素材与来源标签、AI 初审、独立业务复核分开。

## 本地素材与复现

根目录：`D:/drone-agent-data/p6-materials-20261013-round2/`。

- `raw/train-org-img/`：25 张未修改的原图。
- `raw/train-label-img/`：80 张未修改的原始标签图。
- `audit/previews/`：逐图“原图／道路标签叠加”对照。
- `audit/sheets/rescuenet-01.jpg` 至 `rescuenet-04.jpg`：25 对概览。
- `acquire_rescuenet.py`：固定抽样与逐条目续传；有效条目不会重新下载。
- `review_rescuenet.py`：重新核验配对、渲染预览与计算相机邻近候选。
- `publish_round2.py`：仅在 25 对与计划一致时发布候选清单；不填造人工复核结论。

```powershell
.venv/Scripts/python.exe D:/drone-agent-data/p6-materials-20261013-round2/review_rescuenet.py
.venv/Scripts/python.exe D:/drone-agent-data/p6-materials-20261013-round2/publish_round2.py
```

这些是素材审查工具，不是正式准出门禁。

## 署名

Maryam Rahnemoonfar、Tashnim Chowdhury、Robin Murphy，RescueNet，训练发布 DOI [10.6084/m9.figshare.22825511.v1](https://doi.org/10.6084/m9.figshare.22825511.v1)，论文 DOI [10.1038/s41597-023-02799-4](https://doi.org/10.1038/s41597-023-02799-4)。源图片与掩码未修改；预览做了缩小、拼版与标签叠加，绿色为 7、红色为 8，不是新标注。
