"""Build a blind whole-target workbench and validate operator annotation exports.

构建不展示模型答案的完整目标工作台，并校验操作员标注导出。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import shutil
from pathlib import Path

from PIL import Image

CORE = runpy.run_path(str(Path(__file__).with_name("p6_business_eval.py")))
read, write, digest, within, polygon_valid, timestamp = (CORE[k] for k in ("read", "write", "digest", "within", "polygon_valid", "timestamp"))
ASSETS = Path(__file__).with_suffix("")
TYPES = {"debris", "vegetation", "other_entity"}
VERDICTS = CORE["LABELS"] | {"excluded_source"}
FIELDS = {"sample_id", "image_sha256", "revision", "reviewer_id", "origin", "phase", "started_at", "completed_at",
          "active_ms", "target_brief", "target_registered_at", "target_polygon", "target_confirmed", "full_target_inspected",
          "verdict", "objects", "notes", "task_reference", "model_outputs_seen", "source_labels_seen", "metadata_seen",
          "asset_id", "site_id", "capture_batch_id", "near_duplicate_group", "grouping_verified", "grouping_evidence"}


def encoded(value) -> bytes:
    """Make bundle identity deterministic. / 确定性编码素材包身份。"""
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def valid_text(value, maximum: int, required: bool = False) -> bool:
    """Bound operator text without inventing missing facts. / 限制操作员文本长度，不补造缺失事实。"""
    return isinstance(value, str) and len(value) <= maximum and (not required or bool(value.strip()))


def validate(record: dict, sample: dict, qa: bool) -> None:
    """Validate identity, full-target judgment and annotation-only timing.

核对身份、完整目标判断及仅用于标注的计时。
"""
    if not isinstance(record, dict) or set(record) != FIELDS:
        raise ValueError("unexpected annotation fields")
    r = record
    if r["sample_id"] != sample["sample_id"] or r["image_sha256"] != sample["image_sha256"]:
        raise ValueError("annotation image identity mismatch")
    if r["origin"] != ("qa_fixture" if qa else "operator_entered") or r["phase"] != "annotation":
        raise ValueError("annotation origin or phase mismatch")
    if type(r["revision"]) is not int or r["revision"] < 1 or r["verdict"] not in VERDICTS:
        raise ValueError("invalid annotation revision or verdict")
    for key, maximum, required in (("reviewer_id", 120, True), ("notes", 4000, True), ("target_brief", 600, False),
                                   ("task_reference", 600, False), ("grouping_evidence", 1000, False)):
        if not valid_text(r[key], maximum, required):
            raise ValueError("missing or invalid " + key)
    if not qa and (r["reviewer_id"].upper().startswith("QA_") or r["reviewer_id"] == "QA_FIXTURE_NOT_HUMAN"):
        raise ValueError("QA identity cannot be an operator review")
    for key in ("target_confirmed", "full_target_inspected", "model_outputs_seen", "source_labels_seen", "metadata_seen", "grouping_verified"):
        if type(r[key]) is not bool:
            raise ValueError("boolean attestation missing: " + key)
    elapsed = (timestamp(r["completed_at"]) - timestamp(r["started_at"])).total_seconds() * 1000
    if type(r["active_ms"]) is not int or r["active_ms"] <= 0 or elapsed <= 0 or r["active_ms"] > elapsed + 1000:
        raise ValueError("invalid annotation active time")
    if r["target_polygon"] is not None and not polygon_valid(r["target_polygon"]):
        raise ValueError("invalid target geometry")
    if r["target_confirmed"]:
        if not polygon_valid(r["target_polygon"]) or not r["target_brief"].strip():
            raise ValueError("confirmed target needs a description and geometry")
        registered = timestamp(r["target_registered_at"])
        if not timestamp(r["started_at"]) <= registered <= timestamp(r["completed_at"]):
            raise ValueError("target registration time is outside annotation session")
    elif r["target_registered_at"] is not None:
        raise ValueError("unconfirmed target has a registration timestamp")
    if r["verdict"] in {"normal", "obstacle"} and not (r["target_confirmed"] and r["full_target_inspected"]):
        raise ValueError("whole confirmed target must be inspected")
    if r["verdict"] == "wrong_target" and (not r["task_reference"].strip() or not r["target_brief"].strip()):
        raise ValueError("wrong target needs a separately established task reference")
    if not isinstance(r["objects"], list) or len(r["objects"]) > 100:
        raise ValueError("invalid object evidence list")
    if (r["verdict"] == "obstacle") != bool(r["objects"]):
        raise ValueError("obstacle judgment needs object evidence; other verdicts must not retain objects")
    for obj in r["objects"]:
        if (not isinstance(obj, dict) or set(obj) != {"defect_type", "polygon"} or obj["defect_type"] not in TYPES
                or not polygon_valid(obj["polygon"])):
            raise ValueError("invalid object type or geometry")
    for key in ("asset_id", "site_id", "capture_batch_id", "near_duplicate_group"):
        if not isinstance(r[key], str) or (r[key] and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", r[key]) is None):
            raise ValueError("invalid grouping identity: " + key)
    if r["grouping_verified"] and (not all(r[k] for k in ("asset_id", "site_id", "capture_batch_id", "near_duplicate_group"))
                                   or not r["grouping_evidence"].strip()):
        raise ValueError("confirmed grouping needs identities and evidence")


def build(pool_file: Path, data_root: Path, permission_file: Path, protocol_file: Path, output: Path, *, qa: bool = False) -> dict:
    """Copy original bytes and metadata; never prefill labels or allocate a test split.

