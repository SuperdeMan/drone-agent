"""Negative P5 gate cases: S1 receipts of another revision or with nonzero counts, a desk without the P5 catalogs or the
switch drill, a main scenario that was not closed in the page, a soak that is not the frozen full plan, and a restore
drill that touched the live ledger cannot pass. Git reads are injected, so the cases also hold in the cloud checks
image without a repository.

P5 门禁反例：其他版本或计数非零的 S1 回执、没有 P5 目录或切换演练的任务台、未在页面中关单的主场景、不是冻结完整计划的长稳，
以及触碰了在用账本的恢复演练，都不能通过。Git 读取以注入方式提供，因此这些用例在没有仓库的云端检查镜像中同样成立。
"""

from __future__ import annotations

import copy
import hashlib
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p5_release.py"))
SHA = "a" * 40
PLAN = (ROOT / "configs/soak/p5_soak_v1.yaml").read_bytes()


@pytest.fixture
def repository(monkeypatch):
    files = {"configs/scenarios/p5_suite.yaml": (ROOT / "configs/scenarios/p5_suite.yaml").read_bytes(),
             "configs/soak/p5_soak_v1.yaml": PLAN}
    for name in ("s1", "soak"):
        monkeypatch.setitem(GATE[name].__globals__, "show", lambda sha, path: files[path])
    return files


def s1_receipt(**overrides) -> dict:
    from drone_agent.eval.judge_p5 import COUNTS

    cases = (("p5_s1_road", (7, 19, 41)), ("p5_s1_road_not_cleared", (7,)))
    results = [{"scenario": case, "seed": seed, "passed": True, "replay_agrees": True, "judge_exit_code": 0,
                "problems": [], "counts": dict.fromkeys(COUNTS, 0), "flown_versions": ["m-1-v1", "m-2-v1"]}
               for case, seeds in cases for seed in seeds]
    value = {"status": "passed", "layer": "S1", "source_sha": SHA, "signer_key_id": "k",
             "certificate_fingerprints": {"ca": "1", "service": "2", "robot": "3"}, "results": results,
             "other_containers_before": {"count": 1}, "other_containers_after": {"count": 1}}
    value.update(overrides)
    return value


def test_s1_needs_every_case_on_this_candidate_with_zero_counts(repository):
    assert GATE["s1"]([s1_receipt()], SHA)["status"] == "passed"
    assert GATE["s1"]([], SHA)["status"] == "missing"
    assert GATE["s1"]([s1_receipt(source_sha="b" * 40)], SHA)["status"] == "failed"
    unsafe = s1_receipt()
    unsafe["results"][0]["counts"]["false_closure"] = 1
    assert GATE["s1"]([unsafe], SHA)["status"] == "failed"
    short = s1_receipt()
    short["results"].pop()
    assert GATE["s1"]([short], SHA)["missing"] == [("p5_s1_road_not_cleared", 7)]


def activation() -> dict:
    parts = {name: {"catalog_id": "p5_desk_v1", "migration": {"status": "current"}, "drill": {"status": "passed"}}
             for name in ("operations", "workflows", "scheduling", "business")}
    parts["business"]["vision"] = "live:minimax/MiniMax-M3"
    return {"status": "verified", "source_sha": SHA, **parts, "switch_drill": {"status": "passed"},
            "execution_backends": ["px4_sitl", "logical_sim", "vendor_protocol_sim"], "vendor": {"docks": ["dock_vd"]},
            "dock_sessions": dict.fromkeys(("dock_s1", "dock_fa", "dock_fb", "dock_vd"), "active"),
            "containers": {name: {"docker_access": False} for name in ("desk", "desk-service", "desk-dock", "desk-fleet",
                                                                       "desk-vendor", "desk-uplink",
                                                                       "desk-model-proxy")}}


def http() -> dict:
    return {"page": {"status": 200}, "script": {"matches_checkout": True}, "fixed_page": {"status": 200},
            "fixed_script": {"matches_checkout": True},
            "health": {"ok": True, "console_source_sha": SHA, "service_source_sha": SHA}}


def browser() -> dict:
    views = {ws: {"rendered": True, "overflow_px": 0} for ws in GATE["WORKSPACES"]}
    return {"tours": {"light-1440x900": {"offered": sorted(GATE["WORKSPACES"]), "views": views}},
            "fixed": {"light": {"status": 200, "steps": 8}}, "console_errors": []}


