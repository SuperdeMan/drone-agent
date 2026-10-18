"""Synthetic provenance fixtures exercise admission, never real business quality.

合成来源夹具只验证准入代码，不证明真实业务质量。
"""
from __future__ import annotations

import copy
import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
import rasterio
from affine import Affine
from numpy import ascontiguousarray, indices, stack
from PIL import Image
from rasterio.enums import ColorInterp
from rasterio.io import MemoryFile

BASE = runpy.run_path(str(Path(__file__).with_name("test_p6_business_eval.py")))
C = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_business_eval.py")))
corpus = BASE["corpus"]


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return C.digest(path)


def refresh(value):
    manifest, _, protocol, _, root = value
    sources = {s["orthomosaic"]: s for s in C.read(root / "uas.json")}
    rows = []
    for sample in manifest["samples"]:
        if sample["media_origin"] != "real_capture_derivative":
            continue
        binding = sample["derivation"]
        for name in ("extraction", "extraction_plan", "source_inventory", "range_manifest", "replay"):
            binding[name + "_sha256"] = C.digest(root / binding[name + "_file"])
        replay = C.read(root / binding["replay_file"])
        replay.update(extraction_sha256=binding["extraction_sha256"], range_manifest_sha256=binding["range_manifest_sha256"])
        binding["replay_sha256"] = save(root / binding["replay_file"], replay)
        receipt = C.read(root / binding["extraction_file"])
        source = sources[receipt["source_orthomosaic"]]
        date = C.author_date_group(source)
        row = {"source_observation_id": receipt["sample_id"], "source_id": sample["source_id"],
               "image_sha256": sample["image_sha256"], "source_orthomosaic": source["orthomosaic"],
               "source_commit": source["source_commit"], "parent_sha256": source["imagery_published_lfs_sha256"],
               "author_date_group": date, "author_date_verified": date is not None,
               "observed": sample["observed"], "derivation_sha256": C.canonical_digest(binding)}
        rows.append(row)
        sample["capture_record"] = {"inventory_file": "captures.json", **{k: row[k] for k in
            ("source_observation_id", "parent_sha256", "author_date_group", "author_date_verified")}}
    capture_sha = save(root / "captures.json", {"format": "drone.p6-derived-capture-inventory/v1", "rows": rows})
    for sample in manifest["samples"]:
        if sample["media_origin"] == "real_capture_derivative":
            sample["capture_record"]["inventory_sha256"] = capture_sha
    protocol["derived_capture_policy"]["source_inventory_sha256"] = C.digest(root / "uas.json")