复制原图和元数据，不预填标签或分配测试拆分。
"""
    pool, permission, protocol = read(pool_file), read(permission_file), read(protocol_file)
    if pool.get("format") != "drone.floodnet-acquisition-pool/v1" or protocol.get("corpus_id") != "p6-road-business-v1":
        raise ValueError("unsupported source pool or protocol")
    license_file = within(data_root, permission["license_snapshot_file"])
    if permission.get("permission_status") != "documented" or digest(license_file) != permission["license_snapshot_sha256"]:
        raise ValueError("source permission evidence missing or changed")
    if output.exists():
        raise ValueError("output directory must be new")
    selected = [r for r in pool["rows"] if r.get("completeness") == "complete_original"]
    if not selected or len(selected) > 2000 or len({r["image_id"] for r in selected}) != len(selected):
        raise ValueError("empty, excessive or duplicated image inventory")
    verified = []
    for row in selected:
        if re.fullmatch(r"[0-9]+", row["image_id"]) is None or re.fullmatch(r"[0-9a-f]{64}", row["image_sha256"]) is None:
            raise ValueError("invalid source image identity")
        original = within(data_root, row["file"])
        if digest(original) != row["image_sha256"]:
            raise ValueError("original bytes changed")
        with Image.open(original) as picture:
            picture.verify()
            if list(picture.size) != [row["width"], row["height"]]:
                raise ValueError("image dimensions changed")
        verified.append((row, original))
    output.mkdir(parents=True)
    (output / "images").mkdir()
    samples = []
    for row, original in verified:
        name = "images/" + row["image_sha256"] + original.suffix.lower()
        if not (output / name).exists():
            shutil.copyfile(original, output / name)
        group = next((g for g in pool["gps_group_candidates"]["250"] if row["image_id"] in g["image_ids"]), None)
        samples.append({"sample_id": "floodnet-" + row["image_id"], "image_id": row["image_id"], "image_file": name,
                        "image_sha256": row["image_sha256"], "width": row["width"], "height": row["height"],
                        "capture_time_text": row.get("capture_time_text", ""), "capture_timezone": None,
                        "gps": row.get("gps"), "camera_model": row.get("exif_model", "").strip("\0 "),
                        "gps_group_hint": group["candidate_group_id"] if group else None,
                        "related_image_ids": [i for p in pool.get("near_duplicate_candidates", []) if row["image_id"] in p["image_ids"]
                                              for i in p["image_ids"] if i != row["image_id"]], "p6_split": "unassigned_quarantine"})
    files = {name: ASSETS / name for name in ("index.html", "review.css", "core.js", "app.js")}
    files["shared-core.js"] = Path(__file__).with_name("p6_boundary_review") / "core.js"
    for name, source in files.items():
        shutil.copyfile(source, output / name)
    shutil.copyfile(license_file, output / "license.html")
    shutil.copyfile(pool_file, output / "source-inventory.json")
    bundle = {"format": "drone.p6-road-label-bundle/v1", "corpus_id": protocol["corpus_id"], "qa_fixture": qa,
              "source_pool_sha256": digest(pool_file), "permission_sha256": digest(permission_file),
              "protocol_sha256": digest(protocol_file), "builder_sha256": digest(Path(__file__)),
              "geometry_tool_sha256": digest(Path(__file__).with_name("p6_boundary_drafts.py")),
              "ui_sha256": {name: digest(file) for name, file in files.items()},
              "source": {"permission_status": "documented", "license_evidence_file": "license.html",
                         "license_evidence_sha256": digest(license_file), "license": permission["license"],
                         "source_url": permission.get("author_repository", ""), "attribution": permission.get("attribution", "")},
              "samples": samples, "formal_release_eligible": False, "human_reviews_created": 0}
    bundle["bundle_id"] = hashlib.sha256(encoded(bundle)).hexdigest()
    (output / "bundle.json").write_bytes(encoded(bundle))
    javascript = json.dumps(bundle, sort_keys=True, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    (output / "data.js").write_text("window.ROAD_LABEL_DATA = " + javascript + ";\n", encoding="utf-8", newline="\n")
    return {"bundle_id": bundle["bundle_id"], "bundle_sha256": digest(output / "bundle.json"), "samples": len(samples),
            "qa_fixture": qa, "human_reviews_created": 0, "formal_release_eligible": False}


def verify_bundle(package: Path, expected_sha: str) -> dict:
    """Require an independently supplied bundle digest and all bound file bytes.

