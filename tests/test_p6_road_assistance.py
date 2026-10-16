"""Exercise provenance and real-human boundaries with synthetic fixtures only.

仅用合成夹具核对来源及真实人工确认边界。
"""
from __future__ import annotations

import copy
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

C = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_road_labeling.py")))
POLYGON = [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]


@pytest.fixture
def assisted(tmp_path):
    source = tmp_path / "synthetic-source"
    source.mkdir()
    (source / "license.txt").write_text("Synthetic unit-test permission, not a real corpus", encoding="utf-8")
    Image.new("RGB", (400, 300), (60, 90, 80)).save(source / "1.png")
    image_sha = C.digest(source / "1.png")
    C.write(source / "pool.json", {"format": "drone.floodnet-acquisition-pool/v1", "gps_group_candidates": {"250": []}, "rows": [
        {"image_id": "1", "file": "1.png", "image_sha256": image_sha, "width": 400, "height": 300, "completeness": "complete_original"}]})
    C.write(source / "permission.json", {"permission_status": "documented", "license_snapshot_file": "license.txt",
                                         "license_snapshot_sha256": C.digest(source / "license.txt"), "license": "synthetic QA"})
    context = {"format": "drone.p6-independent-ai-context/v1", "authorization_reference": "SYNTHETIC UNIT TEST ONLY",
        "evaluated_provider_id": "minimax-vl", "evaluated_model": "MiniMax-M3.1-Flash-Preview", "human_reviews_created": 0,
        "formal_release_eligible": False, "annotators": {"synthetic-annotator": {"task_name": "synthetic-task", "history_fork": "none",
        "evaluation_prompt_provided": False, "evaluation_outputs_provided": False, "source_labels_provided": False,
        "provider": "synthetic-assistant", "assigned_images": {"floodnet-1": image_sha}}}}
    C.write(source / "context.json", context)
    policy = {"mode": "blind_or_independent_ai_human_adjudicated", "approval_reference": "SYNTHETIC UNIT TEST ONLY",
              "independent_context_sha256": C.digest(source / "context.json"), "human_adjudication_required": True,
              "evaluated_model_outputs_must_be_unseen": True, "evaluated_provider_id": "minimax-vl", "evaluated_model": "MiniMax-M3.1-Flash-Preview"}
    protocol = {"format": "drone.p6-road-protocol/v1", "corpus_id": "p6-road-business-v1", "annotation_policy": policy}
    C.write(source / "protocol.json", protocol)
    document = {"format": "drone.p6-road-ai-suggestions/v1", "author_kind": "isolated_ai_annotator", "author_id": "synthetic-annotator",
        "created_at": "2026-10-15T00:00:00Z", "isolation": {"parent_history": "none", "evaluation_prompt_seen": False,
        "evaluation_outputs_seen": False, "source_labels_seen": False}, "human_reviews_created": 0, "formal_release_eligible": False,
        "rows": [{"sample_id": "floodnet-1", "image_sha256": image_sha, "target_brief": "Synthetic road", "target_polygon": POLYGON,
                  "verdict": "normal", "objects": [], "notes": "Synthetic suggestion only", "uncertainties": [],
                  "inspected_at": "2026-10-15T00:00:00Z", "inspection_files": [str(source / "1.png")]}]}
    C.write(source / "suggestions.json", document)
    package = tmp_path / "package"
    receipt = C.build(source / "pool.json", source, source / "permission.json", source / "protocol.json", package,
                      suggestions=[source / "suggestions.json"], context_file=source / "context.json")
    return package, receipt, source, protocol


def operator(sample):
    return {"sample_id": sample["sample_id"], "image_sha256": sample["image_sha256"], "revision": 1,
            "reviewer_id": "synthetic-unit-test-reviewer", "origin": "operator_entered", "phase": "annotation",
            "started_at": "2026-10-15T00:01:00Z", "completed_at": "2026-10-15T00:02:00Z", "active_ms": 20000,
            "target_brief": "Synthetic road", "target_registered_at": "2026-10-15T00:01:10Z", "target_polygon": copy.deepcopy(POLYGON),
            "target_confirmed": True, "full_target_inspected": True, "verdict": "normal", "objects": [], "notes": "Synthetic suggestion only",
            "task_reference": "", "model_outputs_seen": True, "source_labels_seen": False, "metadata_seen": False,
            "asset_id": "", "site_id": "", "capture_batch_id": "", "near_duplicate_group": "", "grouping_verified": False,
            "grouping_evidence": "", "ai_draft_sha256": sample["ai_suggestion_sha256"], "ai_draft_decision": "accepted",
            "ai_original_checked": True, "evaluated_model_outputs_seen": False}


def imported(assisted, tmp_path):
    package, receipt, _, protocol = assisted
    bundle = C.read(package / "bundle.json")
    row = operator(bundle["samples"][0])
    file = tmp_path / "operator.json"
    C.write(file, {"format": "drone.p6-road-operator-labels/v1", "bundle_id": bundle["bundle_id"], "corpus_id": bundle["corpus_id"],
                   "qa_fixture": False, "records": [row], "formal_release_eligible": False, "reviewer_identity_authenticated": False})
    output = tmp_path / "imported"
    result = C.import_records(package, receipt["bundle_sha256"], file, output)
    manifest, labels = C.read(output / "manifest-draft.json"), C.read(output / "labels.json")
    return manifest["samples"][0], labels["rows"][0], protocol, Path(manifest["data_root"]), result


