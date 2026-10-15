"""Per-proposal unknown semantics and zero-call lock contention are development boundaries.

逐提案未知语义与锁竞争零调用是开发边界。
"""
from __future__ import annotations

import copy
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
C = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/p6_candidate_review.py")))
P = [{"proposal_id": "view-p01", "bbox": [.1, .1, .9, .9]}]


def answer(**changes):
    row = {"proposal_id": "view-p01", "entity_kind": "physical_object", "target_relation": "inside",
           "separability": "isolated", "refined_bbox": [.2, .2, .3, .3], "entity_evidence": "QA material",
           "relation_evidence": "QA relative position", "uncertainty": ""}
    row.update(changes)
    return {"schema_version": "0.1.0", "image_usable": True, "target_visible": True, "observations": [row]}


def result(value=None):
    value = answer() if value is None else value
    return {"case_id": "view", "status": "answered", "model_used": "qa-model", "strict_replay": "passed",
            "finish": "stop", "parsed": value, "raw_content": json.dumps(value), "schema_issues": []}


@pytest.mark.parametrize("changes,group", [
    ({}, "candidate"),
    ({"target_relation": "outside"}, "outside_target"),
    ({"entity_kind": "shadow", "refined_bbox": None}, "non_entity"),
    ({"entity_kind": "surface_change", "refined_bbox": None}, "non_entity"),
    ({"entity_kind": "vehicle", "uncertainty": "Cannot establish obstruction"}, "needs_review"),
    ({"entity_kind": "vehicle", "target_relation": "outside"}, "outside_target"),
    ({"entity_kind": "unknown", "uncertainty": "Nature unknown", "refined_bbox": None}, "needs_review"),
    ({"target_relation": "edge_uncertain", "uncertainty": "Edge unclear"}, "needs_review"),
    ({"separability": "mixed", "uncertainty": "Several appearances overlap"}, "needs_review"),
    ({"uncertainty": "Material uncertain despite categorical fields"}, "needs_review"),
])
def test_explicit_axes_control_only_model_groups(changes, group):
    value = answer(**changes)
    parsed, issues = C.validate_answer(json.dumps(value), "stop", P)
    assert issues == []
    assert C.disposition(parsed["observations"][0], parsed) == group


@pytest.mark.parametrize("changes,issue", [
    ({"entity_kind": "vehicle"}, "uncertainty_reason_missing"),
    ({"target_relation": "unknown"}, "uncertainty_reason_missing"),
    ({"refined_bbox": None}, "physical_localization_missing"),
    ({"refined_bbox": [.01, .2, .4, .4]}, "refinement_leaves_proposal"),
    ({"refined_bbox": [True, .2, .4, .4]}, "refined_bbox"),
    ({"refined_bbox": [.3, .2, .3, .4]}, "refined_bbox"),
    ({"entity_evidence": " "}, "evidence_text"),
    ({"entity_kind": []}, "observation_enums"),
    ({"proposal_id": "other"}, "proposal_identity"),
])
def test_invalid_or_unexplained_assertions_are_rejected(changes, issue):
    _, issues = C.validate_answer(json.dumps(answer(**changes)), "stop", P)
    assert issue in issues


def test_bad_quality_and_target_visibility_cannot_create_positive_labels():
    value = answer()
    value["target_visible"] = False
    assert "invisible_target_relation" in C.validate_answer(json.dumps(value), "stop", P)[1]
    value = answer(entity_kind="unknown", target_relation="unknown", separability="unknown", refined_bbox=None,
                   uncertainty="Blur prevents all judgments")
    value["image_usable"] = False
    parsed, issues = C.validate_answer(json.dumps(value), "stop", P)
    assert not issues and C.disposition(parsed["observations"][0], parsed) == "needs_review"


def test_no_business_size_floor_and_legacy_scene_fields_are_rejected():
    value = answer(refined_bbox=[.200001, .200001, .200002, .200002])
    assert C.validate_answer(json.dumps(value), "stop", P)[1] == []
    value["verdict"] = "normal"
    assert "response_fields" in C.validate_answer(json.dumps(value), "stop", P)[1]


