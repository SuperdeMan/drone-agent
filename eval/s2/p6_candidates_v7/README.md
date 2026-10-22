# P6 无大小门槛开发探针

本包冻结 `road-region-contact-dev-v3`：保持 v4 六个整图输入及模型参数，更新业务口径。它不是独立评测集，也不把模型输出转为人工真值。见[方案及语义](../../../docs/p6-road-contact-development.md)。

准备工具为 [p6_road_contact.py](../../../scripts/p6_road_contact.py)；图像、传输包及运行日志留在仓库外。原始回答、录制与失败已独立归档，不改写 v1–v6。正式评测门槛仍未满足。

## 本批状态：环境核对失败，不采用

- 冻结提交 `424caa3cc347855ad3d4a4119a97657ddf19ec76`；[协议](protocol.json)、[提示](config.json)、[计划](plan.json)、[输入摘要](input-manifest.json)均在调用前提交。
- 运行 `p6-road-contact-dev-20261015T021842Z`：6 次调用、6 条回答、云端 / 本地严格回放各 6 条；[原始录制](diagnostic/recordings/)、[回答](diagnostic/results.json)和[回放汇总](replay-summary.json)完整保留。所有回答为疑似，共 33 个原始框，不是准确率或独立缺陷数。
- [环境回执](diagnostic/execution.json)记录 4 个容器被替换、2 个新增，原定容器身份不变条件失败。容器内诊断进程返回 0，外层启动器因环境核对失败返回 1。9 个未变身份不替代全量核对，不把这批标成隔离通过。
- [分析](analysis.json)分开保存响应、环境差异、原始用量和定位核对；[AI 语义复核](semantic-review.json)指出宽框、车辆 / 阴影混入及目标范围风险；[六项目录](review-queue.json)的人工结论和复核耗时仍为空。
- 分析脚本字节快照在 [tool-source/](tool-source/)，仅用于产物核对，不进入模型输入。生产画像没有变更；独立人工业务真值仍为 0。

配置 SHA-256：`b294393e78c0a50204c1a1e1e51715e3ae4b7b2ba8b7aef25186d48a3970e4b0`；输入清单 SHA-256：`f997962c9094afb1a5ffb36647a35a7f7e70d6efc27b8630b833595c946fead0`。本机原始包在 `D:/drone-agent-data/p6-road-contact-20261015/diagnostic/`。验证范围见[回执](../../../docs/verification/p6-road-contact-2026-10-15.json)，下一步见[当前交接](../../../docs/p6-materials-handoff-2026-10-15.md)。
