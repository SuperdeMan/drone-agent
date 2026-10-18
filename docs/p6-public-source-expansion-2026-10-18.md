# P6 公开来源补充调查 — 2026-10-18

**新增取得 12 张 LADI 原图，当前只用于来源适用性调查，正式准入为 0。** 用户是否接受低空有人机航拍作为 S2 补充仍待答复；已有 415 张无人机照片的二审不依赖该答复。总门禁与当前进度见[交接](p6-ai-verification-handoff-2026-10-18.md)。

## 已核实与取得

[LADI 官方概览](https://github.com/LADI-Dataset/ladi-overview)提供低空灾后航拍数据及直接访问方式；[AWS Open Data 登记](https://registry.opendata.aws/ladi/)列明管理方与 CC BY 4.0 数据许可。当前来源包含 Civil Air Patrol 的航拍，不能标作无人机实拍，也不能由此推断无人机特有成像条件已覆盖。

本机 `D:/drone-agent-data/p6-ai-verification-20261018/` 保留：

- `source-discovery/`：作者说明、AWS 来源页、S3 清单、数据加载器文本、HTTP 下载回执和摘要。没有执行远端加载器代码。首次请求作者 `main/README.md` 返回 404，后续取得 `master/README.md`，错误回执保留。
- `source-discovery/ladi-v2a-all.csv`：实际源索引 9,972 行。源标签只用于明确披露的候选筛选，不是本项目业务真值。
- `ladi-pilot/plan.json`：先按源标签选择同时包含道路损坏与残骸、没有淹水标签的八个源事件组，各取字典序第一张；另从前四组各取一张源道路负例。源事件编号不是已确认的独立站点。
- `ladi-pilot/result.json`：12 / 12 张完整原图，合计 110,138,586 字节；保留来源 URL、原图 SHA-256、长度、源 ETag、尺寸和 EXIF，全部像素解码成功。
- `ladi-pilot/source-suitability.json`：实现方实际查看了 12 张原图。它们及已确认相关组需按开发曝光处理；没有生成正式业务标签或模型成绩。

## 适用性判断

`ladi-e3fbb8fec558bb16` 可见跨越道路的倒伏树木，`ladi-d34c615784857ee6` 的住宅道路附近存在进入候选通道的散落物，说明该来源有补充障碍素材的潜力。其他样本同时暴露出路外残骸、普通车辆、机场跑道、自然欠曝、远处道路和草地庭院等问题；不能把源 `roads_damage` / `debris_any` 标签直接映射为道路障碍。

若用户允许该拍摄来源，下一步是在未被实现方查看的原图中做独立目标标注与二审，按来源单独报告，核对资产 / 地点 / 批次隔离并补足各类覆盖。数量、真实跨次和质量目标不因此下降；这 12 张已查看的调查样本不直接补进留出测试。

另查到 [CRASAR-U-DROIDs 作者数据说明](https://huggingface.co/datasets/CRASAR/CRASAR-U-DROIDs/blob/main/README.md)：当前发布以正射拼接影像为主，混合无人机、有人机和卫星来源。此次只取得说明与目录元数据、原图取得数为 0；尚未将拼接或其裁块视为独立实拍资产或跨次证据。
