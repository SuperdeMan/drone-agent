# P6素材冷归档与恢复 · 2026-10-24

**数据归档与云端回收已完成。** 88个分卷、81,380个唯一内容对象（分卷合计约41.43 GiB）均已核摘要并实际写入、复读恢复文件。云端P6材料根已删除，本机原件及工具保留，等待用户复制归档到独立存储后再另行回收。可搬迁状态以归档根`READY.json`为准；准确摘要与实绩见[收尾回执](verification/p6-data-archive-2026-10-24/archive-receipt.json)。

## 位置与覆盖范围

便携目录：`D:/drone-agent-archive/P6-20261024/`。需要整体复制，包括`parts/`、`meta/`、`archive-index.json`、`archive_tool.py`和`README.zh-CN.md`。归档内按内容SHA256存一份，同内容的多个原路径、不同元数据和原硬链接组仍分别记录。

| 命名空间 | 覆盖范围 |
|---|---|
| `cloud` | `/home/ubuntu/drone-agent-data/p6-corpus-completion-20261018-cloud`的全量常规文件、目录及符号链接元数据；冻结时61,552个常规文件、106个符号链接 |
| `local` | 本机所有`D:/drone-agent-data/p6-*`材料目录与`drone-process-audit-20261023`既有清理证据；冻结时44,529个常规文件 |
| 代码 | `meta/drone-agent-head.bundle`包含`fccd22aee476b9186c9cfd85a8586af0084f24df`的完整Git历史；收尾文档提交另存为`meta/drone-agent-final.bundle`，准确提交与CI见`meta/docs-publication.json`；两份均核验bundle |

本机ASCII工具工作树`p6-road-views-20261014/repo`、在用`tools-venv`、其他`.venv`和可再生`__pycache__`未作为素材搬迁；准确排除路径见`meta/local-source-manifest.json.gz`。本机素材原件当前保留，待用户外迁归档并核验后另按清单回收。

原始清单分别保全两端的不同字节版本，不用同名覆盖另一端来源记录。冻结来源清单中的SHA负责原文件，归档分卷及内容对象的SHA负责存储完整性；路径数或内容对象数不代表独立业务资产数。

为缩短云端传输时间，17,264个公开源影像分块从原固定版本的公开镜像取回，逐个匹配本次云端冻结清单中的SHA256后才入包；原云端来源元数据另行保全。其余云端独有内容经SSH取回，传输记录和辅助脚本随归档保存。这是相同字节的取回路径调整，不新增样本、标注或模型调用。

## 核验与恢复

仅需Python 3.10以上标准库。用户外迁时，先复制完整目录，在新目录打开终端执行下列命令；验证通过后再回收D盘归档副本：

```powershell
python archive_tool.py verify --recover-check --report verification-after-move.json
```

`--recover-check`会实际解包每个唯一内容对象，按分卷顺序写入有界临时文件，再重新打开、逐对象核SHA，同时核对全部分卷和来源路径引用。无需同时展开几十GiB工作目录。符号链接目标保存在来源清单；Linux下的权限、纳秒时间戳及链接恢复另经实际样例检验。云端101个含字面反斜杠的兼容链接名按Linux文件名原样保全；Windows默认只保留记录，若要求创建这些链接会在写入恢复目录前拒绝。

恢复本机材料到不存在或为空的目录：

```powershell
python archive_tool.py restore --namespace local --destination D:\p6-restored-local
```

恢复云端材料建议在Linux使用原材料根，保留原绝对路径绑定：

```bash
python3 archive_tool.py restore --namespace cloud \
  --destination /home/ubuntu/drone-agent-data/p6-corpus-completion-20261018-cloud \
  --restore-symlinks
```

恢复默认不覆盖已有目录内容；本机资料可先恢复到临时目录，再核对需要迁回的材料根。未指定`--restore-symlinks`时只保留链接记录，不创建链接，适合Windows查看数据；云端工具环境建议Linux恢复或按清单重建。原UID/GID留在清单，恢复文件由当前账户持有。

## 存储与后续工作

云端来源根已回收，本项目运行服务、`/home/ubuntu/drone-agent/artifacts`历史证据和`desk`均保留；本次自建暂存目录中已取回的529,266,692字节分卷另行清理，回执保留。云端操作留痕在`artifacts/p6-cold-archive-20261024/`；清理前已复核来源清单、进程/文件描述符和容器挂载，并持项目锁；本项目运行容器状态前后一致。

源码、标注/核验、来源许可、曝光/失败和旧中间结果均保持可恢复。这里不把失败载荷转换为成功来源，也不改变候选角色或原用途隔离。分卷只用于保存字节，不是正式模型录制或业务评测。

P6仅按D091工程范围关闭，D089完整道路业务验收仍未通过。继续[运营前业务验证](road-business-validation-backlog.md)前先核验、恢复所需材料，不能因云端工作目录不存在就直接重下全部缓存或宣称数据已无效。已确认的门槛、正式独立资产/配对/冻结/模型调用均0的历史状态不由归档动作改写。

云端源目录原占用约30.80 GiB；包含旧归档暂存回收及并行服务写入的操作区间，可用空间净增加31.29 GiB，结束时可用69.27 GiB。历史盘点与清理分别负责自己的时点，见[2026-10-23记录](p6-postmerge-cleanup-2026-10-23.md)。
