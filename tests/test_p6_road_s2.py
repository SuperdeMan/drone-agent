"""Road inference must not read labels, hide refusals or relabel scripted recordings as live.

道路推理不得读取标签、隐藏拒判或将脚本录制改称实调。
"""
from __future__ import annotations

import asyncio
import copy
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
C = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/p6_road_s2.py")))


def answer(**changes):
    return {"schema_version": "1.0.0", "image_usable": True, "target_present": True, "boundary_certain": True,
            "candidate_present": False, "defect_types": [], "confidence": .9, "reasons": [], "summary": "QA only", **changes}


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    rows, tasks = [], []
    for i in range(2):
        path = source / f"{i}.png"
        Image.new("RGB", (80, 60), (20 + i * 30, 90, 60)).save(path)
        rows.append({"sample_id": f"qa_{i}", "image_sha256": C.digest(path), "image_file": path.name, "width": 80, "height": 60,
                     "split": "development", "evaluation_role": "primary", "asset_id": f"qa_asset_{i}",
                     "business_label": "SECRET_QA_LABEL_MUST_NOT_ENTER_REQUEST"})
        tasks.append({"sample_id": f"qa_{i}", "image_sha256": C.digest(path), "target_brief": "Synthetic registered road",
                      "target_polygon": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]], "registered_at": "2026-10-15T00:00:00Z",
                      "registration_reference": "QA only", "origin": "qa_fixture"})
    C.write(source / "manifest.json", {"format": "drone.p6-road-corpus/v1", "corpus_id": "qa", "samples": rows})
    C.write(source / "tasks.json", {"format": "drone.p6-road-model-tasks/v1", "corpus_id": "qa", "rows": tasks})
    profile = C.read(ROOT / "eval/s2/road_business_v1/road-profile-draft.json")
    C.write(source / "profile.json", profile)
    output = tmp_path / "package"
    C.prepare(source / "manifest.json", source / "tasks.json", source / "profile.json", source, output, "development")
    return output, source


def test_complete_task_requests_do_not_contain_truth_or_normal_reference_claims(package):
    output, _ = package
    profile, inputs, plan = C.verify_package(output)
    request = C.make_messages(profile, inputs["rows"][0], output)
    assert "SECRET_QA_LABEL" not in json.dumps(request)
    assert set(inputs["rows"][0]) == {"sample_id", "image_sha256", "target_brief", "task_sha256", "task_origin", "images"}
    assert len(request[1]["content"]) == 3
    assert not plan["truth_used_in_model_messages"] and not plan["normal_reference_claimed"]


@pytest.mark.parametrize("changes", [
    {"image_usable": False}, {"target_present": False}, {"boundary_certain": False}, {"candidate_present": 1},
    {"candidate_present": True, "defect_types": []}, {"candidate_present": None, "reasons": []},
    {"confidence": True}, {"confidence": float("nan")}, {"defect_types": ["made_up"]},
    {"reasons": ["entity.uncertain"]}, {"summary": ""},
])
def test_bad_or_contradictory_answers_are_refusals(package, changes):
    profile, inputs, _ = C.verify_package(package[0])
    result = C.prediction(inputs["rows"][0], json.dumps(answer(**changes)), profile["model"], "stop", profile)
    assert result["verdict"] == "refused" and result["reasons"]


def test_duplicate_json_keys_and_unfinished_answer_are_not_salvaged(package):
    profile, inputs, _ = C.verify_package(package[0])
    text = json.dumps(answer()).replace('"confidence": 0.9', '"confidence": 0.9, "confidence": 0.8')
    assert C.prediction(inputs["rows"][0], text, profile["model"], "stop", profile)["verdict"] == "refused"
    assert C.prediction(inputs["rows"][0], json.dumps(answer()), profile["model"], "length", profile)["verdict"] == "refused"


def test_explicit_uncertainty_and_low_confidence_remain_refusals(package):
    profile, inputs, _ = C.verify_package(package[0])
    uncertain = answer(candidate_present=None, reasons=["vehicle.context_missing"])
    row = C.prediction(inputs["rows"][0], json.dumps(uncertain), profile["model"], "stop", profile)
    assert row["reasons"] == ["vehicle.context_missing"]
    low = C.prediction(inputs["rows"][0], json.dumps(answer(confidence=.49)), profile["model"], "stop", profile)
    assert low["reasons"] == ["model.low_confidence"]
    positive = C.prediction(inputs["rows"][0], json.dumps(answer(candidate_present=True, defect_types=["other_entity"])), profile["model"], "stop", profile)
    assert positive["verdict"] == "suspected" and positive["defect_types"] == ["other_entity"]


