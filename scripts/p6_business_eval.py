"""Audit road benchmark inputs and compute explicitly scoped observation metrics.

核对道路业务评测输入，并计算明确限定到观察级的指标。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import runpy
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

from PIL import Image

LABELS = {"normal", "obstacle", "indeterminate", "wrong_target"}
VERDICTS = {"normal", "suspected", "refused"}
SPLITS = {"development", "calibration", "test"}
MINIMUM_KEYS = LABELS | {"stability_pairs", "sites"}
LIMIT_KEYS = {"false_alerts_per_100_normal_max", "typed_miss_rate_max", "determinable_coverage_min",
              "false_normal_on_refusable_max", "stable_and_covered_min", "review_p95_ms_max"}
EXTRA_KEYS = {"minimum_calibration_by_label", "minimum_test_by_defect_type", "maximum_test_site_fraction",
              "minimum_stability_assets", "minimum_timed_reviews", "required_review_coverage"}
SIMPLE_POLYGON = runpy.run_path(str(Path(__file__).with_name("p6_boundary_drafts.py")))["simple_polygon"]
VERIFY_RASTER_WINDOW = runpy.run_path(str(Path(__file__).with_name("p6_raster_replay.py")))["verify_window"]
VERIFY_FULL_RASTER_WINDOW = runpy.run_path(str(Path(__file__).with_name("p6_raster_replay.py")))["verify_full_window"]
VERIFY_OAM_CAPTURE = runpy.run_path(str(Path(__file__).with_name("p6_oam_capture.py")))["derived_capture"]
GROUP_KEYS = ("asset_id", "site_id", "capture_batch_id", "near_duplicate_group")
DERIVED_SCOPE = "distinct_author_dated_derived_captures_unordered"


def digest(path: Path) -> str:
    """Hash original bytes. / 对原字节求摘要。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path):
    """Reject duplicate keys and nonfinite values. / 拒绝重复键和非有限数值。"""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def constant(value):
        raise ValueError("nonfinite JSON value: " + value)

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def write(path: Path, value) -> None:
    """Keep each evidence output immutable. / 每份证据输出只创建一次。"""
    with path.open("xb") as stream:
        stream.write((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def within(root: Path, name: str) -> Path:
    """Read only explicit relative files within the data root. / 只读取数据根内明确的相对文件。"""
    relative = Path(name)
    target = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root.resolve()):
        raise ValueError("file outside data root")
    return target


def unique(rows: list[dict], key: str) -> dict:
    """Never silently overwrite duplicated observations. / 不静默覆盖重复观察。"""
    result = {}
    for row in rows:
        ident = row.get(key)
        if not isinstance(ident, str) or not ident or ident in result:
            raise ValueError("missing or duplicate " + key)
        result[ident] = row
    return result


def timestamp(value: str) -> datetime:
    """Require an explicit timezone for temporal claims. / 时序声明必须有明确时区。"""
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed


def polygon_valid(points) -> bool:
    """Check normalized nondegenerate target outlines; business review is separate.

检查归一化非退化目标轮廓，业务确认另行记录。
"""
    return SIMPLE_POLYGON(points)


def canonical_digest(value: dict) -> str:
    """Bind a suggestion independent of JSON formatting. / 不依赖 JSON 排版绑定建议。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def ai_suggestions(document: dict) -> dict:
    """Validate isolated AI drafts without creating human annotations. / 校验隔离 AI 草稿，不创建人工标注。"""
    if (document.get("format") != "drone.p6-road-ai-suggestions/v1" or document.get("author_kind") != "isolated_ai_annotator"
            or not document.get("author_id") or document.get("human_reviews_created") != 0
            or document.get("formal_release_eligible") is not False
            or document.get("isolation") != {"parent_history": "none", "evaluation_prompt_seen": False,
                                             "evaluation_outputs_seen": False, "source_labels_seen": False}):
        raise ValueError("AI draft independence or scope missing")
    timestamp(document["created_at"])
    rows = unique(document["rows"], "sample_id")
    fields = {"sample_id", "image_sha256", "target_brief", "target_polygon", "verdict", "objects", "notes",
              "uncertainties", "inspected_at", "inspection_files"}
    for row in rows.values():
        if (set(row) != fields or row["verdict"] not in {"normal", "obstacle", "indeterminate", "excluded_source"}
                or not isinstance(row["notes"], str) or not row["notes"].strip() or len(row["notes"]) > 4000
                or not isinstance(row["target_brief"], str) or len(row["target_brief"]) > 600):
            raise ValueError("invalid AI suggestion fields")
        timestamp(row["inspected_at"])
        if (row["target_polygon"] is not None and not polygon_valid(row["target_polygon"])) or (
                row["verdict"] in {"normal", "obstacle"} and (not polygon_valid(row["target_polygon"]) or not row["target_brief"].strip())):
            raise ValueError("invalid AI target geometry")
        if not isinstance(row["objects"], list) or len(row["objects"]) > 100 or ((row["verdict"] == "obstacle") != bool(row["objects"])):
            raise ValueError("AI obstacle evidence missing or inconsistent")
        for obj in row["objects"]:
            if (set(obj) != {"defect_type", "polygon"} or obj["defect_type"] not in {"debris", "vegetation", "other_entity"}
                    or not polygon_valid(obj["polygon"])):
                raise ValueError("invalid AI object geometry")
        for key in ("uncertainties", "inspection_files"):
            if not isinstance(row[key], list) or any(not isinstance(v, str) or not v.strip() for v in row[key]):
                raise ValueError("invalid AI inspection evidence")
        if not row["inspection_files"]:
            raise ValueError("AI original inspection evidence missing")
    return rows


def ai_context(context: dict, document: dict, rows: dict) -> None:
    """Check the orchestrator's separate assignment record. / 核对编排者的独立分工登记。"""
    author = context.get("annotators", {}).get(document["author_id"], {})
    if (context.get("format") != "drone.p6-independent-ai-context/v1" or not context.get("authorization_reference")
            or context.get("human_reviews_created") != 0 or context.get("formal_release_eligible") is not False
            or not author.get("task_name") or author.get("history_fork") != "none"
            or any(author.get(k) is not False for k in ("evaluation_prompt_provided", "evaluation_outputs_provided", "source_labels_provided"))
            or not author.get("provider") or author["provider"] == context.get("evaluated_provider_id")):
        raise ValueError("AI assignment is not independently isolated")
    if any(author.get("assigned_images", {}).get(i) != r["image_sha256"] for i, r in rows.items()):
        raise ValueError("AI draft is outside the registered assignment")


def delegated_ai_approved(protocol: dict) -> bool:
    """Recognize explicit AI delegation without calling it human truth. / 识别明确 AI 授权，不称为真人真值。"""
    policy = protocol.get("annotation_policy", {})
    return (policy.get("mode") == "independent_ai_verified_reference" and bool(policy.get("approval_reference"))
            and policy.get("human_adjudication_required") is False and policy.get("delegated_ai_adjudication_approved") is True
            and policy.get("evaluated_model_outputs_must_be_unseen") is True
            and all(isinstance(policy.get(k), str) and policy[k].strip() for k in ("evaluated_provider_id", "evaluated_model")))


def timing_deferred(protocol: dict) -> bool:
    """Only an explicit scope amendment can defer human efficiency. / 只有明确范围修订可以延期真人效率验证。"""
    policy = protocol.get("review_timing_policy", {})
    return (policy.get("mode") == "deferred_to_operations" and bool(policy.get("approval_reference"))
            and policy.get("human_efficiency_verified") is False
            and set(policy.get("retained_targets", [])) == {"review_p95_ms_max", "minimum_timed_reviews", "required_review_coverage"})


def bound_evidence(data_root: Path, file: str, expected: str, cache: dict):
    """Check bound review evidence including original absolute inspection paths.

核对绑定的复核证据，兼容原始检查记录中的绝对路径。
"""
    path = (data_root / file).resolve()
    if not path.is_relative_to(data_root.resolve()):
        raise ValueError("review evidence outside data root")
    key = (str(path), expected)
    if key not in cache:
        if digest(path) != expected:
            raise ValueError("review evidence bytes changed")
        cache[key] = read(path)
    return cache[key]


def sha256_value(value) -> bool:
    """Accept only complete SHA-256 identities. / 只接受完整 SHA-256 身份。"""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def derived_file(data_root: Path, file: str, expected: str, cache: dict) -> Path:
    """Recheck acquired bytes once per unchanged file inside one audit.

每次审计中，对数据根内未变化的实际文件只重算一次摘要。
"""
    path = (data_root / file).resolve()
    if not path.is_relative_to(data_root.resolve()) or ".." in Path(file).parts or not sha256_value(expected):
        raise ValueError("invalid derived evidence path or digest")
    stat = path.stat()
    key = ("derived_bytes", str(path), expected, stat.st_size, stat.st_mtime_ns)
    if key not in cache:
        if digest(path) != expected:
            raise ValueError("derived evidence bytes changed")
        cache[key] = True
    return path


def author_date_group(source: dict) -> str | None:
    """Use agreeing author dates without inventing a flight time.

只采用来源表与作者代码一致的日期，不虚构飞行时刻。
"""
    try:
        table = datetime.strptime(source["author_statistics"]["Date (mm/dd/yyy)"], "%m/%d/%Y").date()
        code = datetime.strptime(source["author_code_date_mm_dd_yyyy"], "%m-%d-%Y").date()
        if table == code and source.get("author_code_date_marked_double_check") is False:
            return table.isoformat()
    except (KeyError, TypeError, ValueError):
        pass
    return None


def verify_source_ranges(document: dict, source: dict, data_root: Path, cache: dict) -> None:
    """Verify every acquired range, its HTTP receipt and its exact parent identity.

重核全部已取得范围、HTTP 回执及准确父图身份，不把局部下载说成完整父图。
"""
    identity_keys = ("orthomosaic", "source_commit", "imagery_path", "imagery_url", "imagery_published_bytes",
                     "imagery_published_lfs_sha256", "author_statistics", "author_code_date_mm_dd_yyyy",
                     "author_code_date_marked_double_check")
    if (document.get("format") != "drone.p6-raster-source-ranges/v1"
            or any(document["source"].get(k) != source[k] for k in identity_keys)):
        raise ValueError("range manifest parent mismatch")
    key = ("verified_source_ranges", canonical_digest(document))
    if key in cache:
        return
    size, block = source["imagery_published_bytes"], document["block_bytes"]
    if type(size) is not int or size <= 0 or type(block) is not int or block <= 0:
        raise ValueError("invalid parent size or range block size")
    rows = document["ranges"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("no actual source ranges")
    previous, acquired, files, complete, parent_hash = -1, 0, set(), True, hashlib.sha256()
    for row in sorted(rows, key=lambda r: r["range"]):
        start, end = row["range"]
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end < size
                or start <= previous or start % block or end != min(start + block, size) - 1):
            raise ValueError("duplicate, overlapping or invalid source range")
        complete = complete and start == previous + 1
        previous = end
        path = derived_file(data_root, row["file"], row["sha256"], cache)
        if path in files or path.stat().st_size != end - start + 1:
            raise ValueError("source range length or file reuse mismatch")
        files.add(path)
        receipt_path = derived_file(data_root, row["receipt_file"], row["receipt_sha256"], cache)
        receipt = bound_evidence(data_root, str(receipt_path), row["receipt_sha256"], cache)
        if (receipt.get("url") != source["imagery_url"] or receipt.get("range") != [start, end]
                or receipt.get("total_bytes") != size or receipt.get("received_bytes") != end - start + 1
                or receipt.get("sha256") != row["sha256"] or receipt.get("http_status") != 206
                or receipt.get("content_range") != f"bytes {start}-{end}/{size}"
                or receipt.get("linked_etag_seen") is not True or receipt.get("repo_commit_seen") is not True):
            raise ValueError("source HTTP range receipt mismatch")
        timestamp(receipt["recorded_at"])
        acquired += end - start + 1
        if document.get("whole_parent_sha256_verified") is True:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    parent_hash.update(chunk)
    if document.get("cached_bytes_accessed") != acquired or type(document.get("whole_parent_sha256_verified")) is not bool:
        raise ValueError("source byte accounting or full-parent claim missing")
    if document["whole_parent_sha256_verified"] and (not complete or previous != size - 1
            or parent_hash.hexdigest() != source["imagery_published_lfs_sha256"]):
        raise ValueError("whole parent hash not actually verified")
    cache[key] = True


def verify_extracted_image(receipt: dict, data_root: Path, cache: dict, *, expected_bands: int = 4) -> None:
    """Check native window geometry, stored JPEG and separate alpha bytes.

核对原生窗口几何、已保存 JPEG 和单独 alpha 字节。
"""
    raster = receipt["raster"]
    width, height = receipt["width"], receipt["height"]
    x, y, w, h = receipt["native_window_xywh"]
    transform, window_transform = raster["transform"], receipt["window_transform"]
    if (any(type(v) is not int for v in (width, height, x, y, w, h, raster["width"], raster["height"]))
            or min(width, height, w, h) <= 0 or min(x, y) < 0 or [width, height] != [w, h]
            or x + w > raster["width"] or y + h > raster["height"] or expected_bands not in {3, 4} or raster.get("bands") != expected_bands
            or raster.get("dtypes") != ["uint8"] * expected_bands or not raster.get("crs")
            or len(transform) != 9 or len(window_transform) != 9
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in transform + window_transform)
            or transform[6:] != [0, 0, 1] or transform[0] * transform[4] == transform[1] * transform[3]
            or not sha256_value(receipt.get("native_rgb_sha256"))):
        raise ValueError("invalid native extraction window or raster")
    expected = list(transform)
    expected[2] += x * transform[0] + y * transform[1]
    expected[5] += x * transform[3] + y * transform[4]
    if any(not math.isclose(a, b, rel_tol=0, abs_tol=1e-8) for a, b in zip(expected, window_transform, strict=True)):
        raise ValueError("window affine transform mismatch")
    image_path = derived_file(data_root, receipt["image_file"], receipt["image_sha256"], cache)
    mask_path = derived_file(data_root, receipt["mask_file"], receipt["mask_sha256"], cache)
    for path, image_format, mode in ((image_path, "JPEG", "RGB"), (mask_path, "PNG", "L")):
        with Image.open(path) as image:
            if image.format != image_format or image.mode != mode or image.size != (w, h):
                raise ValueError("extraction image or alpha dimensions/format mismatch")
            image.verify()
    with Image.open(image_path) as image:
        image.load()
    with Image.open(mask_path) as alpha:
        if not math.isclose(1 - alpha.histogram()[0] / (w * h), receipt["valid_pixel_fraction"], abs_tol=1e-9):
            raise ValueError("alpha coverage differs from extraction receipt")