@pytest.fixture
def derived(corpus):
    manifest, labels, protocol, _, root = corpus
    for label in labels["rows"]:
        label["recorded_at"] = "2026-10-19T00:01:00Z"
    sources, planned = [], []
    for n, ident in enumerate(("p", "r"), 1):
        sample = next(s for s in manifest["samples"] if s["sample_id"] == ident)
        yy, xx = indices((50, 50))
        pixels = stack(((xx * 5 + n * 30) % 256, (yy * 3 + n) % 256, ((xx + yy) * 2) % 256, xx * 0 + 255)).astype("uint8")
        pixels[3, :5, :5] = 0
        with MemoryFile() as memory:
            with memory.open(driver="GTiff", width=50, height=50, count=4, dtype="uint8", crs="EPSG:32617",
                             transform=Affine(1, 0, 100, 0, -1, 200), photometric="RGB", compress="deflate",
                             tiled=True, blockxsize=16, blockysize=16) as dataset:
                dataset.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
                dataset.write(pixels)
            parent_bytes = memory.read()
        native = ascontiguousarray(pixels[:3, 3:23, 2:22].transpose(1, 2, 0))
        alpha = pixels[3, 3:23, 2:22]
        image = Image.fromarray(native)
        image.save(root / f"{ident}.jpg", quality=98, subsampling=0)
        Image.fromarray(alpha).save(root / f"{ident}-mask.png")
        sample.update(image_file=f"{ident}.jpg", image_sha256=C.digest(root / f"{ident}.jpg"),
                      media_origin="real_capture_derivative", captured_at=None,
                      observed={k: sample[k] for k in (*C.GROUP_KEYS, "development_exposed")})
        next(row for row in labels["rows"] if row["sample_id"] == ident)["image_sha256"] = sample["image_sha256"]
        name = f"synthetic-{ident}.geo.tif"
        parent = hashlib.sha256(parent_bytes).hexdigest()
        source = {"orthomosaic": name, "source_commit": "c" * 40,
                  "imagery_path": f"train/imagery/UAS/{name}",
                  "imagery_url": f"https://example.test/resolve/{'c' * 40}/train/imagery/UAS/{name}",
                  "imagery_published_lfs_sha256": parent, "imagery_published_bytes": len(parent_bytes),
                  "author_statistics": {"Orthomosaic": name, "Source": "sUAS", "Platform / Provider": "SYNTHETIC ONLY",
                                        "Date (mm/dd/yyy)": f"10/{n}/2022"},
                  "author_code_date_mm_dd_yyyy": f"10-0{n}-2022", "author_code_date_marked_double_check": False}
        (root / f"{ident}-geometry.json").write_bytes(b"SYNTHETIC GEOMETRY ONLY")
        (root / f"{ident}-alignment.json").write_bytes(b"SYNTHETIC ALIGNMENT ONLY")
        source["rda_json_sha256"] = C.digest(root / f"{ident}-geometry.json")
        sources.append(source)
        scope = {"freeze_scope_sha256": "f" * 64, "frozen_at_utc": "2026-10-18T00:00:00Z",
                 "frozen_before_business_annotation_and_model_output": True,
                 "source_condition_polygons_or_model_outputs_used_for_scope": False}
        planned.append({"orthomosaic": name, "imagery_lfs_sha256": parent, "imagery_url": source["imagery_url"],
                        "items": [{"extraction_item_id": ident, "asset_group_id": sample["asset_id"], "bounded_target_id": "synthetic-target",
                                   "target": scope, "view": {"source_alignment_sha256": C.digest(root / f"{ident}-alignment.json")}}]})
        receipt = {"sample_id": ident, "asset_group_id": sample["asset_id"], "bounded_target_id": "synthetic-target",
                   "source_orthomosaic": name, "source_commit": source["source_commit"], "source_published_lfs_sha256": parent,
                   "media_origin": "real_capture_derivative", "status": "extracted", "world_scope": scope,
                   "started_at": "2026-10-18T00:02:00Z", "finished_at": "2026-10-18T00:03:00Z",
                   "raster": {"width": 50, "height": 50, "bands": 4, "dtypes": ["uint8"] * 4,
                              "crs": "EPSG:32617", "transform": [1, 0, 100, 0, -1, 200, 0, 0, 1]},
                   "native_window_xywh": [2, 3, 20, 20], "window_transform": [1, 0, 102, 0, -1, 197, 0, 0, 1],
                   "width": 20, "height": 20, "image_file": str(root / sample["image_file"]), "image_sha256": sample["image_sha256"],
                   "mask_file": str(root / f"{ident}-mask.png"), "mask_sha256": C.digest(root / f"{ident}-mask.png"),
                   "native_rgb_sha256": hashlib.sha256(image.tobytes()).hexdigest(), "valid_pixel_fraction": float((alpha != 0).mean()),
                   "source_geometry_file": str(root / f"{ident}-geometry.json"), "source_geometry_sha256": source["rda_json_sha256"],
                   "source_alignment_file": str(root / f"{ident}-alignment.json"), "source_alignment_sha256": C.digest(root / f"{ident}-alignment.json"),
                   "source_date": source["author_statistics"]["Date (mm/dd/yyy)"], "source_date_double_check_flag": False}
        save(root / f"{ident}-extract.json", receipt)
        ranges = []
        for start in range(0, len(parent_bytes), 1024):
            end = min(start + 1024, len(parent_bytes)) - 1
            prefix = f"{ident}-range" + (f"-{start}" if start else "")
            block_path = root / f"{prefix}.bin"
            block_path.write_bytes(parent_bytes[start:end + 1])
            range_sha = C.digest(block_path)
            range_receipt = {"url": source["imagery_url"], "range": [start, end], "total_bytes": len(parent_bytes), "received_bytes": end - start + 1,
                             "sha256": range_sha, "http_status": 206, "content_range": f"bytes {start}-{end}/{len(parent_bytes)}",
                             "linked_etag_seen": True, "repo_commit_seen": True, "recorded_at": "2026-10-18T00:02:00Z"}
            receipt_sha = save(root / f"{prefix}-receipt.json", range_receipt)
            ranges.append({"range": [start, end], "file": block_path.name, "sha256": range_sha,
                           "receipt_file": f"{prefix}-receipt.json", "receipt_sha256": receipt_sha})
        save(root / f"{ident}-ranges.json", {"format": "drone.p6-raster-source-ranges/v1", "source": source, "block_bytes": 1024,
             "ranges": ranges, "cached_bytes_accessed": len(parent_bytes), "whole_parent_sha256_verified": False})
        reproduced = copy.deepcopy(receipt)
        reproduced.update(image_file=str(root / f"{ident}-replay.jpg"), mask_file=str(root / f"{ident}-replay-mask.png"),
                          started_at="2026-10-18T00:04:00Z", finished_at="2026-10-18T00:05:00Z")
        (root / f"{ident}-replay.jpg").write_bytes((root / f"{ident}.jpg").read_bytes())
        (root / f"{ident}-replay-mask.png").write_bytes((root / f"{ident}-mask.png").read_bytes())
        replay_sha = save(root / f"{ident}-replay-extract.json", reproduced)
        save(root / f"{ident}-replay.json", {"format": "drone.p6-derived-capture-replay/v1", "mode": "offline_cached_ranges",
             "network_requests": 0, "source_observation_id": ident, "parent_sha256": parent,
             "extraction_sha256": C.digest(root / f"{ident}-extract.json"), "range_manifest_sha256": C.digest(root / f"{ident}-ranges.json"),
             "extractor_sha256": "a" * 64, "range_tool_sha256": "b" * 64,
             "started_at": "2026-10-18T00:04:00Z", "finished_at": "2026-10-18T00:05:00Z",
             "replay_extraction_file": f"{ident}-replay-extract.json", "replay_extraction_sha256": replay_sha})
        sample["derivation"] = {"format": "drone.p6-derived-capture-binding/v1", "extraction_file": f"{ident}-extract.json",
                                "extraction_plan_file": "plan.json", "source_inventory_file": "uas.json",
                                "range_manifest_file": f"{ident}-ranges.json", "replay_file": f"{ident}-replay.json"}
    save(root / "uas.json", sources)
    source_plan_sha = save(root / "source-plan.json", {"images": planned})
    save(root / "plan.json", {"source_plan_file": "source-plan.json", "source_plan_sha256": source_plan_sha,
                             "tool_sha256": "a" * 64, "range_tool_sha256": "b" * 64, "created_at": "2026-10-18T00:01:00Z",
                             "source_names": [s["orthomosaic"] for s in sources]})
    protocol["derived_capture_policy"] = {"mode": "traceable_uas_orthomosaic", "source_inventory_sha256": "",
                                          "extractor_sha256": "a" * 64, "range_tool_sha256": "b" * 64,
                                          "claim_raw_camera_frames": False, "synthetic_scene_content_allowed": False}
    manifest["stability_pairs"][0].update(comparison_scope=C.DERIVED_SCOPE, distinct_captures_human_confirmed=True)
    refresh(corpus)
    return corpus


