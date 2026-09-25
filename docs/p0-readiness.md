# P0 实现与验收记录

[路线图](roadmap.md) · [P0 工作包](operations-implementation.md) · [来源实现](../src/drone_agent/fleet/provenance.py) · [发布门禁](../scripts/verify_p0_release.py)

**状态：实现候选，本地检查已通过，云端与任务台验证待完成；P0 尚未关闭。** 本页对应 D054 的来源贯穿与独立产品门禁，不改变历史 M0–M3 的验收结论。

## 实现范围

- 受信运行入口绑定执行后端、运行 ID、准确源码 / 脏代码摘要、场景、平台和登记表摘要；网页与 A2A 不能指定这些字段。
- 规划按实际 Provider 实现区分脚本、录制、缓存、实调与未运行；不凭模型名称推断。确定性重试单独记录。
- 来源保存在既有 `versions.decision.run_provenance`，单条记录只能追加、不能覆盖；重启、驳回与迟到证据不会把旧任务标成当前部署。SQLite schema v1 与冻结 wire 均不改。
- 每份影像绑定采集身份、媒体摘要与来源；分析尝试独立记录，包括失败 / 拒判；版本、三列报告、任务台与离线证据页展示同一投影。来源标签不参与飞行授权或三元判定。
- 旧任务未保存来源时显示 `legacy_unknown`；不补造旧运行信息。P0 当前贯穿本机无设备模式及 M2 服务 / SITL 链，S0 用测试夹具验证契约；S2 数据导入与 S3 厂商网关仍属于后续里程碑。

## 本地检查

Windows 完整测试：995 项，994 通过、1 项按既有规则跳过（Unix socket 的 Linux 检查）；失败和错误均为 0。ruff、任务台 JavaScript 语法、字段清单与 v1 wire 冻结检查通过。字段清单为 54 个模型 / 371 个字段 / 16 个枚举；本次未新增机载契约。

新增 11 项测试覆盖：脚本冒充实调、请求注入后端、来源冻结与重启、驳回保留、历史缺项、混合媒体、缓存 / 录制区别、报告三元状态不变，以及来源 / 门禁缺失、混版本、篡改和硬件状态分离。完整云端检查还必须补齐 Linux 下的 socket 验证。

## 发布门禁

| 判据 | 必需证据 | 当前状态 |
|---|---|---|
| scope | 从审阅基线到候选的变更限于 P0；控制执行、适配器、wire 和恢复配置不变 | 待候选固定 |
| checks | 同候选云端完整检查、0 失败 / 错误 / 跳过、项目隔离 | 待执行 |
| adversarial | 确定性 / 脚本规划对抗集、授权包 0 | 待门禁执行 |
| e2e | M2 全部 6 场景 × 3 种子、独立裁判与回放 | 待执行 |
| source_views | 每个 E2E 导出视图与裁判保存的文件摘要匹配；来源投影、配置、报告一致 | 待执行 |
| live_sources | 真实 Tailnet HTTPS 实调规划、批准、飞行、影像、裁判；来源按准确候选核对 | 待执行 |
| historical_boundaries | 原 M3-SITL passed、M3 总 not_passed、JIL missing；旧回执字节不变 | 待门禁核对 |

最终门禁 JSON 同时输出 `capabilities`：P0 当前候选、M1/M2/M3 的准确历史 SHA、H 线缺项及 P1–P5/X 线计划分开。缺任一必需证据即 `not_passed`，不能用历史成绩补齐。

## 复现入口

先在同一已提交候选上运行 `dev_stack.py deploy --sha <SHA> --apply`，再分批执行完整 M2 场景。`fetch` 拉取和校验各运行，把接受批次的 `service-export/view.json` 按 `<scenario>-<seed>.json` 原字节保存在独立来源目录；不重排 JSON 或改写摘要。

按 D037 顺序激活同部署的 `console-cloud --apply` 与 `desk-cloud --apply`，经 `desk_probe.py http/session` 获取真实入口与实调任务证据。源码、场景和 Provider 的记录不一致时先修正再重验，不混用不同候选。

```powershell
uv run python scripts/verify_p0_release.py --sha <完整候选SHA> --deployment <部署回执> --e2e <M2回执列表> --views-dir <来源视图目录> --live-probe <实调探针回执> --output <P0门禁结果>
```

本次不启用真实设备，不更改数据库 schema，也不把 P0 软件通过视为 H1/JIL 或原 M3 组合门禁通过。硬件与后续产品工作仍按路线图单独准出。
