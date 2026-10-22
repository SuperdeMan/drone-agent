"""No-size-threshold probes preserve inputs and never manufacture evaluation truth.

无大小门槛探针保持输入来源，且不制造评测真值。
"""
from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from drone_agent.providers.replay import Recording

TOOL = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_road_contact.py")))
MODEL = "MiniMax-M3.1-Flash-Preview"


def result(verdict="normal", ident="full"):
    answer = {"image_usable": True, "target_visible": True, "verdict": verdict,
              "obstacles": [{"type": "debris", "bbox": [.2, .3, .4, .5]}] if verdict == "suspected" else [],
              "reason": "QA fixture only", "uncertainty": "QA fixture uncertainty" if verdict == "unknown" else ""}
    return {"case_id": ident, "status": "answered", "model_used": MODEL, "finish": "stop",
            "strict_replay": "passed", "schema_issues": [], "raw_content": json.dumps(answer), "parsed": answer}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def previous(tmp_path):
    prev, inputs = tmp_path / "previous", tmp_path / "images"
    prev.mkdir()
    inputs.mkdir()
    for name, color in [("original.jpg", "gray"), ("outline.jpg", "cyan")]:
        Image.new("RGB", (100, 80), color).save(inputs / name)
    images = [{"file": name, "sha256": TOOL.digest(inputs / name)} for name in ("original.jpg", "outline.jpg")]
    config = {"prompt_version": "road-region-views-dev-v2", "model": MODEL, "system_prompt": "QA system",
              "instruction": "QA old instruction", "temperature": 0, "max_tokens": 8192, "thinking": True,
              "reasoning_effort": "medium", "timeout_s": 180, "calls_per_case": 1,
              "formal_release_eligible": False, "human_ground_truth_available": False}
    write(prev / "config.json", config)
    write(prev / "input-manifest.json", [{"case_id": "tile", "images": images}, {"case_id": "full", "images": images}])
    write(prev / "diagnostic/results.json", [result(ident="tile"), result()])
    runtime = {"runtime_source_sha": "qa-runtime", "runner_sha256": "qa-runner", "provider": "qa-provider", "endpoint_host": "qa.invalid"}
    write(prev / "diagnostic/run.json", dict(runtime, model_requested=MODEL))
    parent = {"case_id": "target", "source_image_sha256": "qa-original", "source_target_sha256": "qa-support",
              "source_size": [100, 80], "registered_asset_boundary": False,
              "views": [{"case_id": "full", "arm": "full", "crop_px": [0, 0, 100, 80]},
                        {"case_id": "tile", "arm": "tiles", "crop_px": [0, 0, 60, 48]}]}
    write(prev / "plan.json", {"config_sha256": TOOL.digest(prev / "config.json"),
                              "manifest_sha256": TOOL.digest(prev / "input-manifest.json"), "source_index_sha256": "qa-index",
                              "model": MODEL, "parents": [parent]})
    config.update(prompt_version=TOOL.VERSION, instruction="QA changed policy instruction")
    write(tmp_path / "new.json", config)
    return prev, inputs, tmp_path / "new.json", tmp_path / "prepared"


def test_full_views_and_image_bytes_are_retained_without_tiles(previous):
    prev, inputs, config, output = previous
    plan = TOOL.prepare(*previous)
    manifest = TOOL.read(output / "inputs/manifest.json")
    assert [r["case_id"] for r in manifest] == ["full"]
    assert (output / "inputs/original.jpg").read_bytes() == (inputs / "original.jpg").read_bytes()
    assert plan["parents"][0]["historical_verdict"] == "normal"
    assert plan["planned_calls"] == 1 and plan["minimum_size_threshold"] is None
    assert not plan["human_ground_truth_available"] and not plan["formal_release_eligible"]


@pytest.mark.parametrize("field,value", [("model", "other"), ("temperature", .8), ("system_prompt", "other"),
                                        ("formal_release_eligible", True), ("calls_per_case", 2)])
def test_preparation_rejects_confounding_configuration_changes(previous, field, value):
    config = TOOL.read(previous[2])
    config[field] = value
    write(previous[2], config)
    with pytest.raises(ValueError, match="prompt"):
        TOOL.prepare(*previous)
    assert not previous[3].exists()


def test_changed_image_bytes_and_changed_old_manifest_rejected(previous):
    (previous[1] / "original.jpg").write_bytes(b"corrupted image")
    with pytest.raises(ValueError, match="digest"):
        TOOL.prepare(*previous)
    (previous[0] / "input-manifest.json").write_text("[]")
    with pytest.raises(ValueError, match="frozen inputs"):
        TOOL.prepare(*previous)