def verify_extraction_replay(replay: dict, receipt: dict, binding: dict, policy: dict, data_root: Path, cache: dict) -> None:
    """Require a separately persisted cache-only extraction with identical native/output bytes.

要求单独保存的纯缓存重提取，原生像素与输出字节均须一致。
"""
    expected = {"format": "drone.p6-derived-capture-replay/v1", "mode": "offline_cached_ranges", "network_requests": 0,
                "source_observation_id": receipt["sample_id"], "parent_sha256": receipt["source_published_lfs_sha256"],
                "extraction_sha256": binding["extraction_sha256"], "range_manifest_sha256": binding["range_manifest_sha256"],
                "extractor_sha256": policy["extractor_sha256"], "range_tool_sha256": policy["range_tool_sha256"]}
    if (any(replay.get(k) != v for k, v in expected.items()) or type(replay.get("network_requests")) is not int
            or timestamp(replay["started_at"]) < timestamp(receipt["finished_at"])
            or timestamp(replay["finished_at"]) < timestamp(replay["started_at"])):
        raise ValueError("offline replay identity, isolation or timing mismatch")
    reproduced = bound_evidence(data_root, replay["replay_extraction_file"], replay["replay_extraction_sha256"], cache)
    keys = ("sample_id", "asset_group_id", "bounded_target_id", "source_orthomosaic", "source_commit",
            "source_published_lfs_sha256", "media_origin", "status", "world_scope", "raster", "native_window_xywh",
            "window_transform", "width", "height", "image_sha256", "mask_sha256", "native_rgb_sha256", "valid_pixel_fraction",
            "source_geometry_sha256", "source_alignment_sha256", "source_date", "source_date_double_check_flag")
    if (any(reproduced.get(k) != receipt[k] for k in keys)
            or timestamp(reproduced["started_at"]) < timestamp(replay["started_at"])
            or timestamp(reproduced["finished_at"]) > timestamp(replay["finished_at"])
            or timestamp(reproduced["finished_at"]) < timestamp(reproduced["started_at"])
            or any((data_root / reproduced[k]).resolve() == (data_root / receipt[k]).resolve() for k in ("image_file", "mask_file"))):
        raise ValueError("offline replay does not reproduce this extraction")
    verify_extracted_image(reproduced, data_root, cache)


