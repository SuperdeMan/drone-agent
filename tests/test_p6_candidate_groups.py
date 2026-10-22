"""Keep candidate existence separate from instance detail without erasing unknowns.

区分候选存在和实例细节，同时保留未知及原始证据。
"""
from __future__ import annotations

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
C = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/p6_candidate_groups.py")))
P = [{"proposal_id": "view-p01", "bbox": [.1, .1, .9, .9]}]


def answer(**changes):
    row = {"proposal_id": "view-p01", "entity_kind": "physical_object", "target_relation": "intersects",
           "localization_status": "entity_group", "entity_evidence": "QA visible material group",
           "relation_evidence": "QA visible material on selected road", "decision_uncertainty": "",
           "description_uncertainty": "QA material subtype and fragment endpoints unresolved"}
    row.update(changes)
    return {"schema_version": "0.2.0", "image_usable": True, "target_visible": True, "observations": [row]}


def result(value=None):
    value = answer() if value is None else value
    raw = json.dumps(value)
    parsed, issues = C.validate_answer(raw, "stop", P)
    return {"case_id": "view", "status": "answered", "model_used": "qa-model", "strict_replay": "passed",
            "finish": "stop", "parsed": parsed, "raw_content": raw, "schema_issues": issues}


@pytest.mark.parametrize("localization", ["single_entity", "entity_group", "unresolved"])
def test_visible_physical_overlap_does_not_require_material_certainty_or_instance_boxes(localization):
    small = [{"proposal_id": "view-p01", "bbox": [.200001, .200001, .200002, .200002]}]
    value = answer(localization_status=localization)
    parsed, issues = C.validate_answer(json.dumps(value), "stop", small)
    assert not issues and C.disposition(parsed["observations"][0], parsed) == "candidate"
    reduced = C.reduce_results([{"case_id": "view", "proposals": small}], [result(value)], "qa-model")
    assert reduced["rows"][0]["instance_bbox"] is None
    assert reduced["rows"][0]["source_bbox"] == small[0]["bbox"]
    assert reduced["rows"][0]["human_verdict"] is None


@pytest.mark.parametrize("changes,group", [
    ({"decision_uncertainty": "Material might be a flat appearance"}, "needs_review"),
    ({"entity_kind": "unknown", "decision_uncertainty": "Presence unresolved"}, "needs_review"),
    ({"target_relation": "uncertain", "decision_uncertainty": "Actual road edge unresolved"}, "needs_review"),
    ({"target_relation": "unknown", "decision_uncertainty": "Road not identifiable"}, "needs_review"),
    ({"entity_kind": "vehicle"}, "needs_review"),
    ({"entity_kind": "vehicle", "target_relation": "outside"}, "outside_target"),
    ({"target_relation": "outside"}, "outside_target"),
    ({"entity_kind": "shadow", "localization_status": "not_applicable"}, "non_entity"),
    ({"entity_kind": "surface_change", "localization_status": "not_applicable"}, "non_entity"),
])
def test_decision_uncertainty_vehicles_and_non_entities_keep_distinct_outcomes(changes, group):
    parsed, issues = C.validate_answer(json.dumps(answer(**changes)), "stop", P)
    assert not issues and C.disposition(parsed["observations"][0], parsed) == group


@pytest.mark.parametrize("changes,issue", [
    ({"entity_kind": "unknown"}, "decision_uncertainty_missing"),
    ({"target_relation": "uncertain"}, "decision_uncertainty_missing"),
    ({"refined_bbox": [.1, .1, .2, .2]}, "observation_fields"),
    ({"localization_status": "not_applicable"}, "physical_localization_status"),
    ({"localization_status": []}, "observation_enums"),
    ({"decision_uncertainty": False}, "evidence_text"),
    ({"entity_evidence": " "}, "evidence_text"),
    ({"proposal_id": "foreign"}, "proposal_identity"),
])
def test_unexplained_decisions_and_unrequested_geometry_are_rejected(changes, issue):
    assert issue in C.validate_answer(json.dumps(answer(**changes)), "stop", P)[1]


def test_quality_flags_cannot_be_turned_into_candidate_success():
    value = answer()
    value["target_visible"] = False
    assert "invisible_target_relation" in C.validate_answer(json.dumps(value), "stop", P)[1]
    value = answer(entity_kind="unknown", target_relation="unknown", localization_status="unresolved",
                   decision_uncertainty="Image unusable")
    value.update(image_usable=False, target_visible=False)
    parsed, issues = C.validate_answer(json.dumps(value), "stop", P)
    assert not issues and C.disposition(parsed["observations"][0], parsed) == "needs_review"
    value["target_visible"] = True
    assert "unusable_target_visible" in C.validate_answer(json.dumps(value), "stop", P)[1]


def test_repeated_keys_nonfinite_values_truncation_and_old_wire_are_rejected():
    raw = json.dumps(answer())
    duplicate = raw.replace('"image_usable": true', '"image_usable": true,"image_usable":true')
    assert "invalid_json" in C.validate_answer(duplicate, "stop", P)[1]
    assert "invalid_json" in C.validate_answer(raw.replace('"image_usable": true', '"image_usable": NaN'), "stop", P)[1]
    assert "finish_not_stop" in C.validate_answer(raw, "length", P)[1]
    for row in C.read(ROOT / "eval/s2/p6_candidates_v8/attempts/20261015T040711Z/cloud/output/results.json"):
        proposals = next(r["proposals"] for r in C.read(ROOT / "eval/s2/p6_candidates_v8/input-manifest.json") if r["case_id"] == row["case_id"])
        assert C.validate_answer(row["raw_content"], row["finish"], proposals)[1]


