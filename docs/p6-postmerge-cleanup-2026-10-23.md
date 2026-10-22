# P6合并与清理记录 · 2026-10-23

PR [#2](https://github.com/SuperdeMan/drone-agent/pull/2)已以合并提交`03e695c2650674fccf71213bdc26e03871b042c6`进入main，保留原提交历史。P6仍仅按D091工程交付范围关闭，完整道路业务验收见[后续任务](road-business-validation-backlog.md)。

本机与远端的`docs/p6-public-materials-20261014`、`work/p6-road-views-20261014`均已删除，删除前逐分支核对未合入main的独有提交为0。旧`D:/drone-agent-data/p6-closeout-20261014/repo`工作树及再生缓存已清理，D盘净可用增加约199.7 MiB。`D:/drone-agent-data/p6-road-views-20261014/repo`仍由材料工具引用，保留为detached main工具工作树；新开发从main创建分支。

主工作区原58份未提交文件已保全：51份与合并内容精确一致，另7份保留原字节恢复副本；stash `f2a7a5da095837cd2600789c877bd5c6242c474f`未丢弃。完整恢复与CI对象bundle为`D:/drone-agent-data/p6-postmerge-cleanup-20261022/main-and-recovery.bundle`，已验证，SHA256`18f60b5d7489adc67e5fa628555f6180fe7c9f7c6c851eeeede5d81247397f7c`。两份原CI检出提交另由`refs/evidence/p6/ci-*`保护，避免临时提交对象回收。没有修改.env或密钥。

云端本轮仅只读盘点：截至`2026-10-22T16:33:44.506252+00:00`可用35.67 GiB，P6目录约32.19 GiB。980组同摘要媒体的988个路径可通过保留路径/内容的硬链接替换，预计额外释放1.06 GiB；尚未执行，inode和文件时间会变化。正式评测环境及pip引导工具另有约0.33 GiB可再生候选，需保留重建方式。源影像、TIFF源块、标注和审核记录仍供后续D089使用，保留。

常驻运行档案和发布目录可见占用约12.55 GiB，处于任务台挂载树且部分统计不完整；需逐运行核本机备份和留存，未计作可释放量。Docker可回收字段出现负值，未据此估算或执行清理。

清理回执、恢复副本、只读扫描、逐项去重清单及交互报告位于材料根`D:/drone-agent-data/p6-postmerge-cleanup-20261022/`；报告`cloud-storage-report-v1.html`，清单`cloud-cleanup-candidates-v1.json`（SHA256`494285940e9d6a7a3d6c35071e9cce918cb7dd7e84fc5a25aa459e47374a499d`）。此记录不授予新增云端删除权限，执行前须重新核内容摘要、链接数和运行依赖。
