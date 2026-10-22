"""Exercise conditional representation provenance without visual inspection.

验证条件响应表示的来源，不进行视觉检查。
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

C = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_business_eval.py")))
G = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_oam_capture.py")))
BASE = runpy.run_path(str(Path(__file__).with_name("test_p6_oam_captures.py")))
corpus, oam = BASE["corpus"], BASE["oam"]
ID = "5f515554805b400005d7e598"
URL = "https://oin-hotosm-temp.s3.amazonaws.com/5f5154e2805b400005d7e596/0/5f5154e2805b400005d7e597.tif"
ETAG = '"' + "a" * 32 + '-3"'


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return C.digest(path)


def rebind(case):
    for part, record in zip(case["receipt"]["parts"], case["records"], strict=True):
        part["http_record_sha256"] = save(case["root"] / part["http_record_file"], record)
    case["source"]["download_receipt_sha256"] = save(case["root"] / "parts.json", case["receipt"])


def make_case(root, parent, catalog, *, prefix=True):
    (root / "parent.tif").write_bytes(parent)
    catalog_hash = save(root / "catalog.json", catalog)
    size, cut = len(parent), len(parent) // 3
    ranges = [(0, cut), (cut - 2, cut + 2), (2 * cut, size - 2 * cut)]
    parts, records = [], []
    for index, (offset, length) in enumerate(ranges):
        body = root / f"body-{index}.bin"
        body.write_bytes(parent[offset:offset + length])
        body_hash = C.digest(body)
        record = {"method": "GET", "request_url": URL,
            "request_headers": {"If-Match": ETAG, "Accept-Encoding": "identity"},
            "status": 206, "final_url": URL, "headers": {"ETag": ETAG, "Content-Length": str(length)},
            "started_at_utc": f"2026-10-19T00:00:{index * 10:02}Z",
            "finished_at_utc": f"2026-10-19T00:00:{index * 10 + 5:02}Z"}
        kind = "complete_206"
        if index == 0 and prefix:
            kind = "incomplete_200_prefix"
            record.update(status=200, local_file=body.name, expected_bytes=size,
                          outcome="incomplete_or_failed", error={"type": "OSError", "message": "Actual fixture EOF"},
                          received_bytes=length, saved_bytes=length, saved_sha256=body_hash)
            record["headers"]["Content-Length"] = str(size)
        else:
            end = offset + length - 1
            record["request_headers"]["Range"] = f"bytes={offset}-{end}"
            record["headers"]["Content-Range"] = f"bytes {offset}-{end}/{size}"
            record.update(range_start=offset, range_end=end, expected_bytes=length, complete=True,
                          file=body.name, body_bytes=length, sha256=body_hash)
        parts.append({"kind": kind, "evidence_layout": "flat_request_response_v1", "offset": offset, "body_bytes": length,
                      "http_record_file": f"http-{index}.json", "body_file": body.name, "body_sha256": body_hash})
        records.append(record)
    parent_hash = C.digest(root / "parent.tif")
    receipt = {"format": "drone.p6-oam-conditional-parts-http/v1", "status": "assembled_and_verified", "catalog_object_id": ID,
        "source_metadata_url": "https://api.openaerialmap.org/meta/" + ID, "source_metadata_sha256": catalog_hash,
        "expected_etag": ETAG, "expected_bytes": size, "body_bytes": size, "local_file": "parent.tif",
        "local_full_object_sha256": parent_hash, "checksum_origin": "locally_computed_full_object", "publisher_sha256": None,
        "started_at_utc": "2026-10-19T00:00:00Z", "acquisition_finished_at_utc": "2026-10-19T00:00:25Z",
        "verification_started_at_utc": "2026-10-19T00:00:30Z", "finished_at_utc": "2026-10-19T00:01:00Z", "parts": parts}
    source = {"catalog_object_id": ID, "catalog_url": receipt["source_metadata_url"], "catalog_file": "catalog.json",
        "catalog_sha256": catalog_hash, "object_file": "parent.tif", "local_full_object_sha256": parent_hash,
        "checksum_origin": "locally_computed_full_object", "publisher_sha256": None, "download_receipt_file": "parts.json"}
    case = {"root": root, "source": source, "receipt": receipt, "records": records, "catalog": catalog}
    rebind(case)
    return case


@pytest.fixture
def parts(tmp_path):
    catalog = {"results": {"_id": ID, "uuid": URL, "platform": "uav", "file_size": 64,
        "acquisition_start": "2020-08-18T23:00:00Z", "acquisition_end": "2020-08-19T00:00:00Z",
        "properties": {"license": "CC-BY 4.0", "sensor": "SYNTHETIC BYTE FIXTURE", "bands": 3, "dtype": "uint8",
                       "colorinterp": ["red", "green", "blue"], "crs": "EPSG:32630", "dimensions": [4, 4]}}}
    return make_case(tmp_path, bytes(range(64)), catalog)


def check(case):
    return G.verify_source(case["source"], case["root"], {},
        {"read_bound": C.bound_evidence, "file_bound": C.derived_file, "timestamp": C.timestamp})


def assign(value, path, replacement):
    keys = path.split(".")
    for key in keys[:-1]:
        value = value[int(key)] if isinstance(value, list) else value[key]
    if isinstance(value, list):
        value[int(keys[-1])] = replacement
    else:
        value[keys[-1]] = replacement


def test_incomplete_200_and_206_cover_full_representation_with_consistent_overlap(parts):
    _, parent, receipt = check(parts)
    assert parent.read_bytes() == bytes(range(64))
    assert receipt["parts"][0]["kind"] == "incomplete_200_prefix"
    assert parts["records"][0]["outcome"] == "incomplete_or_failed"
    assert sum(p["body_bytes"] for p in receipt["parts"]) - receipt["body_bytes"] == 2
    assert "response" not in receipt and receipt["status"] != "downloaded_and_verified"


def test_pure_206_complete_coverage_and_official_equivalent_endpoint(parts):
    case = make_case(parts["root"], bytes(range(64)), parts["catalog"], prefix=False)
    for record in case["records"]:
        record["request_url"] = record["final_url"] = URL.replace("s3.amazonaws.com", "s3.us-east-1.amazonaws.com")
    rebind(case)
    check(case)


@pytest.mark.parametrize("path,value", [
    ("method", "HEAD"), ("status", 200), ("status", True), ("complete", False),
    ("error", {"type": "OSError", "message": "Contradictory failed response"}), ("outcome", "incomplete_or_failed"),
    ("request_url", URL + "?unbound=1"), ("final_url", URL.replace("597.tif", "599.tif")),
    ("request_headers.If-Match", '"' + "b" * 32 + '"'), ("headers.ETag", "W/" + ETAG),
    ("request_headers.Accept-Encoding", "gzip"), ("headers.Content-Encoding", "gzip"),
    ("request_headers.Range", "bytes=19-40"), ("request_headers.Range", "bytes=-23"),
    ("headers.Content-Range", "bytes 19-40/64"), ("headers.Content-Range", "bytes 19-41/*"),
    ("headers.Content-Length", "24"), ("body_bytes", True), ("range_start", True),
    ("range_end", 99), ("expected_bytes", "23"), ("sha256", "b" * 64),
    ("finished_at_utc", "2026-10-18T00:00:00Z"),
])
def test_rebound_206_transport_must_still_match_real_bytes(parts, path, value):
    assign(parts["records"][1], path, value)
    rebind(parts)
    with pytest.raises(ValueError):
        check(parts)


@pytest.mark.parametrize("path,value", [
    ("status", 206), ("saved_bytes", True), ("received_bytes", 20),
    ("expected_bytes", 63), ("outcome", "completed"), ("error", None), ("complete", True),
    ("saved_sha256", "b" * 64), ("request_headers.Range", "bytes=0-20"),
    ("headers.Content-Range", "bytes 0-20/64"), ("headers.Content-Length", "21"),
])
def test_truncated_200_is_only_a_truthfully_recorded_offset_zero_prefix(parts, path, value):
    assign(parts["records"][0], path, value)
    rebind(parts)
    with pytest.raises(ValueError):
        check(parts)


@pytest.mark.parametrize("path,value", [
    ("expected_etag", "*"), ("expected_etag", "W/" + ETAG), ("expected_etag", None),
    ("status", "downloaded_and_verified"), ("expected_bytes", True), ("body_bytes", 63),
    ("publisher_sha256", "a" * 64), ("request", {}), ("response", {"status": 200}),
    ("parts.0.offset", 1), ("parts.0.offset", False), ("parts.1.body_bytes", 0),
    ("parts.1.offset", -1), ("parts.2.body_bytes", 25), ("parts.1.evidence_layout", "invented"),
    ("parts.1.kind", "complete_200"), ("parts", []),
    ("acquisition_finished_at_utc", "2026-10-19T00:00:24Z"),
    ("verification_started_at_utc", "2026-10-19T00:00:24Z"),
    ("finished_at_utc", "2026-10-19T00:00:29Z"), ("started_at_utc", "2026-10-19T00:00:01Z"),
])
def test_recipe_cannot_invent_status_bounds_or_completion_time(parts, path, value):
    assign(parts["receipt"], path, value)
    parts["source"]["download_receipt_sha256"] = save(parts["root"] / "parts.json", parts["receipt"])
    with pytest.raises(ValueError):
        check(parts)


@pytest.mark.parametrize("path,value", [
    ("platform", "aircraft"), ("properties.license", "CC BY-NC 4.0"), ("properties.sensor", ""),
    ("properties.dimensions", [True, 4]), ("properties.dimensions", [4, 0]),
    ("properties.crs", ""), ("properties.dtype", "float32"), ("properties.bands", 4),
    ("acquisition_end", "2019-01-01T00:00:00Z"),
])
def test_parts_keep_per_object_uav_permission_and_native_metadata_gates(parts, path, value):
    assign(parts["catalog"]["results"], path, value)
    catalog_hash = save(parts["root"] / "catalog.json", parts["catalog"])
    parts["source"]["catalog_sha256"] = parts["receipt"]["source_metadata_sha256"] = catalog_hash
    rebind(parts)
    with pytest.raises(ValueError):
        check(parts)


@pytest.mark.parametrize("change", ["gap", "missing_tail", "record_digest", "body_digest", "parent_digest",
                                    "rebound_overlap_conflict", "rebound_parent_conflict", "duplicate_header"])
def test_hash_coverage_and_overlap_are_recomputed(parts, change):
    root = parts["root"]
    if change == "gap":
        del parts["receipt"]["parts"][1]
        del parts["records"][1]
    elif change == "missing_tail":
        parts["receipt"]["parts"].pop()
        parts["records"].pop()
    elif change == "record_digest":
        (root / "http-1.json").write_text("{}")
    elif change == "body_digest":
        (root / "body-1.bin").write_bytes(b"x" * 23)
    elif change == "parent_digest":
        (root / "parent.tif").write_bytes(b"x" * 64)
    elif change == "rebound_overlap_conflict":
        body = bytearray((root / "body-1.bin").read_bytes())
        body[0] ^= 1
        (root / "body-1.bin").write_bytes(body)
        parts["receipt"]["parts"][1]["body_sha256"] = parts["records"][1]["sha256"] = C.digest(root / "body-1.bin")
    elif change == "rebound_parent_conflict":
        body = bytearray((root / "parent.tif").read_bytes())
        body[-1] ^= 1
        (root / "parent.tif").write_bytes(body)
        parts["source"]["local_full_object_sha256"] = parts["receipt"]["local_full_object_sha256"] = C.digest(root / "parent.tif")
    elif change == "duplicate_header":
        parts["records"][1]["headers"]["etag"] = ETAG
    if change not in {"record_digest", "body_digest", "parent_digest"}:
        rebind(parts)
    with pytest.raises(ValueError):
        check(parts)


def test_actual_derived_capture_keeps_pixels_replay_and_verification_time(oam):
    root = oam[-1]
    catalog = C.read(root / "catalog.json")
    case = make_case(root, (root / "parent.tif").read_bytes(), catalog)
    source = C.read(root / "oam-sources.json")[0]
    source.update(case["source"])
    save(root / "oam-sources.json", [source])
    (root / "get.json").write_bytes((root / "parts.json").read_bytes())
    source["download_receipt_file"] = "get.json"
    save(root / "oam-sources.json", [source])
    BASE["refresh"](oam)
    row = BASE["check"](oam)
    assert row["repeated_capture_eligible"] is False and row["author_date_verified"] is False
    receipt = C.read(root / "get.json")
    receipt["finished_at_utc"] = "2026-10-19T00:02:01Z"
    save(root / "get.json", receipt)
    BASE["refresh"](oam)
    with pytest.raises(ValueError, match="source or tools"):
        BASE["check"](oam)


def test_legacy_full200_semantics_stay_separate(parts):
    source = parts["source"]
    receipt = {"format": "drone.p6-oam-full-object-http/v1", "status": "downloaded_and_verified", "catalog_object_id": ID,
        "source_metadata_url": source["catalog_url"], "source_metadata_sha256": source["catalog_sha256"],
        "checksum_origin": "locally_computed_full_object", "publisher_sha256": None,
        "request": {"method": "GET", "url": URL, "headers": {"If-Match": ETAG}},
        "response": {"status": 200, "final_url": URL, "headers": {"ETag": ETAG, "Content-Length": "64"}},
        "expected_etag": ETAG, "response_etag": ETAG, "expected_bytes": 64, "body_bytes": 64,
        "response_content_length": 64, "local_file": "parent.tif", "local_full_object_sha256": source["local_full_object_sha256"],
        "started_at_utc": "2026-10-19T00:00:00Z", "finished_at_utc": "2026-10-19T00:01:00Z"}
    source["download_receipt_sha256"] = save(parts["root"] / "parts.json", receipt)
    check(parts)
    receipt["response"]["status"] = 206
    source["download_receipt_sha256"] = save(parts["root"] / "parts.json", receipt)
    with pytest.raises(ValueError, match="complete object GET"):
        check(parts)
