# P6 真实无人机派生影像的来源与准入

本实现继续执行用户“推进至关闭”的公开数据 S2 任务，补充来自真实无人机采集的公开正射影像。验收数量、独立性、误报 / 漏检、可判覆盖及跨次门槛不变。**派生窗口明确记为 `real_capture_derivative`，不冒充原始相机单帧；裁切、缩放、不同文件名及同日拼接版本都不增加资产或采集次数。** 当前仍未冻结正式数据或授予 WP-P6-01 准出。

## 来源及固定物理目标

CRASAR-U-DROIDs 固定源版本为 `c3f5421e1167bb7e88a765e3afa38bebd9898479`，只使用作者明确列为 UAS / sUAS 的部分，许可声明和原元数据另行绑定。有人机与卫星不混入该来源。原图来自作者的正射产品，原始帧未公开的事实保留；报告只评价该公开派生影像范围，不外推为实时原始单帧或现场效果。

- 先按世界几何合并重复、相邻链和近邻并行候选，随后每个保守道路组仅登记一个固定世界端点范围。来源 line id 或窗口 id 不直接成为独立资产结论。
- 目标范围在业务标注 / 模型结果之前按道路节点和共同视野登记；可以是一个明确路段，不要求整条街全部进入画面。同组不能因再取一个窗口而增加独立资产数，也不能因发现障碍再缩小范围。
- 源道路调整向量只作为中性定位提示。原生图像、真实有效区、整段宽度、两端点和物理身份仍需独立 AI 初标与另一 AI 核验；源灾损类别不作为业务真值。
- FloodNet / RescueNet 与该来源具有原始影像关联，按共同事件 / 地点保守隔离。本批主池排除已有开发关联的 Harvey / Michael，以及实现方已查看试提取的 Sanibel 图。

## 派生来源绑定

正式观察的 `derivation` 使用 `drone.p6-derived-capture-binding/v1`，包含下面每份文件的相对路径与 SHA-256：

| 绑定 | 内容 |
|---|---|
| `extraction_file` / `extraction_sha256` | 实际提取回执：输出图片与摘要、原生窗口、父图尺寸 / 仿射变换、alpha、固定目标与定位提示 |
| `extraction_plan_file` / `extraction_plan_sha256` | 提取代码 / 范围读取代码摘要、参数、先登记的候选计划摘要与运行时间 |
| `source_inventory_file` / `source_inventory_sha256` | 固定作者版本的 UAS 清单、公开对象大小 / LFS 摘要、平台、日期、源道路及调整文件摘要 |
| `range_manifest_file` / `range_manifest_sha256` | `drone.p6-raster-source-ranges/v1`：实际取得的源字节块、请求范围、HTTP Content-Range、字节数及摘要 |
| `replay_file` / `replay_sha256` | 实际纯缓存重提取：零网络请求、独立输出文件、原生 RGB / alpha / JPEG 摘要及执行时间 |

协议的 `derived_capture_policy` 明确模式、源清单摘要和采用的提取 / 范围读取代码摘要，禁止生成场景或声称原始单帧。核对时必须重新验证绑定文件、输出图片、原生窗口与变换、源 UAS 身份以及所有声明的字节块；不能仅信任 `extracted=true`。

审计还通过开发依赖中固定的 `rasterio` 从绑定的本机字节块重新解码指定窗口；缺块即失败，不发网络请求、不读取整幅父图。它直接复算原生像素和编码输出，不能用两份内容一致但未经源像素重建的 JSON 回执替代。目标范围的冻结时间必须早于首轮标注、最终标签及相关事前任务，不能在看到业务答案后改范围。

父图完整 LFS 摘要仅在全部字节实际齐全并重算后声明已验证。只按范围取得时明确报告 `whole_parent_sha256_verified=false`；保存取得的真实范围，并用相同缓存离线重放证明提取可重复，不填补未下载区域。原生 RGB 窗口可以按登记的 JPEG 参数编码，alpha 另存；不得生成、修补、删除或替换场景实体。辅助预览不增加观察数。

