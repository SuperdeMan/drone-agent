"""Synthetic RGB GeoTIFF fixtures verify OAM provenance, not business quality.

合成 RGB GeoTIFF 夹具验证 OAM 来源，不证明业务效果。
"""
from __future__ import annotations

import copy
import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from affine import Affine
from PIL import Image
from rasterio.enums import ColorInterp
from rasterio.io import MemoryFile

BASE = runpy.run_path(str(Path(__file__).with_name("test_p6_business_eval.py")))
C = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_business_eval.py")))
corpus = BASE["corpus"]
ID = "5f515554805b400005d7e598"
URL = "https://oin-hotosm-temp.s3.amazonaws.com/5f5154e2805b400005d7e596/0/5f5154e2805b400005d7e597.tif"


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return C.digest(path)


def refresh(value):
    manifest, labels, protocol, _, root = value
    sample = next(s for s in manifest["samples"] if s["sample_id"] == "p")
    source = C.read(root / "oam-sources.json")[0]
    source["catalog_sha256"] = C.digest(root / "catalog.json")
    download = C.read(root / "get.json")
    download["source_metadata_sha256"] = source["catalog_sha256"]
    source["download_receipt_sha256"] = save(root / "get.json", download)
    save(root / "oam-sources.json", [source])
    plan = C.read(root / "oam-plan.json")
    plan["source_plan_sha256"] = C.digest(root / "oam-source-plan.json")
    save(root / "oam-plan.json", plan)
    binding = sample["derivation"]
    for name in ("extraction", "extraction_plan", "source_inventory"):
        binding[name + "_sha256"] = C.digest(root / binding[name + "_file"])
    replay = C.read(root / "oam-replay.json")
    replay.update(extraction_sha256=binding["extraction_sha256"], replay_extraction_sha256=C.digest(root / "oam-replay-extract.json"))
    binding["replay_sha256"] = save(root / "oam-replay.json", replay)
    row = {"source_observation_id": "p", "source_id": sample["source_id"], "image_sha256": sample["image_sha256"],
           "catalog_object_id": ID, "parent_sha256": source["local_full_object_sha256"],
           "author_date_group": None, "author_date_verified": False, "repeated_capture_eligible": False,
           "observed": sample["observed"], "derivation_sha256": C.canonical_digest(binding)}
    sample["capture_record"] = {"inventory_file": "oam-captures.json",
        "inventory_sha256": save(root / "oam-captures.json", {"format": "drone.p6-derived-capture-inventory/v1", "rows": [row]}),
        **{k: row[k] for k in ("source_observation_id", "parent_sha256", "author_date_group", "author_date_verified")}}
    protocol["oam_capture_policy"]["source_inventory_sha256"] = binding["source_inventory_sha256"]
    next(r for r in labels["rows"] if r["sample_id"] == "p")["image_sha256"] = sample["image_sha256"]


