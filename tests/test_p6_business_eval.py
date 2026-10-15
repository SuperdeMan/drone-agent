"""Road benchmark admission and metrics must not turn missing evidence into success.

道路评测准入与计分不得把缺失证据转为成功。
"""
from __future__ import annotations

import copy
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

C = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_business_eval.py")))


@pytest.fixture
def corpus(tmp_path):
    license_file = tmp_path / "license.txt"
    license_file.write_bytes(b"QA source permission evidence only")
    rows, labels = [], []
    for n, (ident, split, kind, role, asset) in enumerate([
        ("cal", "calibration", "normal", "primary", "cal_asset"),
        ("n", "test", "normal", "primary", "a"),
        ("p", "test", "obstacle", "primary", "b"),
        ("r", "test", "obstacle", "repeat", "b"),
        ("u", "test", "indeterminate", "primary", "c"),
        ("w", "test", "wrong_target", "primary", "d"),
        ("cal_p", "calibration", "obstacle", "primary", "cal_p_asset"),
        ("cal_u", "calibration", "indeterminate", "primary", "cal_u_asset"),
        ("cal_w", "calibration", "wrong_target", "primary", "cal_w_asset"),
    ]):
        file = tmp_path / (ident + ".png")
        Image.new("RGB", (20, 20), (n * 30, 30, 30)).save(file)
        rows.append({"sample_id": ident, "image_file": file.name, "image_sha256": C.digest(file), "width": 20, "height": 20,
                     "source_id": "qa", "split": split, "evaluation_role": role, "asset_id": asset, "site_id": split + "_site",
                     "capture_batch_id": split + "_capture", "near_duplicate_group": asset, "grouping_verified": True,
                     "grouping_evidence": "Synthetic unit-test grouping", "development_exposed": False, "media_origin": "real_capture",
                     "captured_at": f"2026-10-15T00:00:0{n}Z", "target": {"status": "human_confirmed", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}})
        labels.append({"sample_id": ident, "image_sha256": C.digest(file), "verdict": kind,
                       "defect_types": ["debris", "vegetation"] if kind == "obstacle" else [], "origin": "human",
                       "reviewer_id": "synthetic-test-reviewer", "full_target_inspected": True, "notes": "Synthetic label fixture",
                       "hints_seen": False, "model_outputs_seen": False, "recorded_at": "2026-10-15T00:01:00Z"})
    manifest = {"format": "drone.p6-road-corpus/v1", "corpus_id": "qa", "sources": {"qa": {
        "permission_status": "documented", "license_evidence_file": "license.txt", "license_evidence_sha256": C.digest(license_file)}},
        "samples": rows, "stability_pairs": [{"sample_ids": ["p", "r"], "same_condition_human_confirmed": True,
            "reviewer_id": "synthetic-test-reviewer", "evidence": "Synthetic pair fixture"}]}
    truth = {"format": "drone.p6-road-labels/v1", "corpus_id": "qa", "rows": labels}
    protocol = {"format": "drone.p6-road-protocol/v1", "corpus_id": "qa", "status": "frozen",
        "approval_reference": "synthetic-unit-test-approval-not-real", "defect_types": ["debris", "vegetation"],
        "minimum_samples": {k: 1 for k in C.MINIMUM_KEYS}, "thresholds": {k: .5 for k in C.LIMIT_KEYS},
        "additional_requirements": {"minimum_calibration_by_label": {k: 1 for k in C.LABELS},
            "minimum_test_by_defect_type": {"debris": 1, "vegetation": 1}, "maximum_test_site_fraction": 1,
            "minimum_stability_assets": 1, "minimum_timed_reviews": 1, "required_review_coverage": 1}}
    return manifest, truth, protocol, {"image_sha256": []}, tmp_path


def predictions(corpus):
    hashes = {s["sample_id"]: s["image_sha256"] for s in corpus[0]["samples"]}
    return {"format": "drone.p6-road-predictions/v1", "corpus_id": "qa",
            "rows": [{"sample_id": i, "image_sha256": hashes[i], "verdict": v, "defect_types": t, "reasons": ["quality.unresolved"] if v == "refused" else []}
                     for i, v, t in [("n", "suspected", ["debris"]), ("p", "refused", []), ("r", "refused", []),
                                     ("u", "normal", []), ("w", "refused", [])]]}


def test_valid_shape_is_not_a_formal_model_release(corpus):
    result = C.audit(*corpus)
    assert result["data_ready"] and result["human_labels_present"] == 9
    assert not result["formal_release_eligible"] and not result["model_quality_verified"]


@pytest.mark.parametrize("field", ["asset_id", "site_id", "capture_batch_id", "near_duplicate_group"])
def test_split_leakage_is_rejected_even_when_labels_are_present(corpus, field):
    corpus[0]["samples"][0][field] = corpus[0]["samples"][1][field]
    checked = C.audit(*corpus)
    assert not checked["data_ready"] and "cross_split_group_leakage" in checked["issue_counts"]


