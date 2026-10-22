# OAM来源与软件证据 · 2026-10-22（4800ea0）

本capsule支持**来源字节和软件能力**，不授予业务、资产、站点或跨次准入。目标归档位置为`eval/s2/road_business_v1/oam_source_checkpoint_20261022/`，与C数据检查点独立；本次仅写外置staging。

`4800ea0726acbd3054e07f31abf3e8b44b94f0f4`新增显式`drone.p6-oam-conditional-parts-http/v1`分支，沿用原full200严格路径。实际来源由**50份body**组成：失败完整GET保留的51,545,353字节前缀，加49份完整206，恰好连续覆盖253,851,020字节，body重叠0。旧“2483重叠”是API元数据2467+辅助probe16字节，均不属于组成body。父对象SHA为`1127710341b5b785dd151e7e3fa8de9c0ab1cc3dc9f7f8b4df0acdee767be4c5`，明确为本机计算，未伪造发布者摘要或将失败200改为成功。

显式recipe、candidate、原HTTP回执、两次真实来源核对及本次只读逐body/hash/父对象分段比较另存。原full200失败、地域回退耗尽时限及主动停止回执原字节保留；大媒体/body/tar不入Git，当前可核的外部媒体摘要见`external-byte-references.json`。

4800完整QA实际完成：Ruff rc0；pytest2150通过、1项既有Windows Unix-socket跳过、0失败/错误。JUnit XML、日志及全部3578 tracked文件before/after映射原字节保留，映射一致；这些成绩仅负责4800。独立代码审查的可读机器字段为无阻断发现，其部分中文说明已编码损坏为问号，原字节保留并另行披露，未由归档者重建。

native32目前只归档用途预留、32计划及原算法等价比较：整站校准，32主候选/10预期参考/10未来任务当前观察/269后续主候选。固定世界范围与稳定次序保持，native窗口是用原算法25m上下文新确定的窗口，不能称为旧冻结像素。**实际32条完整来源/像素/独立重放审核仍未获本capsule证明，不记成功。** 同站单对象不构成真实跨次。

v1 runtime的`source_gate_function='frozen_f132...'`是错误标签；实际runtime request和cloud-core-install回执绑定四核4800，纠正见新supplement，原脚本未回改。后续窄性能修复`cdf5382d4b4ce54600921532ecbe2a5591775b58`的新QA和native32结果由root/source agent另补增量，本capsule不转借4800成绩。

图片不查看，模型调用0；正式资产/配对/冻结0，P6与WP-P6-01仍开放。C的f132实际16观察审核保持独立。所有复制程序均为`.py.txt`；`file-bindings.json`绑定除自身外每个payload叶文件，外部引用均按当下原字节核SHA。历史回执内嵌路径保留其历史含义，不自动成为新的现存媒体/成功声明。