def test_valid_derivative_is_scoped_and_keeps_original_capture_path(derived):
    result = C.audit(*derived)
    assert result["data_ready"] and result["derived_captures_verified"] == 2
    assert not result["formal_release_eligible"] and not result["model_quality_verified"]
    metrics = C.scores(derived[0], derived[1], BASE["predictions"](derived),
                       {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")
    assert metrics["stability"]["comparison_scope_counts"] == {C.DERIVED_SCOPE: 1}


@pytest.mark.parametrize("file", ["p-range.bin", "p-range-receipt.json", "p-extract.json", "p.jpg", "p-mask.png", "source-plan.json", "captures.json",
                                 "p-replay.json", "p-replay-extract.json", "p-replay.jpg", "p-replay-mask.png"])
def test_changed_or_missing_evidence_never_admits_derivatives(derived, file):
    (derived[-1] / file).write_bytes(b"CHANGED SYNTHETIC EVIDENCE")
    result = C.audit(*derived)
    assert "derived_capture_not_verified" in result["issue_counts"]
    assert "derived_pair_inventory_not_verified" in result["issue_counts"]


def test_missing_range_file_is_rejected(derived):
    root = derived[-1]
    ranges = C.read(root / "p-ranges.json")
    ranges["ranges"][0]["file"] = "never-created.bin"
    save(root / "p-ranges.json", ranges)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("field,value", [("content_range", "bytes 1-4/8"), ("received_bytes", 3),
                                        ("url", "https://example.test/different-source"), ("http_status", 200),
                                        ("linked_etag_seen", False), ("repo_commit_seen", False)])
def test_rebound_http_receipt_must_still_describe_the_actual_source(derived, field, value):
    root = derived[-1]
    receipt = C.read(root / "p-range-receipt.json")
    receipt[field] = value
    ranges = C.read(root / "p-ranges.json")
    ranges["ranges"][0]["receipt_sha256"] = save(root / "p-range-receipt.json", receipt)
    save(root / "p-ranges.json", ranges)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("tamper", ["duplicate", "wrong_source", "false_full_hash", "wrong_count"])
def test_range_manifest_does_not_prove_unacquired_or_foreign_bytes(derived, tamper):
    root = derived[-1]
    ranges = C.read(root / "p-ranges.json")
    if tamper == "duplicate":
        ranges["ranges"] *= 2
        ranges["cached_bytes_accessed"] *= 2
    elif tamper == "wrong_source":
        ranges["source"] = C.read(root / "uas.json")[1]
    elif tamper == "false_full_hash":
        ranges["whole_parent_sha256_verified"] = True
        omitted = ranges["ranges"].pop()
        ranges["cached_bytes_accessed"] -= omitted["range"][1] - omitted["range"][0] + 1
    else:
        ranges["cached_bytes_accessed"] = 8
    save(root / "p-ranges.json", ranges)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("field,value", [("width", 19), ("native_window_xywh", [2, 3, 21, 20]),
                                        ("native_window_xywh", [45, 3, 20, 20]), ("window_transform", [1, 0, 103, 0, -1, 197, 0, 0, 1]),
                                        ("valid_pixel_fraction", .5), ("asset_group_id", "wrong-physical-asset"), ("status", "failed")])
def test_rebound_extraction_must_match_pixels_geometry_and_fixed_target(derived, field, value):
    root = derived[-1]
    receipt = C.read(root / "p-extract.json")
    receipt[field] = value
    save(root / "p-extract.json", receipt)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


def test_a_rebound_alpha_image_cannot_have_different_dimensions(derived):
    root = derived[-1]
    Image.new("L", (19, 20), 255).save(root / "p-mask.png")
    receipt = C.read(root / "p-extract.json")
    receipt["mask_sha256"] = C.digest(root / "p-mask.png")
    save(root / "p-extract.json", receipt)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("tamper", ["source_type", "source_path", "extractor", "range_tool", "generated_allowed", "raw_claim"])
def test_only_explicit_uas_and_approved_extraction_policy_are_allowed(derived, tamper):
    root = derived[-1]
    if tamper.startswith("source_"):
        source = C.read(root / "uas.json")
        if tamper == "source_type":
            source[0]["author_statistics"]["Source"] = "Satellite"
        else:
            source[0]["imagery_path"] = "train/imagery/Manned/synthetic-p.geo.tif"
        save(root / "uas.json", source)
    else:
        policy = derived[2]["derived_capture_policy"]
        policy[{"extractor": "extractor_sha256", "range_tool": "range_tool_sha256",
                "generated_allowed": "synthetic_scene_content_allowed", "raw_claim": "claim_raw_camera_frames"}[tamper]] = "d" * 64 if tamper in {"extractor", "range_tool"} else True
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("tamper", ["same_parent", "same_date", "conflicting_date", "unverified_date"])
def test_derived_pairs_need_distinct_clean_author_date_groups(derived, tamper):
    manifest, _, _, _, root = derived
    if tamper == "same_parent":
        samples = {s["sample_id"]: s for s in manifest["samples"]}
        samples["r"]["capture_record"]["parent_sha256"] = samples["p"]["capture_record"]["parent_sha256"]
    else:
        sources = C.read(root / "uas.json")
        source = sources[1]
        if tamper == "same_date":
            source["author_statistics"]["Date (mm/dd/yyy)"] = "10/1/2022"
            source["author_code_date_mm_dd_yyyy"] = "10-01-2022"
        elif tamper == "conflicting_date":
            source["author_code_date_mm_dd_yyyy"] = "10-03-2022"
        else:
            source["author_code_date_marked_double_check"] = True
        save(root / "uas.json", sources)
        receipt = C.read(root / "r-extract.json")
        receipt.update(source_date=source["author_statistics"]["Date (mm/dd/yyy)"],
                       source_date_double_check_flag=source["author_code_date_marked_double_check"])
        save(root / "r-extract.json", receipt)
        reproduced = C.read(root / "r-replay-extract.json")
        reproduced.update(source_date=receipt["source_date"], source_date_double_check_flag=receipt["source_date_double_check_flag"])
        replay = C.read(root / "r-replay.json")
        replay["replay_extraction_sha256"] = save(root / "r-replay-extract.json", reproduced)
        save(root / "r-replay.json", replay)
        ranges = C.read(root / "r-ranges.json")
        ranges["source"] = source
        save(root / "r-ranges.json", ranges)
        refresh(derived)
    result = C.audit(*derived)
    assert "distinct_derived_capture_identity_missing" in result["issue_counts"]
    if tamper != "same_parent":
        assert result["derived_captures_verified"] == 2


def test_capture_inventory_cannot_reassign_an_observation(derived):
    root = derived[-1]
    captures = C.read(root / "captures.json")
    captures["rows"][0]["observed"]["asset_id"] = "invented-new-asset"
    sha = save(root / "captures.json", captures)
    for s in derived[0]["samples"]:
        if s["media_origin"] == "real_capture_derivative":
            s["capture_record"]["inventory_sha256"] = sha
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


def test_same_asset_pair_cannot_substitute_another_preregistered_physical_extent(derived):
    root = derived[-1]
    source_plan = C.read(root / "source-plan.json")
    source_plan["images"][1]["items"][0]["target"]["freeze_scope_sha256"] = "e" * 64
    plan = C.read(root / "plan.json")
    plan["source_plan_sha256"] = save(root / "source-plan.json", source_plan)
    save(root / "plan.json", plan)
    receipt = C.read(root / "r-extract.json")
    receipt["world_scope"] = source_plan["images"][1]["items"][0]["target"]
    save(root / "r-extract.json", receipt)
    reproduced = C.read(root / "r-replay-extract.json")
    reproduced["world_scope"] = receipt["world_scope"]
    replay = C.read(root / "r-replay.json")
    replay["replay_extraction_sha256"] = save(root / "r-replay-extract.json", reproduced)
    save(root / "r-replay.json", replay)
    refresh(derived)
    result = C.audit(*derived)
    assert result["derived_captures_verified"] == 2
    assert "derived_pair_fixed_target_mismatch" in result["issue_counts"]


def register_human_task(sample, label, reference, root):
    sample["target"]["brief"] = "Synthetic preregistered road target"
    reference.update(source_reference="Synthetic fixture source only", target_brief="Synthetic expected road",
                     target_polygon=reference["target"]["polygon"])
    task = {"format": "drone.p6-road-prior-task/v1", "origin": "human_task", "registrar_id": "synthetic-registrar",
            "registration_evidence": "Synthetic registration only", "registered_at": "2026-10-18T00:06:00Z",
            "sample_id": sample["sample_id"], "image_sha256": sample["image_sha256"], "expected_asset_id": sample["asset_id"],
            "target_brief": sample["target"]["brief"], "target_polygon": sample["target"]["polygon"], "expected_reference": reference}
    label["task_evidence"] = {"prior_task_file": "prior-task.json", "prior_task_sha256": save(root / "prior-task.json", task),
                              "expected_target_present": False, "expected_reference_checked": True, "evidence": "Synthetic mismatch inspection",
                              "inspection_file_sha256": {sample["image_file"]: sample["image_sha256"], reference["image_file"]: reference["image_sha256"]}}
    return task, label


def prior_task(value):
    manifest, labels, _, _, root = value
    sample = next(s for s in manifest["samples"] if s["sample_id"] == "p")
    sample["asset_id"] = "expected-asset"
    label = next(s for s in labels["rows"] if s["sample_id"] == "p")
    label.update(verdict="wrong_target", defect_types=[])
    reference = copy.deepcopy(next(s for s in manifest["samples"] if s["sample_id"] == "n"))
    reference["asset_id"] = "expected-asset"
    return register_human_task(sample, label, reference, root)


def test_wrong_target_keeps_requested_and_observed_asset_identity_separate(derived):
    prior_task(derived)
    result = C.audit(*derived)
    assert result["derived_captures_verified"] == 2
    assert "derived_capture_not_verified" not in result["issue_counts"]


def test_wrong_target_expected_reference_may_not_hide_development_exposure(derived):
    task, _ = prior_task(derived)
    derived[3]["image_sha256"].append(task["expected_reference"]["image_sha256"])
    assert "exposed_expected_reference_outside_development" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("field", C.GROUP_KEYS)
def test_wrong_target_observed_and_expected_groups_cannot_cross_splits(derived, field):
    task, label = prior_task(derived)
    calibration = next(s for s in derived[0]["samples"] if s["sample_id"] == "cal")
    task["expected_reference"][field] = calibration[field]
    if field == "asset_id":
        task["expected_asset_id"] = calibration[field]
        next(s for s in derived[0]["samples"] if s["sample_id"] == "p")["asset_id"] = calibration[field]
    label["task_evidence"]["prior_task_sha256"] = save(derived[-1] / "prior-task.json", task)
    assert "cross_split_group_leakage" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("field", C.GROUP_KEYS)
def test_wrong_target_current_observed_groups_are_also_isolated(derived, field):
    prior_task(derived)
    sample = next(s for s in derived[0]["samples"] if s["sample_id"] == "p")
    calibration = next(s for s in derived[0]["samples"] if s["sample_id"] == "cal")
    sample["observed"][field] = calibration[field]
    refresh(derived)
    assert "cross_split_group_leakage" in C.audit(*derived)["issue_counts"]


def test_expected_reference_parent_exposure_is_checked_even_for_a_new_crop(derived):
    manifest, labels, _, exposure, root = derived
    sample = next(s for s in manifest["samples"] if s["sample_id"] == "w")
    reference = copy.deepcopy(next(s for s in manifest["samples"] if s["sample_id"] == "p"))
    sample["observed"] = {k: sample[k] for k in (*C.GROUP_KEYS, "development_exposed")}
    sample["asset_id"] = reference["asset_id"]
    label = next(s for s in labels["rows"] if s["sample_id"] == "w")
    task, label = register_human_task(sample, label, reference, root)
    result = C.audit(*derived)
    assert "wrong_target_reference_provenance_not_verified" not in result["issue_counts"]
    exposure["groups"] = {"parent_sha256": [reference["capture_record"]["parent_sha256"]]}
    assert "exposed_expected_reference_outside_development" in C.audit(*derived)["issue_counts"]
    task["expected_reference"]["derivation"]["extraction_sha256"] = "0" * 64
    label["task_evidence"]["prior_task_sha256"] = save(root / "prior-task.json", task)
    assert "wrong_target_reference_provenance_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("field,value", [("network_requests", 1), ("network_requests", False), ("mode", "online"),
                                        ("extractor_sha256", "d" * 64), ("range_tool_sha256", "d" * 64),
                                        ("parent_sha256", "d" * 64), ("started_at", "2026-10-18T00:00:00Z")])