## OpenAerialMap 完整对象补充来源

普通道路候选可采用 OpenAerialMap 的逐对象 UAV 正射影像，独立使用 `drone.p6-oam-derived-capture-binding/v1` 和 `oam_capture_policy`。它不具备 CRASAR 的 Git / LFS、作者道路调整或重复采集证明，不能填造这些字段，也不能借用 CRASAR 来源校验结论。

- 绑定官方 `api.openaerialmap.org/meta/<id>` 原始 JSON 与摘要，逐对象核对 `platform=uav`、具体 sensor、RGB 三通道、原生尺寸 / CRS 及 `properties.license=CC-BY 4.0`。平台顶层通用许可不能覆盖对象自己的其他许可。
- 小型父图完整下载，保留实际 GET 200、`If-Match`、ETag、响应 URL、完整字节数及本机计算的 SHA-256。多段 ETag 不当作 SHA-256；本机摘要明确是 `locally_computed_full_object`，不是作者发布的摘要。来源模式只读取已取得的本地完整 TIFF，不伪造 HTTP 206 范围回执。
- OSM 只提供中性道路定位和保守分组，保留快照 / way 版本以及地图晚于影像的事实。固定世界目标先登记，按真实 TIFF 仿射变换投影；角度 CRS 不按米处理。使用 `georeferencing_only`，物理目标和边界仍须逐张核验。
- RGB 三通道通过 TIFF 内部 `dataset_mask` 取得有效区；未知掩膜不填成全有效。单独重提取与门禁解码均须复算 RGB、JPEG、mask 和仿射变换。解码器只允许已绑定 TIFF，不读取网络、外部 mask、VRT 或旁路文件。
- 来源采集时间原样保留为发布者元数据，`author_date_verified=false`、`repeated_capture_eligible=false`；本分支不计入正式跨次配对。一个父图 / 地点在校准与测试之间隔离，同名道路或同一路段的多个裁切不增加资产数。

该分支仅提供来源准入能力。没有经过独立标注、身份核验及正式冻结的道路组仍是候选；不能从道路名称或公开许可推导“正常”。

## OpenAerialMap 条件响应组合来源（独立 v1）

`drone.p6-oam-conditional-parts-http/v1` 是显式的新来源回执，不是完整单次 GET 200 回执。依据 [RFC 9110 §15.3.7.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.7.3)，同一资源、同一强验证器的已取得响应部分可以组合；只有完整覆盖才构成完整表示。旧 `drone.p6-oam-full-object-http/v1` 继续严格要求真实完整 200、无 Range / Content-Range，所有既有语义不变；不得把分段来源改记成旧格式的 `downloaded_and_verified`。

新回执记录 `status=assembled_and_verified`、官方逐对象元数据 URL / 摘要、`expected_etag`、`expected_bytes` / `body_bytes`、完整本地文件路径 / SHA-256、`checksum_origin=locally_computed_full_object`、`publisher_sha256=null` 和非空 `parts`。它不伪造一个汇总 HTTP 请求或响应。UAV、逐对象许可、sensor、RGB / uint8、有效尺寸 / CRS、发布者采集区间和官方对象 key 仍须验证；允许的两个官方 OIN 主机不因此扩大。

每条 part 明确 `kind=complete_206` 或 `incomplete_200_prefix`、严格整数 `offset` / `body_bytes`，以及原始 `http_record_file` / `http_record_sha256`、实际保存的 `body_file` / `body_sha256`。当前唯一支持的原始记录布局是 `flat_request_response_v1`：同一不可改写的 JSON 中保留 `method`、`request_url`、`request_headers`、真实 `status`、`final_url`、响应 `headers` 和起止时间；不是从组合结论反向补造的传输记录。