def test_duplicate_keys_nonfinite_json_and_truncation_are_invalid():
    text = json.dumps(answer())
    assert "invalid_json" in C.validate_answer(text.replace('"image_usable": true', '"image_usable": false,"image_usable":true'), "stop", P)[1]
    assert "invalid_json" in C.validate_answer(text.replace("0.2", "NaN", 1), "stop", P)[1]
    assert "finish_not_stop" in C.validate_answer(text, "length", P)[1]


def test_missing_duplicate_or_bad_responses_preserve_all_frozen_proposals():
    proposals = [*copy.deepcopy(P), {"proposal_id": "view-p02", "bbox": [.1, .1, .9, .9]}]
    manifest = [{"case_id": "view", "proposals": proposals, "images": []}]
    duplicate = answer()
    duplicate["observations"].append(copy.deepcopy(duplicate["observations"][0]))
    for results in [[], [result(answer())], [result(duplicate)], [dict(result(), model_used="other")]]:
        reduced = C.reduce_results(manifest, results, "qa-model")
        assert reduced["model_disposition_counts"] == {"needs_review": 2}
        assert reduced["scene_verdict"] is None and reduced["human_reviews_completed"] == 0


def test_excluding_every_given_proposal_does_not_mean_clear_scene():
    manifest = [{"case_id": "view", "proposals": P, "images": []}]
    reduced = C.reduce_results(manifest, [result(answer(entity_kind="shadow"))], "qa-model")
    assert reduced["model_disposition_counts"] == {"non_entity": 1}
    assert reduced["scene_verdict"] is None and not reduced["recall_computed"] and not reduced["accuracy_computed"]
    with pytest.raises(ValueError, match="duplicate or foreign"):
        C.reduce_results(manifest, [result(), result()], "qa-model")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def previous(tmp_path):
    previous, inputs = tmp_path / "v7", tmp_path / "images"
    inputs.mkdir()
    images = []
    for name in ["original.jpg", "support.jpg"]:
        (inputs / name).write_bytes(b"QA bytes only; never submitted to a provider")
        images.append({"file": name, "sha256": C.digest(inputs / name)})
    config = {"model": "qa-model", "prompt_version": "road-region-contact-dev-v3", "instruction": "old",
              "system_prompt": "QA only", "formal_release_eligible": False, "human_ground_truth_available": False, "calls_per_case": 1}
    write(previous / "config.json", config)
    write(previous / "input-manifest.json", [{"case_id": "view", "images": images}])
    old_answer = {"obstacles": [{"type": "debris", "bbox": [.1, .1, .9, .9]}], "verdict": "suspected", "reason": "OLD_HINT_NOT_FOR_NEW_REQUEST"}
    write(previous / "diagnostic/results.json", [dict(result(old_answer))])
    write(previous / "replay-summary.json", {"results_sha256": C.digest(previous / "diagnostic/results.json")})
    write(previous / "diagnostic/execution.json", {"resident_container_identities_unchanged": False})
    write(previous / "plan.json", {"config_sha256": C.digest(previous / "config.json"),
        "manifest_sha256": C.digest(previous / "input-manifest.json"), "parents": [],
        "expected_runtime": {"runtime_source_sha": "qa-runtime", "runner_sha256": "old", "provider": "qa", "endpoint_host": "qa.invalid"}})
    config.update(prompt_version=C.VERSION, instruction="NEW QA INSTRUCTION")
    write(tmp_path / "new.json", config)
    return previous, inputs, tmp_path / "new.json", tmp_path / "prepared"


def test_preparation_copies_images_and_boxes_without_legacy_labels(previous):
    plan = C.prepare(*previous)
    assert plan["source_batch_environment_qualified"] is False
    manifest = C.read(previous[3] / "inputs/manifest.json")
    assert set(manifest[0]["proposals"][0]) == {"proposal_id", "bbox"}
    messages = C.make_messages(C.read(previous[2]), manifest[0], previous[3] / "inputs")
    assert "OLD_HINT_NOT_FOR_NEW_REQUEST" not in json.dumps(messages)
    assert "Frozen unverified proposals" in messages[1]["content"][0]["text"]
    assert (previous[3] / "inputs/original.jpg").read_bytes() == (previous[1] / "original.jpg").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        C.prepare(*previous)