def test_task_missing_for_a_refusal_class_cannot_shrink_the_batch(package, tmp_path):
    _, source = package
    tasks = C.read(source / "tasks.json")
    tasks["rows"].pop()
    C.write(source / "incomplete-tasks.json", tasks)
    with pytest.raises(ValueError, match="exactly once"):
        C.prepare(source / "manifest.json", source / "incomplete-tasks.json", source / "profile.json", source, tmp_path / "missing", "development")
    assert not (tmp_path / "missing").exists()


def test_business_truth_fields_are_rejected_in_model_task_file(package, tmp_path):
    _, source = package
    tasks = C.read(source / "tasks.json")
    tasks["rows"][0]["verdict"] = "normal"
    C.write(source / "leaking-tasks.json", tasks)
    with pytest.raises(ValueError, match="registered"):
        C.prepare(source / "manifest.json", source / "leaking-tasks.json", source / "profile.json", source, tmp_path / "leak", "development")


def test_formal_preparation_never_proceeds_without_actual_data_admission(package, tmp_path):
    _, source = package
    manifest = C.read(source / "manifest.json")
    for row in manifest["samples"]:
        row["split"] = "test"
    C.write(source / "formal-manifest.json", manifest)
    with pytest.raises(ValueError, match="requires labels"):
        C.prepare(source / "formal-manifest.json", source / "tasks.json", source / "profile.json", source, tmp_path / "test", "test")
    assert not (tmp_path / "test").exists()


@pytest.mark.parametrize("item", ["profile.json", "image", "source_binding"])
def test_changed_input_or_missing_source_binding_is_rejected(package, item):
    output, _ = package
    if item == "profile.json":
        (output / item).write_bytes((output / item).read_bytes() + b" ")
    elif item == "image":
        image = C.read(output / "inputs.json")["rows"][0]["images"][0]["file"]
        (output / image).write_bytes(b"changed")
    else:
        plan = C.read(output / "plan.json")
        plan["source_sha256"] = {}
        (output / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError):
        C.verify_package(output)