def derived_capture(sample: dict, protocol: dict, data_root: Path, cache: dict, *, scope_before: datetime | None = None) -> dict:
    """Admit only a bound, acquired UAS derivative; return its reverified capture row.

只准入绑定了实际 UAS 字节与提取产物的派生图，返回重新核对的采集条目。
"""
    if sample["derivation"].get("format") == "drone.p6-oam-derived-capture-binding/v1":
        return VERIFY_OAM_CAPTURE(sample, protocol, data_root, cache, scope_before=scope_before,
                                  helpers={"read_bound": bound_evidence, "file_bound": derived_file, "within": within,
                                           "timestamp": timestamp, "canonical": canonical_digest, "unique": unique,
                                           "sha256_value": sha256_value, "image": verify_extracted_image,
                                           "pixels": VERIFY_FULL_RASTER_WINDOW, "group_keys": GROUP_KEYS})
    binding, policy = sample["derivation"], protocol["derived_capture_policy"]
    if (binding.get("format") != "drone.p6-derived-capture-binding/v1"
            or policy.get("mode") != "traceable_uas_orthomosaic" or policy.get("claim_raw_camera_frames") is not False
            or policy.get("synthetic_scene_content_allowed") is not False
            or sample.get("captured_at") is not None
            or binding["source_inventory_sha256"] != policy.get("source_inventory_sha256")):
        raise ValueError("derived capture policy or binding missing")
    bound = {}
    for name in ("extraction", "extraction_plan", "source_inventory", "range_manifest", "replay"):
        path = within(data_root, binding[name + "_file"])
        bound[name] = bound_evidence(data_root, str(path), binding[name + "_sha256"], cache)
    receipt, plan = bound["extraction"], bound["extraction_plan"]
    inventory = unique(bound["source_inventory"], "orthomosaic")
    source = inventory[receipt["source_orthomosaic"]]
    if (receipt.get("status") != "extracted" or receipt.get("media_origin") != "real_capture_derivative"
            or receipt["source_commit"] != source["source_commit"]
            or receipt["source_published_lfs_sha256"] != source["imagery_published_lfs_sha256"]
            or not sha256_value(source["imagery_published_lfs_sha256"])
            or source["author_statistics"].get("Source") not in {"UAS", "sUAS"}
            or source["author_statistics"].get("Orthomosaic") != source["orthomosaic"]
            or not source["author_statistics"].get("Platform / Provider")
            or "/imagery/UAS/" not in source["imagery_path"]
            or f"/resolve/{source['source_commit']}/{source['imagery_path']}" not in source["imagery_url"]
            or plan.get("tool_sha256") != policy.get("extractor_sha256")
            or plan.get("range_tool_sha256") != policy.get("range_tool_sha256")
            or any(not sha256_value(policy.get(k)) for k in ("extractor_sha256", "range_tool_sha256"))
            or receipt["source_orthomosaic"] not in plan["source_names"]
            or timestamp(receipt["finished_at"]) < timestamp(receipt["started_at"])
            or timestamp(receipt["started_at"]) < timestamp(plan["created_at"])):
        raise ValueError("derived capture source or extraction tool mismatch")
    source_plan = bound_evidence(data_root, plan["source_plan_file"], plan["source_plan_sha256"], cache)
    planned_source = unique(source_plan["images"], "orthomosaic")[source["orthomosaic"]]
    planned = unique(planned_source["items"], "extraction_item_id")[receipt["sample_id"]]
    scope = receipt["world_scope"]
    if (planned_source["imagery_lfs_sha256"] != source["imagery_published_lfs_sha256"]
            or planned_source["imagery_url"] != source["imagery_url"] or planned["target"] != scope
            or any(planned[k] != receipt[k] for k in ("asset_group_id", "bounded_target_id"))
            or scope.get("frozen_before_business_annotation_and_model_output") is not True
            or scope.get("source_condition_polygons_or_model_outputs_used_for_scope") is not False
            or not sha256_value(scope.get("freeze_scope_sha256"))
            or timestamp(scope["frozen_at_utc"]) > timestamp(plan["created_at"])
            or scope_before is not None and timestamp(scope["frozen_at_utc"]) > scope_before
            or receipt["source_geometry_sha256"] != source["rda_json_sha256"]
            or receipt["source_alignment_sha256"] != planned["view"]["source_alignment_sha256"]
            or receipt["source_date"] != source["author_statistics"]["Date (mm/dd/yyy)"]
            or receipt["source_date_double_check_flag"] is not source["author_code_date_marked_double_check"]):
        raise ValueError("extraction does not bind the preregistered physical target")
    for name in ("source_geometry", "source_alignment"):
        derived_file(data_root, receipt[name + "_file"], receipt[name + "_sha256"], cache)
    verify_source_ranges(bound["range_manifest"], source, data_root, cache)
    verify_extracted_image(receipt, data_root, cache)
    VERIFY_RASTER_WINDOW(receipt, bound["range_manifest"], data_root, cache, binding["range_manifest_sha256"])
    verify_extraction_replay(bound["replay"], receipt, binding, policy, data_root, cache)
    if (receipt["image_sha256"] != sample["image_sha256"]
            or derived_file(data_root, receipt["image_file"], receipt["image_sha256"], cache) != within(data_root, sample["image_file"])
            or [receipt["width"], receipt["height"]] != [sample["width"], sample["height"]]):
        raise ValueError("extraction output is not this observation")
    reference = sample["capture_record"]
    capture_inventory = bound_evidence(data_root, str(within(data_root, reference["inventory_file"])), reference["inventory_sha256"], cache)
    if capture_inventory.get("format") != "drone.p6-derived-capture-inventory/v1":
        raise ValueError("unsupported derived capture inventory")
    row = unique(capture_inventory["rows"], "source_observation_id")[reference["source_observation_id"]]
    date = author_date_group(source)
    expected = {"source_observation_id": receipt["sample_id"], "source_id": sample["source_id"],
                "image_sha256": sample["image_sha256"], "source_orthomosaic": source["orthomosaic"],
                "source_commit": source["source_commit"], "parent_sha256": source["imagery_published_lfs_sha256"],
                "author_date_group": date, "author_date_verified": date is not None,
                "observed": sample["observed"], "derivation_sha256": canonical_digest(binding)}
    if (any(row.get(k) != v for k, v in expected.items())
            or any(reference.get(k) != expected[k] for k in ("source_observation_id", "parent_sha256", "author_date_group", "author_date_verified"))
            or sample["observed"].get("asset_id") != receipt["asset_group_id"]
            or any(not isinstance(sample["observed"].get(k), str) or not sample["observed"][k] for k in GROUP_KEYS)
            or type(sample["observed"].get("development_exposed")) is not bool):
        raise ValueError("derived capture inventory does not bind this observation")
    return row


def first_annotation_at(sample: dict, label: dict, data_root: Path, cache: dict) -> datetime:
    """Use actual bound first-pass and final annotation times, never a claimed scope flag.

使用真实绑定的初标与最终标注时间，不以范围声明布尔值代替。
"""
    moments = [timestamp(label["recorded_at"])]
    assistance = label.get("ai_assistance", {})
    if assistance or sample.get("ai_suggestion") or label.get("origin") == "delegated_ai":
        draft = bound_evidence(data_root, assistance["draft_file"], assistance["draft_sha256"], cache)
        first = unique(draft["rows"], "sample_id")[sample["sample_id"]]
        if first["image_sha256"] != sample["image_sha256"]:
            raise ValueError("first annotation belongs to another observation")
        moments.append(timestamp(first["inspected_at"]))
    return min(moments)


def verified_prior_task(sample: dict, label: dict, protocol: dict, data_root: Path, cache: dict) -> dict:
    """Require a prior task and an inspected real reference even for human derivative labels.

派生人工标签同样必须有事前任务及实际检查的真实参考。
"""
    assistance = label.get("ai_assistance", {})
    evidence = assistance if "prior_task_file" in assistance or "prior_task_sha256" in assistance else label["task_evidence"]
    prior_sha256 = evidence["prior_task_sha256"]
    task = bound_evidence(data_root, evidence["prior_task_file"], evidence["prior_task_sha256"], cache)
    expected = task["expected_reference"]
    first_at = first_annotation_at(sample, label, data_root, cache)
    if (task.get("format") != "drone.p6-road-prior-task/v1" or task.get("origin") not in {"human_task", "delegated_ai_task"}
            or not task.get("registrar_id") or not task.get("registration_evidence")
            or task.get("sample_id") != sample["sample_id"] or task.get("image_sha256") != sample["image_sha256"]
            or not task.get("expected_asset_id") or task["expected_asset_id"] != sample["asset_id"]
            or expected.get("asset_id") != task["expected_asset_id"] or expected.get("image_sha256") == sample["image_sha256"]
            or not task.get("target_brief") or task["target_brief"] != sample.get("target", {}).get("brief")
            or not polygon_valid(task.get("target_polygon")) or task["target_polygon"] != sample.get("target", {}).get("polygon")
            or not expected.get("source_reference") or not expected.get("target_brief")
            or not polygon_valid(expected.get("target_polygon")) or timestamp(task["registered_at"]) > first_at):
        raise ValueError("prior task identity, target geometry or registration time mismatch")
    if expected.get("media_origin") == "real_capture_derivative":
        derived_capture(expected, protocol, data_root, cache, scope_before=min(timestamp(task["registered_at"]), first_at))
        if expected["asset_id"] != expected["observed"]["asset_id"]:
            raise ValueError("expected reference observes another asset")
    elif expected.get("media_origin") != "real_capture" or expected.get("derivation"):
        raise ValueError("expected reference is not an original or verified derivative")
    expected_path = derived_file(data_root, expected["image_file"], expected["image_sha256"], cache)
    with Image.open(expected_path) as image:
        image.verify()
    if label.get("origin") == "human":
        evidence = label["task_evidence"]
        if (evidence.get("prior_task_sha256") != prior_sha256
                or evidence.get("expected_reference_checked") is not True or evidence.get("expected_target_present") is not False
                or not isinstance(evidence.get("evidence"), str) or not evidence["evidence"].strip()):
            raise ValueError("human wrong-target reference inspection missing")
        inspected = {(data_root / file).resolve(): sha for file, sha in evidence["inspection_file_sha256"].items()}
        if (inspected.get(expected_path) != expected["image_sha256"]
                or inspected.get((data_root / sample["image_file"]).resolve()) != sample["image_sha256"]):
            raise ValueError("human wrong-target original and reference were not both inspected")
        for file, sha in evidence["inspection_file_sha256"].items():
            derived_file(data_root, file, sha, cache)
    return expected