def test_source_and_configuration_changes_are_not_silent(previous):
    config = C.read(previous[2])
    config["model"] = "other"
    write(previous[2], config)
    with pytest.raises(ValueError, match="prompt"):
        C.prepare(*previous)
    config["model"] = "qa-model"
    write(previous[2], config)
    (previous[1] / "original.jpg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="binding"):
        C.prepare(*previous)
    with pytest.raises(ValueError, match="binding"):
        C.input_file(previous[1], {"file": "../new.json", "sha256": C.digest(previous[2])})


def load_cloud(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/p6_candidate_cloud.py")))


def test_only_a_proven_zero_call_busy_attempt_can_be_retried(tmp_path, monkeypatch):
    cloud = load_cloud(monkeypatch)
    path = tmp_path / "attempts/first/cloud/execution.json"
    value = {"status": "blocked_lock_busy", "provider_invocations": 0, "container_started": False, "project_lock_acquired": False}
    write(path, value)
    cloud.check_attempts(tmp_path)
    for changed in [dict(value, provider_invocations=False), dict(value, provider_invocations=1), dict(value, status="failed_execution")]:
        write(path, changed)
        with pytest.raises(ValueError, match="may have invoked"):
            cloud.check_attempts(tmp_path)
    (tmp_path / "attempts/unknown").mkdir()
    write(path, value)
    with pytest.raises(ValueError, match="reconciliation"):
        cloud.check_attempts(tmp_path)


def remote_tool():
    return runpy.run_path(str(ROOT / "scripts/p6_candidate_remote.py"))


def test_busy_lock_never_reaches_preflight_or_docker(tmp_path, monkeypatch):
    remote = remote_tool()
    globals_ = remote["execute"].__globals__
    monkeypatch.setitem(globals_, "try_lock", lambda _: None)
    monkeypatch.setitem(globals_, "preflight", lambda: pytest.fail("preflight called while busy"))
    monkeypatch.setitem(globals_, "run_container", lambda *_: pytest.fail("provider path called while busy"))
    receipt = remote["execute"](tmp_path, tmp_path)
    assert receipt["status"] == "blocked_lock_busy" and receipt["provider_invocations"] == 0
    assert receipt["container_started"] is False


@pytest.mark.parametrize("change_inventory", [False, True])
def test_lock_covers_preflight_execution_and_postflight(tmp_path, monkeypatch, change_inventory):
    remote = remote_tool()
    globals_ = remote["execute"].__globals__
    lock = SimpleNamespace(closed=False)
    lock.close = lambda: setattr(lock, "closed", True)
    calls = []
    def snapshot():
        assert not lock.closed
        calls.append("snapshot")
        return [{"id": "changed" if change_inventory and len(calls) > 1 else "original"}]
    def run(*args):
        assert not lock.closed
        write(tmp_path / "output/run.json", {"cases_attempted": 6})
        return 0
    monkeypatch.setitem(globals_, "try_lock", lambda _: lock)
    monkeypatch.setitem(globals_, "preflight", lambda: {"held": not lock.closed})
    monkeypatch.setitem(globals_, "identities", snapshot)
    monkeypatch.setitem(globals_, "run_container", run)
    write(tmp_path / "config.json", {})
    write(tmp_path / "input/manifest.json", [])
    receipt = remote["execute"](tmp_path, tmp_path)
    assert lock.closed and receipt["lock_held_through_checks"]
    assert receipt["resident_container_identities_unchanged"] is not change_inventory
    assert receipt["status"] == ("not_qualified_execution_or_environment" if change_inventory else "completed")


def test_flock_is_nonblocking_and_busy_descriptor_is_closed(tmp_path, monkeypatch):
    remote = remote_tool()
    (tmp_path / "stack.lock").write_text("")
    streams = []
    def busy(stream, flags):
        assert flags == 12
        streams.append(stream)
        raise BlockingIOError()
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=4, LOCK_NB=8, flock=busy))
    assert remote["try_lock"](tmp_path) is None
    assert streams[0].closed