要求外部提供的受信包摘要，并核对全部绑定文件字节。
"""
    if digest(package / "bundle.json") != expected_sha:
        raise ValueError("trusted bundle digest mismatch")
    bundle = read(package / "bundle.json")
    identity = bundle.pop("bundle_id")
    if hashlib.sha256(encoded(bundle)).hexdigest() != identity:
        raise ValueError("bundle identity mismatch")
    bundle["bundle_id"] = identity
    if bundle.get("format") != "drone.p6-road-label-bundle/v1" or type(bundle.get("qa_fixture")) is not bool:
        raise ValueError("unsupported annotation bundle")
    for name, value in bundle["ui_sha256"].items():
        if digest(within(package, name)) != value:
            raise ValueError("UI source changed")
    for row in bundle["samples"]:
        if digest(within(package, row["image_file"])) != row["image_sha256"]:
            raise ValueError("bound image changed")
    expected = "window.ROAD_LABEL_DATA = " + json.dumps(bundle, sort_keys=True, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c") + ";\n"
    if (package / "data.js").read_text(encoding="utf-8") != expected:
        raise ValueError("page data differs from trusted bundle")
    source = bundle["source"]
    if digest(within(package, source["license_evidence_file"])) != source["license_evidence_sha256"]:
        raise ValueError("license evidence changed")
    inventory = package / "source-inventory.json"
    if inventory.exists() and digest(inventory) != bundle["source_pool_sha256"]:
        raise ValueError("capture inventory differs from the bound source pool")
    return bundle


def import_records(package: Path, expected_sha: str, export_file: Path, output: Path) -> dict:
    """Retain exports and produce unassigned annotation drafts, never final test truth.

