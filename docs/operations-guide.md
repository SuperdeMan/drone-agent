# 运维手册 · 无硬件平台 v0.1（P5）

[任务台指南](tailnet-desk.md) · [P5 方案](p5-implementation.md) · [云端开发](cloud-development.md) · [决策记录](decisions.md)

本手册面向维护常驻任务台（`8448`，即平台 v0.1）的人：部署与升级、回退、备份与恢复、监控、故障处理、密钥与成员管理，以及 72 小时长稳。平台只有仿真：`campus_s1` 是 PX4 SITL，`fleet_s0` 是两台逻辑飞行器，`vendor_s3` 是厂商协议模拟器；没有任何真实设备、真实厂商固件或真实工单系统（D069）。所有命令在本机仓库根目录运行，每条命令前设置镜像：

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
```

带 `--apply` 的命令才会改变云端；不带时只返回计划。共享云主机上只操作本项目（`drone-agent-cloud`）的容器与镜像，其他应用的容器身份在每次激活与批次前后核对（[共享主机约定](cloud-development.md)）。

## 1. 组件

| 组件 | 角色 | 网络 | 读写 |
|---|---|---|---|
| `desk` | 页面与 hri.v0 会话，`127.0.0.1:8769`，经 Tailscale Serve `8448` | `desk_ingress` | API 套接字只读；不持任何密钥 |
| `desk-service` | 任务服务：账本、目录、审批签名、领取闸门、工作流 / 调度 / 业务引擎、厂商网关 | `desk_uplink`、`desk_model` | 账本与媒体；成员列表、签名密钥、模型 key 只读；厂商套接字只读 |
| `desk-model-proxy` | 允许列表 CONNECT 代理，唯一出站路径（D036） | `desk_model`、`desk_egress` | 只读自己的限流开关 |
| `desk-uplink` | PX4 飞行器 `uav_01` 的机载 uplink | `desk_uplink` | 机器人 inbox / 信箱 |
| `desk-dock` | `dock_s1` 的逻辑机场后端 | 无 | API 套接字只读、飞行真值只读 |
| `desk-fleet` | 逻辑飞行器 `uav_fa` / `uav_fb` 与其机场（真实机载进程 + 逻辑飞行） | `desk_uplink` | 自己的状态目录；世界文件只读；各自的客户端证书只读 |
| `desk-vendor` | 厂商协议模拟器 `dock_vd` / `uav_v1` | 无 | 自己的套接字与日志；世界文件与厂商故障开关只读 |
| 监管者（systemd `drone-agent-desk-supervisor`） | 按 inbox 在 PX4 SITL 中飞每个已接受版本；放置世界文件要求的模型；独立裁判 | 主机 | 持项目锁飞行 |
| 长稳编排（systemd `drone-agent-desk-soak`，只在长稳期间） | 按冻结计划启动运行、扮演人、注入整容器故障、采样 | 主机 | 只经 API 套接字与开关文件 |

任务台工作区（主机 `~/drone-agent/desk/`）：`service/`（账本、媒体、备份）、`api/`、`robot/`、`flights/`、`judge/`、`fleet/`、`vendor/`、`world/`、`faults/`、`soak/`、`backups/`、`deployments/`（每次激活的回执与演练）、`supervisor/`。密钥在 `~/drone-agent/secrets/desk/`（签名密钥、信任文件、CA 与各证书、成员列表）与 `secrets/m2-model/`（模型 key），只属主可读。

## 2. 部署与升级

1. **本地**：`uv run ruff check .`、`uv run pytest -q`；改动页面时另跑 `node --test tests/console/test_mission_client.cjs tests/console/test_live_client.cjs`。提交后在 `git archive` 副本上再跑一次全量（云端检查镜像没有 `.git`）。
2. **部署与云端检查**：
   ```powershell
   uv run python scripts/dev_stack.py deploy --sha HEAD --apply --artifacts D:/drone-agent-cloud
   uv run python scripts/dev_stack.py test
   ```
3. **成员列表**：列表要指明新项目时（如 P4 → P5 增加 `fleet_s0`、`vendor_s3`），先暂存给下一次激活：`desk-members --next --apply`。旧服务在激活前重启也不会读到它；激活成功后才替换在用列表，失败则恢复原列表。成员不变时用 `desk-members --apply`（立即生效于下次服务启动）。
4. **激活**：先 `console-cloud --apply`（使 `/fixed/` 的代理与部署版本一致），再 `desk-cloud` 看计划、`desk-cloud --apply`。激活依次：构建 M2 与 P5 仿真镜像、签发证书（含逻辑飞行器）、在当前账本副本上演练 P1–P4 迁移与 P5 目录切换（只保留计数与摘要）、写入激活记录、启动常驻容器与监管者、核对服务以四个任务台目录（P6 起工作流与业务目录为 `p6_desk_v1`，运营与调度目录仍为 `p5_desk_v1`，D078）、三种执行后端与厂商链路启动，并等待四个机场会话都变为活动；最后核对容器边界、Serve 映射与其他应用未变。任何一步失败只恢复本项目组件。
5. **核对**：`desk-cloud --status`；`scripts/desk_probe.py http` 与 `spoof`；`uv run scripts/desk_browser.py --origin <origin> --output <目录>`（全部工作区与 `/fixed/`）。

## 3. 回退

- 每个激活记录（`desk/current.json`）都指向一次部署；回退即重新部署旧提交并激活：`deploy --sha <旧提交> --apply` → `console-cloud --apply` → `desk-cloud --apply`。
- 账本只增不减；目录按摘要固定在运行上，旧运行在任何目录下都按自己固定的版本继续。回退到 P5 之前的版本时，P5 新增的项目（`fleet_s0`、`vendor_s3`）在旧目录中不存在：先用 `desk-members --apply` 写回旧版成员列表（只含旧项目），否则旧服务因未知项目的成员资格拒绝启动。
- 回退不删除任何数据；新版本写入的目录行与审计行保留。

## 4. 备份与恢复

- **在线备份**：`desk-backup --apply`。用 SQLite 在线备份复制在用账本到 `desk/backups/<运行ID>/`，记录逐表行数、完整性检查与摘要，并为媒体文件写清单（路径、大小、摘要）。不停服务。
- **恢复演练**：`desk-restore-drill --apply`。把最近一次备份复制到临时目录，用激活版本的镜像在无网络容器中以 P5 目录离线打开并读回（任务、运行、工单与各项目审计），核对摘要、逐表行数（只允许目录表增加）与目录摘要；演练从不触碰在用账本，结束即删除副本，只保留回执。
- **真实恢复**（人工步骤，执行前须确认）：
  1. 记录当前状态：`desk-cloud --status`，并对在用账本再做一次 `desk-backup --apply`。
  2. 在主机上停止监管者与常驻容器：`sudo systemctl stop drone-agent-desk-supervisor`，再对任务台 compose 文件执行 `docker compose -p drone-agent-cloud -f <激活部署>/source/sim/compose.desk.yaml stop desk desk-service desk-dock desk-fleet desk-vendor desk-uplink desk-model-proxy`。
  3. 把 `desk/service/ledger.sqlite3*` 移到带日期的目录（不删除），复制所选备份的 `ledger.sqlite3` 到 `desk/service/`，核对其摘要与备份回执一致。媒体按清单从备份时的媒体目录核对；缺失的文件在任务视图中显示为不可读，不影响账本。
  4. 重新激活：`desk-cloud --apply`（演练在恢复后的账本副本上重跑），再按第 2 节核对。
  5. 已在备份之后发生的飞行与审批不会出现在恢复后的账本中；机载已接受版本按各自的审批有效期处理，过期的不会飞行。

## 5. 监控

| 看什么 | 命令或位置 | 正常 |
|---|---|---|
| 入口、服务、监管者 | `desk-cloud --status` | `status: ready`、`supervisor_active`、`revision_matches` |
| 服务健康 | 页面顶栏；`/health` | `ok`，`execution_backends` 为三种后端 |
| 机场会话 | 「机队与机场」工作区；`resources.list` | 四个机场 `active`，报告新鲜 |
| 云端飞行 | 顶栏「云端飞行」、`desk/supervisor/public/status.json` | `idle` 或 `flying`；`waiting_for_workspace` 表示其他批次持有项目锁 |
| 审计 | 「运行审计」工作区；`audit.list` | 人工动作、系统动作与成员的越权尝试（`access.denied`） |
| 长稳 | `desk-soak --status` | `running`，`harness_errors` 为 0 或可解释 |
| 资源 | 长稳采样 `desk/soak/<ID>/samples.jsonl`；`docker stats` | 内存低于限额，重启次数只随注入增加 |

## 6. 故障处理

| 现象 | 处理 |
|---|---|
| 页面报服务不可用 | `docker ps` 看 `desk-service`；它按 `unless-stopped` 自动重启，账本不变。连续失败时看 `docker logs`，常见原因是成员列表指明未知项目（按第 3 节写回匹配的列表）。 |
| 服务重启后 CPU 持续接近配额几分钟 | 正常：重启后会复核全部历史任务的派生状态，仍可能变化的任务优先，其余在后台分片进行，API 与机队端点照常应答（D073 2026-10-05 补记）。任务越多耗时越长；若 API 本身数十秒不应答，属缺陷，按第 9 节记录。 |
| 机场会话 `lost` / 报告过期 | 逻辑机场：`desk-dock` 或 `desk-fleet` 重启即新会话（P1 对账后恢复）；厂商机场：看 `desk/vendor/log/state.json` 的 `link_up`，模拟器断线会在设定时间后自动恢复。 |
| 厂商任务停在「取消中」或不完成 | 网关只在终态与证据齐备后收尾；终态永久丢失时任务保持未完成、预约不释放（D072）。这是正确行为；需要人工判断后再处理，不要直接改账本。 |
| 模型规划或分析失败 | 看模型代理日志（`throttled` / `upstream_unreachable`）与 `desk/faults/model/throttle.json`；代理恢复后新的请求即成功。分析以带原因的拒判结束，不会被当作正常。 |
| 云端飞行一直等待工作区 | 其他批次或部署持有项目锁；监管者会在锁释放后继续。长稳期间编排只在锁空闲时审批 PX4 任务。 |
| 某资产的新发现总是并入一张旧工单、复检永远「不确定」 | 该工单的复检模板用脚本夹具（如 P4 任务台的 `desk_watch` / `desk_reinspection`），按设计不能结算轮次。当前任务台上 `asset_red` 就是这样（P4 遗留）。P5 不提供接替或终止工单的入口：闭环演示与长稳改用其他资产（`asset_blue`）；该能力列为接手项。 |
| 磁盘增长 | `soak` 采样记录账本与媒体增长率；构建缓存与旧镜像按[镜像清理做法](cloud-development.md)处理，只删本项目的。 |

## 7. 密钥与成员

- **模型 key**：`m2-key --apply` 从本机环境的 `MINIMAX_API_KEY` 写入 `secrets/m2-model/minimax.key`（0600）；更换后重新 `desk-cloud --apply`。key 不进入仓库、日志或回执。
- **成员列表**：`desk-members [--next] --apply` 以任务台 hello 中的 tailnet 身份生成列表（操作者对三个项目的 operator / approver / admin / reviewer、`legacy_m2` 只读），加监管者的只读身份与长稳编排的 `harness:soak-*` 身份（各一个角色，另有一个只属于 `vendor_s3` 的成员）。真实登录名只在云端 secrets 中。
- **证书**：激活时生成；签名密钥保留，TLS 叶证书在缺失或三天内过期时重签，逻辑飞行器各有客户端证书。审批签名只信任任务台自己的信任文件。

## 8. 世界变更

`desk-world --section s1|s0|s3 --asset <资产> --state normal|damaged|obstructed|glare --apply` 修改任务台世界文件 `desk/world/appearance.json`，并在 `history.jsonl` 记一行。监管者在每架次起飞前按 `s1` 一节放置损伤贴片、道路障碍物或标记中心上的反光圆片（`glare`，P6 补拍演示，D078）；逻辑机队按 `s0`、厂商模拟器按 `s3` 渲染采集。页面与任务服务读不到这个文件；入口没有任何世界或飞控能力（D070）。

补拍会话（P6，D078）：先 `desk-world --section s1 --asset asset_blue --state glare --apply`，再用 `scripts/desk_probe.py workflow --start --workflow recapture_watch --asset-input asset_blue --clear-before-recapture asset_blue` 经入口启动 `recapture_watch`。首次采集因目标区域反光被拒后，补拍任务等待审批；探针先以 `desk-world ... --state normal --apply` 清除反光，再像人一样审批补拍。补拍与其他任务一样逐次审批，入口没有补拍按钮。

## 9. 72 小时长稳

- **开始**：确认任务台在候选版本上 `ready`、四个机场会话活动，再 `desk-soak --start --apply`。编排写入 `desk/soak/<ID>/manifest.json`（T0、候选 SHA、部署 ID、计划摘要与展开的发生时刻），启用计划中的三个服务排班，并以 systemd unit 运行；服务重启不影响 T0。`--hours` 只用于演练，门禁不接受。
- **进行中**：`desk-soak --status` 查看各类发生时刻的状态、编排错误数与最近一次采样。编排只经 API 行动（审批确切任务包、按世界真值复核、维修反馈、约 5% 的 S0 排班运行取消、越权探测），故障只经 Docker 与开关文件注入。长稳期间请勿在任务台上操作同一批项目，也不要激活新版本；需要在云端跑其他批次时照常进行，PX4 审批会等项目锁空闲。
- **停止**：`desk-soak --stop --apply`（提前结束；编排先停用排班、结束所有故障、恢复世界）。到时自动结束。
- **裁判**：`desk-soak --judge --apply` 复制账本后在无网络容器中运行 `eval/judge_p5_soak.py`，结果写在 `desk/soak/<ID>/judge/<运行ID>/result.json`，七条判据逐条给出；回执与结果按 P5 验收记录归档。长稳进行中也可以运行（只读，不影响长稳）：时长与发生时刻两项在结束前必然未完成，排班、恢复、资源与分层判据中尚未到时的条目也列为未完成，其余（运行、计数、已发生故障的恢复、意外重启）可作早期预警；中期结果只作预警，不替代结束后的裁判。
- **缺陷**：长稳中出现阻断缺陷时停止，修复后以新候选重新开始 72 h，旧记录保留为负记录（D073）。
