"""P4 business contracts: catalogs, profiles, the strict model answer and the `reinspection-v1` settlement (D063).

P4 业务契约：目录、画像、严格的模型回答与 `reinspection-v1` 结算（D063）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from drone_agent.fleet.business_models import (
    BusinessCatalog,
    Feedback,
    JobResult,
    ModelAnswer,
    load_catalog,
    load_profile,
    load_quality,
    settle_round,
)

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)


def feedback(at: datetime = T0) -> Feedback:
    return Feedback(feedback_id="fb-1", request_id="repair-1", reported_by="harness:op", reported_at=at)


def inspection(**changes) -> dict:
    return {"mission_id": "m-new", "evidence_id": "capture:new", "evidence_sha256": "a" * 64,
            "captured_at": (T0 + timedelta(minutes=5)).isoformat(), **changes}


def settle(**changes):
    values = {"feedback": feedback(), "inspection": inspection(), "run_missions": {"m-new"},
              "job": {"job_id": "job-1", "verdict": "normal", "source": "deterministic"},
              "review": {"review_id": "rv-1", "decision": "confirmed"}, "seen_evidence": {"capture:old"},
              "seen_media": {"b" * 64}, "allowed_sources": ("deterministic", "live_model"), "now": T0}
    values.update(changes)
    return settle_round(**values)


def test_settlement_passes_only_when_every_condition_holds():
    assert settle().status == "passed"
    cases = {
        "reinspection.evidence_missing": {"inspection": None},
        "reinspection.capture_time_unknown": {"inspection": inspection(captured_at=None)},
        "reinspection.before_feedback": {"inspection": inspection(captured_at=(T0 - timedelta(seconds=1)).isoformat())},
        "reinspection.not_new_acquisition": {"run_missions": {"m-other"}},
        "reinspection.replayed_media": {"seen_media": {"a" * 64}},
        "reinspection.analysis_refused": {"job": {"job_id": "job-1", "verdict": "refused", "source": "not_run"}},
        "reinspection.source_not_allowed": {"job": {"job_id": "job-1", "verdict": "normal", "source": "scripted"}},
        "reinspection.review_missing": {"review": None},
    }
    for reason, change in cases.items():
        result = settle(**change)
        assert (result.status, result.reasons) == ("unknown", (reason,)), reason
    anomalous = settle(job={"job_id": "job-1", "verdict": "suspected", "source": "deterministic"})
    assert (anomalous.status, anomalous.reasons) == ("failed", ("reinspection.still_anomalous",))
    dismissed = settle(review={"review_id": "rv-1", "decision": "dismissed"})
    assert (dismissed.status, dismissed.reasons) == ("failed", ("reinspection.review_dismissed",))
    # The same evidence ID seen before is not a new acquisition even with new media.
    # 此前出现过的证据 ID 即使媒体不同也不是新采集。
    assert settle(seen_evidence={"capture:new"}).reasons == ("reinspection.not_new_acquisition",)
    # A missing analysis never closes, whatever the reviewer said. / 没有分析时无论复核人说什么都不关单。
    assert settle(job=None).status == "unknown"


def test_model_answer_is_strict():
    good = {"image_usable": True, "target_matches_reference": True, "anomaly_score": 0.8, "defect_type": "damage",
            "description": "scratch", "unusable_reason": "none"}
    assert ModelAnswer.model_validate(good).anomaly_score == 0.8
    for bad in ({**good, "approve": True}, {**good, "decision": "confirmed"},
                {k: v for k, v in good.items() if k != "anomaly_score"}, {**good, "image_usable": "yes"},
                {**good, "anomaly_score": 1.5}, {**good, "unusable_reason": "sunny"}):
        with pytest.raises(ValidationError):
            ModelAnswer.model_validate(bad)


def test_results_carry_reasons_and_types():
    with pytest.raises(ValidationError):
        JobResult(verdict="refused", source="not_run")
    with pytest.raises(ValidationError):
        JobResult(verdict="suspected", source="scripted")
    assert JobResult(verdict="normal", source="deterministic").reasons == ()


def test_repository_catalogs_and_profiles_load():
    catalog = load_catalog(ROOT / "configs/analysis/p4_campus_v1.yaml")
    assert catalog.family_of(catalog.generic_defect) == "appearance"
    profile, sha = load_profile(ROOT, "configs/analysis/vlm_scripted_s0_v1.yaml")
    assert len(sha) == 64 and profile.threshold == 0.5 and profile.generic_type in profile.defect_types
    quality, _ = load_quality(ROOT, "configs/analysis/quality_sim_v1.yaml")
    assert quality.min_width == 160
    with pytest.raises(ValueError):
        load_profile(ROOT, "../outside.yaml")


def test_catalog_rejects_a_generic_defect_outside_every_family():
    data = yaml.safe_load((ROOT / "configs/analysis/p4_campus_v1.yaml").read_text(encoding="utf-8"))
    data["generic_defect"] = "rust"
    with pytest.raises(ValidationError):
        BusinessCatalog.model_validate(data)