def test_identical_photo_cannot_cross_splits(corpus):
    corpus[0]["samples"][0]["image_sha256"] = corpus[0]["samples"][1]["image_sha256"]
    corpus[1]["rows"][0]["image_sha256"] = corpus[0]["samples"][0]["image_sha256"]
    assert "cross_split_group_leakage" in C.audit(*corpus)["issue_counts"]


def test_exposure_registry_overrides_a_false_unseen_claim(corpus):
    corpus[3]["image_sha256"].append(corpus[0]["samples"][1]["image_sha256"])
    assert "exposed_image_outside_development" in C.audit(*corpus)["issue_counts"]


def test_missing_label_is_distinct_from_an_indeterminate_label(corpus):
    assert C.audit(*corpus)["data_ready"]
    corpus[1]["rows"][1]["verdict"] = None
    checked = C.audit(*corpus)
    assert checked["issue_counts"]["business_label_missing"] == 1 and not checked["data_ready"]


@pytest.mark.parametrize("origin", ["ai", "qa_fixture"])
def test_ai_and_test_labels_do_not_establish_human_truth(corpus, origin):
    corpus[1]["rows"][1]["origin"] = origin
    assert "independent_business_review_missing" in C.audit(*corpus)["issue_counts"]


def test_geometry_and_full_target_review_are_required_for_normal(corpus):
    corpus[1]["rows"][1]["full_target_inspected"] = False
    corpus[0]["samples"][1]["target"]["polygon"] = [[0, 0], [1, 1], [0, 1], [1, 0]]
    issues = C.audit(*corpus)["issue_counts"]
    assert "whole_target_not_reviewed" in issues and "confirmed_target_geometry_missing" in issues


def test_unfrozen_or_empty_requirements_cannot_admit_data(corpus):
    corpus[2]["status"] = "draft"
    corpus[2]["minimum_samples"] = {}
    corpus[2]["thresholds"] = {}
    issues = C.audit(*corpus)["issue_counts"]
    assert "protocol_not_frozen_or_approved" in issues and "protocol_requirements_incomplete" in issues


def test_insufficient_samples_are_not_made_up_from_repeat_captures(corpus):
    corpus[2]["minimum_samples"]["obstacle"] = 2
    result = C.audit(*corpus)
    assert any(r["code"] == "minimum_samples_not_met" and r["field"] == "obstacle" and r["observed"] == 1 for r in result["issues"])