def test_one_bad_item_still_quarantines_the_entire_case_without_losing_proposals():
    proposals = [*copy.deepcopy(P), {"proposal_id": "view-p02", "bbox": [.1, .1, .9, .9]}]
    value = answer()
    value["observations"].append(dict(value["observations"][0], proposal_id="view-p02", target_relation="uncertain"))
    raw = json.dumps(value)
    parsed, issues = C.validate_answer(raw, "stop", proposals)
    response = dict(result(), raw_content=raw, parsed=parsed, schema_issues=issues)
    reduced = C.reduce_results([{"case_id": "view", "proposals": proposals}], [response], "qa-model")
    assert reduced["model_disposition_counts"] == {"needs_review": 2}
    assert all(r["observation"] is None for r in reduced["rows"])
    assert reduced["cases"][0]["issues"] == ["decision_uncertainty_missing"]


def test_missing_duplicate_and_unverified_responses_cannot_form_observations():
    manifest = [{"case_id": "view", "proposals": P}]
    for responses in [[], [dict(result(), model_used="other")], [dict(result(), parsed={})], [dict(result(), schema_issues=["foreign"])]]:
        reduced = C.reduce_results(manifest, responses, "qa-model")
        assert reduced["model_disposition_counts"] == {"needs_review": 1}
        assert reduced["rows"][0]["observation"] is None
    with pytest.raises(ValueError, match="duplicate or foreign"):
        C.reduce_results(manifest, [result(), result()], "qa-model")
    value = answer()
    value["observations"] *= 2
    assert "proposal_coverage" in C.validate_answer(json.dumps(value), "stop", P)[1]


def test_excluding_given_windows_never_means_a_clear_scene_or_completed_review():
    reduced = C.reduce_results([{"case_id": "view", "proposals": P}],
                               [result(answer(entity_kind="shadow", localization_status="not_applicable"))], "qa-model")
    assert reduced["model_disposition_counts"] == {"non_entity": 1}
    assert reduced["scene_verdict"] is None and reduced["human_reviews_completed"] == 0
    assert not reduced["accuracy_computed"] and not reduced["recall_computed"] and not reduced["formal_release_eligible"]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value) + "\n").encode())


@pytest.fixture
def previous(tmp_path):
    previous, inputs = tmp_path / "v8", tmp_path / "images"
    inputs.mkdir()
    images = []
    for name in ["original.jpg", "support.jpg"]:
        (inputs / name).write_bytes(b"QA bytes; never submitted to a provider")
        images.append({"file": name, "sha256": C.digest(inputs / name)})
    config = {"model": "qa-model", "prompt_version": "road-candidate-review-dev-v1", "instruction": "OLD_NOT_FOR_MODEL",
              "system_prompt": "QA", "formal_release_eligible": False, "human_ground_truth_available": False, "calls_per_case": 1}
    write(previous / "config.json", config)
    write(previous / "input-manifest.json", [{"case_id": "view", "images": images, "proposals": P}])
    write(previous / "plan.json", {"config_sha256": C.digest(previous / "config.json"),
        "manifest_sha256": C.digest(previous / "input-manifest.json"), "parents": [], "source_results_sha256": "qa-source",
        "source_batch_environment_qualified": False, "expected_runtime": {"runtime_source_sha": "qa-runtime"}})
    config.update(prompt_version=C.VERSION, instruction="NEW_QA")
    write(tmp_path / "new.json", config)
    return previous, inputs, tmp_path / "new.json", tmp_path / "prepared"


def test_preparation_preserves_input_bytes_without_reading_old_answers(previous):
    plan = C.prepare(*previous)
    assert not plan["source_batch_environment_qualified"] and not plan["formal_release_eligible"]
    assert (previous[3] / "inputs/manifest.json").read_bytes() == (previous[0] / "input-manifest.json").read_bytes()
    manifest = C.read(previous[3] / "inputs/manifest.json")
    messages = C.make_messages(C.read(previous[2]), manifest[0], previous[3] / "inputs")
    assert "OLD_NOT_FOR_MODEL" not in json.dumps(messages) and "NEW_QA" in json.dumps(messages)
    assert (previous[3] / "inputs/original.jpg").read_bytes() == (previous[1] / "original.jpg").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        C.prepare(*previous)


def test_preparation_rejects_model_and_source_changes(previous):
    config = C.read(previous[2])
    write(previous[2], dict(config, model="other"))
    with pytest.raises(ValueError, match="prompt"):
        C.prepare(*previous)
    write(previous[2], config)
    (previous[1] / "original.jpg").write_bytes(b"different")
    with pytest.raises(ValueError, match="binding"):
        C.prepare(*previous)
    with pytest.raises(ValueError, match="binding"):
        C.input_file(previous[1], {"file": "../new.json", "sha256": C.digest(previous[2])})


def test_container_alias_loads_new_contract_without_modifying_legacy_helpers(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    cloud = runpy.run_path(str(ROOT / "scripts/p6_candidate_groups_cloud.py"))
    for mounted, source in cloud["SOURCE_MAPPING"].items():
        (tmp_path / mounted).write_bytes((ROOT / source).read_bytes())
    code = "import p6_candidate_review as c; print(c.VERSION); print(c.disposition({'entity_kind':'physical_object','target_relation':'intersects','decision_uncertainty':''},{'image_usable':True,'target_visible':True}))"
    output = subprocess.check_output([sys.executable, "-c", code], cwd=tmp_path).decode().splitlines()
    assert output == [C.VERSION, "candidate"]
    assert (tmp_path / "p6_candidate_runner.py").read_bytes() == (ROOT / "scripts/p6_candidate_runner.py").read_bytes()
