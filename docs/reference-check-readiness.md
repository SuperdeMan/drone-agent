# 跨站点指代的确定性检查（D082）验收记录

**结论**：候选 `4398938`，门禁六项全部通过（记录 `a35c668`，[门禁结果](verification/reference-check-2026-10-11/release.json)）。

- **检查做什么**：在调用模型之前检查请求文字，点名本任务绑定以外的项目、站点、机器人或机场的请求，以 `scope.foreign_reference` 拒绝。
- **评测结果**：在三份已标注语料（114 例）与任务台用例上，误拦 0、拦下 17、漏拦 5，任务台用例 15/15。
- **评测要披露的问题**：第一版规则在首次评测中误拦 1 例，未通过；修订后在同一批数据上复评，所以复评不是独立评测。

范围是软件 / SITL，确定性检查，不调用模型。方案见[跨站点指代检查](reference-check.md)。

## 版本与门禁

| 项 | 值 |
|---|---|
| 规则首版 | `d4d74e7`：别名、任务台用例与检查代码在评测之前提交 |
| 首次评测 | 误拦 1、拦下 19、漏拦 3，任务台用例 14/14，**未通过**（[not-counted/](verification/reference-check-2026-10-11/not-counted/)） |
| 修订 | `4398938`：项目别名只登记带「项目」的说法；任务台用例改一例、加一例 |
| 候选 | `4398938`，部署 `20261011T115157Z-596345dd`，云端检查 1470 项全部通过 |
| 门禁 | `scripts/verify_reference_check.py`：六项全部通过 |

## 评测

| 语料 | 拦下 | 漏拦 | 误拦 | 放行 |
|---|---|---|---|---|
| `plan_ops_v1`（38） | 4 | 2 | 0 | 32 |
| `plan_ops_v2`（38） | 4 | 2 | 0 | 32 |
| `plan_ops_v3`（38） | 9 | 1 | 0 | 28 |

**拦下的例子**（其中多例是 planner-v2 / v3 实调中出过错的请求）：

- 「C 站那个绿色标记」「C 站的 asset_north2」（D 站机）；
- 「园区 A 站的绿色标记」（港区机）；
- 「让 uav_b 去查」（港区机）；
- 「harbor_ops' red marker」（B 站机）；
- 「p3_site_b_v1 地图上的蓝色标记」（A 站机）。

**漏拦的 5 例**：

- 只用描述指代：「the harbor's rooftop zone」「The campus crew asked us …」；
- 其他体积：`campus_rooftop` 两例；
- 外站资产 id：一例。

名称表不含体积与资产，这些请求继续由规划器与审批人把关。外站资产 id 本来就不在本站的草案 schema 里，规划不出来。

**任务台用例**：15/15。站点 B 的飞机请求「站点 A 的红色设备标记」（D081 的任务台观察）会被拒绝；本机自己的称呼都放行，包括「site B」「site S1」「厂商机场」，以及与本站同名的「site A」。

## 回归

- **门禁当场回归**：P1 S0 42/42，授权矩阵越权 0。
- **云端全量检查**：1470 项，包含 D079–D081 全部 17 份实调回执的严格回放。规划评测关闭了这项检查，所以这些回放不受影响。

## 任务台

- **激活**：激活到候选。四个目录与迁移演练、目录切换演练都通过；四个机场会话在线；常驻容器都没有 Docker 权限。
- **入口核对**：页面、脚本与健康都在候选上，伪造身份头被忽略。
- **跨站点请求**：经入口，`uav_fb` 提交 “Inspect the red equipment marker at site A.”，0.2 s 内以 `scope.foreign_reference` 被拒绝。没有规划记录，也没有可审批的版本。D081 的任务台观察中，planner-v3 曾把同一条请求规划成本站的 `asset_red_b`。
- **本站请求**：两条照常由 planner-v3 实调规划，随后驳回：
  - `road_north`，用时 5.0 s；
  - `asset_red_b`，用时 3.6 s。
- **回执**：证据在 [desk/](verification/reference-check-2026-10-11/desk/)，Tailnet 主机名已替换为 `<tailnet-host>`。

## 不计入

证据在 [not-counted/](verification/reference-check-2026-10-11/not-counted/)。

- **第一版规则的首次评测**：误拦了 `plan_ops_v2` 的 `same-name-harbor-id-en`（“… not the campus marker …”），按预先写定的判据未通过。

## 接手项

- **描述性指代**：「港区那边的屋顶区」「园区那帮人」这类说法，名称表管不到。
- **体积 id 进名称表**：要进一步覆盖，可以考虑把体积 id 加入名称表；这需要另行设计并重新评测。
- **人名、组织名的歧义**：别名只登记「X 项目」与站点字母。将来接入真实站点名时，要注意它们与普通名词、人名的歧义。

## 复现

```powershell
uv run python -m drone_agent.eval.reference_check --output outputs/reference-check.json
uv run pytest tests/fleet/test_references.py tests/eval/test_reference_check.py
uv run python scripts/desk_probe.py plan --origin <origin> --project fleet_s0 --robot uav_fb --text "Inspect the red equipment marker at site A." --output <dir>/plan-foreign.json
uv run python scripts/verify_reference_check.py --sha <full sha> --deployment ... --desk ... --desk-http ... --desk-spoof ... --desk-plan <campus> <fleet> --desk-foreign <foreign> --output docs/verification/reference-check-2026-10-11/release.json
```
