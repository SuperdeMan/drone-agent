# OAM真实派生来源与软件证据 · 2026-10-22

本目录记录已完成的来源字节、软件QA及原生像素/重放审核。**不授予业务标签、独立资产、站点或真实跨次准入；P6/WP-P6-01仍开放。**

| 准确版本与范围 | 已完成证据 |
|---|---|
| 4800ea0来源能力 | 显式条件分段recipe；51,545,353字节失败200前缀+49份206，共50份body连续覆盖253,851,020字节，重叠0；本机完整对象SHA已核 |
| 4800ea0软件 | [完整QA](software-4800ea0/result.json)：Ruff rc0，2150通过/1项既有Unix-socket跳过；3578 tracked文件前后相同 |
| cdf5382窄性能修复 | [定向221项](performance-fix/verification.json)；[完整QA](software-cdf5382/result.json)为Ruff rc0、2174通过/1项既有跳过，3579 tracked文件前后相同 |
| cdf5382 C16复验 | [真实执行](c16-cdf5382/audit/execution-receipt.json)：329输入不变，16来源核对、个案问题0，报告原字节SHA仍738ec0…；整体missing、15项全局缺口保持 |
| cdf5382 native32 | [实际审核](native32-cdf5382/result.json)32/32通过；复用已生成32原生提取+32独立离线重放输出，同候选fresh-cache重新核来源/原生像素/重放；没有新增提取或下载 |

32条属于一个整站`calibration_candidate`的真实无人机正射派生窗口，**不是相机原始单帧**。仍未初标、未确认物理身份/分组或独立资产，来源通过不证明完整道路或业务状态；重复采集资格为false，不能凑跨次配对。旧882窗口索引保留，不能自动写成914或增加正式分母。

原4800慢任务的原生与重放文件已生成，但逐样本审核在传输失败后停止；[停止原件](history/native32-v1-stop/stop-receipt.json)保持`stopped_incomplete`、最终完成数unknown，不能记为0/32失败或追认为旧审核成功。后续cdf32成功是独立真实审核。`frozen_f132`旧字面实际绑定4800四核；source-only的`development_exposed=true`为保守placeholder，未发生本工作流真实看图或业务标注。正式inventory须按实际曝光记录重建并绑定审核，不改原布尔值或洗白其他上下文。

历史问号损坏审查文本、旧pending状态、错标签、原失败回执全部保留；当前范围见[完成覆盖](completion-overlay.json)，旧README及manifest在history原字节保存。2483额外网络字节是API2467+probe16，不是body重叠。父对象SHA为`1127710341b5b785dd151e7e3fa8de9c0ab1cc3dc9f7f8b4df0acdee767be4c5`，只称本机计算摘要。

407份旧提取/重放文件及42份新审核文件按原SHA核对；媒体仅列外部SHA，JSON/日志/程序证据原字节归档，程序均为`.py.txt`。最终磁盘与冻结核心回执见native32-control-v2。SSH原stderr、登录URL和潜在token值仅列hash引用，不封存正文。

[C数据检查点](../corpus_checkpoint_20261022c/README.md)与[已归档清理](../cloud_cleanup_20261022/README.md)独立引用，不重复大payload/journal。正式资产、配对、冻结及模型调用仍0；真人候选计时deferred、现场清障未验证，D089门槛不变。

`file-bindings.json`绑定除自身外全部叶字节与来源；`.gitattributes`为`* -text`，用于保留CRLF/原JSON摘要。最终Git index字节由root核对，本外置包装不写仓库。
