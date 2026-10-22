"""Build an offline boundary workbench and audit operator exports. / 构建离线边界复核页并校验操作员导出。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

ASSETS = Path(__file__).with_suffix("")
# Reuse the exact offline geometry tool without requiring scripts to be an installed package. / 复用离线几何工具，无需将 scripts 安装为包。
GEOMETRY = runpy.run_path(str(Path(__file__).with_name("p6_boundary_drafts.py")))
audit, digest, envelope, relation, safe_source = (GEOMETRY[name] for name in ("audit", "digest", "envelope", "relation", "safe_source"))
FORMAT = "drone.p6-boundary-operator-reviews/v1"
COMMON = {"case_id", "image_sha256", "support_sha256", "reviewer", "notes", "origin", "started_at",
          "completed_at", "active_review_ms", "ai_hints_viewed", "source_support_viewed"}
ROAD_KEYS = COMMON | {"revision", "decision", "geometry"}
ENTITY_KEYS = COMMON | {"entity_id", "road_revision", "entity_kind", "target_relation", "group_confirmed",
                        "geometry_source", "geometry"}


def encoded(value: object) -> bytes:
    """Use deterministic UTF-8 bytes for package identity. / 用确定性 UTF-8 字节生成包身份。"""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def read_json(path: Path) -> dict:
    """Reject duplicate keys and nonfinite JSON constants. / 拒绝重复键和非有限 JSON 常量。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite JSON constant: " + value)

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def build(drafts_file: Path, index_file: Path, prior_file: Path, data_root: Path, output: Path) -> dict:
    """Copy verified source bytes into a new, self-contained local package. / 将已核对源字节复制到新的独立本地包。"""
    if output.exists():
        raise ValueError("output must be a new directory")
    checked = audit(drafts_file, index_file, prior_file, data_root)
    drafts, index = read_json(drafts_file), read_json(index_file)
    cases = {c["case_id"]: c for c in index["cases"]}
    candidates = {}
    for row in read_json(prior_file)["rows"]:
        ordinals = {}
        for obstacle in row["obstacles"]:
            view = obstacle["view_id"]
            ordinal = ordinals.get(view, 0)
            ordinals[view] = ordinal + 1
            candidates[f"{row['case_id']}:{view}:{ordinal}"] = obstacle["bbox_in_source"]
    output.mkdir(parents=True)
    (output / "images").mkdir()
    (output / "supports").mkdir()
    targets = []
    for target in drafts["targets"]:
        source = cases[target["case_id"]]
        image_name = f"images/{source['image_sha256']}.jpg"
        support_name = f"supports/{source['target_region']['sha256']}.png"
        for name, path in [(image_name, source["image_file"]), (support_name, source["target_region"]["file"])]:
            destination = output / name
            if not destination.exists():
                shutil.copyfile(safe_source(data_root, path), destination)
        entities = []
        for entity in target["entities"]:
            row = next(r for r in checked["rows"] if r["entity_id"] == entity["entity_id"])
            entities.append({"entity_id": entity["entity_id"], "candidate_refs": entity["candidate_refs"],
                             "candidate_boxes": [candidates[ref] for ref in entity["candidate_refs"]],
                             "draft_geometry": entity["object_envelope"], "ai_description": entity["description"],
                             "ai_notes": entity["ai_notes"], "ai_relation": row["relation"]})
        targets.append({"case_id": target["case_id"], "image_id": target["image_id"], "image_file": image_name,
                        "image_sha256": source["image_sha256"], "support_file": support_name,
                        "support_sha256": source["target_region"]["sha256"], "width": source["width"],
                        "height": source["height"], "ai_road_description": target["road_description"],
                        "draft_geometry": target["road_envelope"], "entities": entities})
    sources = {name: digest(ASSETS / name) for name in ("index.html", "review.css", "core.js", "app.js")}
    for name in sources:
        shutil.copyfile(ASSETS / name, output / name)
    bundle = {"format": "drone.p6-boundary-review-bundle/v1", "policy_id": "road-contact-dev-v1",
              "drafts_sha256": digest(drafts_file), "source_index_sha256": digest(index_file),
              "prior_summary_sha256": digest(prior_file), "builder_sha256": digest(Path(__file__)),
              "geometry_tool_sha256": digest(Path(__file__).with_name("p6_boundary_drafts.py")),
              "ui_sha256": sources, "targets": targets, "formal_release_eligible": False,
              "development_selection_exposed_to_ai": True, "minimum_size_threshold": None,
              "minimum_occupancy_ratio": None, "independent_ground_truth": False}
    bundle["bundle_id"] = hashlib.sha256(encoded(bundle)).hexdigest()
    (output / "bundle.json").write_bytes(encoded(bundle))
    # Escape markup even though the data is loaded as an external script. / 即使数据通过外部脚本载入也转义标记字符。
    text = json.dumps(bundle, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    (output / "data.js").write_text("window.BOUNDARY_DATA = " + text + ";\n", encoding="utf-8", newline="\n")
    return {"bundle_id": bundle["bundle_id"], "bundle_sha256": digest(output / "bundle.json"),
            "data_js_sha256": digest(output / "data.js"), "targets": len(targets),
            "entities": sum(len(t["entities"]) for t in targets), "human_reviews_created": 0,
            "formal_release_eligible": False}


def verified_bundle(package: Path, expected_sha: str) -> dict:
    """Bind validation to a trusted bundle digest and its source assets. / 将校验绑定到受信包摘要与源素材。"""
    if digest(package / "bundle.json") != expected_sha:
        raise ValueError("trusted bundle digest mismatch")
    bundle = read_json(package / "bundle.json")
    identity = bundle.pop("bundle_id")
    if hashlib.sha256(encoded(bundle)).hexdigest() != identity:
        raise ValueError("bundle identity mismatch")
    bundle["bundle_id"] = identity
    for target in bundle["targets"]:
        for name, sha in [("image_file", "image_sha256"), ("support_file", "support_sha256")]:
            path = (package / target[name]).resolve()
            if not path.is_relative_to(package.resolve()) or digest(path) != target[sha]:
                raise ValueError("source asset changed or leaves package")
    for name, sha in bundle["ui_sha256"].items():
        path = (package / name).resolve()
        if not path.is_relative_to(package.resolve()) or digest(path) != sha:
            raise ValueError("review UI source changed")
    expected_data = "window.BOUNDARY_DATA = " + json.dumps(bundle, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c") + ";\n"
    # Compare parsed data as bundle.json sorts keys while data.js preserves construction order. / 包文件排序键，脚本保留构造顺序，因此比较解析值。
    data_text = (package / "data.js").read_text(encoding="utf-8")
    if not data_text.startswith("window.BOUNDARY_DATA = ") or not data_text.endswith(";\n"):
        raise ValueError("invalid UI data wrapper")
    if json.loads(data_text[len("window.BOUNDARY_DATA = "):-2]) != json.loads(expected_data[len("window.BOUNDARY_DATA = "):-2]):
        raise ValueError("UI data differs from trusted bundle")
    return bundle


def require(condition: bool, message: str) -> None:
    """Fail closed on malformed records. / 对不合格记录明确拒绝。"""
    if not condition:
        raise ValueError(message)


def common(record: dict, target: dict, keys: set) -> None:
    """Validate identity, provenance and self-reported timing. / 校验身份绑定、来源与自报告耗时。"""
    require(isinstance(record, dict) and set(record) == keys, "unexpected record fields")
    require(all(record[k] == target[k] for k in ("case_id", "image_sha256", "support_sha256")), "record source mismatch")
    for key, limit in [("reviewer", 120), ("notes", 4000)]:
        require(isinstance(record[key], str) and 0 < len(record[key].strip()) <= limit, "missing reviewer or rationale")
    require(record["origin"] in ("operator_entered", "qa_fixture"), "invalid record origin")
    if record["origin"] == "qa_fixture":
        require(record["reviewer"] == "QA_FIXTURE_NOT_HUMAN", "fixture must identify itself")
    else:
        require(record["reviewer"] != "QA_FIXTURE_NOT_HUMAN", "fixture cannot claim operator origin")
    require(all(type(record[k]) is bool for k in ("ai_hints_viewed", "source_support_viewed")), "invalid hint exposure")
    dates = []
    for key in ("started_at", "completed_at"):
        require(isinstance(record[key], str), "invalid review timestamp")
        try:
            date = datetime.fromisoformat(record[key].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid review timestamp") from exc
        require(date.tzinfo is not None, "review timestamp needs timezone")
        dates.append(date)
    elapsed = (dates[1] - dates[0]).total_seconds() * 1000
    require(type(record["active_review_ms"]) is int and 0 <= record["active_review_ms"] <= elapsed + 1000,
            "active time exceeds wall time or is invalid")
    require(elapsed >= 0, "review finished before start")


def validate_reviews(value: dict, bundle: dict, package: Path) -> dict:
    """Audit partial exports without upgrading declarations to independent truth. / 核对部分导出，不将自声明升级为独立真值。"""
    require(isinstance(value, dict) and set(value) == {"format", "bundle_id", "policy_id", "formal_release_eligible",
            "reviewer_identity_authenticated", "roads", "entities"}, "unexpected export fields")
    require(value["format"] == FORMAT and value["bundle_id"] == bundle["bundle_id"] and
            value["policy_id"] == bundle["policy_id"], "export belongs to another bundle or policy")
    require(value["formal_release_eligible"] is False and value["reviewer_identity_authenticated"] is False,
            "export cannot grant authority")
    require(isinstance(value["roads"], list) and isinstance(value["entities"], list), "record lists required")
    targets = {t["case_id"]: t for t in bundle["targets"]}
    roads, entities, findings = {}, {}, []
    for record in value["roads"]:
        require(isinstance(record, dict) and record.get("case_id") in targets, "unknown road")
        target = targets[record["case_id"]]
        common(record, target, ROAD_KEYS)
        require(record["case_id"] not in roads, "duplicate road")
        require(isinstance(record["revision"], str) and re.fullmatch(r"[A-Za-z0-9-]{8,80}", record["revision"]) is not None,
                "invalid road revision")
        require(record["decision"] in ("accepted_draft", "revised", "unknown"), "invalid road decision")
        if record["decision"] == "unknown":
            require(record["geometry"] is None, "unknown road must not carry confirmed geometry")
        else:
            require(isinstance(record["geometry"], dict) and set(record["geometry"]) == {"core", "outer"}, "invalid road geometry")
            envelope(record["geometry"], (target["width"], target["height"]), core_optional=False)
            if record["decision"] == "accepted_draft":
                require(record["ai_hints_viewed"] is True and record["geometry"] == target["draft_geometry"],
                        "accepting draft requires exposure and exact geometry")
        roads[record["case_id"]] = record
    for record in value["entities"]:
        require(isinstance(record, dict) and record.get("case_id") in roads, "entity needs a reviewed road")
        target, road = targets[record["case_id"]], roads[record["case_id"]]
        entity = next((e for e in target["entities"] if e["entity_id"] == record.get("entity_id")), None)
        require(entity is not None, "unknown entity or wrong target")
        common(record, target, ENTITY_KEYS)
        eid = record["entity_id"]
        require(eid not in entities, "duplicate entity")
        require(record["road_revision"] == road["revision"], "stale road revision")
        require(record["origin"] == road["origin"], "mixed QA and operator dependency")
        require(record["entity_kind"] in ("physical", "shadow", "surface", "unknown"), "invalid entity kind")
        require(record["target_relation"] in ("inside", "outside", "edge_uncertain", "unknown"), "invalid target relation")
        require(type(record["group_confirmed"]) is bool, "grouping decision required")
        geometry = record["geometry"]
        if record["entity_kind"] == "physical":
            require(isinstance(geometry, dict) and set(geometry) == {"core", "outer"}, "physical entity needs geometry")
            envelope(geometry, (target["width"], target["height"]), core_optional=record["target_relation"] != "inside")
            require(record["geometry_source"] in ("ai_draft", "drawn"), "geometry origin required")
            if record["geometry_source"] == "ai_draft":
                require(record["ai_hints_viewed"] is True and geometry == entity["draft_geometry"], "draft geometry mismatch or unreported exposure")
        else:
            require(geometry is None and record["geometry_source"] is None and record["target_relation"] == "unknown",
                    "nonphysical or unknown entity must not claim an obstruction footprint")
        require(road["decision"] != "unknown" or record["target_relation"] == "unknown", "unknown road cannot establish location")
        measured, conflicts = None, []
        if geometry is not None and road["geometry"] is not None:
            with Image.open(package / target["support_file"]) as mask:
                support = np.asarray(mask) > 0
            measured = relation(road["geometry"], geometry, support)["relation"]
            if record["target_relation"] in ("inside", "outside") and record["target_relation"] != measured:
                conflicts.append("declared_relation_disagrees_with_geometry")
        if not record["group_confirmed"]:
            conflicts.append("ai_grouping_not_confirmed")
        disposition = "unresolved"
        if not conflicts:
            if record["entity_kind"] in ("shadow", "surface"):
                disposition = "non_entity"
            elif record["entity_kind"] == "physical" and record["target_relation"] == measured == "inside":
                disposition = "candidate"
            elif record["entity_kind"] == "physical" and record["target_relation"] == measured == "outside":
                disposition = "outside_target"
        entities[eid] = record
        findings.append({"entity_id": eid, "case_id": record["case_id"], "candidate_refs": entity["candidate_refs"],
                         "declared_relation": record["target_relation"], "conditional_geometry_relation": measured,
                         "development_disposition": disposition, "conflicts": conflicts, "origin": record["origin"]})
    return {"format": "drone.p6-boundary-review-audit/v1", "bundle_id": bundle["bundle_id"],
            "operator_road_entries": sum(r["origin"] == "operator_entered" for r in roads.values()),
            "operator_entity_entries": sum(r["origin"] == "operator_entered" for r in entities.values()),
            "qa_fixture_entries": sum(r["origin"] == "qa_fixture" for r in [*roads.values(), *entities.values()]),
            "pending_roads": [i for i in targets if i not in roads or roads[i]["origin"] != "operator_entered"],
            "pending_entities": [e["entity_id"] for t in bundle["targets"] for e in t["entities"]
                                 if e["entity_id"] not in entities or entities[e["entity_id"]]["origin"] != "operator_entered"],
            "unresolved_operator_entities": [r["entity_id"] for r in findings
                                             if r["origin"] == "operator_entered" and r["development_disposition"] == "unresolved"],
            "rows": findings, "reviewer_identity_authenticated": False, "independent_ground_truth": False,
            "timing_is_client_reported": True, "accuracy_computed": False, "scene_verdict": None,
            "formal_release_eligible": False}


def main() -> None:
    """Create a package or a new validation receipt. / 创建复核包或新的校验回执。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("build")
    for name in ("drafts", "index", "previous-summary", "data-root", "output"):
        prepare.add_argument("--" + name, type=Path, required=True)
    validate = commands.add_parser("validate")
    for name in ("package", "reviews", "output"):
        validate.add_argument("--" + name, type=Path, required=True)
    validate.add_argument("--bundle-sha256", required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build(args.drafts, args.index, args.previous_summary, args.data_root, args.output)
    else:
        bundle = verified_bundle(args.package, args.bundle_sha256)
        result = validate_reviews(read_json(args.reviews), bundle, args.package)
        result.update(reviews_sha256=digest(args.reviews), bundle_sha256=args.bundle_sha256,
                      validator_sha256=digest(Path(__file__)),
                      geometry_tool_sha256=digest(Path(__file__).with_name("p6_boundary_drafts.py")))
        with args.output.open("xb") as stream:
            stream.write(encoded(result))
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
