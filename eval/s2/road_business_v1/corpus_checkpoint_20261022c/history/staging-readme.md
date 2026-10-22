# 道路材料检查点 · 2026-10-22 C（归档staging）

**WP-P6-01仍开放，P6未整体准出。** 本payload可由root核对后拷贝到`eval/s2/road_business_v1/corpus_checkpoint_20261022c/`；制备过程未写仓库、提交或改原证据。

- 新增48条独立AI初标及另一隔离AI整批二审，当前首稿338、二审并集245/245绑定通过：正常22、障碍62、不可判114、错目标4、排除源43；待二审93。观察不等于独立资产。
- 48条补充物理记录全部纳入，首稿对二审可见，**不是另一次盲身份审查**。独立元数据/契约审查支持精确10条组合，38条继续hold，未将几何组自动升级为资产。
- 只在新局部副本采用10条组合，与既有6观察合为16观察、15个主候选、1个既有跨次配对候选；16份标签只重绑四个实际context元数据字段。主245及主分组规则未改。
- 原f132四核心在原云缓存真实审核一次：16/16来源及原生解码通过，个案和既有配对问题0，实际rc0；**整体missing，15条全局缺口全部保留**，含测试单站集中度9/11。正式资产、正式配对、冻结和评测调用仍0。

[资格覆盖](candidate-qualification.json)、[245条结构普查](admission-census.json)、[真实核心报告](qa/bulk-integrated/core-audit.json)分别限定范围；本普查继承未变197条v5结构并加48条，只对精确16条使用新的审核覆盖，不更改主快照false分组字段。最终文字审查245条疑点0；历史损坏和未完成记录保留。

原33整12停滞，仅有9条部分观察、无正式reviews；预登记后由全新fork-none的37整12替换，旧部分答案不传递。36已完成12并通过12条核心绑定，root停止余下等待，不伪造其额外final消息。各自首稿、实际payload、严格入库、四份新review快照及context v30/v31另存。

观察覆盖仍紧缺：校准正常4、测试正常17（含同资产重复）；测试类型观察debris26/vegetation12/other_entity11。即便最乐观加入旧组外正常48/障碍23，仍至少缺正常50、障碍35、错目标36条参考**观察**，这不是资产缺口或工时。真实配对只1候选、正式0；未来native32整站校准来源不能算重复采集。不得承诺很快正式关闭。

图片、源缓存、原生裁片和输入tar不入Git；Python辅助代码以`.py.txt`保留。原837行inventory、旧review快照及四份冻结源码按SHA引用，未重复大payload；[B父检查点](../corpus_checkpoint_20261022b/README.md)保留历史。[已完成云端清理](../cloud_cleanup_20261022/README.md)仅以准确摘要引用，不重复大journal，也不增加业务准出。

本C只绑定`f132b98fd0f6148ca3ab5f7e168de147433fc7be`的实际数据审核，业务SHA`d0aacfc56845ff6e8af5f02e5366a30b41fb90d9f7bea97f4c0b5e023a3e2fe2`。4800ea0的OAM条件分段来源能力、其QA以及未完成OAM32均不在本capsule；root后补独立capsule，不转借原f132全套成绩。

D089数值、数量、独立性、地点和真实跨次要求不变；真人候选p95继续deferred，现场清障未验证。入口建议见[简短交接草案](handoff-proposal.md)，旧B交接原字节见[history](history/handoff-before-c.md)。本目录含参考答案，不向后续盲标者整包提供。

`file-bindings.json`列出除自身外每个叶文件的精确SHA/bytes及源路径；`external-references.json`绑定未复制的父证据。staging可归档不表示数据可准入。