@pytest.fixture
def oam(corpus):
    manifest, labels, protocol, _, root = corpus
    sample = next(s for s in manifest["samples"] if s["sample_id"] == "p")
    for row in labels["rows"]:
        row["recorded_at"] = "2026-10-19T00:10:00Z"
    yy, xx = np.indices((50, 50))
    pixels = np.stack(((xx * 5 + 1) % 256, (yy * 3 + 5) % 256, ((xx + yy) * 2 + 1) % 256)).astype("uint8")
    mask = np.full((50, 50), 255, dtype="uint8")
    mask[:5, :5] = 0
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=50, height=50, count=3, dtype="uint8", crs="EPSG:32630",
                      transform=Affine(1, 0, 100, 0, -1, 200), photometric="RGB", tiled=True, blockxsize=16, blockysize=16) as ds:
            ds.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
            ds.write(pixels)
            ds.write_mask(mask)
        parent_bytes = mem.read()
    (root / "parent.tif").write_bytes(parent_bytes)
    parent_hash = C.digest(root / "parent.tif")
    native = np.ascontiguousarray(pixels[:, 3:23, 2:22].transpose(1, 2, 0))
    alpha = mask[3:23, 2:22]
    Image.fromarray(native).save(root / "p.jpg", quality=98, subsampling=0)
    Image.fromarray(alpha).save(root / "p-mask.png")
    sample.update(image_file="p.jpg", image_sha256=C.digest(root / "p.jpg"), media_origin="real_capture_derivative", captured_at=None,
                  observed={k: sample[k] for k in (*C.GROUP_KEYS, "development_exposed")})
    catalog = {"results": {"_id": ID, "uuid": URL, "platform": "uav", "file_size": len(parent_bytes),
        "acquisition_start": "2020-08-18T23:00:00Z", "acquisition_end": "2020-08-19T00:00:00Z",
        "properties": {"license": "CC-BY 4.0", "sensor": "SYNTHETIC FIXTURE", "bands": 3, "dtype": "uint8",
                       "colorinterp": ["red", "green", "blue"], "crs": "EPSG:32630", "dimensions": [50, 50]}}}
    save(root / "catalog.json", catalog)
    etag = '"' + "a" * 32 + '-3"'
    save(root / "get.json", {"format": "drone.p6-oam-full-object-http/v1", "catalog_object_id": ID,
        "source_metadata_url": "https://api.openaerialmap.org/meta/" + ID,
        "status": "downloaded_and_verified", "checksum_origin": "locally_computed_full_object", "publisher_sha256": None,
        "request": {"method": "GET", "url": URL, "headers": {"If-Match": etag}},
        "response": {"status": 200, "final_url": URL, "headers": {"ETag": etag, "Content-Length": str(len(parent_bytes))}},
        "expected_etag": etag, "response_etag": etag, "expected_bytes": len(parent_bytes), "body_bytes": len(parent_bytes),
        "response_content_length": len(parent_bytes), "local_file": str(root / "parent.tif"), "local_full_object_sha256": parent_hash,
        "started_at_utc": "2026-10-19T00:00:00Z", "finished_at_utc": "2026-10-19T00:01:00Z"})
    save(root / "oam-sources.json", [{"catalog_object_id": ID, "catalog_url": "https://api.openaerialmap.org/meta/" + ID,
        "catalog_file": "catalog.json", "object_file": "parent.tif", "local_full_object_sha256": parent_hash,
        "download_receipt_file": "get.json", "publisher_sha256": None, "checksum_origin": "locally_computed_full_object"}])
    (root / "geometry.xml").write_bytes(b"SYNTHETIC OSM GEOMETRY ONLY")
    scope = {"freeze_scope_sha256": "b" * 64, "frozen_at_utc": "2026-10-18T00:00:00Z",
             "frozen_before_business_annotation_and_model_output": True, "source_condition_polygons_or_model_outputs_used_for_scope": False}
    save(root / "oam-source-plan.json", {"items": [{"extraction_item_id": "p", "catalog_object_id": ID,
        "asset_group_id": sample["asset_id"], "bounded_target_id": "fixture-target", "target": scope,
        "source_geometry_sha256": C.digest(root / "geometry.xml")}]})
    save(root / "oam-plan.json", {"tool_sha256": "a" * 64, "created_at": "2026-10-19T00:01:00Z", "source_plan_file": "oam-source-plan.json"})
    receipt = {"sample_id": "p", "asset_group_id": sample["asset_id"], "bounded_target_id": "fixture-target", "catalog_object_id": ID,
        "source_kind": "oam_uav", "parent_sha256": parent_hash, "status": "extracted", "alignment_method": "georeferencing_only",
        "media_origin": "real_capture_derivative", "started_at": "2026-10-19T00:02:00Z", "finished_at": "2026-10-19T00:03:00Z",
        "raster": {"width": 50, "height": 50, "bands": 3, "dtypes": ["uint8"] * 3, "crs": "EPSG:32630",
                   "transform": [1, 0, 100, 0, -1, 200, 0, 0, 1]}, "native_window_xywh": [2, 3, 20, 20],
        "window_transform": [1, 0, 102, 0, -1, 197, 0, 0, 1], "width": 20, "height": 20,
        "image_file": str(root / "p.jpg"), "image_sha256": sample["image_sha256"],
        "mask_file": str(root / "p-mask.png"), "mask_sha256": C.digest(root / "p-mask.png"),
        "native_rgb_sha256": hashlib.sha256(native.tobytes()).hexdigest(), "valid_pixel_fraction": float((alpha != 0).mean()),
        "source_geometry_file": str(root / "geometry.xml"), "source_geometry_sha256": C.digest(root / "geometry.xml"), "world_scope": scope}
    save(root / "oam-extract.json", receipt)
    reproduced = copy.deepcopy(receipt)
    reproduced.update(image_file=str(root / "p-replay.jpg"), mask_file=str(root / "p-replay-mask.png"),
                      started_at="2026-10-19T00:04:00Z", finished_at="2026-10-19T00:05:00Z")
    (root / "p-replay.jpg").write_bytes((root / "p.jpg").read_bytes())
    (root / "p-replay-mask.png").write_bytes((root / "p-mask.png").read_bytes())
    save(root / "oam-replay-extract.json", reproduced)
    save(root / "oam-replay.json", {"format": "drone.p6-oam-derived-capture-replay/v1", "mode": "offline_complete_object",
        "network_requests": 0, "source_observation_id": "p", "parent_sha256": parent_hash, "extractor_sha256": "a" * 64,
        "started_at": "2026-10-19T00:04:00Z", "finished_at": "2026-10-19T00:05:00Z", "replay_extraction_file": "oam-replay-extract.json"})
    sample["derivation"] = {"format": "drone.p6-oam-derived-capture-binding/v1", "source_inventory_file": "oam-sources.json",
        "extraction_file": "oam-extract.json", "extraction_plan_file": "oam-plan.json", "replay_file": "oam-replay.json"}
    protocol["oam_capture_policy"] = {"mode": "traceable_oam_uav_orthomosaic", "claim_raw_camera_frames": False,
        "synthetic_scene_content_allowed": False, "repeated_capture_eligible": False, "extractor_sha256": "a" * 64}
    refresh(corpus)
    return corpus


