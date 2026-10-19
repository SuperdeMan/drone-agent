"""Verify complete OpenAerialMap UAV objects without borrowing HF provenance.

验证 OpenAerialMap 完整 UAV 对象，不借用 Hugging Face 来源字段。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def object_key(url: str) -> str:
    """Accept only the published OIN bucket and its equivalent regional endpoint.

只接受已发布 OIN 桶及其等价地区端点。
"""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "oin-hotosm-temp.s3.amazonaws.com", "oin-hotosm-temp.s3.us-east-1.amazonaws.com"}
            or parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment
            or re.fullmatch(r"/[a-f0-9]{24}/\d+/[a-f0-9]{24}\.tif", parsed.path) is None):
        raise ValueError("unsupported OAM object URL")
    return parsed.path


def verify_source(source, root, cache, h):
    """Bind per-object publisher metadata, conditional full GET and actual bytes.

绑定逐对象发布元数据、条件完整 GET 与真实本地字节。
"""
    ident = source["catalog_object_id"]
    if not isinstance(ident, str) or re.fullmatch(r"[a-f0-9]{24}", ident) is None:
        raise ValueError("invalid OAM catalog identity")
    url = "https://api.openaerialmap.org/meta/" + ident
    catalog = h["read_bound"](root, source["catalog_file"], source["catalog_sha256"], cache)["results"]
    properties = catalog["properties"]
    if (source.get("catalog_url") != url or catalog.get("_id") != ident or catalog.get("platform") != "uav"
            or properties.get("license") != "CC-BY 4.0" or not isinstance(properties.get("sensor"), str)
            or not properties["sensor"].strip() or properties.get("bands") != 3 or properties.get("dtype") != "uint8"
            or properties.get("colorinterp") != ["red", "green", "blue"] or not properties.get("crs")
            or type(catalog.get("file_size")) is not int or catalog["file_size"] <= 0
            or source.get("publisher_sha256") is not None
            or source.get("checksum_origin") != "locally_computed_full_object"):
        raise ValueError("OAM per-object permission, platform or native metadata missing")
    start, finish = (h["timestamp"](catalog[k]) for k in ("acquisition_start", "acquisition_end"))
    if finish < start:
        raise ValueError("publisher acquisition interval is reversed")
    parent_hash = source["local_full_object_sha256"]
    parent = h["file_bound"](root, source["object_file"], parent_hash, cache)
    receipt = h["read_bound"](root, source["download_receipt_file"], source["download_receipt_sha256"], cache)
    request, response = receipt["request"], receipt["response"]
    request_headers = {k.lower(): v for k, v in request["headers"].items()}
    response_headers = {k.lower(): v for k, v in response["headers"].items()}
    size = catalog["file_size"]
    etag = receipt["expected_etag"]
    if (receipt.get("format") != "drone.p6-oam-full-object-http/v1"
            or receipt.get("status") != "downloaded_and_verified" or receipt.get("catalog_object_id") != ident
            or receipt.get("source_metadata_url") != url or receipt.get("source_metadata_sha256") != source["catalog_sha256"]
            or receipt.get("checksum_origin") != "locally_computed_full_object" or receipt.get("publisher_sha256") is not None
            or request.get("method") != "GET" or response.get("status") != 200
            or "range" in request_headers or "content-range" in response_headers
            or object_key(request["url"]) != object_key(catalog["uuid"])
            or object_key(response["final_url"]) != object_key(catalog["uuid"])
            or not isinstance(etag, str) or re.fullmatch(r'"[a-f0-9]{32}(?:-\d+)?"', etag) is None
            or any(value != etag for value in (request_headers.get("if-match"), response_headers.get("etag"), receipt.get("response_etag")))
            or any(type(receipt.get(k)) is not int or receipt[k] != size for k in ("expected_bytes", "body_bytes", "response_content_length"))
            or response_headers.get("content-length") != str(size) or parent.stat().st_size != size
            or receipt.get("local_full_object_sha256") != parent_hash
            or h["file_bound"](root, receipt["local_file"], parent_hash, cache) != parent
            or h["timestamp"](receipt["finished_at_utc"]) < h["timestamp"](receipt["started_at_utc"])):
        raise ValueError("OAM complete object GET or byte identity mismatch")
    return catalog, parent, receipt


def derived_capture(sample, protocol, root, cache, *, scope_before, helpers):
    """Validate an OAM derivative while refusing repeated-capture claims.