def test_ai_package_retains_sources_without_creating_human_records(assisted):
    package, receipt, source, _ = assisted
    bundle = C.verify_bundle(package, receipt["bundle_sha256"])
    assert receipt["human_reviews_created"] == 0
    assert not (package / "operator-export.json").exists()
    assert bundle["samples"][0]["p6_split"] == "unassigned_quarantine"
    assert (package / "ai-drafts/1.json").read_bytes() == (source / "suggestions.json").read_bytes()


@pytest.mark.parametrize("changes", [
    {"model_outputs_seen": False}, {"ai_original_checked": False}, {"ai_draft_decision": ""},
    {"ai_draft_sha256": "0" * 64}, {"evaluated_model_outputs_seen": None}, {"active_ms": 0},
    {"reviewer_id": ""}, {"origin": "ai_draft"}, {"ai_draft_decision": "revised"}, {"notes": "Changed but claimed accepted"},
])
def test_suggestions_cannot_be_saved_as_completed_unseen_or_unreviewed_truth(assisted, changes):
    sample = C.read(assisted[0] / "bundle.json")["samples"][0]
    with pytest.raises(ValueError):
        C.validate({**operator(sample), **changes}, sample, False)


def test_import_preserves_ai_exposure_without_manufacturing_candidate_review_time(assisted, tmp_path):
    sample, label, protocol, root, receipt = imported(assisted, tmp_path)
    assert label["origin"] == "human" and label["model_outputs_seen"] is True and label["hints_seen"] is True
    assert sample["development_exposed"] is False and sample["asset_id"] is None and sample["split"] is None
    assert receipt["candidate_review_sessions"] == 0 and not receipt["formal_release_eligible"]
    assert C.CORE["assisted_label_issues"](sample, label, protocol, root, {}) == []


@pytest.mark.parametrize("field,value", [
    ("evaluated_model_outputs_seen", True), ("original_checked", False), ("context_sha256", "0" * 64),
    ("draft_sha256", "0" * 64), ("suggestion_sha256", "0" * 64), ("operator_export_sha256", "0" * 64),
    ("author_id", "another-annotator"), ("decision", "revised"),
])
def test_formal_admission_requires_independent_draft_and_actual_operator_evidence(assisted, tmp_path, field, value):
    sample, label, protocol, root, _ = imported(assisted, tmp_path)
    label["ai_assistance"][field] = value
    assert C.CORE["assisted_label_issues"](sample, label, protocol, root, {})


def test_old_blind_policy_does_not_implicitly_accept_ai_assisted_truth(assisted, tmp_path):
    sample, label, protocol, root, _ = imported(assisted, tmp_path)
    protocol.pop("annotation_policy")
    assert C.CORE["assisted_label_issues"](sample, label, protocol, root, {})


@pytest.mark.parametrize("file", ["ai-drafts/1.json", "annotation-context.json"])
def test_ai_provenance_files_are_byte_bound(assisted, file):
    package, receipt, _, _ = assisted
    target = package / file
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError):
        C.verify_bundle(package, receipt["bundle_sha256"])


def test_annotator_cannot_use_another_workers_photo_or_evaluation_context(assisted):
    _, _, source, _ = assisted
    context, document = C.read(source / "context.json"), C.read(source / "suggestions.json")
    rows = C.CORE["ai_suggestions"](document)
    context["annotators"]["synthetic-annotator"]["evaluation_prompt_provided"] = True
    with pytest.raises(ValueError):
        C.CORE["ai_context"](context, document, rows)
    context["annotators"]["synthetic-annotator"]["evaluation_prompt_provided"] = False
    rows["floodnet-1"]["image_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        C.CORE["ai_context"](context, document, rows)


def test_post_import_label_edits_cannot_bypass_the_actual_operator_export(assisted, tmp_path):
    sample, label, protocol, root, _ = imported(assisted, tmp_path)
    label["notes"] = "Not the operator's exported judgment"
    assert C.CORE["assisted_label_issues"](sample, label, protocol, root, {})


@pytest.mark.parametrize("exposed", [True, False])
def test_assistance_neither_erases_development_exposure_nor_hides_its_provenance(assisted, tmp_path, exposed):
    sample, label, protocol, root, _ = imported(assisted, tmp_path)
    sample.update(split="test", evaluation_role="primary")
    if not exposed:
        label.pop("ai_assistance")
        label["model_outputs_seen"] = False
    manifest = C.read(tmp_path / "imported/manifest-draft.json")
    manifest.update(format="drone.p6-road-corpus/v1", samples=[sample])
    protocol.update(status="frozen", approval_reference="SYNTHETIC", defect_types=["debris", "vegetation", "other_entity"],
                    minimum_samples={}, thresholds={}, additional_requirements={})
    result = C.CORE["audit"](manifest, {"format": "drone.p6-road-labels/v1", "corpus_id": protocol["corpus_id"], "rows": [label]},
                              protocol, {"image_sha256": [sample["image_sha256"]] if exposed else []}, root)
    expected = "exposed_image_outside_development" if exposed else "assisted_annotation_provenance_missing"
    assert expected in result["issue_counts"] and not result["data_ready"]


def test_bound_operator_record_still_requires_real_target_confirmation(assisted, tmp_path):
    sample, label, protocol, root, _ = imported(assisted, tmp_path)
    path = root / label["ai_assistance"]["operator_export_file"]
    value = C.read(path)
    value["records"][0]["target_confirmed"] = False
    new = tmp_path / "unconfirmed-export.json"
    C.write(new, value)
    label["ai_assistance"].update(operator_export_file=new.relative_to(root).as_posix(), operator_export_sha256=C.digest(new))
    assert "assisted_annotation_target_confirmation_missing" in C.CORE["assisted_label_issues"](sample, label, protocol, root, {})
