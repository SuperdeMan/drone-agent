"""Negative P4 gate cases: missing, mixed-revision or unsafe evidence, an S2 run that is not the frozen protocol's single
test, a desk without the business catalog or a desk reinspection that closes an order cannot pass. Git reads are
injected, so the cases also hold in the cloud checks image without a repository.

P4 门禁反例：缺失、混版本或不安全的证据，不是冻结协议唯一一次测试的 S2 运行，没有业务目录的任务台，或关了单的任务台复检，都不能
通过。Git 读取以注入方式提供，因此这些用例在没有仓库的云端检查镜像中同样成立。
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
GATE = runpy.run_path(str(ROOT / "scripts/verify_p4_release.py"))
SHA = "a" * 40
ZERO = dict.fromkeys(GATE["COUNTS"], 0)
CASES = (("p4_s1_close", (7, 19, 41)), ("p4_s1_not_repaired", (7,)), ("p4_s1_dedup", (7,)))
PROFILE = yaml.safe_dump({"threshold": 0.4, "profile_id": "vlm_change"}).encode()
QUALITY = b"quality"
MANIFEST = b'{"samples": []}'
QUERIES = b"queries"
PROTOCOL = yaml.safe_dump({"manifest_sha256": hashlib.sha256(MANIFEST).hexdigest()}).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def repository(monkeypatch):
    """The candidate's files and the profile's freeze commit without a repository. / 无仓库时的候选文件与画像冻结提交。"""
    files = {GATE["PROFILE"]: PROFILE, GATE["QUALITY"]: QUALITY, GATE["MANIFEST"]: MANIFEST,
             GATE["PROTOCOL"]: PROTOCOL, GATE["QUERIES"]: QUERIES,
             "configs/scenarios/p4_suite.yaml": (ROOT / "configs/scenarios/p4_suite.yaml").read_bytes()}
    names = GATE["s2"].__globals__
    monkeypatch.setitem(names, "show", lambda sha, path: files[path])
    monkeypatch.setitem(names, "committed_at", lambda path, sha, predicate: (
        "c" * 40, datetime.fromisoformat("2026-09-27T12:00:00+08:00")))
    return files


def s1_receipt(**overrides) -> dict:
    results = [{"scenario": case, "seed": seed, "layer": "S1", "passed": True, "replay_agrees": True,
                "judge_exit_code": 0, "problems": [], "source_sha": SHA, "counts": dict(ZERO), "orders": 1,
                "flown_versions": ["m-1-v1", "m-2-v1"]} for case, seeds in CASES for seed in seeds]
    value = {"status": "passed", "layer": "S1", "source_sha": SHA, "signer_key_id": "k",
             "certificate_fingerprints": {"ca": "1", "service": "2", "robot": "3"}, "results": results,
             "other_containers_before": {"count": 1}, "other_containers_after": {"count": 1}}
    value.update(overrides)
    return value


def s2_receipts() -> dict:
    manifest = sha256(MANIFEST)
    usage = {"latency_p50_ms": 900.0, "latency_p95_ms": 2000.0, "input_tokens": 10, "output_tokens": 5,
             "cost": {"amount": 0.1, "currency": "USD", "source": "list"}}
    live = {"split": "test", "mode": "live", "deployment_id": "20260927T100000Z-11111111",
            "artifact_directory": "/x/p4-20260927T100000Z-22222222", "source_sha": "b" * 40,
            "run": {"profile_sha256": sha256(PROFILE), "quality_sha256": sha256(QUALITY), "manifest_sha256": manifest,
                    "threshold": 0.4, "source": "live_model", "started_at": "2026-09-27T06:00:00Z"},
            "metrics": {"status": "passed", "counted": True, "minimum_met": True, "usage": usage}}
    return {
        "calibration": {"split": "calibration", "mode": "live", "calibration": {"chosen": {"threshold": 0.4}},
                        "run": {"manifest_sha256": manifest}},
        "live": [live],
        "replay": {"source_sha": SHA, "mode": "replay", "replay_of": "20260927T100000Z-11111111/p4-20260927T100000Z-"
                   "22222222", "replay": {"status": "passed"}, "metrics": {"counted": False}},
        "scripted": {"mode": "scripted", "metrics": {"counted": False}, "run": {"source": "scripted"}},
        "retrieval": {"source_sha": SHA, "status": "completed",
                      "retrieval": {"query_set_sha256": sha256(QUERIES), "manifest_sha256": manifest,
                                    "threshold": None, "text_to_image": {"map_at_10": 0.3},
                                    "image_to_reference": {"top1": 0.9}}}}


def s2(receipts: dict) -> dict:
    return GATE["s2"](receipts["calibration"], receipts["live"], receipts["replay"], receipts["scripted"],
                      receipts["retrieval"], SHA)


def test_s1_requires_every_case_on_this_revision_with_zero_counts(repository):
    assert GATE["s1"]([], SHA)["status"] == "missing"
    assert GATE["s1"]([s1_receipt()], SHA)["status"] == "passed"
    assert GATE["s1"]([s1_receipt(source_sha="b" * 40)], SHA)["status"] == "failed"
    short = s1_receipt()
    short["results"] = short["results"][:-1]
    assert GATE["s1"]([short], SHA)["missing"] == [("p4_s1_dedup", 7)]
    for count in GATE["COUNTS"]:
        unsafe = s1_receipt()
        unsafe["results"][0]["counts"][count] = 1
        assert GATE["s1"]([unsafe], SHA)["status"] == "failed", count
    assert GATE["s1"]([s1_receipt(other_containers_after={"count": 2})], SHA)["status"] == "failed"


def test_s2_passes_only_the_frozen_protocols_single_live_test_reproduced_here(repository):
    assert s2(s2_receipts())["status"] == "passed"
    missing = s2_receipts()
    missing["replay"] = None
    assert s2(missing)["status"] == "missing"
    other_tau = s2_receipts()
    other_tau["calibration"]["calibration"]["chosen"]["threshold"] = 0.3
    assert "profile_tau_not_the_calibration_choice" in s2(other_tau)["problems"]
    twice = s2_receipts()
    twice["live"].append(copy.deepcopy(twice["live"][0]))
    assert s2(twice)["status"] == "failed", "a second live test of the same profile must be disclosed and fails"
    early = s2_receipts()
    early["live"][0]["run"]["started_at"] = "2026-09-27T03:00:00Z"
    assert "profile_not_committed_before_the_live_test_run" in s2(early)["problems"]
    weak = s2_receipts()
    weak["live"][0]["metrics"]["status"] = "failed"
    assert "frozen_thresholds_not_met_or_too_few_samples" in s2(weak)["problems"]
    free = s2_receipts()
    free["live"][0]["metrics"]["usage"]["cost"] = None
    assert "latency_tokens_or_cost_incomplete" in s2(free)["problems"]
    elsewhere = s2_receipts()
    elsewhere["replay"]["source_sha"] = "b" * 40
    assert "no_replay_on_this_candidate_reproducing_the_live_run" in s2(elsewhere)["problems"]
    counted = s2_receipts()
    counted["scripted"]["metrics"]["counted"] = True
    assert "scripted_pipeline_run_missing_or_counted" in s2(counted)["problems"]
    gated = s2_receipts()
    gated["retrieval"]["retrieval"]["threshold"] = 0.5
    assert "retrieval_report_missing_or_on_other_inputs" in s2(gated)["problems"]


def desk_inputs():
    parts = {"operations": "p1_s1_v1", "workflows": "p4_s1_v1", "scheduling": "p3_desk_v1", "business": "p4_s1_v1"}
    activation = {"status": "verified", "source_sha": SHA,
                  **{name: {"catalog_id": catalog, "migration": {"status": "migrated"}, "drill": {"status": "passed"}}
                     for name, catalog in parts.items()},
                  "containers": {"desk-dock": {"docker_access": False}}}
    activation["business"]["vision"] = "live:minimax-vl/MiniMax-M3"
    http = {"page": {"status": 200}, "script": {"matches_checkout": True},
            "health": {"ok": True, "console_source_sha": SHA, "service_source_sha": SHA}}
    probe = {"project": "campus_s1", "projects": [{"project_id": "campus_s1", "business": True}],
             "panel": {"catalog": {"catalog_id": "p4_s1_v1"}, "roles": ["admin", "approver", "operator", "reviewer"]},
             "refused": [{"frame": f"f{i}", "error": "unknown type 'x'"} for i in range(8)],
             "unknown_subject": {"code": "service.not_found"}}
    return activation, http, probe


def test_the_desk_needs_all_four_catalogs_a_live_vision_role_and_every_refusal():
    activation, http, probe = desk_inputs()
    assert GATE["desk"](activation, http, probe, SHA)["status"] == "passed"
    assert GATE["desk"](None, http, probe, SHA)["status"] == "missing"
    for change in ({"business": {"catalog_id": "p4_s1_v1", "migration": {"status": "migrated"},
                                 "drill": {"status": "passed"}, "vision": "unavailable: no key"}},
                   {"workflows": {"catalog_id": "p2_s1_v1", "migration": {"status": "current"},
                                  "drill": {"status": "passed"}}}):
        assert GATE["desk"]({**activation, **change}, http, probe, SHA)["status"] == "failed", change
    few = copy.deepcopy(probe)
    few["refused"] = few["refused"][:5]
    assert GATE["desk"](activation, http, few, SHA)["status"] == "failed"
    leaked = copy.deepcopy(probe)
    leaked["unknown_subject"] = {"code": "auth.project_denied"}
    assert GATE["desk"](activation, http, leaked, SHA)["status"] == "failed"


def session(order_state: str = "reinspection_unknown", round_state: str = "unknown") -> tuple[dict, list[dict]]:
    flight = {"version": 1, "status": "finished", "epoch": 1, "started_at": "t0", "ended_at": "t1",
              "manual_cleanup": False}
    judge = {"passed": True, "replay_agrees": True, "false_success_reports": 0, "problems": []}
    missions = {m: {"status": "completed", "binding": {"project_id": "campus_s1", "robot_id": "uav_01",
                                                       "dock_id": "dock_s1"}, "request": {"channel": "workflow"},
                    "cloud": {"flights": [flight], "judge": judge}} for m in ("m-1", "m-2")}
    receipt = {"errors": [], "reference": {"registered": ["m-0"], "error": None, "references": 3}, "root_run": "wf-1",
               "findings": {"fd-1": {"review": {"decision": "confirmed", "reviewer": "tailnet:<redacted>"},
                                     "jobs": [{"source": "scripted"}]}},
               "jobs": [{"source": "scripted", "evidence_id": "capture:1", "state": "completed"},
                        {"source": "live_model", "evidence_id": "capture:1", "state": "refused",
                         "reasons": ["analysis.undeterminable", "model.blurred"]}],
               "runs": {"wf-1": {"run": {"state": "completed"}, "nodes": []},
                        "wf-2": {"run": {"state": "completed"}, "nodes": []}},
               "orders": {"wo-1": {"order": {"state": order_state},
                                   "rounds": [{"round": 1, "state": round_state, "reinspection_run": "wf-2"}]}},
               "sent": [{"action": "order_repair"}], "missions": missions}
    records = [{"mission_id": m, "source_sha": SHA, **flight} for m in missions]
    return receipt, records


def test_the_desk_session_may_never_close_an_order_and_needs_a_person_and_a_reference():
    receipt, records = session()
    assert GATE["desk_session"](receipt, records, SHA)["status"] == "passed"
    closed, records = session("closed", "passed")
    assert "the_desk_reinspection_closed_an_order" in GATE["desk_session"](closed, records, SHA)["problems"]
    unreferenced, records = session()
    unreferenced["reference"] = None
    assert GATE["desk_session"](unreferenced, records, SHA)["status"] == "failed"
    machine, records = session()
    machine["findings"]["fd-1"]["review"]["reviewer"] = "harness:p4-reviewer"
    assert GATE["desk_session"](machine, records, SHA)["status"] == "failed"
    offline, records = session()
    offline["jobs"][1]["evidence_id"] = "capture:2"
    assert "no_live_model_analysis_of_the_scripted_capture" in GATE["desk_session"](offline, records, SHA)["problems"]
    assert GATE["desk_session"](None, None, SHA)["status"] == "missing"


def test_scope_allows_only_appended_evaluation_stages_in_the_m3_image_file(monkeypatch):
    before = "FROM a AS checks\nFROM b AS aircraft3\n"
    names = GATE["scope"].__globals__
    monkeypatch.setitem(names, "subprocess", type("S", (), {"run": staticmethod(
        lambda *a, **k: type("R", (), {"returncode": 0})()), "CalledProcessError": Exception}))
    monkeypatch.setitem(names, "git", lambda *args: "sim/m3.Dockerfile\nsrc/drone_agent/fleet/business.py")
    for after, verdict in ((before + "FROM x AS retrievalqueries\nFROM y AS retrieval\n", "passed"),
                           (before.replace("aircraft3", "aircraft4"), "failed"),
                           (before + "FROM x AS other\n", "failed")):
        monkeypatch.setitem(names, "show", lambda sha, path, a=after: (before if sha == GATE["P3_SHA"] else a)
                            .encode())
        assert GATE["scope"](SHA, [])["status"] == verdict, after
    monkeypatch.setitem(names, "git", lambda *args: "src/drone_agent/planner/llm.py")
    monkeypatch.setitem(names, "show", lambda sha, path: before.encode())
    assert GATE["scope"](SHA, [])["outside_p4_scope"] == ["src/drone_agent/planner/llm.py"]


def test_the_gate_reads_receipts_written_by_the_runners():
    # The keys the gate reads are the keys remote_p4 writes. / 门禁读取的键即 remote_p4 写出的键。
    runner = (ROOT / "scripts/remote_p4.py").read_text(encoding="utf-8")
    for key in ("replay_of", "artifact_directory", "deployment_id", '"run"', '"metrics"', '"calibration"',
                '"retrieval"', "certificate_fingerprints", "other_containers_after"):
        assert key in runner, key
    assert json.loads(json.dumps(s2_receipts()))  # receipts are plain JSON / 回执是纯 JSON
