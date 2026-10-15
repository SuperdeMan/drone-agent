"""Audit road benchmark inputs and compute explicitly scoped observation metrics.

核对道路业务评测输入，并计算明确限定到观察级的指标。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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


def digest(path: Path) -> str:
    """Hash original bytes. / 对原字节求摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed


def polygon_valid(points) -> bool:
    """Check normalized nondegenerate target outlines; business review is separate.

检查归一化非退化目标轮廓，业务确认另行记录。
"""
    return SIMPLE_POLYGON(points)


def pair_problems(pair: dict, samples: dict, labels: dict) -> list[str]:
    """Only distinct, ordered real captures of the same verified state form a pair.

只有同一已确认状态的不同、有序实拍才能构成跨次配对。
"""
    ids = pair.get("sample_ids", [])
    if len(ids) != 2 or ids[0] == ids[1] or any(i not in samples for i in ids):
        return ["invalid_stability_pair_identity"]
    left, right = (samples[i] for i in ids)
    issues = []
    if (not left.get("asset_id") or left["asset_id"] != right.get("asset_id") or left["split"] != right["split"]
            or left["image_sha256"] == right["image_sha256"] or pair.get("same_condition_human_confirmed") is not True
            or not pair.get("reviewer_id") or not pair.get("evidence")
            or any(s.get("media_origin") != "real_capture" for s in (left, right))):
        issues.append("stability_pair_not_verified")
    try:
        if timestamp(right["captured_at"]) <= timestamp(left["captured_at"]):
            issues.append("stability_capture_order_invalid")
    except (KeyError, TypeError, ValueError):
        issues.append("stability_capture_time_missing")
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
    associations = {key: defaultdict(set) for key in ("asset_id", "site_id", "capture_batch_id", "near_duplicate_group", "image_sha256")}

    def issue(code, sample_id=None, **details):
        issues.append({"code": code, "sample_id": sample_id, **details})

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
        for key, groups in associations.items():
            value = sample.get(key)
            if not isinstance(value, str) or not value:
                issue("grouping_identity_missing", ident, field=key)
            else:
                groups[value].add(split)
        if sample.get("grouping_verified") is not True or not sample.get("grouping_evidence"):
            issue("grouping_not_verified", ident)
        if split != "development" and (sample["image_sha256"] in exposed or sample.get("development_exposed") is not False):
            issue("exposed_image_outside_development", ident)
        if split != "development" and sample.get("media_origin") != "real_capture":
            issue("nonreal_sample_in_formal_split", ident)
        label = annotations.get(ident)
        if not label or label.get("verdict") is None:
            issue("business_label_missing", ident)
            continue
        if label["verdict"] not in LABELS or not isinstance(label.get("defect_types"), list) or set(label["defect_types"]) - types:
            raise ValueError("invalid business label or defect type")
        if (label["verdict"] == "obstacle") != bool(label["defect_types"]):
            raise ValueError("obstacle types and label disagree")
        if label.get("image_sha256") != sample["image_sha256"]:
            raise ValueError("annotation image mismatch")
        if (label.get("origin") != "human" or not isinstance(label.get("reviewer_id"), str) or not label["reviewer_id"].strip()
                or label["reviewer_id"] == "QA_FIXTURE_NOT_HUMAN"
                or not isinstance(label.get("notes"), str) or not label["notes"].strip() or type(label.get("hints_seen")) is not bool):
            issue("independent_business_review_missing", ident)
        else:
            reviewed += 1
        try:
            timestamp(label["recorded_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            issue("business_review_timestamp_missing", ident)
        if label["verdict"] in {"normal", "obstacle"}:
            if label.get("full_target_inspected") is not True:
                issue("whole_target_not_reviewed", ident)
            target = sample.get("target") or {}
            if target.get("status") != "human_confirmed" or not polygon_valid(target.get("polygon")):
                issue("confirmed_target_geometry_missing", ident)
        if split == "test" and label.get("model_outputs_seen") is not False:
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
                          "unique_assets": len({samples[p["sample_ids"][0]]["asset_id"] for p in pairs})},
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
    check("review_p95_ms", review.get("p95_ms"), limits.get("review_p95_ms_max"))
    check("timed_reviews", review.get("sessions"), extra.get("minimum_timed_reviews"), True)
    check("review_coverage", review.get("coverage", {}).get("value"), extra.get("required_review_coverage"), True)
    check("stability_assets", stability.get("unique_assets"), extra.get("minimum_stability_assets"), True)
    for name in sorted(LABELS):
        check("test_samples." + name, metrics.get("label_counts", {}).get(name, 0), protocol.get("minimum_samples", {}).get(name), True)
    check("stability_pairs", stability.get("consistent_and_covered_fraction", {}).get("denominator"), protocol.get("minimum_samples", {}).get("stability_pairs"), True)
    states = {c["status"] for c in checks}
    return {"format": "drone.p6-road-target-comparison/v1", "corpus_id": protocol["corpus_id"],
            "metric_status": "missing" if "missing" in states else "not_met" if "not_met" in states else "met",
            "protocol_status": protocol.get("status"), "split": metrics.get("split"), "checks": checks,
            "formal_release_eligible": False, "scope": "observed targets only; data admission, frozen protocol, actual run and human evidence must be verified separately"}


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
