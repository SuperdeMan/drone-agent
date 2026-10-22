"""Only unanswered attempts may be retried, and the gate must see every attempt (D090).

只有无回答的尝试可以重试，门禁必须看到每一次尝试（D090）。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
C = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/p6_road_s2.py")))
SOURCES = ("p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py", "p6_raster_replay.py", "p6_oam_capture.py",
           "p6_road_s2_runner.py", "p6_road_s2_remote.py", "p6_candidate_remote.py")


def answer():
    return json.dumps({"schema_version": "1.0.0", "image_usable": True, "target_present": True, "boundary_certain": True,
                       "candidate_present": False, "defect_types": [], "confidence": .9, "reasons": [], "summary": "QA only"})


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    rows, tasks = [], []
    for i in range(2):
        path = source / f"{i}.png"
        Image.new("RGB", (80, 60), (20 + i * 30, 90, 60)).save(path)
        rows.append({"sample_id": f"qa_{i}", "image_sha256": C.digest(path), "image_file": path.name, "width": 80, "height": 60,
                     "split": "development", "evaluation_role": "primary", "asset_id": f"qa_asset_{i}"})
        tasks.append({"sample_id": f"qa_{i}", "image_sha256": C.digest(path), "target_brief": "Synthetic registered road",
                      "target_polygon": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]], "registered_at": "2026-10-15T00:00:00Z",
                      "registration_reference": "QA only", "origin": "development_draft"})
    C.write(source / "manifest.json", {"format": "drone.p6-road-corpus/v1", "corpus_id": "qa", "samples": rows})
    C.write(source / "tasks.json", {"format": "drone.p6-road-model-tasks/v1", "corpus_id": "qa", "rows": tasks})
    C.write(source / "profile.json", C.read(ROOT / "eval/s2/road_business_v1/road-profile-draft.json"))
    output = tmp_path / "package"
    C.prepare(source / "manifest.json", source / "tasks.json", source / "profile.json", source, output, "development")
    return output


class FlakyProvider:
    """Fail the first call of chosen observations without answering. / 指定观察的首次调用不回答而失败。"""

    def __init__(self, failing: dict[str, int]):
        self.failing, self.current = dict(failing), None

    async def complete(self, messages, model, temperature, max_tokens, **kwargs):
        if self.failing.get(self.current, 0):
            self.failing[self.current] -= 1
            error = RuntimeError("synthetic upstream 429")
            error.status_code, error.retry_after = 429, 0
            raise error
        return answer(), model, "stop", (10, 10)


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = runpy.run_path(str(ROOT / "scripts/p6_road_s2_runner.py"))

    async def no_wait(delay, *args, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    return module


def run(runner, package, output, failing, *, source="scripted", context=None):
    provider = FlakyProvider(failing)
    original = C.make_messages

    def tracking(profile, sample, folder):
        provider.current = sample["sample_id"]
        return original(profile, sample, folder)

    runner["record_run"].__globals__["make_messages"] = tracking
    try:
        return asyncio.run(runner["record_run"](package, output, provider, context or {
            "provider_id": "synthetic_qa", "runtime_source_sha": "0" * 40}, source=source))
    finally:
        runner["record_run"].__globals__["make_messages"] = original


def test_unanswered_attempt_is_retried_and_counted(runner, package, tmp_path):
    header = run(runner, package, tmp_path / "run", {"qa_0": 1})
    assert header["status"] == "completed" and header["responses_failed"] == 0
    assert header["responses_recorded"] == 2 and header["attempts_started"] == 3 and header["failed_attempts"] == 1
    assert header["retry_policy"] == C.RETRY_POLICY
    result = C.read(tmp_path / "run/qa_0.result.json")
    assert result["status"] == "answered" and result["attempt"] == 2
    assert result["attempts"] == [{"attempt": 1, "error_type": "RuntimeError", "http_status": 429, "retry_after_s": 0,
                                   "answer_recorded": False, "duration_s": result["attempts"][0]["duration_s"]}]
    assert (tmp_path / "run/qa_0.started.json").is_file() and (tmp_path / "run/qa_0.retry-2.started.json").is_file()
    assert not (tmp_path / "run/qa_1.retry-2.started.json").exists()


def test_observation_without_any_answer_stays_failed(runner, package, tmp_path):
    header = run(runner, package, tmp_path / "run", {"qa_0": 9, "qa_1": 9})
    assert header["responses_recorded"] == 0 and header["responses_failed"] == 2
    assert header["attempts_started"] == 2 * C.RETRY_POLICY["max_attempts_per_observation"] == header["failed_attempts"]
    assert not any((tmp_path / "run/recordings").iterdir())


def test_recorded_answer_is_never_asked_again(runner, package, tmp_path, monkeypatch):
    replay = importlib.import_module("drone_agent.providers.replay")

    class Disagreeing(replay.ReplayProvider):
        async def complete(self, *args, **kwargs):
            content, used, finish, usage = await super().complete(*args, **kwargs)
            return content + " ", used, finish, usage

    monkeypatch.setattr(replay, "ReplayProvider", Disagreeing)
    header = run(runner, package, tmp_path / "run", {})
    result = C.read(tmp_path / "run/qa_0.result.json")
    assert result["status"] == "recorded_but_invalid" and len(result["attempts"]) == 1
    assert result["attempts"][0]["answer_recorded"] is True and header["attempts_started"] == 2
    assert header["responses_failed"] == 2 and not (tmp_path / "run/qa_0.retry-2.started.json").exists()


def live_folder(gate, package, tmp_path, runner, failing):
    cloud, candidate = tmp_path / "cloud", "c" * 40
    cloud.mkdir()
    header = run(runner, package, cloud / "output/batch", failing, source="recorded", context={
        "provider_id": "minimax-vl", "endpoint_host": "api.minimaxi.com", "runtime_source_sha": gate.RUNTIME,
        "eval_source_commit": candidate})
    envelope = {"eval_source_commit": candidate, "data_plan_sha256": C.digest(package / "plan.json"), "runtime_image": gate.IMAGE,
                "runtime_source_sha": gate.RUNTIME, "run_id": "p6-road-s2-20261020T000000Z",
                "source_sha256": {name: C.digest(ROOT / "scripts" / name) for name in SOURCES}}
    (cloud / "execution-plan.json").write_text(json.dumps(envelope), encoding="utf-8")
    execution = {"format": "drone.p6-road-cloud-execution/v1", "status": "completed", "run_id": envelope["run_id"],
                 "project_lock_acquired": True, "container_started": True, "lock_held_through_checks": True,
                 "resident_container_identities_unchanged": True, "before": [{"name": "synthetic"}], "after": [{"name": "synthetic"}],
                 "container_exit_code": 0, "runtime_source_sha": gate.RUNTIME, "runtime_image": gate.IMAGE,
                 "eval_source_commit": candidate, "execution_plan_sha256": C.digest(cloud / "execution-plan.json"),
                 "data_plan_sha256": C.digest(package / "plan.json"), "attempts_started": header["attempts_started"],
                 "responses_recorded": header["responses_recorded"]}
    (cloud / "execution.json").write_text(json.dumps(execution), encoding="utf-8")
    return cloud, candidate


def test_gate_accepts_retried_unanswered_attempt_with_full_accounting(runner, package, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    gate = importlib.import_module("verify_p6_road_business")
    cloud, candidate = live_folder(gate, package, tmp_path, runner, {"qa_1": 2})
    predictions = asyncio.run(gate.live_evidence(package, cloud, candidate))
    assert predictions["live_environment_verified"] and predictions["strict_replay_complete"]
    assert predictions["recorded_resources"]["responses"] == 2


@pytest.mark.parametrize("tamper", ["hidden_attempt", "count", "answered_twice"])
def test_gate_rejects_unaccounted_attempts(runner, package, tmp_path, monkeypatch, tamper):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    gate = importlib.import_module("verify_p6_road_business")
    cloud, candidate = live_folder(gate, package, tmp_path, runner, {"qa_1": 1})
    folder = cloud / "output/batch"
    if tamper == "hidden_attempt":
        result = C.read(folder / "qa_1.result.json")
        result.update(attempt=1, attempts=[])
        (folder / "qa_1.result.json").write_text(json.dumps(result), encoding="utf-8")
    elif tamper == "count":
        header = C.read(folder / "run.json")
        header["failed_attempts"] = 0
        (folder / "run.json").write_text(json.dumps(header), encoding="utf-8")
    else:
        result = C.read(folder / "qa_1.result.json")
        result["attempts"][0]["answer_recorded"] = True
        (folder / "qa_1.result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError):
        asyncio.run(gate.live_evidence(package, cloud, candidate))
