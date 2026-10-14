# P6 道路整图与分块开发比较

本包是已曝光素材上的开发实验；[方案与结果](../../../docs/p6-road-views-development.md)管理解释边界。没有独立业务真值，不计 P6 准出。

- [config.json](config.json)：两组共用的 `road-region-views-dev-v2` 提示与模型参数。
- [plan.json](plan.json)：6 个目标、5 张原图；6 个整图视图与 18 个分块，24 次计划调用。包含源摘要、支持域覆盖与原图坐标变换。
- [input-manifest.json](input-manifest.json)：48 张实际输入 JPEG 的顺序、相对文件名与摘要；图片在仓库外。
- [准备与汇总工具](../../../scripts/p6_road_views.py)：只做离线处理，无模型或控制服务调用。

本机数据根为 `D:/drone-agent-data/p6-road-views-20261014/data/diagnostic/`，`inputs/` 存放输入，运行后 `cloud/output/` 保存云端录制。源文件仍在上一批数据目录中，未重新获取或改写。

复现准备时使用新的输出目录，不覆盖冻结输入：

```powershell
uv run python scripts/p6_road_views.py prepare --index eval/s2/p6_candidates_v3/input-index.json --data-root D:/drone-agent-data/p6-materials-20261013-round3 --base-config eval/s2/p6_candidates_v3/diagnostic/config.json --case case-d1b8866d1484 --case case-98c37eea9470 --case case-3796b34d624b --case case-8305aeb37905 --case case-b2816a2e7bb3 --case case-d382a1ce7ef4 --output <新的仓库外目录>
uv run pytest -q tests/test_p6_road_views.py
```

执行云端调用沿用 v3 [诊断器原始源码](../p6_candidates_v3/tool-source/round3/diagnostic_runner.py.txt)，SHA-256 为 `c3e0d56264b2ed22adb78347494a7f96089ddb81e2ea0261ca0adab0f8682536`；运行时代码仍使用已记录镜像 `6249129596b7cfe88c7322f8b425c7b432dba24b`，不能冒充本次 Git 提交的业务服务验证。

每个视图单独录制、严格回放；汇总前重新核对输入与录制摘要。重叠框保留来源，不自动合并为命中数。各分块全正常仍只覆盖已知源支持域，不证明完整资产边界、通行或清障结果。
