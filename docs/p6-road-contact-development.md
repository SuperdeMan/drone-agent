# P6 无大小门槛道路候选开发探针

本轮落实用户确认的 `road-contact-dev-v1` 口径：目标内有可辨认的实体遮挡即可进入开发候选，不增加最小尺寸或道路占用比例门槛。旧 v3 / v4 提示要求大件或大量堆积，不能直接当作这一口径的结果。新提示为 `road-region-contact-dev-v3`，与在用视觉画像分开。

## 冻结方案

- 复用 v4 的六个整图输入（五张不同原图）：每例无标记图 + 青色支持域轮廓图，共 12 个既有 JPEG；逐字节相同，不重新裁剪、缩放、编码或增加分块。
- 原图及支持域已在开发中曝光，六个目标不等于六个独立地点。v5 的 AI 实体轮廓、几何结论和复核页草稿不输入模型；没有独立人工真值。
- 只改提示版本和业务指令；系统提示、MiniMax-M3.1-Flash-Preview、温度、thinking / effort、8192 token 上限、180 秒超时及至少 30 秒启动间隔与 v4 一致。每个目标一次调用，最多六次；失败或缺席不当作正常，连续三个调用失败即停止，不重问以挑选答案。
- 复用既有录制器和不可变云端运行时；先核对内部模型网络与只读镜像。没有业务 / 飞行 API、服务激活、数据库迁移或密钥修改。在用画像不自动采用新提示。

## 语义及核对范围

实体与阴影、涂装、纹理及单纯表面损坏分开。边缘位置或实体性质无法分辨且可能改变结论时保持 `unknown`；有明确实体候选时可以 `suspected`，同时保留其他不确定点。普通道路车辆本身不能证明障碍，单帧也不能判断其运动状态。边界来自暂定可见支持域，不是已登记资产；掩码孔洞不能当作道路外。

输出沿用旧录制协议，保持正常 / 疑似 / 未知与最多八个候选框。框仅用于定位，没有变成实例轮廓或业务真值。`normal` 只表示当前输入中未见符合口径的候选；不得解释为完整道路畅通、可安全通行、无需清障或已修复。

离线校验重建冻结请求、核对录制来源及模型 / 运行时 / 提示摘要，逐条严格回放。新增拒绝重复 JSON 键、非有限值、空依据、未知却未说明不确定原因等检查。缺失、失败、无效回答保留为未知，并与有效 `unknown` 分栏，不用解析成功率代替识别质量。

仅报告调用 / 回答 / 结构有效 / 严格回放数量、返回分布、候选框与文本、用量以及相对旧整图回答的变化。历史对照不是同期随机比较；模型后端可能变化，不能把差异单独归因于提示，更不能报告准确率、召回率或提升比例。独立人工复核、数据许可、地点 / 批次隔离及清障前后证据仍是正式评测的前提。

## 入口

工具：[scripts/p6_road_contact.py](../scripts/p6_road_contact.py)。在隔离工作树 `D:/drone-agent-data/p6-road-views-20261014/repo` 执行：

```powershell
uv run --frozen python scripts/p6_road_contact.py prepare --previous-batch eval/s2/p6_candidates_v4 --previous-inputs D:/drone-agent-data/p6-road-views-20261014/data/diagnostic/inputs --config eval/s2/p6_candidates_v7/config.json --output <新的外部诊断目录>
uv run --frozen python scripts/p6_road_contact.py replay --package <诊断目录> --cloud-output <拉回的output目录> --output <新的汇总JSON>
```

协议、冻结输入与后续结果放在 [v7 包](../eval/s2/p6_candidates_v7/README.md)。P6 当前状态见[交接](p6-materials-handoff-2026-10-15.md)，不因本轮开发探针改变准出状态。