def test_scripted_roundtrip_is_never_reported_as_live(package, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    runner = runpy.run_path(str(ROOT / "scripts/p6_road_s2_runner.py"))

    class FakeProvider:
        async def complete(self, messages, model, temperature, max_tokens, **kwargs):
            return json.dumps(answer()), model, "stop", (10, 10)

    output = tmp_path / "run"
    header = asyncio.run(runner["record_run"](package[0], output, FakeProvider(), {"provider_id": "synthetic_qa", "runtime_source_sha": "0" * 40}, source="scripted"))
    assert header["responses_recorded"] == 2 and header["source"] == "scripted"
    result = asyncio.run(C.replay(package[0], output / "recordings"))
    assert result["strict_replay_complete"] and result["recording_origins"] == ["scripted"]
    assert not result["live_environment_verified"] and not result["formal_release_eligible"] and result["new_provider_calls"] == 0
    with pytest.raises(FileExistsError):
        asyncio.run(runner["record_run"](package[0], output, FakeProvider(), {"provider_id": "synthetic_qa", "runtime_source_sha": "0" * 40}, source="scripted"))


def test_missing_recordings_stay_in_all_denominators(package, tmp_path):
    result = asyncio.run(C.replay(package[0], tmp_path / "no-recordings"))
    assert len(result["rows"]) == 2 and not result["strict_replay_complete"]
    assert all(r["verdict"] == "refused" and r["reasons"] == ["execution.recording_missing"] for r in result["rows"])


def test_source_image_edits_fail_before_preparing_a_package(package, tmp_path):
    _, source = package
    (source / "0.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="source image changed"):
        C.prepare(source / "manifest.json", source / "tasks.json", source / "profile.json", source, tmp_path / "changed", "development")
    assert not (tmp_path / "changed").exists()


def test_actual_profile_draft_remains_unselected():
    profile = copy.deepcopy(C.read(ROOT / "eval/s2/road_business_v1/road-profile-draft.json"))
    assert profile["status"] == "draft" and profile["calibration_record_sha256"] is None


@pytest.mark.parametrize("approved", [True, False])
def test_delegated_tasks_require_scope_approval_and_do_not_send_review_notes(package, tmp_path, monkeypatch, approved):
    _, source = package
    manifest, tasks = C.read(source / "manifest.json"), C.read(source / "tasks.json")
    labels = []
    for sample, task in zip(manifest["samples"], tasks["rows"], strict=True):
        sample["split"] = "calibration"
        sample["target"] = {"polygon": task["target_polygon"], "brief": task["target_brief"]}
        task["origin"] = "delegated_ai_task"
        labels.append({"sample_id": sample["sample_id"], "recorded_at": "2026-10-15T00:01:00Z", "notes": "SECRET_REVIEW_NOT_FOR_MODEL"})
    protocol = {"annotation_policy": {"mode": "independent_ai_verified_reference", "approval_reference": "SYNTHETIC" if approved else "",
        "human_adjudication_required": False, "delegated_ai_adjudication_approved": True, "evaluated_model_outputs_must_be_unseen": True}}
    for name, value in [("ai-manifest.json", manifest), ("ai-tasks.json", tasks), ("labels.json", {"rows": labels}),
                         ("protocol.json", protocol), ("exposure.json", {})]:
        C.write(source / name, value)
    monkeypatch.setitem(C.METRICS, "audit", lambda *args: {"data_ready": True, "ai_assisted_labels_declared": 0})
    arguments = (source / "ai-manifest.json", source / "ai-tasks.json", source / "profile.json", source, tmp_path / "delegated", "calibration")
    keywords = {"labels_file": source / "labels.json", "protocol_file": source / "protocol.json", "exposure_file": source / "exposure.json"}
    if not approved:
        with pytest.raises(ValueError, match="formal task registration"):
            C.prepare(*arguments, **keywords)
        return
    C.prepare(*arguments, **keywords)
    profile, inputs, plan = C.verify_package(tmp_path / "delegated")
    assert plan["delegated_ai_reference"] is True
    messages = C.make_messages(profile, inputs["rows"][0], tmp_path / "delegated")
    assert "SECRET_REVIEW" not in json.dumps(messages)


@pytest.mark.parametrize("changed", ["polygon", "brief"])
def test_formal_model_task_cannot_use_a_different_labeled_target(package, tmp_path, monkeypatch, changed):
    _, source = package
    manifest, tasks = C.read(source / "manifest.json"), C.read(source / "tasks.json")
    labels = []
    for sample, task in zip(manifest["samples"], tasks["rows"], strict=True):
        sample["split"] = "calibration"
        sample["target"] = {"polygon": copy.deepcopy(task["target_polygon"]), "brief": task["target_brief"]}
        task["origin"] = "human_task"
        labels.append({"sample_id": sample["sample_id"], "recorded_at": "2026-10-15T00:01:00Z"})
    if changed == "polygon":
        manifest["samples"][0]["target"]["polygon"][0][0] = .2
    else:
        manifest["samples"][0]["target"]["brief"] = "A different road segment"
    for name, value in [("formal-manifest.json", manifest), ("formal-tasks.json", tasks), ("labels.json", {"rows": labels}), ("protocol.json", {}), ("exposure.json", {})]:
        C.write(source / name, value)
    monkeypatch.setitem(C.METRICS, "audit", lambda *args: {"data_ready": True, "ai_assisted_labels_declared": 0})
    with pytest.raises(ValueError, match="differs from the labeled target"):
        C.prepare(source / "formal-manifest.json", source / "formal-tasks.json", source / "profile.json", source,
                  tmp_path / "mismatched", "calibration", labels_file=source / "labels.json", protocol_file=source / "protocol.json", exposure_file=source / "exposure.json")
    assert not (tmp_path / "mismatched").exists()


def test_orientation_cannot_silently_change_model_coordinates(package, tmp_path):
    _, source = package
    with Image.open(source / "0.png") as image:
        picture = image.copy()
    exif = Image.Exif()
    exif[274] = 3
    picture.save(source / "0.png", exif=exif)
    manifest, tasks = C.read(source / "manifest.json"), C.read(source / "tasks.json")
    manifest["samples"][0]["image_sha256"] = tasks["rows"][0]["image_sha256"] = C.digest(source / "0.png")
    C.write(source / "oriented-manifest.json", manifest)
    C.write(source / "oriented-tasks.json", tasks)
    with pytest.raises(ValueError, match="orientation"):
        C.prepare(source / "oriented-manifest.json", source / "oriented-tasks.json", source / "profile.json", source, tmp_path / "oriented", "development")