def wrong_target_review_issues(sample: dict, original: dict, final: dict, source: dict, record: dict,
                              assistance: dict, data_root: Path, cache: dict, protocol: dict | None = None) -> list[str]:
    """Require a prior task and an inspected expected-asset reference for wrong-target truth.

错目标裁定必须有先登记任务及实际检查过的预期资产参考，不把无道路当错目标。
"""
    try:
        expected_sha = assistance["prior_task_sha256"]
        task = bound_evidence(data_root, assistance["prior_task_file"], expected_sha, cache)
        mismatch = record["task_mismatch"]
        reference = task["expected_reference"]
        reference_path = (data_root / reference["image_file"]).resolve()
        if (task.get("format") != "drone.p6-road-prior-task/v1" or task.get("origin") not in {"human_task", "delegated_ai_task"}
                or not task.get("registrar_id") or not task.get("registration_evidence")
                or task.get("sample_id") != sample["sample_id"] or task.get("image_sha256") != sample["image_sha256"]
                or not task.get("expected_asset_id") or task["expected_asset_id"] != sample.get("asset_id")
                or source.get("prior_task_sha256") != expected_sha or mismatch.get("prior_task_sha256") != expected_sha
                or mismatch.get("expected_target_present") is not False or mismatch.get("expected_reference_checked") is not True
                or not isinstance(mismatch.get("evidence"), str) or not mismatch["evidence"].strip()
                or any(task.get(k) != final[k] for k in ("target_brief", "target_polygon"))
                or not polygon_valid(task.get("target_polygon"))
                or timestamp(task["registered_at"]) > timestamp(original["inspected_at"])):
            return ["wrong_target_prior_task_or_mismatch_missing"]
        if (reference.get("asset_id") != task["expected_asset_id"] or reference.get("media_origin") not in {"real_capture", "real_capture_derivative"}
                or not reference.get("source_reference") or not reference_path.is_relative_to(data_root.resolve())
                or reference.get("image_sha256") == sample["image_sha256"]
                or digest(reference_path) != reference.get("image_sha256")
                or not polygon_valid(reference.get("target_polygon")) or not reference.get("target_brief")):
            return ["wrong_target_expected_reference_invalid"]
        if reference["media_origin"] == "real_capture_derivative":
            derived_capture(reference, protocol or {}, data_root, cache,
                            scope_before=min(timestamp(task["registered_at"]), timestamp(original["inspected_at"])))
        inspected = {(data_root / name).resolve(): expected for name, expected in record["inspection_file_sha256"].items()}
        if inspected.get(reference_path) != reference["image_sha256"]:
            return ["wrong_target_expected_reference_not_inspected"]
        with Image.open(reference_path) as picture:
            picture.verify()
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return ["wrong_target_evidence_missing_or_invalid"]
    return []


def delegated_label_issues(sample: dict, label: dict, protocol: dict, data_root: Path, cache: dict) -> list[str]:
    """Bind each AI adjudication to its separate review, first draft and assignment.

将每条 AI 裁定绑定到独立二审、初稿和分工，不制造人类记录。
"""
    try:
        a, policy = label["ai_assistance"], protocol.get("annotation_policy", {})
        if (not delegated_ai_approved(protocol) or label.get("origin") != "delegated_ai"
                or a.get("method") != "independent_ai_then_ai" or label.get("model_outputs_seen") is not True
                or label.get("hints_seen") is not True or a.get("original_checked") is not True
                or a.get("evaluated_model_outputs_seen") is not False
                or a.get("context_sha256") != policy.get("independent_context_sha256")
                or a.get("verification_context_sha256") != policy.get("verification_context_sha256")):
            return ["delegated_annotation_policy_or_exposure_invalid"]
        def bound(file, expected):
            return bound_evidence(data_root, file, expected, cache)
        document = bound(a["draft_file"], a["draft_sha256"])
        first_rows = ai_suggestions(document)
        context = bound(a["context_file"], a["context_sha256"])
        ai_context(context, document, first_rows)
        if any(context.get(k) != policy.get(k) for k in ("evaluated_provider_id", "evaluated_model")):
            return ["delegated_annotation_evaluated_model_mismatch"]
        original = first_rows[sample["sample_id"]]
        registration = bound(a["verification_context_file"], a["verification_context_sha256"])
        review = bound(a["review_file"], a["review_sha256"])
        isolation = {"parent_history": "none", "evaluation_prompt_seen": False, "evaluation_outputs_seen": False,
                     "source_labels_seen": False, "assigned_first_pass_seen": True}
        if (registration.get("format") != "drone.p6-ai-review-registration/v1" or not registration.get("approval_reference")
                or registration.get("method") != "independent_ai_draft_then_separate_ai_verification"
                or not registration.get("provider") or registration["provider"] == policy.get("evaluated_provider_id")
                or registration.get("human_reviews_created") != 0 or registration.get("formal_release_eligible") is not False
                or review.get("format") != "drone.p6-road-ai-verification/v1" or review.get("reviewer_kind") != "isolated_ai_reviewer"
                or not review.get("reviewer_id") or review["reviewer_id"] == document["author_id"]
                or review.get("isolation") != isolation or review.get("human_reviews_created") != 0
                or review.get("formal_release_eligible") is not False):
            return ["delegated_annotation_independence_missing"]
        registered = unique(registration["assignments"], "reviewer_id")[review["reviewer_id"]]
        if registered["sha256"] != review["assignment_sha256"]:
            return ["delegated_annotation_assignment_mismatch"]
        assignment = bound(registered["file"], registered["sha256"])
        if (assignment.get("format") != "drone.p6-ai-review-assignment/v1" or assignment.get("parent_history") != "none"
                or assignment.get("reviewer_id") != review["reviewer_id"]
                or any(assignment.get(k) != "forbidden" for k in ("evaluation_prompt_access", "evaluation_outputs_access", "source_class_label_access"))):
            return ["delegated_annotation_assignment_mismatch"]
        source = unique(assignment["samples"], "sample_id")[sample["sample_id"]]
        record = unique(review["rows"], "sample_id")[sample["sample_id"]]
        final = record["final_row"]
        if (source["image_sha256"] != sample["image_sha256"] or original["image_sha256"] != sample["image_sha256"]
                or source["first_author_id"] != document["author_id"] or a.get("author_id") != document["author_id"]
                or source["first_row"] != original or record["first_row_sha256"] != canonical_digest(original)
                or a.get("suggestion_sha256") != canonical_digest(original)
                or record["image_sha256"] != sample["image_sha256"] or record["reviewer_id"] != review["reviewer_id"]
                or record["first_author_id"] != document["author_id"] or record.get("original_image_checked") is not True
                or record.get("decision") not in {"accepted", "revised"} or record.get("decision") != a.get("decision")
                or not isinstance(record.get("findings"), list) or not record["findings"]
                or any(not isinstance(v, str) or not v.strip() for v in record["findings"])
                or canonical_digest(final) != record["final_row_sha256"]
                or canonical_digest(record) != a.get("review_row_sha256")):
            return ["delegated_annotation_source_or_review_mismatch"]
        changes = record["changes"]
        if (not isinstance(changes, dict) or set(changes) - {"target_brief", "target_polygon", "verdict", "objects", "notes", "uncertainties"}
                or (record["decision"] == "revised") != bool(changes)
                or (changes and all(original[k] == v for k, v in changes.items()))):
            return ["delegated_annotation_decision_mismatch"]
        expected_final = {**original, **changes, "inspected_at": record["reviewed_at"], "inspection_files": record["inspected_files"]}
        if final != expected_final or timestamp(record["reviewed_at"]) < max(timestamp(original["inspected_at"]), timestamp(assignment["created_at"]), timestamp(review["created_at"])):
            return ["delegated_annotation_revision_or_time_mismatch"]
        final_for_shape = final
        if final["verdict"] == "wrong_target":
            problems = wrong_target_review_issues(sample, original, final, source, record, a, data_root, cache, protocol)
            if problems:
                return problems
            final_for_shape = {**final, "verdict": "indeterminate"}
        ai_suggestions({**document, "rows": [final_for_shape]})
        paths = [Path(p).resolve() for p in record["inspected_files"]]
        if Path(source["image_file"]).resolve() not in paths or set(record["inspection_file_sha256"]) != set(record["inspected_files"]):
            return ["delegated_annotation_original_inspection_missing"]
        if digest((data_root / source["image_file"]).resolve()) != sample["image_sha256"]:
            return ["delegated_annotation_inspection_bytes_changed"]
        for file, expected in record["inspection_file_sha256"].items():
            path = (data_root / file).resolve()
            if not path.is_relative_to(data_root.resolve()) or digest(path) != expected:
                return ["delegated_annotation_inspection_bytes_changed"]
        if (any(label.get(k) != final[k] for k in ("sample_id", "image_sha256", "verdict", "objects", "notes"))
                or label.get("reviewer_id") != review["reviewer_id"] or label.get("recorded_at") != record["reviewed_at"]
                or label.get("full_target_inspected") is not True
                or sample.get("target", {}).get("polygon") != final["target_polygon"]
                or sample.get("target", {}).get("brief") != final["target_brief"]
                or set(label.get("defect_types", [])) != {o["defect_type"] for o in final["objects"]}):
            return ["delegated_annotation_final_label_mismatch"]
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return ["delegated_annotation_evidence_missing_or_invalid"]
    return []


