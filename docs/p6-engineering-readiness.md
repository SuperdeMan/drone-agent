# P6 工程交付验收（D091）

**P6 已按工程交付范围关闭；道路识别业务效果尚未验收。** 用户于2026-10-22明确采用D091范围，WP-P6-01按“道路业务评测工程与公开素材交付”完成；WP-P6-02–07仍分别按自己的历史版本及范围验收。完整D089移交[运营前业务验证](road-business-validation-backlog.md)，不计为通过。

## 准确版本与结果

| 检查 | 结果 |
|---|---|
| 软件候选 | `900abf1a88e8c6e62f8c26c6ddfb8274ca130f15` |
| 实际CI检出提交 | `394676776bf36d7fa3a418c419391b23762fa724`；候选与检出源码由Git边界实际比对 |
| 完整持续检查 | [GitHub checks #39792483834](https://github.com/SuperdeMan/drone-agent/actions/runs/39792483834)通过：锁文件环境、Ruff、契约字段、全量pytest及客户端测试；pytest：`2351 passed in 773.97s (0:12:53)` |
| 工程关闭门禁 | [实际结果](../eval/s2/road_business_v1/engineering_closeout_20261022/readiness.json)：11/11通过，`engineering_delivery_closed=true`、`d089_business_accepted=false` |
| 不可变材料 | D检查点750份叶文件逐字节一致；331条已二审参考绑定通过，93条待二审首稿作为未完成库存移交 |
| 局部来源结论 | 19观察逐观察/逐对问题0，全局仍15项；22观察负结果和三条来源hold保留，不转换为完整数据准入 |
| 完整业务状态 | 正式独立资产、正式配对、数据冻结及正式待评模型调用均0；真人效率和现场清障未验证 |

CI原始API元数据、检出/测试摘要和摘要绑定见[工程证据包](../eval/s2/road_business_v1/engineering_closeout_20261022/README.md)。原`cdf5382`的2174通过/1项Windows跳过仅负责原候选，本次不转借其成绩，也不将此次软件CI当成新的SITL、模型或硬件验证。

## 交付与复核

原候选`65fc421`的Linux CI为2344通过/1失败，未登记C盘被当作相对路径接受；[失败回执](../eval/s2/road_business_v1/engineering_closeout_20261022/failed-65fc421.json)保留。修正候选跨平台拒绝未登记盘符、盘符相对路径和UNC共享，并规范相对分隔符，原断言保留；本地路径15项与原生回放24项通过，完整CI另由上表负责。

交付包含授权来源核验、原生像素及独立回放、AI标注绑定、数据准入、任务制备、真实录制入口、严格回放、计分和原严格业务门禁；[D检查点](../eval/s2/road_business_v1/corpus_checkpoint_20261022d/README.md)及[材料库存](p6-road-corpus-handoff-2026-10-22.md)保留精确参考和失败记录。

工程入口`verify_p6_engineering_delivery.py`核范围批准、原D089协议摘要、后续任务、固定归档、331绑定、局部审核全局缺失和完整CI，Git边界拒绝借用不同源码或未提交源码的成绩。新增9项门禁测试覆盖越界业务声明、未批准、CI不完整/失败、候选不一致、协议变化、归档篡改及源码不一致。

```powershell
uv run python scripts/verify_p6_engineering_delivery.py --scope eval/s2/road_business_v1/engineering_closeout_20261022/scope.json --ci eval/s2/road_business_v1/engineering_closeout_20261022/ci.json --candidate 900abf1a88e8c6e62f8c26c6ddfb8274ca130f15 --output <新的复核输出.json>
```

复核前需具备该候选和CI检出提交的Git对象。原`verify_p6_road_business.py`、协议、失败及`wp_p6_01_closed`字段继续保留D089严格业务含义，工程关闭不将其置为true，不改变生产画像、服务或控制链。

工程结果SHA256：`a2e567771de3e77fce4a1e2ed36c4f0ce5926143564ccf1da1c1622d99c573ef`；CI回执SHA256：`d0250aec437f143b45b6ffafd4d7c27eeb3af44b86db956e6869b22a976e7e32`。此结论只负责表中候选和本页工程范围，后续版本按自己的证据验收。