验证 OAM 派生观察，同时拒绝把发布者日期当作跨次采集证明。
"""
    h = helpers
    binding, policy = sample["derivation"], protocol["oam_capture_policy"]
    if (binding.get("format") != "drone.p6-oam-derived-capture-binding/v1"
            or policy.get("mode") != "traceable_oam_uav_orthomosaic" or sample.get("captured_at") is not None
            or policy.get("claim_raw_camera_frames") is not False or policy.get("synthetic_scene_content_allowed") is not False
            or policy.get("repeated_capture_eligible") is not False
            or binding["source_inventory_sha256"] != policy.get("source_inventory_sha256")
            or not h["sha256_value"](policy.get("extractor_sha256"))):
        raise ValueError("OAM derivative policy missing")
    bound = {name: h["read_bound"](root, str(h["within"](root, binding[name + "_file"])), binding[name + "_sha256"], cache)
             for name in ("source_inventory", "extraction", "extraction_plan", "replay")}
    receipt, plan = bound["extraction"], bound["extraction_plan"]
    source = h["unique"](bound["source_inventory"], "catalog_object_id")[receipt["catalog_object_id"]]
    catalog, parent, download = verify_source(source, root, cache, h)
    parent_hash = source["local_full_object_sha256"]
    if (receipt.get("source_kind") != "oam_uav" or receipt.get("status") != "extracted"
            or receipt.get("media_origin") != "real_capture_derivative" or receipt.get("parent_sha256") != parent_hash
            or receipt.get("alignment_method") != "georeferencing_only"
            or any(k in receipt for k in ("source_commit", "source_published_lfs_sha256"))
            or plan.get("tool_sha256") != policy["extractor_sha256"]
            or h["timestamp"](receipt["finished_at"]) < h["timestamp"](receipt["started_at"])
            or h["timestamp"](receipt["started_at"]) < max(h["timestamp"](plan["created_at"]), h["timestamp"](download["finished_at_utc"]))
            or [receipt["raster"]["width"], receipt["raster"]["height"]] != catalog["properties"]["dimensions"]
            or receipt["raster"]["crs"] != catalog["properties"]["crs"]):
        raise ValueError("OAM extraction source or tools mismatch")
    source_plan = h["read_bound"](root, plan["source_plan_file"], plan["source_plan_sha256"], cache)
    planned = h["unique"](source_plan["items"], "extraction_item_id")[receipt["sample_id"]]
    scope = receipt["world_scope"]
    if (planned["catalog_object_id"] != source["catalog_object_id"] or planned["target"] != scope
            or any(planned[k] != receipt[k] for k in ("asset_group_id", "bounded_target_id"))
            or scope.get("frozen_before_business_annotation_and_model_output") is not True
            or scope.get("source_condition_polygons_or_model_outputs_used_for_scope") is not False
            or not h["sha256_value"](scope.get("freeze_scope_sha256"))
            or h["timestamp"](scope["frozen_at_utc"]) > h["timestamp"](plan["created_at"])
            or scope_before is not None and h["timestamp"](scope["frozen_at_utc"]) > scope_before
            or receipt["source_geometry_sha256"] != planned["source_geometry_sha256"]):
        raise ValueError("OAM frozen world target is not bound")
    h["file_bound"](root, receipt["source_geometry_file"], receipt["source_geometry_sha256"], cache)
    h["image"](receipt, root, cache, expected_bands=3)
    h["pixels"](receipt, parent, parent_hash, root, cache)
    replay = bound["replay"]
    expected_replay = {"format": "drone.p6-oam-derived-capture-replay/v1", "mode": "offline_complete_object", "network_requests": 0,
                       "source_observation_id": receipt["sample_id"], "parent_sha256": parent_hash,
                       "extraction_sha256": binding["extraction_sha256"], "extractor_sha256": policy["extractor_sha256"]}
    reproduced = h["read_bound"](root, replay["replay_extraction_file"], replay["replay_extraction_sha256"], cache)
    same = ("sample_id", "asset_group_id", "bounded_target_id", "catalog_object_id", "source_kind", "parent_sha256",
            "media_origin", "status", "alignment_method", "world_scope", "raster", "native_window_xywh", "window_transform",
            "width", "height", "image_sha256", "mask_sha256", "native_rgb_sha256", "valid_pixel_fraction", "source_geometry_sha256")
    if (any(replay.get(k) != v for k, v in expected_replay.items()) or type(replay.get("network_requests")) is not int
            or any(reproduced.get(k) != receipt[k] for k in same)
            or not h["timestamp"](receipt["finished_at"]) <= h["timestamp"](replay["started_at"]) <= h["timestamp"](reproduced["started_at"])
            <= h["timestamp"](reproduced["finished_at"]) <= h["timestamp"](replay["finished_at"])
            or any((root / reproduced[k]).resolve() == (root / receipt[k]).resolve() for k in ("image_file", "mask_file"))):
        raise ValueError("OAM independent offline replay mismatch")
    h["image"](reproduced, root, cache, expected_bands=3)
    if (receipt["image_sha256"] != sample["image_sha256"]
            or h["file_bound"](root, receipt["image_file"], receipt["image_sha256"], cache) != h["within"](root, sample["image_file"])
            or [receipt["width"], receipt["height"]] != [sample["width"], sample["height"]]):
        raise ValueError("OAM extraction is not this observation")
    reference = sample["capture_record"]
    inventory = h["read_bound"](root, str(h["within"](root, reference["inventory_file"])), reference["inventory_sha256"], cache)
    if inventory.get("format") != "drone.p6-derived-capture-inventory/v1":
        raise ValueError("unsupported OAM capture inventory")
    row = h["unique"](inventory["rows"], "source_observation_id")[reference["source_observation_id"]]
    expected = {"source_observation_id": receipt["sample_id"], "source_id": sample["source_id"], "image_sha256": sample["image_sha256"],
                "catalog_object_id": source["catalog_object_id"], "parent_sha256": parent_hash,
                "author_date_group": None, "author_date_verified": False, "repeated_capture_eligible": False,
                "observed": sample["observed"], "derivation_sha256": h["canonical"](binding)}
    if (any(row.get(k) != v for k, v in expected.items())
            or any(reference.get(k) != expected[k] for k in ("source_observation_id", "parent_sha256", "author_date_group", "author_date_verified"))
            or sample["observed"].get("asset_id") != receipt["asset_group_id"]
            or any(not isinstance(sample["observed"].get(k), str) or not sample["observed"][k] for k in h["group_keys"])
            or type(sample["observed"].get("development_exposed")) is not bool):
        raise ValueError("OAM capture inventory or unsupported repeat claim")
    return row