def assisted_label_issues(sample: dict, label: dict, protocol: dict, data_root: Path, cache: dict) -> list[str]:
    """Require original drafts and actual operator export for assisted truth. / 辅助真值必须绑定原始草稿和操作员导出。"""
    if label.get("origin") == "delegated_ai":
        return delegated_label_issues(sample, label, protocol, data_root, cache)
    def bound(file, expected):
        key = (file, expected)
        if key not in cache:
            path = within(data_root, file)
            if digest(path) != expected:
                raise ValueError("assistance evidence bytes changed")
            cache[key] = read(path)
        return cache[key]

    try:
        a, policy = label["ai_assistance"], protocol.get("annotation_policy", {})
        if (policy.get("mode") != "blind_or_independent_ai_human_adjudicated" or not policy.get("approval_reference")
                or policy.get("human_adjudication_required") is not True or policy.get("evaluated_model_outputs_must_be_unseen") is not True
                or a.get("method") != "independent_ai_then_human" or a.get("context_sha256") != policy.get("independent_context_sha256")
                or label.get("model_outputs_seen") is not True or label.get("hints_seen") is not True
                or type(a.get("evaluated_model_outputs_seen")) is not bool or a.get("original_checked") is not True
                or (sample.get("split") != "development" and a["evaluated_model_outputs_seen"] is not False)):
            return ["assisted_annotation_policy_or_exposure_invalid"]
        document = bound(a["draft_file"], a["draft_sha256"])
        rows = ai_suggestions(document)
        context = bound(a["context_file"], a["context_sha256"])
        ai_context(context, document, rows)
        if any(context.get(k) != policy.get(k) for k in ("evaluated_provider_id", "evaluated_model")):
            return ["assisted_annotation_evaluated_model_mismatch"]
        original = rows[sample["sample_id"]]
        if (original["image_sha256"] != sample["image_sha256"] or canonical_digest(original) != a["suggestion_sha256"]
                or document["author_id"] != a["author_id"] or a.get("decision") not in {"accepted", "revised", "rejected"}):
            return ["assisted_annotation_source_mismatch"]
        export = bound(a["operator_export_file"], a["operator_export_sha256"])
        record = unique(export["records"], "sample_id")[sample["sample_id"]]
        if (export.get("format") != "drone.p6-road-operator-labels/v1" or export.get("qa_fixture") is not False
                or export.get("formal_release_eligible") is not False or export.get("corpus_id") != protocol["corpus_id"]
                or record.get("origin") != "operator_entered" or record.get("phase") != "annotation"
                or record.get("ai_draft_sha256") != a["suggestion_sha256"] or record.get("ai_draft_decision") != a["decision"]
                or record.get("ai_original_checked") is not True or record.get("model_outputs_seen") is not True
                or record.get("evaluated_model_outputs_seen") is not a["evaluated_model_outputs_seen"]
                or type(record.get("source_labels_seen")) is not bool
                or any(record.get(k) != label.get(k) for k in ("sample_id", "image_sha256", "reviewer_id", "verdict", "objects", "notes", "full_target_inspected"))
                or record.get("completed_at") != label.get("recorded_at")
                or record.get("target_polygon") != sample.get("target", {}).get("polygon")
                or record.get("target_brief") != sample.get("target", {}).get("brief")):
            return ["assisted_annotation_operator_export_mismatch"]
        elapsed = (timestamp(record["completed_at"]) - timestamp(record["started_at"])).total_seconds() * 1000
        if (type(record.get("active_ms")) is not int or not 0 < record["active_ms"] <= elapsed + 1000
                or elapsed <= 0 or timestamp(original["inspected_at"]) > timestamp(record["started_at"])):
            return ["assisted_annotation_human_timing_invalid"]
        if record["verdict"] in {"normal", "obstacle"} and (record.get("target_confirmed") is not True
                or not timestamp(record["started_at"]) <= timestamp(record["target_registered_at"]) <= timestamp(record["completed_at"])):
            return ["assisted_annotation_target_confirmation_missing"]
        same = all(record.get(k) == original[k] for k in ("target_brief", "target_polygon", "verdict", "objects", "notes"))
        if (a["decision"] == "accepted" and not same) or (a["decision"] == "revised" and same):
            return ["assisted_annotation_decision_mismatch"]
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return ["assisted_annotation_evidence_missing_or_invalid"]
    return []


def delegated_pair_issues(pair: dict, samples: dict, protocol: dict, data_root: Path, cache: dict) -> list[str]:
    """Require a separately registered visual review for an AI-verified capture pair.

AI 跨次配对必须有单独登记的逐对视觉核验。
"""
    try:
        reference, policy = pair["ai_verification"], protocol.get("annotation_policy", {})
        if not delegated_ai_approved(protocol) or reference["context_sha256"] != policy.get("pair_context_sha256"):
            return ["delegated_pair_policy_missing"]
        context = bound_evidence(data_root, reference["context_file"], reference["context_sha256"], cache)
        document = bound_evidence(data_root, reference["file"], reference["sha256"], cache)
        if (context.get("format") != "drone.p6-ai-pair-context/v1" or not context.get("approval_reference")
                or any(context.get(k) != policy.get(k) for k in ("evaluated_provider_id", "evaluated_model"))
                or document.get("format") != "drone.p6-ai-pair-verification/v1"
                or document.get("origin") != "delegated_ai" or not document.get("reviewer_id")
                or document.get("isolation") != {"parent_history": "none", "evaluation_prompt_seen": False,
                                                "evaluation_outputs_seen": False, "source_labels_seen": False}):
            return ["delegated_pair_independence_missing"]
        row = unique(document["rows"], "pair_id")[reference["pair_id"]]
        registered = context["reviewers"][document["reviewer_id"]]
        if (registered.get("history_fork") != "none" or not registered.get("provider")
                or registered["provider"] == policy.get("evaluated_provider_id")
                or any(registered.get(k) is not False for k in ("evaluation_prompt_provided", "evaluation_outputs_provided", "source_labels_provided"))
                or row["sample_ids"] != pair["sample_ids"] or row["sample_ids"] != registered["pairs"][reference["pair_id"]]
                or canonical_digest(row) != reference["row_sha256"] or row.get("same_asset") is not True
                or row.get("same_condition") is not True or row.get("distinct_captures") is not True
                or row.get("asset_id") != samples[row["sample_ids"][0]].get("asset_id")
                or document["reviewer_id"] != pair.get("reviewer_id") or not row.get("evidence")):
            return ["delegated_pair_review_mismatch"]
        timestamp(row["reviewed_at"])
        for ident in pair["sample_ids"]:
            sample = samples[ident]
            observed = row["images"][ident]
            image_path = (data_root / observed["file"]).resolve()
            if (observed.get("image_sha256") != sample["image_sha256"] or observed.get("original_checked") is not True
                    or not image_path.is_relative_to(data_root.resolve()) or digest(image_path) != sample["image_sha256"]
                    or observed.get("target_polygon") != sample.get("target", {}).get("polygon")):
                return ["delegated_pair_original_mismatch"]
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return ["delegated_pair_evidence_missing_or_invalid"]
    return []


def pair_problems(pair: dict, samples: dict, labels: dict) -> list[str]:
    """Require distinct real captures of the same state, with explicit temporal scope.

要求同一已确认状态的不同实拍，并明确是否声明时间顺序。
"""
    ids = pair.get("sample_ids", [])
    if len(ids) != 2 or ids[0] == ids[1] or any(i not in samples for i in ids):
        return ["invalid_stability_pair_identity"]
    left, right = (samples[i] for i in ids)
    issues = []
    delegated = pair.get("origin") == "delegated_ai"
    scope = pair.get("comparison_scope", "ordered_capture_times")
    media_origin = "real_capture_derivative" if scope == DERIVED_SCOPE else "real_capture"
    condition_flag = "same_condition_ai_verified" if delegated else "same_condition_human_confirmed"
    capture_flag = "distinct_captures_ai_verified" if delegated else "distinct_captures_human_confirmed"
    if (not left.get("asset_id") or left["asset_id"] != right.get("asset_id") or left["split"] != right["split"]
            or left["image_sha256"] == right["image_sha256"] or pair.get(condition_flag) is not True
            or not pair.get("reviewer_id") or not pair.get("evidence")
            or any(s.get("media_origin") != media_origin for s in (left, right))):
        issues.append("stability_pair_not_verified")
    if scope == DERIVED_SCOPE:
        records = [s.get("capture_record") or {} for s in (left, right)]
        dates = [r.get("author_date_group") for r in records]
        if (pair.get(capture_flag) is not True or dates[0] == dates[1]
                or records[0].get("parent_sha256") == records[1].get("parent_sha256")
                or any(r.get("author_date_verified") is not True or not sha256_value(r.get("parent_sha256"))
                       or not r.get("source_observation_id") or not r.get("inventory_file")
                       or not sha256_value(r.get("inventory_sha256")) for r in records)
                or any(not isinstance(d, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) is None for d in dates)
                or any(s.get("observed", {}).get("asset_id") != s["asset_id"] for s in (left, right))):
            issues.append("distinct_derived_capture_identity_missing")
        try:
            for value in dates:
                datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError):
            if "distinct_derived_capture_identity_missing" not in issues:
                issues.append("distinct_derived_capture_identity_missing")
    elif scope == "distinct_original_captures_unordered":
        records = [s.get("capture_record") or {} for s in (left, right)]
        identities = [(s.get("source_id"), r.get("source_observation_id")) for s, r in zip((left, right), records, strict=True)]
        if (pair.get(capture_flag) is not True or identities[0] == identities[1]
                or any(not a or not b for a, b in identities)
                or any(not r.get("inventory_file") or not r.get("inventory_sha256") for r in records)):
            issues.append("distinct_original_capture_identity_missing")
    elif scope == "ordered_capture_times":
        try:
            if timestamp(right["captured_at"]) <= timestamp(left["captured_at"]):
                issues.append("stability_capture_order_invalid")
        except (KeyError, TypeError, ValueError):
            issues.append("stability_capture_time_missing")
    else:
        issues.append("unknown_stability_comparison_scope")
    first, second = (labels.get(i) for i in ids)
    if (not first or not second or first.get("verdict") not in {"normal", "obstacle"}
            or first.get("verdict") != second.get("verdict")
            or set(first.get("defect_types", [])) != set(second.get("defect_types", []))):
        issues.append("stability_truth_disagrees_or_missing")
    return issues