保留导出原件并生成未分配拆分的标注草案，不冒充已冻结测试真值。
"""
    bundle, value = verify_bundle(package, expected_sha), read(export_file)
    if (set(value) != {"format", "bundle_id", "corpus_id", "qa_fixture", "records", "formal_release_eligible", "reviewer_identity_authenticated"}
            or value["format"] != "drone.p6-road-operator-labels/v1" or value["bundle_id"] != bundle["bundle_id"]
            or value["corpus_id"] != bundle["corpus_id"] or value["qa_fixture"] is not bundle["qa_fixture"]
            or value["formal_release_eligible"] is not False or value["reviewer_identity_authenticated"] is not False):
        raise ValueError("export identity or scope mismatch")
    samples = CORE["unique"](bundle["samples"], "sample_id")
    records = CORE["unique"](value["records"], "sample_id")
    if set(records) - set(samples):
        raise ValueError("foreign annotation record")
    for ident, record in records.items():
        validate(record, samples[ident], bundle["qa_fixture"])
    manifest, labels, sessions = [], [], []
    for ident, record in records.items():
        sample = samples[ident]
        if record["verdict"] == "excluded_source":
            continue
        manifest.append({**sample, "source_id": "floodnet", "split": None, "evaluation_role": None,
                         **{k: record[k] or None for k in ("asset_id", "site_id", "capture_batch_id", "near_duplicate_group")},
                         "grouping_verified": record["grouping_verified"], "grouping_evidence": record["grouping_evidence"] or None,
                         "captured_at": None, "media_origin": "qa_fixture" if bundle["qa_fixture"] else "real_capture",
                         "development_exposed": record["model_outputs_seen"],
                         "target": {"status": "human_confirmed" if record["target_confirmed"] and not bundle["qa_fixture"] else "unconfirmed",
                                    "polygon": record["target_polygon"], "brief": record["target_brief"], "task_reference": record["task_reference"]}})
        if (package / "source-inventory.json").is_file():
            manifest[-1]["capture_record"] = {"inventory_file": "source-inventory.json", "inventory_sha256": bundle["source_pool_sha256"],
                                               "source_observation_id": sample["image_id"]}
        origin = "qa_fixture" if bundle["qa_fixture"] else "human"
        labels.append({"sample_id": ident, "image_sha256": sample["image_sha256"], "verdict": record["verdict"],
                       "defect_types": sorted({o["defect_type"] for o in record["objects"]}), "objects": record["objects"],
                       "origin": origin, "reviewer_id": record["reviewer_id"], "full_target_inspected": record["full_target_inspected"],
                       "notes": record["notes"], "hints_seen": record["source_labels_seen"] or record["model_outputs_seen"],
                       "model_outputs_seen": record["model_outputs_seen"], "recorded_at": record["completed_at"]})
        sessions.append({"sample_id": ident, "phase": "annotation", "origin": origin, "reviewer_id": record["reviewer_id"],
                         "started_at": record["started_at"], "ended_at": record["completed_at"], "active_ms": record["active_ms"]})
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(export_file, output / "operator-export.json")
    write(output / "manifest-draft.json", {"format": "drone.p6-road-corpus-draft/v1", "corpus_id": bundle["corpus_id"],
          "sources": {"floodnet": bundle["source"]}, "samples": manifest, "stability_pairs": [], "data_root": str(package.resolve()),
          "status": "independent_grouping_and_split_required", "formal_release_eligible": False})
    write(output / "labels.json", {"format": "drone.p6-road-labels/v1", "corpus_id": bundle["corpus_id"], "rows": labels})
    write(output / "annotation-sessions.json", {"format": "drone.p6-road-review-sessions/v1", "corpus_id": bundle["corpus_id"], "sessions": sessions})
    receipt = {"format": "drone.p6-road-label-import/v1", "bundle_sha256": expected_sha, "export_sha256": digest(export_file),
               "tool_sha256": digest(Path(__file__)), "records": len(records), "business_labels": len(labels),
               "parser_dependencies_sha256": {name: digest(Path(__file__).with_name(name)) for name in ("p6_business_eval.py", "p6_boundary_drafts.py")},
               "human_provided_labels": 0 if bundle["qa_fixture"] else len(labels), "excluded_sources": len(records) - len(labels),
               "candidate_review_sessions": 0, "reviewer_identity_authenticated": False, "formal_release_eligible": False}
    write(output / "receipt.json", receipt)
    return receipt


def main() -> None:
    """Provide offline build and import commands. / 提供离线构建及导入命令。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    for name in ("pool", "data-root", "permission", "protocol", "output"):
        create.add_argument("--" + name, type=Path, required=True)
    create.add_argument("--qa-fixture", action="store_true")
    consume = commands.add_parser("import")
    for name in ("package", "export", "output"):
        consume.add_argument("--" + name, type=Path, required=True)
    consume.add_argument("--bundle-sha256", required=True)
    args = parser.parse_args()
    result = (build(args.pool, args.data_root, args.permission, args.protocol, args.output, qa=args.qa_fixture) if args.command == "build"
              else import_records(args.package, args.bundle_sha256, args.export, args.output))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
