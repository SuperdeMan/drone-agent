"""A locator preserves evidence and cannot manufacture reviewed labels.

定位页保留证据，不能制造人工确认标签。
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
V = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/p6_proposal_viewer.py")))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(V.encoded(value))


@pytest.fixture
def inputs(tmp_path):
    repo, data = tmp_path / "repo", tmp_path / "data"
    batch = repo / "eval/s2/p6_candidates_v9"
    attempt = batch / "attempts/test"
    media = tmp_path / "input"
    data.mkdir()
    media.mkdir()
    for p, size, color in [(data / "photo.jpg", (100, 80), "gray"), (media / "original.jpg", (50, 40), "gray"),
                           (media / "support.jpg", (50, 40), "cyan")]:
        Image.new("RGB", size, color).save(p)
    proposals = [{"proposal_id": "view-p01", "bbox": [.1, .2, .2, .3]}]
    manifest = [{"case_id": "view", "proposals": proposals,
                 "images": [{"file": n, "sha256": V.digest(media / n)} for n in ["original.jpg", "support.jpg"]]}]
    config = {"model": "qa-model"}
    write(batch / "config.json", config)
    write(batch / "input-manifest.json", manifest)
    write(batch / "protocol.json", {"formal_release_eligible": False})
    index = repo / "index.json"
    write(index, {"cases": [{"case_id": "case", "image_id": "qa", "image_file": "photo.jpg",
        "image_sha256": V.digest(data / "photo.jpg"), "width": 100, "height": 80,
        "target_region": {"sha256": "qa-support"}}]})
    plan = {"manifest_sha256": V.digest(batch / "input-manifest.json"), "config_sha256": V.digest(batch / "config.json"),
        "parents": [{"view_id": "view", "case_id": "case", "source_image_sha256": V.digest(data / "photo.jpg"),
            "source_target_sha256": "qa-support", "source_size": [100, 80], "input_size": [50, 40]}]}
    write(batch / "plan.json", plan)
    observation = {"proposal_id": "view-p01", "entity_kind": "physical_object", "target_relation": "intersects",
        "localization_status": "entity_group", "entity_evidence": "QA <script>literal</script>",
        "relation_evidence": "QA position only", "decision_uncertainty": "", "description_uncertainty": "QA unknown type"}
    answer = {"schema_version": "0.2.0", "image_usable": True, "target_visible": True, "observations": [observation]}
    results = [{"case_id": "view", "status": "answered", "model_used": "qa-model", "strict_replay": "passed",
                "finish": "stop", "parsed": answer, "raw_content": json.dumps(answer), "schema_issues": []}]
    write(attempt / "cloud/output/results.json", results)
    summary = V.CONTRACT["reduce_results"](manifest, results, "qa-model")
    summary["results_sha256"] = V.digest(attempt / "cloud/output/results.json")
    write(attempt / "replay-summary.json", summary)
    queue = {"human_reviews_completed": 0, "formal_release_eligible": False, "all_proposals_require_human_confirmation": True,
        "rows": [{"proposal_id": "view-p01", "view_id": "view", "source_bbox": proposals[0]["bbox"], "model_observation": observation,
            "model_disposition": "candidate", "priority": "first", "review_reasons": ["unconfirmed_model_group"],
            "conditional_geometry": {"source_support_pixels": 10, "possible_road_pixels": 10}, "instance_bbox": None,
            "human_verdict": None, "reviewer": None, "reviewed_at": None, "human_hint_exposure": None}]}
    write(attempt / "review-queue.json", queue)
    write(attempt / "analysis.json", {"conditional_geometry": {"index_sha256": V.digest(index)},
                                     "review_queue_sha256": V.digest(attempt / "review-queue.json")})
    receipt = repo / "receipt.json"
    write(receipt, {"archive_directory": attempt.relative_to(repo).as_posix(),
        "artifact_sha256": {p.relative_to(repo).as_posix(): V.digest(p) for p in attempt.rglob('*.json')},
        "frozen_inputs_sha256": {n: V.digest(batch / n) for n in ["config.json", "input-manifest.json", "protocol.json", "plan.json"]},
        "source_sha256": {"scripts/p6_candidate_groups.py": V.digest(ROOT / "scripts/p6_candidate_groups.py")},
        "formal_release_eligible": False, "run_id": "QA_NOT_A_MODEL_CALL"})
    return batch, attempt, receipt, index, data, media, tmp_path / "output"


def refresh_fixture_hashes(inputs):
    batch, attempt, receipt, index, *_ = inputs
    analysis = V.read(attempt / "analysis.json")
    analysis["review_queue_sha256"] = V.digest(attempt / "review-queue.json")
    write(attempt / "analysis.json", analysis)
    value = V.read(receipt)
    value["artifact_sha256"] = {p.relative_to(batch.parents[2]).as_posix(): V.digest(p) for p in attempt.rglob('*.json')}
    write(receipt, value)


def test_package_copies_exact_views_preserves_groups_and_contains_no_review_writes(inputs):
    receipt = V.build(*inputs)
    checked = V.verify(inputs[-1], receipt["bundle_sha256"])
    bundle = V.read(inputs[-1] / "bundle.json")
    assert checked["readonly"] and checked["human_reviews_created"] == 0
    assert bundle["proposals"] == V.read(inputs[1] / "review-queue.json")["rows"]
    assert bundle["targets"][0]["size"] == [100, 80]
    assert bundle["targets"][0]["modes"]["model"]["size"] == [50, 40]
    assert receipt["copied_images"] == 3 and receipt["new_provider_calls"] == 0
    text = (inputs[-1] / "data.js").read_text(encoding="utf-8")
    assert "<script>" not in text and "\\u003cscript\\u003e" in text
    with pytest.raises(ValueError, match="new directory"):
        V.build(*inputs)


@pytest.mark.parametrize("change,fragment", [
    (lambda q: q["rows"].append(dict(q["rows"][0])), "coverage"),
    (lambda q: q["rows"][0].update(model_disposition="outside_target"), "group"),
    (lambda q: q["rows"][0].update(source_bbox=[.2, .2, .3, .3]), "geometry"),
    (lambda q: q["rows"][0].update(human_verdict="confirmed"), "unconfirmed"),
    (lambda q: q.update(human_reviews_completed=1), "development"),
])
def test_rehashed_bad_queue_still_cannot_change_semantics_or_human_state(inputs, change, fragment):
    path = inputs[1] / "review-queue.json"
    queue = V.read(path)
    change(queue)
    write(path, queue)
    refresh_fixture_hashes(inputs)
    with pytest.raises(ValueError, match=fragment):
        V.build(*inputs)
    assert not inputs[-1].exists()


def test_original_or_model_image_tampering_is_rejected_before_package_creation(inputs):
    (inputs[5] / "original.jpg").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="binding"):
        V.build(*inputs)
    assert not inputs[-1].exists()


def test_source_results_change_is_not_hidden_by_existing_summary(inputs):
    p = inputs[1] / "cloud/output/results.json"
    p.write_bytes(b"[]")
    with pytest.raises(ValueError, match="archived evidence"):
        V.build(*inputs)


@pytest.mark.parametrize("name", ["app.js", "data.js", "bundle.json"])
def test_package_verifier_rejects_changed_ui_or_bundle(inputs, name):
    receipt = V.build(*inputs)
    p = inputs[-1] / name
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(ValueError):
        V.verify(inputs[-1], receipt["bundle_sha256"])


def test_package_verifier_checks_image_bytes_and_rejects_path_traversal(inputs):
    receipt = V.build(*inputs)
    next((inputs[-1] / "images").glob('*.jpg')).write_bytes(b"changed")
    with pytest.raises(ValueError, match="image hash"):
        V.verify(inputs[-1], receipt["bundle_sha256"])
    with pytest.raises(ValueError, match="leaves package"):
        V.inside(inputs[-1], "../receipt.json")
