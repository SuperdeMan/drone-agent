"""Negative cases of the P4 S2 re-verification gate (D076 §6): an undisclosed earlier use, a profile frozen after the
test or changed beyond its threshold, a missing pre-registration, an unpriced run without a declared basis or a
tampered P4 record cannot pass. Git reads are injected, so the cases also hold in the cloud checks image.

P4 S2 复验门禁的反例（D076 §6）：未披露的早先使用、测试之后才冻结或在阈值之外被改动的画像、缺少预登记、既无价格也无声明口径的
运行，或被改动的 P4 记录，都不能通过。Git 读取以注入方式提供，因此这些用例在云端检查镜像中同样成立。
"""

from __future__ import annotations

import copy
import hashlib
import json
import runpy
from datetime import datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p4_s2_release.py"))
SHA, CALIBRATED_AT = "a" * 40, "c" * 40
UNFROZEN = yaml.safe_dump({"profile_id": "vlm_change", "version": 6, "model": "MiniMax-M3.1-Flash-Preview",
                           "reasoning_effort": "max", "threshold": None}).encode()
FROZEN = yaml.safe_dump({"profile_id": "vlm_change", "version": 6, "model": "MiniMax-M3.1-Flash-Preview",
                         "reasoning_effort": "max", "threshold": 0.95,
                         "calibration": {"run": "x", "precision": "0.95"}}).encode()
QUALITY, MANIFEST, QUERIES = b"quality", b'{"samples": []}', b"queries"
PROTOCOL = yaml.safe_dump({"manifest_sha256": hashlib.sha256(MANIFEST).hexdigest()}).encode()
FIRST = {"split": "test", "mode": "live", "deployment_id": "20260927T162924Z-3d418f0e",
         "artifact_directory": "/x/p4-20260927T163456Z-0a79c6d3", "run": {"profile_sha256": "e" * 64}}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def repository(monkeypatch):
    """The candidate's files and commit times without a repository. / 无仓库时的候选文件与提交时间。"""
    files = {(SHA, GATE["PROFILE"]): FROZEN, (CALIBRATED_AT, GATE["PROFILE"]): UNFROZEN,
             (SHA, GATE["QUALITY"]): QUALITY, (SHA, GATE["MANIFEST"]): MANIFEST, (SHA, GATE["PROTOCOL"]): PROTOCOL,
             (SHA, GATE["QUERIES"]): QUERIES, (GATE["P4_CLOSE"], GATE["FIRST_USE"]): json.dumps(FIRST).encode()}
    times = {GATE["PROFILE"]: "2026-10-10T08:23:21+08:00", GATE["DECISIONS"]: "2026-10-10T12:28:34+08:00"}
    names = GATE["s2"].__globals__
    monkeypatch.setitem(names, "show", lambda sha, path: files[(sha, path)])
    monkeypatch.setitem(names, "committed_at", lambda path, sha, predicate: (
        "d" * 40, datetime.fromisoformat(times[path])))
    return files, times


def receipts() -> dict:
    manifest = sha256(MANIFEST)
    usage = {"latency_p50_ms": 14000.0, "latency_p95_ms": 80000.0, "input_tokens": 10, "output_tokens": 5,
             "cost": None}
    live = {"split": "test", "mode": "live", "deployment_id": "20261010T003416Z-2bae1853",
            "artifact_directory": "/x/p4-20261010T042855Z-d2fd02a6",
            "run": {"profile_sha256": sha256(FROZEN), "quality_sha256": sha256(QUALITY), "manifest_sha256": manifest,
                    "threshold": 0.95, "source": "live_model", "started_at": "2026-10-10T04:28:55Z"},
            "metrics": {"status": "passed", "counted": True, "minimum_met": True, "usage": usage}}
    return {
        "calibration": {"split": "calibration", "mode": "live", "calibration": {"chosen": {"threshold": 0.95}},
                        "run": {"manifest_sha256": manifest, "profile": GATE["PROFILE"],
                                "profile_sha256": sha256(UNFROZEN), "software_revision": CALIBRATED_AT}},
        "live": [copy.deepcopy(FIRST), live],
        "replay": {"source_sha": SHA, "mode": "replay",
                   "replay_of": "20261010T003416Z-2bae1853/p4-20261010T042855Z-d2fd02a6",
                   "replay": {"status": "passed"}, "metrics": {"counted": False}},
        "scripted": {"mode": "scripted", "metrics": {"counted": False},
                     "run": {"source": "scripted", "profile": GATE["PROFILE"]}},
        "retrieval": {"source_sha": SHA, "status": "completed",
                      "retrieval": {"query_set_sha256": sha256(QUERIES), "manifest_sha256": manifest,
                                    "threshold": None, "text_to_image": {"map_at_10": 0.2},
                                    "image_to_reference": {"top1": 0.99}}}}