def test_rebound_replay_must_prove_the_registered_offline_attempt(derived, field, value):
    root = derived[-1]
    replay = C.read(root / "p-replay.json")
    replay[field] = value
    save(root / "p-replay.json", replay)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("field,value", [("native_rgb_sha256", "d" * 64), ("image_sha256", "d" * 64), ("mask_sha256", "d" * 64),
                                        ("image_file", "p.jpg"), ("mask_file", "p-mask.png")])
def test_replay_requires_identical_native_and_encoded_bytes_at_separate_paths(derived, field, value):
    root = derived[-1]
    reproduced = C.read(root / "p-replay-extract.json")
    reproduced[field] = value
    replay = C.read(root / "p-replay.json")
    replay["replay_extraction_sha256"] = save(root / "p-replay-extract.json", reproduced)
    save(root / "p-replay.json", replay)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


def test_derived_observation_cannot_claim_to_be_an_original_frame(derived):
    next(s for s in derived[0]["samples"] if s["sample_id"] == "p")["media_origin"] = "real_capture"
    assert "derived_capture_media_origin_mismatch" in C.audit(*derived)["issue_counts"]


def test_author_date_only_derivative_cannot_invent_a_capture_timestamp(derived):
    next(s for s in derived[0]["samples"] if s["sample_id"] == "p")["captured_at"] = "2022-10-01T12:00:00Z"
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


