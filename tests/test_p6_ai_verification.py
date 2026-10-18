"""AI delegation must retain actual review provenance without inventing human truth.

AI 代行必须保留真实复核来源，不制造真人真值。
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

C = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_business_eval.py")))
POLYGON = [[0, 0], [1, 0], [1, 1], [0, 1]]


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return C.digest(path)


@pytest.fixture
def delegated(tmp_path):
    picture = tmp_path / "image.png"
    Image.new("RGB", (20, 20), (30, 90, 30)).save(picture)
    image_sha = C.digest(picture)
    original = {"sample_id": "one", "image_sha256": image_sha, "target_brief": "Synthetic road",
                "target_polygon": POLYGON, "verdict": "normal", "objects": [], "notes": "Synthetic reference only",
                "uncertainties": [], "inspected_at": "2026-10-18T00:00:00Z", "inspection_files": [str(picture)]}
    draft = {"format": "drone.p6-road-ai-suggestions/v1", "author_kind": "isolated_ai_annotator", "author_id": "first",
             "created_at": original["inspected_at"], "isolation": {"parent_history": "none", "evaluation_prompt_seen": False,
             "evaluation_outputs_seen": False, "source_labels_seen": False}, "human_reviews_created": 0,
             "formal_release_eligible": False, "rows": [original]}
    draft_sha = save(tmp_path / "draft.json", draft)
    context = {"format": "drone.p6-independent-ai-context/v1", "authorization_reference": "SYNTHETIC ONLY",
               "evaluated_provider_id": "minimax-vl", "evaluated_model": "synthetic-evaluated-model", "human_reviews_created": 0,
               "formal_release_eligible": False, "annotators": {"first": {"task_name": "synthetic-first", "history_fork": "none",
               "evaluation_prompt_provided": False, "evaluation_outputs_provided": False, "source_labels_provided": False,
               "provider": "synthetic-ai", "assigned_images": {"one": image_sha}}}}
    context_sha = save(tmp_path / "context.json", context)
    assignment = {"format": "drone.p6-ai-review-assignment/v1", "reviewer_id": "second", "parent_history": "none",
                  "created_at": "2026-10-18T00:01:00Z", "evaluation_prompt_access": "forbidden",
                  "evaluation_outputs_access": "forbidden", "source_class_label_access": "forbidden",
                  "samples": [{"sample_id": "one", "image_file": str(picture), "image_sha256": image_sha,
                  "first_author_id": "first", "first_row": original}]}
    assignment_sha = save(tmp_path / "assignment.json", assignment)
    registry = {"format": "drone.p6-ai-review-registration/v1", "approval_reference": "SYNTHETIC ONLY",
                "method": "independent_ai_draft_then_separate_ai_verification", "provider": "synthetic-ai",
                "human_reviews_created": 0, "formal_release_eligible": False,
                "assignments": [{"file": "assignment.json", "sha256": assignment_sha, "reviewer_id": "second"}]}
    registry_sha = save(tmp_path / "registry.json", registry)
    final = {**original, "inspected_at": "2026-10-18T00:03:00Z"}
    record = {"sample_id": "one", "image_sha256": image_sha, "first_author_id": "first",
              "first_row_sha256": C.canonical_digest(original), "reviewer_id": "second", "decision": "accepted",
              "changes": {}, "findings": ["Synthetic second review, not a visual ground truth claim"],
              "original_image_checked": True, "inspected_files": [str(picture)],
              "inspection_file_sha256": {str(picture): image_sha}, "reviewed_at": final["inspected_at"],
              "final_row": final, "final_row_sha256": C.canonical_digest(final)}
    review = {"format": "drone.p6-road-ai-verification/v1", "reviewer_kind": "isolated_ai_reviewer", "reviewer_id": "second",
              "created_at": "2026-10-18T00:02:00Z", "assignment_sha256": assignment_sha,
              "isolation": {**draft["isolation"], "assigned_first_pass_seen": True},
              "human_reviews_created": 0, "formal_release_eligible": False, "rows": [record]}
    review_sha = save(tmp_path / "review.json", review)
    policy = {"mode": "independent_ai_verified_reference", "approval_reference": "SYNTHETIC ONLY",
              "human_adjudication_required": False, "delegated_ai_adjudication_approved": True,
              "evaluated_model_outputs_must_be_unseen": True, "evaluated_provider_id": "minimax-vl",
              "evaluated_model": "synthetic-evaluated-model", "independent_context_sha256": context_sha,
              "verification_context_sha256": registry_sha}
    protocol = {"format": "drone.p6-road-protocol/v1", "corpus_id": "qa", "annotation_policy": policy}
    label = {"sample_id": "one", "image_sha256": image_sha, "origin": "delegated_ai", "reviewer_id": "second",
             "recorded_at": final["inspected_at"], "full_target_inspected": True, "verdict": "normal", "defect_types": [],
             "objects": [], "notes": final["notes"], "model_outputs_seen": True, "hints_seen": True,
             "ai_assistance": {"method": "independent_ai_then_ai", "original_checked": True, "evaluated_model_outputs_seen": False,
                 "draft_file": "draft.json", "draft_sha256": draft_sha, "author_id": "first",
                 "suggestion_sha256": C.canonical_digest(original), "context_file": "context.json", "context_sha256": context_sha,
                 "verification_context_file": "registry.json", "verification_context_sha256": registry_sha,
                 "review_file": "review.json", "review_sha256": review_sha, "review_row_sha256": C.canonical_digest(record),
                 "decision": "accepted"}}
    sample = {"sample_id": "one", "image_sha256": image_sha, "split": "test",
              "target": {"status": "ai_verified", "brief": final["target_brief"], "polygon": POLYGON}}
    return sample, label, protocol, tmp_path


def check(value):
    return C.assisted_label_issues(*value, {})


def rewrite_review(value, change):
    _, label, _, root = value
    path = root / "review.json"
    document = C.read(path)
    change(document)
    label["ai_assistance"]["review_sha256"] = save(path, document)
    label["ai_assistance"]["review_row_sha256"] = C.canonical_digest(document["rows"][0])


def test_explicit_delegation_accepts_bound_ai_reference_without_human_fields(delegated):
    assert check(delegated) == []
    assert delegated[1]["origin"] == "delegated_ai"
    assert "operator_export_file" not in delegated[1]["ai_assistance"]


@pytest.mark.parametrize("field,value", [("human_adjudication_required", True), ("approval_reference", ""),
    ("delegated_ai_adjudication_approved", False), ("evaluated_model_outputs_must_be_unseen", False),
    ("mode", "blind_or_independent_ai_human_adjudicated"), ("verification_context_sha256", "0" * 64)])
def test_ai_reference_requires_current_explicit_scope_and_registry(delegated, field, value):
    delegated[2]["annotation_policy"][field] = value
    assert check(delegated)


@pytest.mark.parametrize("field,value", [("notes", "Unreviewed change"), ("origin", "human"),
    ("reviewer_id", "first"), ("model_outputs_seen", False), ("full_target_inspected", False),
    ("recorded_at", "2026-10-18T00:04:00Z"), ("defect_types", ["debris"])])
def test_final_label_cannot_drift_or_masquerade_as_human(delegated, field, value):
    delegated[1][field] = value
    assert check(delegated)


@pytest.mark.parametrize("field,value", [("original_checked", False), ("evaluated_model_outputs_seen", True),
    ("review_sha256", "0" * 64), ("suggestion_sha256", "0" * 64), ("decision", "revised")])
def test_review_exposure_and_bytes_are_mandatory(delegated, field, value):
    delegated[1]["ai_assistance"][field] = value
    assert check(delegated)


@pytest.mark.parametrize("change", [
    lambda d: d.update(reviewer_id="first"),
    lambda d: d["isolation"].update(evaluation_outputs_seen=True),
    lambda d: d["rows"][0].update(original_image_checked=False),
    lambda d: d["rows"][0].update(findings=[]),
    lambda d: d["rows"][0].update(decision="revised", changes={}),
    lambda d: d["rows"][0].update(inspected_files=[]),
    lambda d: d["rows"][0]["final_row"].update(notes="Unbound correction"),
])
def test_rehashed_bad_review_cannot_bypass_semantic_provenance(delegated, change):
    rewrite_review(delegated, change)
    assert check(delegated)


def test_actual_revision_is_accepted_and_preserves_the_first_pass(delegated):
    before = C.read(delegated[3] / "draft.json")

    def change(document):
        row = document["rows"][0]
        row.update(decision="revised", changes={"notes": "Revised synthetic observation"})
        row["final_row"]["notes"] = row["changes"]["notes"]
        row["final_row_sha256"] = C.canonical_digest(row["final_row"])

    rewrite_review(delegated, change)
    delegated[1]["notes"] = "Revised synthetic observation"
    delegated[1]["ai_assistance"]["decision"] = "revised"
    assert check(delegated) == []
    assert C.read(delegated[3] / "draft.json") == before


def test_ai_reference_does_not_inflate_human_count_or_asset_coverage(delegated):
    sample, label, protocol, root = delegated
    sample.update(image_file="image.png", width=20, height=20, source_id="qa", evaluation_role="primary",
                  asset_id=None, site_id=None, capture_batch_id=None, near_duplicate_group=None,
                  grouping_verified=False, development_exposed=False, media_origin="real_capture")
    protocol.update(status="frozen", approval_reference="SYNTHETIC", defect_types=["debris"], minimum_samples={},
                    thresholds={}, additional_requirements={})
    manifest = {"format": "drone.p6-road-corpus/v1", "corpus_id": "qa", "sources": {}, "samples": [sample]}
    labels = {"format": "drone.p6-road-labels/v1", "corpus_id": "qa", "rows": [label]}
    result = C.audit(manifest, labels, protocol, {"image_sha256": []}, root)
    assert result["human_labels_present"] == 0 and result["delegated_ai_labels_present"] == 1
    assert not result["data_ready"] and "grouping_identity_missing" in result["issue_counts"]
    assert "independent_business_review_missing" not in result["issue_counts"]
    exposed = C.audit(manifest, labels, protocol, {"image_sha256": [sample["image_sha256"]]}, root)
    assert "exposed_image_outside_development" in exposed["issue_counts"]


def test_ai_pair_flags_alone_do_not_establish_verified_repeat_capture(delegated):
    sample, _, protocol, root = delegated
    pair = {"origin": "delegated_ai", "same_condition_ai_verified": True, "distinct_captures_ai_verified": True}
    assert C.delegated_pair_issues(pair, {"one": sample}, protocol, root, {})


@pytest.mark.parametrize("tamper", [None, "same_original", "unseen", "exposed_reviewer", "changed_state"])
def test_ai_pair_requires_two_originals_and_registered_same_state_review(delegated, tamper):
    sample, _, protocol, root = delegated
    second = root / "second.png"
    Image.new("RGB", (20, 20), (90, 30, 30)).save(second)
    sample["asset_id"] = "synthetic-road"
    samples = {"one": sample, "two": {**sample, "sample_id": "two", "image_sha256": C.digest(second)}}
    context = {"format": "drone.p6-ai-pair-context/v1", "approval_reference": "SYNTHETIC",
        "reviewers": {"pair-reviewer": {"history_fork": "none", "provider": "synthetic-ai",
            "evaluation_prompt_provided": False, "evaluation_outputs_provided": False, "source_labels_provided": False,
            "pairs": {"pair-one": ["one", "two"]}}}}
    row = {"pair_id": "pair-one", "sample_ids": ["one", "two"], "same_asset": True, "same_condition": True,
        "distinct_captures": True, "asset_id": "synthetic-road", "evidence": "Synthetic only",
        "reviewed_at": "2026-10-18T01:00:00Z", "images": {
            "one": {"file": "image.png", "image_sha256": sample["image_sha256"], "original_checked": True, "target_polygon": POLYGON},
            "two": {"file": "second.png", "image_sha256": C.digest(second), "original_checked": True, "target_polygon": POLYGON}}}
    document = {"format": "drone.p6-ai-pair-verification/v1", "origin": "delegated_ai", "reviewer_id": "pair-reviewer",
        "isolation": {"parent_history": "none", "evaluation_prompt_seen": False, "evaluation_outputs_seen": False, "source_labels_seen": False}, "rows": [row]}
    if tamper == "same_original":
        row["images"]["two"]["file"] = "image.png"
    elif tamper == "unseen":
        row["images"]["two"]["original_checked"] = False
    elif tamper == "exposed_reviewer":
        context["reviewers"]["pair-reviewer"]["evaluation_prompt_provided"] = True
    elif tamper == "changed_state":
        row["same_condition"] = False
    context_sha = save(root / "pair-context.json", context)
    protocol["annotation_policy"]["pair_context_sha256"] = context_sha
    reference = {"context_file": "pair-context.json", "context_sha256": context_sha, "file": "pair-review.json",
        "sha256": save(root / "pair-review.json", document), "row_sha256": C.canonical_digest(row), "pair_id": "pair-one"}
    pair = {"origin": "delegated_ai", "sample_ids": ["one", "two"], "reviewer_id": "pair-reviewer", "ai_verification": reference}
    issues = C.delegated_pair_issues(pair, samples, protocol, root, {})
    assert bool(issues) == (tamper is not None)


def test_review_evidence_must_remain_inside_declared_data_root(delegated):
    with pytest.raises(ValueError, match="outside data root"):
        C.bound_evidence(delegated[3], "../outside.json", "0" * 64, {})