def s2(value: dict, basis: str = "Token Plan subscription; no per-token price published") -> dict:
    return GATE["s2"](value["calibration"], value["live"], value["replay"], value["scripted"], value["retrieval"], SHA,
                      basis)


def test_the_reverified_s2_passes_with_the_first_use_disclosed_and_a_declared_cost_basis(repository):
    result = s2(receipts())
    assert result["status"] == "passed", result["problems"]
    assert result["test_split_uses"] == 2 and result["usage"]["cost"] == {
        "basis": "Token Plan subscription; no per-token price published"}
    assert s2(receipts(), basis="")["problems"] == ["latency_tokens_or_cost_incomplete"]


def test_an_undisclosed_first_use_or_a_second_run_of_the_profile_fails(repository):
    hidden = receipts()
    hidden["live"] = hidden["live"][1:]
    assert "earlier_use_of_the_test_split_not_disclosed" in s2(hidden)["problems"]
    twice = receipts()
    twice["live"].append(copy.deepcopy(twice["live"][1]))
    assert any(p.startswith("expected exactly one live test run") for p in s2(twice)["problems"])


def test_the_freeze_and_the_preregistration_must_precede_the_test(repository):
    files, times = repository
    early = receipts()
    early["live"][1]["run"]["started_at"] = "2026-10-10T04:28:00Z"  # 12:28:00 +08:00, before the pre-registration
    assert s2(early)["problems"] == ["preregistration_not_committed_before_the_live_test_run"]
    times[GATE["PROFILE"]] = "2026-10-10T13:00:00+08:00"
    assert "profile_not_committed_before_the_live_test_run" in s2(receipts())["problems"]


def test_the_frozen_profile_may_only_add_its_threshold_to_the_calibrated_bytes(repository):
    files, _ = repository
    changed = yaml.safe_load(FROZEN)
    changed["reasoning_effort"] = "high"
    files[(SHA, GATE["PROFILE"])] = yaml.safe_dump(changed).encode()
    value = receipts()
    value["live"][1]["run"]["profile_sha256"] = sha256(files[(SHA, GATE["PROFILE"])])
    assert "frozen_profile_is_not_the_calibrated_one" in s2(value)["problems"]
    other_tau = receipts()
    other_tau["calibration"]["calibration"]["chosen"]["threshold"] = 0.9
    assert "profile_tau_not_the_calibration_choice" in s2(other_tau)["problems"]


def test_failed_metrics_a_foreign_replay_or_a_counted_scripted_run_fail(repository):
    weak = receipts()
    weak["live"][1]["metrics"]["status"] = "failed"
    assert "frozen_thresholds_not_met_or_too_few_samples" in s2(weak)["problems"]
    elsewhere = receipts()
    elsewhere["replay"]["source_sha"] = "b" * 40
    assert "no_replay_on_this_candidate_reproducing_the_live_run" in s2(elsewhere)["problems"]
    counted = receipts()
    counted["scripted"]["metrics"]["counted"] = True
    assert "scripted_pipeline_run_missing_or_counted" in s2(counted)["problems"]
    assert GATE["s2"](None, [], None, None, None, SHA)["status"] == "missing"


def test_the_p4_record_must_be_unchanged_and_open_only_on_s2(monkeypatch):
    record = json.loads((ROOT / GATE["P4_RECORD"]).read_bytes())
    names = GATE["p4_record"].__globals__

    def with_record(value: dict):
        raw = json.dumps(value).encode()
        monkeypatch.setitem(names, "show", lambda sha, path: raw)
        monkeypatch.setitem(names, "ROOT", type("R", (), {"__truediv__": lambda self, path: type(
            "P", (), {"read_bytes": lambda self: raw})()})())
        return GATE["p4_record"]()

    assert with_record(record)["status"] == "passed"
    assert len(with_record(record)["reused_criteria"]) == 14
    reopened = copy.deepcopy(record)
    reopened["criteria"]["desk"]["status"] = "failed"
    assert "s2_is_not_the_only_open_criterion" in with_record(reopened)["problems"]
    other = copy.deepcopy(record)
    other["source_sha"] = "b" * 40
    assert "not_the_p4_candidate_record" in with_record(other)["problems"]