def test_two_matching_forged_outputs_do_not_override_unchanged_source_pixels(derived):
    manifest, labels, _, _, root = derived
    sample = next(s for s in manifest["samples"] if s["sample_id"] == "p")
    purple = Image.new("RGB", (20, 20), (160, 0, 160))
    purple.save(root / "p.jpg", quality=98, subsampling=0)
    (root / "p-replay.jpg").write_bytes((root / "p.jpg").read_bytes())
    forged = C.digest(root / "p.jpg")
    sample["image_sha256"] = forged
    next(row for row in labels["rows"] if row["sample_id"] == "p")["image_sha256"] = forged
    for name in ("p-extract.json", "p-replay-extract.json"):
        receipt = C.read(root / name)
        receipt.update(image_sha256=forged, native_rgb_sha256=hashlib.sha256(purple.tobytes()).hexdigest())
        save(root / name, receipt)
    replay = C.read(root / "p-replay.json")
    replay["replay_extraction_sha256"] = C.digest(root / "p-replay-extract.json")
    save(root / "p-replay.json", replay)
    refresh(derived)
    result = C.audit(*derived)
    assert not result["data_ready"]
    assert any(i["code"] == "derived_capture_not_verified" and "actual local TIFF pixels" in i["reason"] for i in result["issues"])