def audit(manifest: dict, labels: dict, protocol: dict, exposure: dict, data_root: Path) -> dict:
    """Report missing evidence without manufacturing formal readiness.

报告缺失证据，不制造正式准出状态。
"""
    if (manifest.get("format") != "drone.p6-road-corpus/v1" or labels.get("format") != "drone.p6-road-labels/v1"
            or protocol.get("format") != "drone.p6-road-protocol/v1"):
        raise ValueError("unsupported road evidence format")
    if labels.get("corpus_id") != manifest.get("corpus_id") or protocol.get("corpus_id") != manifest.get("corpus_id"):
        raise ValueError("corpus identity mismatch")
    samples = unique(manifest["samples"], "sample_id")
    annotations = unique(labels["rows"], "sample_id")
    if set(annotations) - set(samples):
        raise ValueError("foreign annotation")
    exposed = set(exposure["image_sha256"])
    types = set(protocol["defect_types"])
    issues, reviewed, counts, primaries = [], 0, Counter(), set()
    primary_observations = set()
    ai_reviewed = 0
    assistance_cache, admitted_derivatives = {}, {}
    associations = {key: defaultdict(set) for key in (*GROUP_KEYS, "image_sha256", "parent_sha256")}

    def issue(code, sample_id=None, **details):
        issues.append({"code": code, "sample_id": sample_id, **details})

    def associate(entity, split, ident, *, reference=False):
        for key in (*GROUP_KEYS, "image_sha256"):
            value = entity.get(key)
            if not isinstance(value, str) or not value:
                issue("grouping_identity_missing", ident, field=key, expected_reference=reference)
            else:
                associations[key][value].add(split)
        parent = entity.get("capture_record", {}).get("parent_sha256")
        if parent:
            associations["parent_sha256"][parent].add(split)
        groups = exposure.get("groups", {})
        known_exposed = (entity.get("image_sha256") in exposed or parent in exposed
                         or any(entity.get(k) in groups.get(k, []) for k in GROUP_KEYS)
                         or parent is not None and parent in groups.get("parent_sha256", []))
        if split != "development" and (known_exposed or entity.get("development_exposed") is not False):
            issue("exposed_expected_reference_outside_development" if reference else "exposed_image_outside_development", ident)

    for ident, sample in samples.items():
        split = sample.get("split")
        if split not in SPLITS:
            raise ValueError("invalid split")
        counts[split] += 1
        role = sample.get("evaluation_role")
        if role not in {"primary", "repeat"}:
            raise ValueError("invalid observation evaluation role")
        if role == "primary" and sample.get("asset_id"):
            key = (split, sample["asset_id"])
            if key in primaries:
                issue("duplicate_primary_asset", ident)
            primaries.add(key)
        if role == "primary":
            observed_asset = (sample.get("observed") or sample).get("asset_id")
            for field, value in (("observed_asset_id", observed_asset), ("image_sha256", sample.get("image_sha256"))):
                if not value:
                    continue
                key = (split, field, value)
                if key in primary_observations:
                    issue("duplicate_primary_observation", ident, field=field)
                primary_observations.add(key)
        image = within(data_root, sample["image_file"])
        if not image.is_file() or digest(image) != sample["image_sha256"]:
            issue("image_bytes_missing_or_changed", ident)
        else:
            try:
                with Image.open(image) as picture:
                    if list(picture.size) != [sample["width"], sample["height"]]:
                        issue("image_dimensions_changed", ident)
                    picture.verify()
            except (KeyError, OSError, ValueError):
                issue("image_not_decodable", ident)
        source = manifest["sources"].get(sample["source_id"])
        if not source or source.get("permission_status") != "documented":
            issue("source_permission_missing", ident)
        else:
            evidence = within(data_root, source["license_evidence_file"])
            if not evidence.is_file() or digest(evidence) != source["license_evidence_sha256"]:
                issue("license_evidence_missing_or_changed", ident)
        associate(sample, split, ident)
        observed = sample.get("observed")
        if observed:
            associate({**observed, "image_sha256": sample["image_sha256"], "capture_record": sample.get("capture_record", {})}, split, ident)
        if sample.get("grouping_verified") is not True or not sample.get("grouping_evidence"):
            issue("grouping_not_verified", ident)
        label = annotations.get(ident)
        if sample.get("media_origin") == "real_capture_derivative":
            try:
                admitted_derivatives[ident] = derived_capture(sample, protocol, data_root, assistance_cache,
                                                              scope_before=first_annotation_at(sample, label or {}, data_root, assistance_cache))
                if annotations.get(ident, {}).get("verdict") != "wrong_target" and sample["asset_id"] != sample["observed"]["asset_id"]:
                    raise ValueError("observed asset differs from the requested target")
            except (OSError, KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
                admitted_derivatives.pop(ident, None)
                issue("derived_capture_not_verified", ident, reason=str(exc))
        elif sample.get("derivation"):
            issue("derived_capture_media_origin_mismatch", ident)
        elif split != "development" and sample.get("media_origin") != "real_capture":
            issue("nonreal_sample_in_formal_split", ident)
        assistance = (label or {}).get("ai_assistance", {})
        needs_task = sample.get("media_origin") == "real_capture_derivative" and (label or {}).get("verdict") == "wrong_target"
        task_valid = False
        if needs_task or (label or {}).get("task_evidence") or "prior_task_file" in assistance or "prior_task_sha256" in assistance:
            try:
                expected = verified_prior_task(sample, label or {}, protocol, data_root, assistance_cache)
                associate(expected, split, ident, reference=True)
                if not observed:
                    raise ValueError("wrong-target observed grouping missing")
                if expected.get("observed"):
                    associate({**expected["observed"], "image_sha256": expected["image_sha256"],
                               "capture_record": expected.get("capture_record", {})}, split, ident, reference=True)
                task_valid = True
            except (OSError, KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
                issue("wrong_target_reference_provenance_not_verified", ident, reason=str(exc))
        if needs_task and not task_valid:
            admitted_derivatives.pop(ident, None)
            issue("derived_wrong_target_prior_task_not_verified", ident)
        if not label or label.get("verdict") is None:
            issue("business_label_missing", ident)
            continue
        if label["verdict"] not in LABELS or not isinstance(label.get("defect_types"), list) or set(label["defect_types"]) - types:
            raise ValueError("invalid business label or defect type")
        if (label["verdict"] == "obstacle") != bool(label["defect_types"]):
            raise ValueError("obstacle types and label disagree")
        if label.get("image_sha256") != sample["image_sha256"]:
            raise ValueError("annotation image mismatch")
        delegated = label.get("origin") == "delegated_ai" and delegated_ai_approved(protocol)
        if ((label.get("origin") != "human" and not delegated) or not isinstance(label.get("reviewer_id"), str) or not label["reviewer_id"].strip()
                or label["reviewer_id"] == "QA_FIXTURE_NOT_HUMAN"
                or not isinstance(label.get("notes"), str) or not label["notes"].strip() or type(label.get("hints_seen")) is not bool):
            issue("independent_business_review_missing", ident)
        else:
            reviewed += not delegated
            ai_reviewed += delegated
        try:
            timestamp(label["recorded_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            issue("business_review_timestamp_missing", ident)
        if label["verdict"] in {"normal", "obstacle"}:
            if label.get("full_target_inspected") is not True:
                issue("whole_target_not_reviewed", ident)
            target = sample.get("target") or {}
            if target.get("status") != ("ai_verified" if delegated else "human_confirmed") or not polygon_valid(target.get("polygon")):
                issue("confirmed_target_geometry_missing", ident)
        if (sample.get("ai_suggestion") or delegated) and "ai_assistance" not in label:
            issue("assisted_annotation_provenance_missing", ident)
        if "ai_assistance" in label:
            for code in assisted_label_issues(sample, label, protocol, data_root, assistance_cache):
                issue(code, ident)
        elif split == "test" and label.get("model_outputs_seen") is not False:
            issue("test_label_model_exposure_unknown_or_present", ident)
    for key, groups in associations.items():
        for value, splits in groups.items():
            if len(splits) > 1:
                issue("cross_split_group_leakage", field=key, group=value, splits=sorted(splits))
    pair_ids = set()
    for pair in manifest.get("stability_pairs", []):
        ids = pair.get("sample_ids", [])
        if len(ids) != 2 or ids[0] == ids[1] or any(i not in samples for i in ids):
            raise ValueError("invalid stability pair")
        pair_key = tuple(sorted(ids))
        if pair_key in pair_ids:
            raise ValueError("duplicate stability pair")
        pair_ids.add(pair_key)
        for problem in pair_problems(pair, samples, annotations):
            issue(problem, ids[0])
        if pair.get("origin") == "delegated_ai":
            for problem in delegated_pair_issues(pair, samples, protocol, data_root, assistance_cache):
                issue(problem, ids[0])
        if pair.get("comparison_scope") == DERIVED_SCOPE:
            if any(ident not in admitted_derivatives for ident in ids):
                issue("derived_pair_inventory_not_verified", ids[0])
            else:
                captures = [bound_evidence(data_root, samples[i]["derivation"]["extraction_file"],
                                          samples[i]["derivation"]["extraction_sha256"], assistance_cache) for i in ids]
                if any(captures[0][k] != captures[1][k] for k in ("bounded_target_id", "world_scope")):
                    issue("derived_pair_fixed_target_mismatch", ids[0])
        if pair.get("comparison_scope") == "distinct_original_captures_unordered":
            for ident in ids:
                reference = samples[ident].get("capture_record") or {}
                try:
                    inventory_file = within(data_root, reference["inventory_file"])
                    if digest(inventory_file) != reference["inventory_sha256"]:
                        raise ValueError("capture inventory bytes changed")
                    inventory = read(inventory_file)
                    if inventory.get("format") != "drone.floodnet-acquisition-pool/v1":
                        raise ValueError("unsupported original capture inventory")
                    entries = unique(inventory["rows"], "image_id")
                    original = entries[reference["source_observation_id"]]
                    if original.get("completeness") != "complete_original" or original.get("image_sha256") != samples[ident]["image_sha256"]:
                        raise ValueError("capture identity does not bind this original")
                except (KeyError, OSError, TypeError, ValueError):
                    issue("original_capture_inventory_not_verified", ident)
    if not samples:
        issue("no_samples")
    if protocol.get("status") != "frozen" or not protocol.get("approval_reference"):
        issue("protocol_not_frozen_or_approved")
    minimum, limits = protocol.get("minimum_samples", {}), protocol.get("thresholds", {})
    if set(minimum) != MINIMUM_KEYS or set(limits) != LIMIT_KEYS:
        issue("protocol_requirements_incomplete")
    test_labels = Counter(r.get("verdict") for ident, r in annotations.items()
                          if samples[ident]["split"] == "test" and samples[ident]["evaluation_role"] == "primary")
    test_labels["stability_pairs"] = sum(samples[p["sample_ids"][0]]["split"] == "test" for p in manifest.get("stability_pairs", []))
    test_labels["sites"] = len({s["site_id"] for s in samples.values() if s["split"] == "test" and s.get("site_id")})
    for key, count in minimum.items():
        if type(count) is not int or count <= 0:
            issue("minimum_sample_requirement_not_frozen", field=key)
        elif test_labels[key] < count:
            issue("minimum_samples_not_met", field=key, observed=test_labels[key], required=count)
    for key, limit in limits.items():
        ceiling = 100 if key == "false_alerts_per_100_normal_max" else math.inf if key == "review_p95_ms_max" else 1
        if type(limit) not in (int, float) or not math.isfinite(limit) or not 0 <= limit <= ceiling:
            issue("metric_threshold_not_frozen", field=key)
    for split in ("calibration", "test"):
        if not counts[split]:
            issue("formal_split_missing", field=split)
    extra = protocol.get("additional_requirements", {})
    if set(extra) != EXTRA_KEYS:
        issue("protocol_additional_requirements_incomplete")
    for name, vocabulary in (("minimum_calibration_by_label", LABELS), ("minimum_test_by_defect_type", types)):
        requirements = extra.get(name, {})
        if set(requirements) != vocabulary:
            issue("protocol_coverage_requirements_incomplete", field=name)
        for key, minimum_count in requirements.items():
            if type(minimum_count) is not int or minimum_count <= 0:
                issue("protocol_coverage_requirement_invalid", field=name + "." + key)
                continue
            wanted = "calibration" if name == "minimum_calibration_by_label" else "test"
            observed = sum(s["split"] == wanted and s["evaluation_role"] == "primary" and
                           (annotations.get(i, {}).get("verdict") == key if wanted == "calibration"
                            else key in annotations.get(i, {}).get("defect_types", [])) for i, s in samples.items())
            if observed < minimum_count:
                issue("coverage_samples_not_met", field=name + "." + key, observed=observed, required=minimum_count)
    site_counts = Counter(s.get("site_id") for s in samples.values() if s["split"] == "test" and s["evaluation_role"] == "primary")
    cap = extra.get("maximum_test_site_fraction")
    if type(cap) not in (int, float) or not 0 < cap <= 1:
        issue("site_fraction_requirement_invalid")
    elif site_counts and max(site_counts.values()) / sum(site_counts.values()) > cap:
        issue("test_site_concentration_exceeded")
    stability_assets = {samples[p["sample_ids"][0]].get("asset_id") for p in manifest.get("stability_pairs", [])
                        if samples[p["sample_ids"][0]]["split"] == "test"}
    minimum_assets = extra.get("minimum_stability_assets")
    if type(minimum_assets) is not int or minimum_assets <= 0:
        issue("stability_asset_requirement_invalid")
    elif len(stability_assets - {None, ""}) < minimum_assets:
        issue("minimum_stability_assets_not_met")
    if type(extra.get("minimum_timed_reviews")) is not int or extra["minimum_timed_reviews"] <= 0:
        issue("timing_sample_requirement_invalid")
    if type(extra.get("required_review_coverage")) not in (int, float) or extra["required_review_coverage"] != 1:
        issue("full_candidate_review_coverage_required")
    return {"format": "drone.p6-road-data-audit/v1", "status": "ready_for_scoped_evaluation" if not issues else "missing",
            "samples": len(samples), "split_counts": dict(counts), "human_labels_present": reviewed,
            "delegated_ai_labels_present": ai_reviewed,
            "ai_verified_pairs_declared": sum(pair.get("origin") == "delegated_ai" for pair in manifest.get("stability_pairs", [])),
            "ai_assisted_labels_declared": sum("ai_assistance" in row for row in annotations.values()),
            "derived_captures_verified": len(admitted_derivatives),
            "annotation_claim": "AI-reviewed reference labels, not independent human truth" if ai_reviewed else
                                "AI-assisted labels are human adjudications, not blind labels; provenance checked separately",
            "stability_pairs": len(pair_ids), "issue_counts": dict(Counter(i["code"] for i in issues)), "issues": issues,
            "data_ready": not issues, "formal_release_eligible": False, "model_quality_verified": False}


def fraction(numerator: int, denominator: int, scale: float = 1) -> dict:
    """Keep missing denominators and descriptive Wilson intervals explicit.

明确区分分母缺失，并报告描述性的 Wilson 区间。
"""
    interval = None
    if denominator:
        z, proportion = 1.959963984540054, numerator / denominator
        divisor = 1 + z * z / denominator
        center = (proportion + z * z / (2 * denominator)) / divisor
        radius = z * math.sqrt(proportion * (1 - proportion) / denominator + z * z / (4 * denominator * denominator)) / divisor
        interval = [max(0, center - radius) * scale, min(1, center + radius) * scale]
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator * scale if denominator else None,
            "wilson_95_interval": interval, "interval_scope": "descriptive; does not adjust for clustered assets or pairs"}


def scores(manifest: dict, labels: dict, predictions: dict, reviews: dict, split: str) -> dict:
    """Compute observation metrics; raw result tables alone never establish live provenance.

计算观察级指标，原始结果表本身不证明实调来源。
"""
    for value, format_name in ((manifest, "corpus"), (labels, "labels"), (predictions, "predictions"), (reviews, "review-sessions")):
        if value.get("format") != f"drone.p6-road-{format_name}/v1" or not value.get("corpus_id") or value["corpus_id"] != manifest.get("corpus_id"):
            raise ValueError("scoring corpus or format mismatch")
    samples = {i: s for i, s in unique(manifest["samples"], "sample_id").items() if s["split"] == split}
    all_labels, found = unique(labels["rows"], "sample_id"), unique(predictions["rows"], "sample_id")
    if not samples or set(found) != set(samples):
        raise ValueError("predictions must cover the entire selected split exactly once")
    if any(i not in all_labels or all_labels[i].get("verdict") not in LABELS for i in samples):
        raise ValueError("all business labels must be present before scoring")
    for ident, sample in samples.items():
        label = all_labels[ident]
        if label.get("image_sha256") != sample["image_sha256"]:
            raise ValueError("annotation image mismatch")
        if not isinstance(label.get("defect_types"), list) or (label["verdict"] == "obstacle") != bool(label["defect_types"]):
            raise ValueError("obstacle types and label disagree")
    for ident, row in found.items():
        if row.get("image_sha256") != samples[ident]["image_sha256"]:
            raise ValueError("prediction image mismatch")
        if row.get("verdict") not in VERDICTS or not isinstance(row.get("reasons"), list) or not isinstance(row.get("defect_types"), list):
            raise ValueError("invalid prediction")
        if row["verdict"] == "refused" and not row["reasons"]:
            raise ValueError("refusal must retain its reason")
        if (row["verdict"] == "suspected") != bool(row["defect_types"]):
            raise ValueError("prediction types and verdict disagree")
    if any(s.get("evaluation_role") not in {"primary", "repeat"} for s in samples.values()):
        raise ValueError("invalid observation evaluation role")
    primary = {i: s for i, s in samples.items() if s["evaluation_role"] == "primary"}
    assets = [s.get("asset_id") for s in primary.values()]
    if not primary or any(not a for a in assets) or len(assets) != len(set(assets)):
        raise ValueError("one primary observation per known asset is required")
    normals = [i for i in primary if all_labels[i]["verdict"] == "normal"]
    positives = [i for i in primary if all_labels[i]["verdict"] == "obstacle"]
    uncertain = [i for i in primary if all_labels[i]["verdict"] in {"indeterminate", "wrong_target"}]
    fp = sum(found[i]["verdict"] == "suspected" for i in normals)
    fn = sum(found[i]["verdict"] != "suspected" for i in positives)
    deterministic = normals + positives
    types = sorted({t for i in positives for t in all_labels[i]["defect_types"]})
    per_type = {}
    for kind in types:
        selected = [i for i in positives if kind in all_labels[i]["defect_types"]]
        per_type[kind] = {"scene_miss": fraction(sum(found[i]["verdict"] != "suspected" for i in selected), len(selected)),
                          "typed_miss": fraction(sum(found[i]["verdict"] != "suspected" or kind not in found[i]["defect_types"] for i in selected), len(selected))}
    pairs = [p for p in manifest.get("stability_pairs", []) if all(i in samples for i in p["sample_ids"])]
    pair_ids = [tuple(sorted(p["sample_ids"])) for p in pairs]
    if len(pair_ids) != len(set(pair_ids)) or any(pair_problems(p, samples, all_labels) for p in pairs):
        raise ValueError("stability evidence is invalid")
    paired, consistent = 0, 0
    for pair in pairs:
        left, right = (found[i]["verdict"] for i in pair["sample_ids"])
        if left != "refused" and right != "refused":
            paired += 1
            consistent += left == right
    required = {i for i in primary if found[i]["verdict"] in {"suspected", "refused"}}
    human_times, seen, excluded, exposures = [], set(), Counter(), Counter()
    for session in reviews.get("sessions", []):
        if session.get("phase") != "candidate_review" or session.get("origin") != "human":
            excluded["not_human_candidate_review"] += 1
            continue
        ident = session.get("sample_id")
        if ident not in required:
            excluded["not_a_required_review"] += 1
            continue
        if ident in seen:
            raise ValueError("duplicate timed review for one observation")
        if (not session.get("reviewer_id") or type(session.get("hints_seen")) is not bool
                or session["reviewer_id"] == "QA_FIXTURE_NOT_HUMAN" or type(session.get("truth_seen_before_review")) is not bool):
            raise ValueError("timed review identity or exposure missing")
        elapsed = (timestamp(session["ended_at"]) - timestamp(session["started_at"])).total_seconds() * 1000
        active = session.get("active_ms")
        if type(active) is not int or active <= 0 or elapsed <= 0 or active > elapsed + 1000:
            raise ValueError("invalid active review duration")
        seen.add(ident)
        human_times.append(active)
        exposures["truth_seen" if session["truth_seen_before_review"] else "truth_not_seen"] += 1
        exposures["hints_seen" if session["hints_seen"] else "hints_not_seen"] += 1
    human_times.sort()
    return {"format": "drone.p6-road-metrics/v1", "corpus_id": manifest["corpus_id"], "split": split,
            "samples": len(primary), "all_observations": len(samples),
            "unique_assets": len({s.get("asset_id") for s in samples.values() if s.get("asset_id")}),
            "label_counts": dict(Counter(all_labels[i]["verdict"] for i in primary)),
            "label_origins": dict(Counter(all_labels[i].get("origin", "missing") for i in samples)),
            "prediction_counts": dict(Counter(found[i]["verdict"] for i in primary)),
            "false_alerts_per_100_normal": fraction(fp, len(normals), 100),
            "false_alerts_per_100_determinable": fraction(fp, len(deterministic), 100),
            "scene_miss_rate": fraction(fn, len(positives)), "miss_by_type": per_type,
            "refusal_rate": fraction(sum(found[i]["verdict"] == "refused" for i in primary), len(primary)),
            "determinable_coverage": fraction(sum(found[i]["verdict"] != "refused" for i in deterministic), len(deterministic)),
            "false_normal_on_refusable": fraction(sum(found[i]["verdict"] == "normal" for i in uncertain), len(uncertain)),
            "refusal_reasons": dict(Counter(reason for i in primary if found[i]["verdict"] == "refused" for reason in found[i]["reasons"])),
            "stability": {"covered_pair_fraction": fraction(paired, len(pairs)), "agreement_given_covered": fraction(consistent, paired),
                          "consistent_and_covered_fraction": fraction(consistent, len(pairs)), "unresolved_pairs": len(pairs) - paired,
                          "unique_assets": len({samples[p["sample_ids"][0]]["asset_id"] for p in pairs}),
                          "comparison_scope_counts": dict(Counter(p.get("comparison_scope", "ordered_capture_times") for p in pairs))},
            "human_review": {"sessions": len(human_times), "required_observations": len(required), "coverage": fraction(len(human_times), len(required)),
                             "median_ms": median(human_times) if human_times else None,
                             "p95_ms": human_times[math.ceil(.95 * len(human_times)) - 1] if human_times else None,
                             "total_active_ms": sum(human_times), "exposure_counts": dict(exposures), "excluded_sessions": dict(excluded)},
            "provenance_verified": False, "counted_as_formal_live_result": False, "formal_release_eligible": False,
            "scope": "observation metrics only; data admission, frozen run provenance, live recording and strict replay are separately required"}


def assess(protocol: dict, metrics: dict) -> dict:
    """Compare observed metrics with targets without granting release or live provenance.

对照目标检查观察指标，不授予准出或实调来源结论。
"""
    if (protocol.get("format") != "drone.p6-road-protocol/v1" or metrics.get("format") != "drone.p6-road-metrics/v1"
            or not protocol.get("corpus_id") or protocol["corpus_id"] != metrics.get("corpus_id")):
        raise ValueError("assessment corpus or format mismatch")
    limits, extra, checks = protocol.get("thresholds", {}), protocol.get("additional_requirements", {}), []

    def check(name, observed, required, minimum=False):
        valid = all(type(v) in (int, float) and math.isfinite(v) for v in (observed, required))
        result = "missing" if not valid else "met" if (observed >= required if minimum else observed <= required) else "not_met"
        checks.append({"name": name, "observed": observed, "required": required, "comparison": ">=" if minimum else "<=", "status": result})

    for name, key, minimum in (("false_alerts_per_100_normal", "false_alerts_per_100_normal_max", False),
                               ("determinable_coverage", "determinable_coverage_min", True),
                               ("false_normal_on_refusable", "false_normal_on_refusable_max", False)):
        check(name, metrics.get(name, {}).get("value"), limits.get(key), minimum)
    for kind in protocol.get("defect_types", []):
        value = metrics.get("miss_by_type", {}).get(kind, {}).get("typed_miss", {})
        check("typed_miss." + kind, value.get("value"), limits.get("typed_miss_rate_max"))
        check("type_samples." + kind, value.get("denominator"), extra.get("minimum_test_by_defect_type", {}).get(kind), True)
    if not protocol.get("defect_types"):
        check("defect_types", None, None)
    stability, review = metrics.get("stability", {}), metrics.get("human_review", {})
    check("stable_and_covered", stability.get("consistent_and_covered_fraction", {}).get("value"), limits.get("stable_and_covered_min"), True)
    deferred = timing_deferred(protocol)
    for name, observed, required, minimum in (("review_p95_ms", review.get("p95_ms"), limits.get("review_p95_ms_max"), False),
            ("timed_reviews", review.get("sessions"), extra.get("minimum_timed_reviews"), True),
            ("review_coverage", review.get("coverage", {}).get("value"), extra.get("required_review_coverage"), True)):
        if deferred:
            checks.append({"name": name, "observed": observed, "required": required, "status": "deferred",
                           "comparison": ">=" if minimum else "<=", "approval_reference": protocol["review_timing_policy"]["approval_reference"]})
        else:
            check(name, observed, required, minimum)
    check("stability_assets", stability.get("unique_assets"), extra.get("minimum_stability_assets"), True)
    for name in sorted(LABELS):
        check("test_samples." + name, metrics.get("label_counts", {}).get(name, 0), protocol.get("minimum_samples", {}).get(name), True)
    check("stability_pairs", stability.get("consistent_and_covered_fraction", {}).get("denominator"), protocol.get("minimum_samples", {}).get("stability_pairs"), True)
    states = {c["status"] for c in checks}
    return {"format": "drone.p6-road-target-comparison/v1", "corpus_id": protocol["corpus_id"],
            "metric_status": "missing" if "missing" in states else "not_met" if "not_met" in states else "met",
            "protocol_status": protocol.get("status"), "split": metrics.get("split"), "checks": checks,
            "human_efficiency_verified": False if deferred else None,
            "formal_release_eligible": False, "scope": "observed in-scope targets only; data admission, approved reference provenance and actual run are separate"}


def main() -> None:
    """Provide offline audit and scoring, with no provider or control calls. / 提供离线准入及计分，无模型或控制调用。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "score"):
        command = commands.add_parser(name)
        for option in ("manifest", "labels", "output"):
            command.add_argument("--" + option, type=Path, required=True)
    check = commands.choices["audit"]
    for option in ("protocol", "exposure", "data-root"):
        check.add_argument("--" + option, type=Path, required=True)
    score = commands.choices["score"]
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--reviews", type=Path, required=True)
    score.add_argument("--split", choices=sorted(SPLITS), required=True)
    comparison = commands.add_parser("assess")
    for option in ("protocol", "metrics", "output"):
        comparison.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "audit":
        result = audit(read(args.manifest), read(args.labels), read(args.protocol), read(args.exposure), args.data_root)
    elif args.command == "score":
        result = scores(read(args.manifest), read(args.labels), read(args.predictions), read(args.reviews), args.split)
    else:
        result = assess(read(args.protocol), read(args.metrics))
    result["input_sha256"] = {key: digest(value) for key, value in vars(args).items()
                              if isinstance(value, Path) and key not in {"data_root", "output"}}
    result["tool_sha256"] = digest(Path(__file__))
    result["geometry_tool_sha256"] = digest(Path(__file__).with_name("p6_boundary_drafts.py"))
    write(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "issues"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