def check(value):
    sample = next(s for s in value[0]["samples"] if s["sample_id"] == "p")
    return C.derived_capture(sample, value[2], value[-1], {}, scope_before=C.timestamp("2026-10-19T00:10:00Z"))


def test_complete_rgb_source_uses_internal_mask_and_cannot_establish_repeat(oam):
    row = check(oam)
    assert row["author_date_verified"] is False and row["author_date_group"] is None
    assert row["repeated_capture_eligible"] is False
    assert "derived_capture_not_verified" not in C.audit(*oam)["issue_counts"]
    assert "stability_capture_time_missing" in C.audit(*oam)["issue_counts"]
    assert C.read(oam[-1] / "oam-extract.json")["valid_pixel_fraction"] < 1


@pytest.mark.parametrize("path,value", [("platform", "aircraft"), ("properties.license", "CC BY-NC 4.0"),
    ("properties.sensor", ""), ("properties.bands", 4), ("properties.crs", "EPSG:4326"),
    ("properties.dimensions", [51, 50]), ("uuid", "https://unrelated.test/scene.tif")])
def test_rebound_catalog_metadata_is_checked_per_object(oam, path, value):
    root = oam[-1]
    doc = C.read(root / "catalog.json")
    item = doc["results"]
    parts = path.split(".")
    for part in parts[:-1]:
        item = item[part]
    item[parts[-1]] = value
    save(root / "catalog.json", doc)
    refresh(oam)
    with pytest.raises(ValueError):
        check(oam)


@pytest.mark.parametrize("path,value", [("response.status", 206), ("request.method", "HEAD"),
    ("body_bytes", 1), ("response_etag", '"' + "b" * 32 + '"'), ("publisher_sha256", "a" * 64),
    ("request.headers.Range", "bytes=0-100"), ("request.headers.If-Match", "*"),
    ("response.headers.Content-Range", "bytes 0-100/101"), ("response.final_url", URL + "?different=1")])
def test_partial_or_mismatched_download_cannot_claim_full_object(oam, path, value):
    root = oam[-1]
    doc = C.read(root / "get.json")
    item = doc
    parts = path.split(".")
    for part in parts[:-1]:
        item = item[part]
    item[parts[-1]] = value
    save(root / "get.json", doc)
    refresh(oam)
    with pytest.raises(ValueError):
        check(oam)


def test_forged_matching_output_and_replay_do_not_override_source_pixels(oam):
    root = oam[-1]
    fake = Image.new("RGB", (20, 20), (160, 0, 160))
    fake.save(root / "p.jpg", quality=98, subsampling=0)
    (root / "p-replay.jpg").write_bytes((root / "p.jpg").read_bytes())
    sample = next(s for s in oam[0]["samples"] if s["sample_id"] == "p")
    sample["image_sha256"] = C.digest(root / "p.jpg")
    for name in ("oam-extract.json", "oam-replay-extract.json"):
        row = C.read(root / name)
        row.update(image_sha256=sample["image_sha256"], native_rgb_sha256=hashlib.sha256(fake.tobytes()).hexdigest())
        save(root / name, row)
    refresh(oam)
    with pytest.raises(ValueError, match="actual local TIFF pixels"):
        check(oam)


@pytest.mark.parametrize("change", ["late_scope", "repeat_claim", "replay_network", "shared_replay", "missing_parent", "invented_lfs"])
def test_oam_keeps_scope_replay_and_capture_boundaries(oam, change):
    root = oam[-1]
    if change == "late_scope":
        plan = C.read(root / "oam-source-plan.json")
        plan["items"][0]["target"]["frozen_at_utc"] = "2026-10-19T00:09:00Z"
        save(root / "oam-source-plan.json", plan)
        row = C.read(root / "oam-extract.json")
        row["world_scope"] = plan["items"][0]["target"]
        save(root / "oam-extract.json", row)
    elif change == "replay_network":
        row = C.read(root / "oam-replay.json")
        row["network_requests"] = 1
        save(root / "oam-replay.json", row)
    elif change == "shared_replay":
        row = C.read(root / "oam-replay-extract.json")
        row["image_file"] = str(root / "p.jpg")
        save(root / "oam-replay-extract.json", row)
    elif change == "missing_parent":
        row = C.read(root / "oam-sources.json")
        row[0]["object_file"] = "absent.tif"
        save(root / "oam-sources.json", row)
    elif change == "invented_lfs":
        row = C.read(root / "oam-extract.json")
        row["source_published_lfs_sha256"] = "a" * 64
        save(root / "oam-extract.json", row)
    refresh(oam)
    if change == "repeat_claim":
        sample = next(s for s in oam[0]["samples"] if s["sample_id"] == "p")
        row = C.read(root / "oam-captures.json")
        row["rows"][0].update(author_date_verified=True, author_date_group="2020-08-19")
        sample["capture_record"].update(inventory_sha256=save(root / "oam-captures.json", row), author_date_verified=True, author_date_group="2020-08-19")
    with pytest.raises((ValueError, OSError)):
        check(oam)