def test_a_missing_required_tiff_block_cannot_be_zero_filled_or_fetched(derived):
    root = derived[-1]
    ranges = C.read(root / "p-ranges.json")
    removed = ranges["ranges"].pop(1)
    ranges["cached_bytes_accessed"] -= removed["range"][1] - removed["range"][0] + 1
    save(root / "p-ranges.json", ranges)
    refresh(derived)
    result = C.audit(*derived)
    assert not result["data_ready"] and "derived_capture_not_verified" in result["issue_counts"]


def test_rebound_non_tiff_source_bytes_cannot_prove_an_extraction(derived):
    root = derived[-1]
    ranges = C.read(root / "p-ranges.json")
    row = ranges["ranges"][0]
    (root / row["file"]).write_bytes(b"x" * (row["range"][1] + 1))
    row["sha256"] = C.digest(root / row["file"])
    receipt = C.read(root / row["receipt_file"])
    receipt["sha256"] = row["sha256"]
    row["receipt_sha256"] = save(root / row["receipt_file"], receipt)
    save(root / "p-ranges.json", ranges)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


def test_decoder_never_opens_source_url_or_falls_back_to_another_driver(derived, monkeypatch):
    opened, original = [], rasterio.open

    def local_only(path, *args, **kwargs):
        opened.append((path, kwargs["driver"]))
        assert path == "p6-local-source.tif" and kwargs["driver"] == "GTiff"
        assert kwargs["opener"] is not None
        with pytest.raises(FileNotFoundError):
            kwargs["opener"]("https://example.test/source.tif")
        with pytest.raises(FileNotFoundError):
            kwargs["opener"](path, "wb")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(rasterio, "open", local_only)
    assert C.audit(*derived)["data_ready"]
    assert opened == [("p6-local-source.tif", "GTiff")] * 2