def test_a_partial_crop_cannot_claim_the_full_view(previous):
    plan = TOOL.read(previous[0] / "plan.json")
    plan["parents"][0]["views"][0]["crop_px"] = [0, 0, 60, 48]
    write(previous[0] / "plan.json", plan)
    with pytest.raises(ValueError, match="whole-image"):
        TOOL.prepare(*previous)


def test_duplicate_targets_and_path_escape_rejected(previous):
    plan = TOOL.read(previous[0] / "plan.json")
    plan["parents"].append(copy.deepcopy(plan["parents"][0]))
    write(previous[0] / "plan.json", plan)
    with pytest.raises(ValueError, match="duplicate"):
        TOOL.prepare(*previous)
    with pytest.raises(ValueError, match="relative"):
        TOOL.bound_file(previous[1], "../new.json", TOOL.digest(previous[2]))


def test_existing_package_is_not_overwritten(previous):
    TOOL.prepare(*previous)
    with pytest.raises(ValueError, match="already exists"):
        TOOL.prepare(*previous)


def test_missing_failed_and_invalid_results_stay_unknown():
    plan = {"model": MODEL, "parents": [{"case_id": "target", "view_id": "full", "historical_verdict": "normal"}]}
    for results in [[], [{"case_id": "full", "status": "failed"}], [dict(result(), model_used="other")]]:
        summary = TOOL.reduce_results(plan, results)
        assert summary["rows"][0]["new_verdict"] == "unknown"
        assert summary["invalid_or_missing_views"] == ["full"]
        assert summary["valid_response_transitions"] == {}
        assert not summary["accuracy_computed"]


def test_valid_historical_transition_is_not_a_score():
    plan = {"model": MODEL, "parents": [{"case_id": "target", "view_id": "full", "historical_verdict": "normal"}]}
    summary = TOOL.reduce_results(plan, [result("suspected")])
    assert summary["valid_response_transitions"] == {"normal->suspected": 1}
    assert summary["rows"][0]["independent_human_verdict"] is None
    assert summary["human_reviews_completed"] == 0 and not summary["causal_improvement_estimated"]


@pytest.mark.parametrize("mode", ["duplicate", "nonfinite", "no_reason", "no_uncertainty"])
def test_unexplained_or_ambiguous_answers_are_invalid(mode):
    r = result("unknown")
    if mode == "duplicate":
        r["raw_content"] = r["raw_content"].replace('"verdict": "unknown"', '"verdict":"normal","verdict":"unknown"')
    elif mode == "nonfinite":
        r["raw_content"] = r["raw_content"].replace('"image_usable": true', '"image_usable": NaN')
    else:
        r["parsed"]["reason" if mode == "no_reason" else "uncertainty"] = " "
        r["raw_content"] = json.dumps(r["parsed"])
    assert TOOL.checked_answer(r, MODEL) is None


def test_duplicate_and_foreign_results_cannot_be_counted():
    plan = {"model": MODEL, "parents": [{"case_id": "target", "view_id": "full", "historical_verdict": "normal"}]}
    for results in [[result(), result()], [result(ident="other")]]:
        with pytest.raises(ValueError, match="duplicate or foreign"):
            TOOL.reduce_results(plan, results)


async def test_scripted_records_cannot_be_reported_as_live(previous):
    plan = TOOL.prepare(*previous)
    cloud = previous[3] / "cloud"
    r = result()
    recording = Recording(source="scripted", provider_id="qa-provider", model=MODEL)
    recording.save(cloud / "recordings/full.json")
    r.update(recording_sha256=TOOL.digest(cloud / "recordings/full.json"), usage=[1, 1])
    write(cloud / "results.json", [r])
    write(cloud / "run.json", {"source": "live_model_development_probe", "prompt_version": TOOL.VERSION,
                              "model_requested": MODEL, "config_sha256": plan["config_sha256"],
                              "manifest_sha256": plan["manifest_sha256"], **plan["expected_runtime"]})
    with pytest.raises(ValueError, match="recording provenance"):
        await TOOL.replay_and_summarize(previous[3], cloud)


def cloud_launcher(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    return SimpleNamespace(**runpy.run_path(str(root / "scripts/p6_road_contact_cloud.py")))


def test_launch_intent_prevents_an_automatic_second_batch(tmp_path, monkeypatch):
    tool = cloud_launcher(monkeypatch)
    (tmp_path / "precall.json").write_text("{}")
    with pytest.raises(ValueError, match="already has launch intent"):
        tool.launch(tmp_path, tmp_path / "missing-runner", "unused")


def test_wrong_freeze_stops_before_writing_intent(tmp_path, monkeypatch):
    tool = cloud_launcher(monkeypatch)
    monkeypatch.setattr(tool.subprocess, "check_output", lambda *a, **kw: b"different-head")
    with pytest.raises(ValueError, match="HEAD"):
        tool.launch(tmp_path, tmp_path / "missing-runner", "declared-head")
    assert not (tmp_path / "precall.json").exists()