- 全部真实请求必须是 GET，`If-Match` 与响应 ETag 必须逐字等于同一个已知强 ETag；请求 `Accept-Encoding=identity`，响应不得有非 identity 内容编码；请求和最终 URL 都必须是发布对象的同一个 key。
- `complete_206` 仅接受原始 `complete=true`、真实整数状态 206。请求单一闭区间 Range、响应 Content-Range 的起止和总长、Content-Length、原记录 `range_start` / `range_end` / `expected_bytes` / `body_bytes`、保存文件长度和摘要全部一致。未知总长、后缀范围、多范围或缺字段均拒绝。
- `incomplete_200_prefix` 仅能从 offset 0 使用，且保存长度严格小于完整表示。原始状态保持 200、无 Range / Content-Range，响应 Content-Length 必须仍是完整表示大小；`outcome=incomplete_or_failed`、真实 `error`、`received_bytes` / `saved_bytes` / `saved_sha256` 和实际保存文件必须一致。不把被截断或主动停止的 200 记成完成响应。
- 按 offset 排序检查覆盖恰为 `[0, expected_bytes)`，不允许空段、越界或缺口。允许真实重复覆盖，但每段每个字节都重新与完整本地文件对应区间比较，因此重叠也必须逐字相同；完整文件 SHA-256 另行重算。重叠不增加独立字节、资产或观察数。
- `started_at_utc` / `acquisition_finished_at_utc` 分别等于各原始 HTTP 起点的最早值 / 终点的最晚值；`verification_started_at_utc` / `finished_at_utc` 单独记录之后的完整组合校验区间。原生提取只能发生在 `finished_at_utc` 之后，不能借较早的最后一段 HTTP 时间提前通过。布尔值不能充当整数；弱 / 缺失 ETag、跨对象、错误长度、摘要改动、重叠冲突或与原始记录不符的状态均拒绝。

新格式只建立完整公共源字节的组合来源。原始失败记录、请求 / 响应、每段 body 及其摘要原样绑定；不会创造发布者 checksum、精确拍摄 UTC 或 flight ID。原生 RGB / 内部 mask 校验、冻结世界范围、单独离线再提取、独立 AI 隔离、物理身份 / 资产独立性、校准 / 测试曝光边界、数量 / 质量及真实跨次门槛全部沿用；来源通过不代表资产准入或 WP-P6-01 关闭。

## 跨次派生观察

`capture_record` 引用 `drone.p6-derived-capture-inventory/v1` 中的具体观察。清单逐条绑定观察图片、来源父图、源日期、提取绑定，不把日期改为虚构 UTC。

派生跨次使用 `comparison_scope=distinct_author_dated_derived_captures_unordered`。除既有同资产、同目标、同状态、不同图片及独立复核外，还要求：

1. 两个父图对象摘要不同，作者采集日期组不同，来源表和作者日期代码一致，且没有待核日期标记。
2. 图像分别绑定实际提取及源字节证据。源表与代码冲突、仅文件序号不同、同日地图版本、同一父图重编码或不同裁切均不计真实跨次。
3. 独立 AI 配对记录仍绑定待评供应商 / 模型及逐对图像、目标和依据，不能用几何匹配自动填入已确认。

这一范围只证明作者来源支持的不同日期组及影像中可比状态，不声称已取得原始帧清单、精确飞行身份、时区、清障事件或现场效果。原始照片路径继续按原协议检查，不能以派生模式替旧素材绕过原门禁。

## 错目标与防泄漏

错目标采用先登记目标、后提供另一地点当前影像的任务错配用例，公开说明其构造方式，不生成或编辑虚假场景。预期参考和当前影像都保留真实来源；每个预期资产仅一个主观察。首轮 / 二审仍必须按原有事前任务、参考图检查及目标缺席证据判定。

正式拆分同时检查预期资产参考和实际影像的开发曝光，以及它们各自的资产、地点、采集和近重复关联。不能把开发图片藏进“参考图”后重新当作未曝光测试；派生图也必须继承其父图 / 地理关联的曝光边界。未达覆盖时继续保留缺口，禁止把未下载、仅元数据候选或未核验的资产填成完成。

同一拆分的主观察还按实际被拍资产与图片摘要去重：改写预期目标或文件名不能让同一个当前观察反复填充分母。跨次观察使用单列的 `repeat` 角色，仍须通过真实配对条件。