def test_decoder_bounds_window_size_before_opening_a_source(derived, monkeypatch):
    root = derived[-1]
    receipt = C.read(root / "p-extract.json")
    receipt["native_window_xywh"] = [0, 0, 10000, 6001]

    def unexpected(*args, **kwargs):
        raise AssertionError("oversized window reached the TIFF decoder")

    monkeypatch.setattr(rasterio, "open", unexpected)
    with pytest.raises(ValueError, match="60 MP"):
        C.VERIFY_RASTER_WINDOW(receipt, C.read(root / "p-ranges.json"), root, {}, "synthetic-range-digest")


@pytest.mark.parametrize("field,value", [("width", 51), ("height", 51), ("crs", "EPSG:4326")])
def test_self_consistent_receipts_cannot_change_actual_parent_georeferencing(derived, field, value):
    root = derived[-1]
    for name in ("p-extract.json", "p-replay-extract.json"):
        receipt = C.read(root / name)
        receipt["raster"][field] = value
        save(root / name, receipt)
    replay = C.read(root / "p-replay.json")
    replay["replay_extraction_sha256"] = C.digest(root / "p-replay-extract.json")
    save(root / "p-replay.json", replay)
    refresh(derived)
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


def test_scope_must_precede_even_a_non_ai_final_annotation(derived):
    next(r for r in derived[1]["rows"] if r["sample_id"] == "p")["recorded_at"] = "2026-10-15T00:01:00Z"
    result = C.audit(*derived)
    assert not result["data_ready"] and "derived_capture_not_verified" in result["issue_counts"]


def test_later_final_label_does_not_hide_an_ai_first_pass_before_scope_freeze(derived):
    root = derived[-1]
    sample = next(s for s in derived[0]["samples"] if s["sample_id"] == "p")
    label = next(r for r in derived[1]["rows"] if r["sample_id"] == "p")
    sha = save(root / "early-draft.json", {"rows": [{"sample_id": "p", "image_sha256": sample["image_sha256"],
                                                    "inspected_at": "2026-10-17T00:00:00Z"}]})
    label["ai_assistance"] = {"draft_file": "early-draft.json", "draft_sha256": sha}
    assert "derived_capture_not_verified" in C.audit(*derived)["issue_counts"]