def test_an_asset_cannot_have_multiple_primary_observations(corpus):
    corpus[0]["samples"][3]["evaluation_role"] = "primary"
    assert "duplicate_primary_asset" in C.audit(*corpus)["issue_counts"]
    with pytest.raises(ValueError, match="one primary"):
        C.scores(corpus[0], corpus[1], predictions(corpus), {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")


def test_source_bytes_and_permission_evidence_are_checked(corpus):
    (corpus[-1] / "n.png").write_bytes(b"different")
    (corpus[-1] / "license.txt").write_bytes(b"different")
    issues = C.audit(*corpus)["issue_counts"]
    assert "image_bytes_missing_or_changed" in issues and "license_evidence_missing_or_changed" in issues


def test_stability_needs_verified_distinct_ordered_captures(corpus):
    corpus[0]["samples"][3]["image_sha256"] = corpus[0]["samples"][2]["image_sha256"]
    corpus[1]["rows"][3]["image_sha256"] = corpus[0]["samples"][3]["image_sha256"]
    assert "stability_pair_not_verified" in C.audit(*corpus)["issue_counts"]
    with pytest.raises(ValueError, match="stability"):
        C.scores(corpus[0], corpus[1], predictions(corpus), {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")


def test_metrics_preserve_denominators_refusals_and_unverified_provenance(corpus):
    result = C.scores(corpus[0], corpus[1], predictions(corpus), {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")
    assert result["samples"] == 4 and result["all_observations"] == 5
    assert result["false_alerts_per_100_normal"]["value"] == 100
    assert result["false_alerts_per_100_determinable"]["value"] == 50
    assert result["scene_miss_rate"]["value"] == 1
    assert result["miss_by_type"]["vegetation"]["typed_miss"]["value"] == 1
    assert result["determinable_coverage"]["value"] == .5
    assert result["false_normal_on_refusable"]["value"] == .5
    assert result["refusal_reasons"] == {"quality.unresolved": 2}
    assert result["stability"]["agreement_given_covered"]["value"] is None
    assert result["stability"]["consistent_and_covered_fraction"]["value"] == 0
    assert result["human_review"]["median_ms"] is None
    assert not result["counted_as_formal_live_result"] and not result["formal_release_eligible"]


def test_typed_miss_is_not_the_same_as_scene_miss(corpus):
    values = predictions(corpus)
    for row in values["rows"]:
        if row["sample_id"] in {"p", "r"}:
            row.update(verdict="suspected", defect_types=["debris"], reasons=[])
    result = C.scores(corpus[0], corpus[1], values, {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")
    assert result["scene_miss_rate"]["value"] == 0
    assert result["miss_by_type"]["vegetation"]["typed_miss"]["value"] == 1
    assert result["stability"]["consistent_and_covered_fraction"]["value"] == 1


def timed(**changes):
    result = {"sample_id": "n", "phase": "candidate_review", "origin": "human", "reviewer_id": "synthetic-reviewer",
              "hints_seen": True, "truth_seen_before_review": True, "started_at": "2026-10-15T00:01:00Z",
              "ended_at": "2026-10-15T00:02:00Z", "active_ms": 20000}
    return {**result, **changes}


def test_annotation_and_qa_time_do_not_become_human_candidate_review_time(corpus):
    result = C.scores(corpus[0], corpus[1], predictions(corpus), {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": [timed(), timed(phase="annotation"), timed(origin="qa_fixture")]}, "test")
    assert result["human_review"]["sessions"] == 1 and result["human_review"]["median_ms"] == 20000
    assert result["human_review"]["coverage"]["denominator"] == 3
    assert result["human_review"]["excluded_sessions"] == {"not_human_candidate_review": 2}


@pytest.mark.parametrize("changes", [{"active_ms": 70000}, {"active_ms": True}, {"reviewer_id": "QA_FIXTURE_NOT_HUMAN"}, {"started_at": "2026-10-15T00:01:00"}])
def test_invalid_timing_and_unidentified_fixture_claims_are_rejected(corpus, changes):
    with pytest.raises(ValueError):
        C.scores(corpus[0], corpus[1], predictions(corpus), {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": [timed(**changes)]}, "test")


def test_missing_or_duplicate_predictions_do_not_shrink_denominators(corpus):
    values = predictions(corpus)
    values["rows"].pop()
    with pytest.raises(ValueError, match="entire"):
        C.scores(corpus[0], corpus[1], values, {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")
    values = predictions(corpus)
    values["rows"].append(copy.deepcopy(values["rows"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        C.scores(corpus[0], corpus[1], values, {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")


def test_zero_denominator_stays_missing():
    assert C.fraction(0, 0)["value"] is None
    assert C.fraction(0, 0)["wilson_95_interval"] is None


def test_wilson_interval_does_not_claim_a_zero_population_error():
    assert C.fraction(0, 30)["wilson_95_interval"][1] == pytest.approx(.1135, abs=.0001)
    assert C.fraction(5, 100, 100)["wilson_95_interval"][1] == pytest.approx(11.175, abs=.01)


@pytest.mark.parametrize("requirement", ["minimum_calibration_by_label", "minimum_test_by_defect_type"])
def test_additional_coverage_cannot_be_filled_by_repeated_assets(corpus, requirement):
    extra = corpus[2]["additional_requirements"][requirement]
    extra[next(iter(extra))] = 2
    assert "coverage_samples_not_met" in C.audit(*corpus)["issue_counts"]


def test_site_concentration_and_stability_asset_coverage_are_enforced(corpus):
    corpus[2]["additional_requirements"].update(maximum_test_site_fraction=.5, minimum_stability_assets=2)
    result = C.audit(*corpus)
    assert "test_site_concentration_exceeded" in result["issue_counts"]
    assert "minimum_stability_assets_not_met" in result["issue_counts"]


@pytest.mark.parametrize("tamper", ["corpus", "image", "manifest_duplicate", "types"])
def test_scoring_rejects_foreign_or_ambiguous_evidence(corpus, tamper):
    values = predictions(corpus)
    if tamper == "corpus":
        values["corpus_id"] = "different"
    elif tamper == "image":
        values["rows"][0]["image_sha256"] = "0" * 64
    elif tamper == "manifest_duplicate":
        corpus[0]["samples"].append(copy.deepcopy(corpus[0]["samples"][1]))
    else:
        values["rows"][0]["defect_types"] = []
    with pytest.raises(ValueError):
        C.scores(corpus[0], corpus[1], values, {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")


def test_missing_human_time_cannot_meet_adopted_targets(corpus):
    metrics = C.scores(corpus[0], corpus[1], predictions(corpus), {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": []}, "test")
    result = C.assess(corpus[2], metrics)
    assert result["metric_status"] == "missing" and not result["formal_release_eligible"]
    assert any(c["name"] == "review_p95_ms" and c["status"] == "missing" for c in result["checks"])
    assert any(c["name"] == "typed_miss.vegetation" and c["status"] == "not_met" for c in result["checks"])


def test_timing_discloses_truth_and_hint_exposure(corpus):
    metrics = C.scores(corpus[0], corpus[1], predictions(corpus), {"format": "drone.p6-road-review-sessions/v1", "corpus_id": "qa", "sessions": [timed()]}, "test")
    assert metrics["human_review"]["exposure_counts"] == {"truth_seen": 1, "hints_seen": 1}
    assert metrics["human_review"]["total_active_ms"] == 20000