def test_the_desk_needs_the_p5_catalogs_the_switch_drill_every_workspace_and_no_forged_identity():
    spoof = {"forged_identity_used": False}
    assert GATE["desk"](activation(), http(), spoof, browser(), SHA)["status"] == "passed"
    assert GATE["desk"](None, http(), spoof, browser(), SHA)["status"] == "missing"
    for change in (lambda a, b, s: a["workflows"].update(catalog_id="p4_s1_v1"),
                   lambda a, b, s: a.update(switch_drill={"status": "failed"}),
                   lambda a, b, s: a["dock_sessions"].update(dock_vd="lost"),
                   lambda a, b, s: a["containers"].pop("desk-vendor"),
                   lambda a, b, s: b["tours"]["light-1440x900"]["offered"].remove("audit"),
                   lambda a, b, s: b["console_errors"].append("x"),
                   lambda a, b, s: s.update(forged_identity_used=True)):
        a, b, s = activation(), browser(), dict(spoof)
        change(a, b, s)
        assert GATE["desk"](a, http(), s, b, SHA)["status"] == "failed"


def scenario() -> tuple[dict, list[dict]]:
    receipt = {"hello": {"identity_scheme": "tailnet"}, "console_errors": [], "loop": {
        "world": [{"state": "damaged", "ok": True}, {"state": "normal", "ok": True}],
        "decided": {"state": "confirmed", "order": "ord-1"}, "round_reviewed_in_page": True,
        "order": {"state": "closed", "rounds": [{"round": 1, "state": "passed"}]}, "finding_after": "resolved",
        "approved": ["m-000000000001", "m-000000000002"],
        "audit": [{"action": a, "actor_scheme": "tailnet"} for a in GATE["CHAIN"]]}}
    flights = [{"mission_id": "m-000000000001", "version": 1, "source_sha": SHA, "status": "finished",
                "world": {"placed": [{"asset_id": "asset_red", "ok": True}]}},
               {"mission_id": "m-000000000002", "version": 1, "source_sha": SHA, "status": "finished",
                "world": {"placed": []}}]
    return receipt, flights


def test_the_main_scenario_must_close_in_the_page_with_the_person_in_the_audit_trail():
    receipt, flights = scenario()
    assert GATE["main_scenario"](receipt, flights, SHA)["status"] == "passed"
    for change in (lambda r: r["loop"].update(round_reviewed_in_page=False),
                   lambda r: r["loop"]["order"].update(state="reinspection_failed"),
                   lambda r: r["loop"].update(world=[{"state": "damaged", "ok": True}]),
                   lambda r: r["loop"].update(audit=[{"action": "mission.approved", "actor_scheme": "harness"}]),
                   lambda r: r["loop"].update(finding_after="confirmed")):
        bad = copy.deepcopy(receipt)
        change(bad)
        assert GATE["main_scenario"](bad, flights, SHA)["status"] == "failed"
    assert GATE["main_scenario"](receipt, [dict(f, source_sha="b" * 40) for f in flights], SHA)["status"] == "failed"


def soak_result(**overrides) -> dict:
    criteria = {name: {"status": "passed"} for name in ("duration", "schedules", "occurrences", "runs", "safety",
                                                        "recovery", "resources", "layers")}
    criteria["duration"]["hours"] = 72.01
    value = {"passed": True, "source_sha": SHA, "plan_sha256": hashlib.sha256(PLAN).hexdigest(),
             "counts": {"false_closure": 0}, "criteria": criteria, "soak_id": "soak-x", "t0": "t", "end": "e"}
    value.update(overrides)
    return value


def test_the_soak_must_be_the_frozen_full_plan_on_this_candidate(repository):
    assert GATE["soak"](soak_result(), SHA)["status"] == "passed"
    assert GATE["soak"](None, SHA)["status"] == "missing"
    assert GATE["soak"](soak_result(source_sha="b" * 40), SHA)["status"] == "failed"
    assert GATE["soak"](soak_result(plan_sha256="0" * 64), SHA)["status"] == "failed"
    assert GATE["soak"](soak_result(counts={"false_closure": 1}), SHA)["status"] == "failed"
    short = soak_result()
    short["criteria"]["duration"]["hours"] = 30
    assert GATE["soak"](short, SHA)["status"] == "failed"


def test_a_restore_drill_must_read_the_same_backup_without_touching_the_live_ledger():
    taken = {"source_sha": SHA, "backup_id": "b1", "ledger": {"integrity": "ok", "tables": {"missions": 3}},
             "media_files": 2}
    drill = {"status": "passed", "backup_id": "b1", "live_ledger_touched": False}
    assert GATE["backup"](taken, drill, SHA)["status"] == "passed"
    assert GATE["backup"](taken, dict(drill, live_ledger_touched=True), SHA)["status"] == "failed"
    assert GATE["backup"](taken, dict(drill, backup_id="b0"), SHA)["status"] == "failed"
    assert GATE["backup"](None, drill, SHA)["status"] == "missing"