@pytest.mark.parametrize("tamper", ["missing", "late_task", "unchecked_reference", "missing_inspection", "wrong_task_identity"])
def test_derived_wrong_target_always_requires_equivalent_prior_task_evidence(derived, tamper):
    task, label = prior_task(derived)
    root = derived[-1]
    if tamper == "missing":
        label.pop("task_evidence")
    elif tamper == "late_task":
        task["registered_at"] = "2026-10-20T00:00:00Z"
        label["task_evidence"]["prior_task_sha256"] = save(root / "prior-task.json", task)
    elif tamper == "unchecked_reference":
        label["task_evidence"]["expected_reference_checked"] = False
    elif tamper == "missing_inspection":
        label["task_evidence"]["inspection_file_sha256"].pop(task["expected_reference"]["image_file"])
    else:
        task["image_sha256"] = "0" * 64
        label["task_evidence"]["prior_task_sha256"] = save(root / "prior-task.json", task)
    result = C.audit(*derived)
    assert not result["data_ready"] and "derived_wrong_target_prior_task_not_verified" in result["issue_counts"]


def test_expected_derivative_scope_must_precede_prior_task_registration(derived):
    manifest, labels, _, _, root = derived
    sample = next(s for s in manifest["samples"] if s["sample_id"] == "w")
    reference = copy.deepcopy(next(s for s in manifest["samples"] if s["sample_id"] == "p"))
    sample["observed"] = {k: sample[k] for k in (*C.GROUP_KEYS, "development_exposed")}
    sample["asset_id"] = reference["asset_id"]
    label = next(s for s in labels["rows"] if s["sample_id"] == "w")
    task, label = register_human_task(sample, label, reference, root)
    task["registered_at"] = "2026-10-17T23:59:59Z"
    label["task_evidence"]["prior_task_sha256"] = save(root / "prior-task.json", task)
    assert "wrong_target_reference_provenance_not_verified" in C.audit(*derived)["issue_counts"]


def test_ai_assisted_human_review_can_bind_its_prior_task_in_task_evidence(derived):
    task, label = prior_task(derived)
    sample = next(s for s in derived[0]["samples"] if s["sample_id"] == "p")
    root = derived[-1]
    sha = save(root / "first-draft.json", {"rows": [{"sample_id": "p", "image_sha256": sample["image_sha256"],
                                                    "inspected_at": "2026-10-18T01:00:00Z"}]})
    label["ai_assistance"] = {"draft_file": "first-draft.json", "draft_sha256": sha}
    assert C.verified_prior_task(sample, label, derived[2], root, {}) == task["expected_reference"]


def test_derived_pair_ai_origin_still_requires_registered_independent_review(derived):
    pair = derived[0]["stability_pairs"][0]
    pair.update(origin="delegated_ai", same_condition_ai_verified=True, distinct_captures_ai_verified=True)
    assert "delegated_pair_evidence_missing_or_invalid" in C.audit(*derived)["issue_counts"]


def test_shared_parent_range_bytes_are_not_rehashed_per_observation(derived, monkeypatch):
    root = derived[-1]
    sample = next(s for s in derived[0]["samples"] if s["sample_id"] == "p")
    checked, original_digest = [], C.derived_capture.__globals__["digest"]

    def counted(path):
        checked.append(path.name)
        return original_digest(path)

    monkeypatch.setitem(C.derived_capture.__globals__, "digest", counted)
    cache = {}
    for _ in range(3):
        C.derived_capture(sample, derived[2], root, cache)
    assert checked.count("p-range.bin") == 1


def test_full_parent_claim_requires_complete_ranges_and_recomputed_public_digest(derived):
    root = derived[-1]
    ranges = C.read(root / "p-ranges.json")
    source = copy.deepcopy(ranges["source"])
    source["imagery_published_bytes"] = 8
    (root / "parent-head.bin").write_bytes(b"head")
    (root / "parent-tail.bin").write_bytes(b"tail")
    rows = []
    for start, name in ((0, "head"), (4, "tail")):
        sha = C.digest(root / f"parent-{name}.bin")
        receipt = C.read(root / "p-range-receipt.json")
        receipt.update(range=[start, start + 3], sha256=sha, content_range=f"bytes {start}-{start + 3}/8", total_bytes=8, received_bytes=4)
        receipt_sha = save(root / f"parent-{name}.json", receipt)
        rows.append({"range": [start, start + 3], "file": f"parent-{name}.bin", "sha256": sha,
                     "receipt_file": f"parent-{name}.json", "receipt_sha256": receipt_sha})
    source["imagery_published_lfs_sha256"] = hashlib.sha256(b"headtail").hexdigest()
    ranges.update(source=source, ranges=rows, block_bytes=4, cached_bytes_accessed=8, whole_parent_sha256_verified=True)
    C.verify_source_ranges(ranges, source, root, {})
    source["imagery_published_lfs_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="whole parent"):
        C.verify_source_ranges(ranges, source, root, {})
