"""Negative P2 gate cases: missing, mixed-revision or unsafe evidence, active drafts and changed history cannot pass.

P2 门禁反例：缺失、混版本或不安全的证据、生效的草案以及被改写的历史，都不能通过。
"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p2_release.py"))
SHA = "a" * 40
ZERO = dict.fromkeys(GATE["COUNTS"], 0)


def s1_receipt(**overrides) -> dict:
    results = [{"scenario": case, "seed": seed, "passed": True, "replay_agrees": True, "judge_exit_code": 0,
                "problems": [], "source_sha": SHA, "counts": dict(ZERO), "flown_versions": ["m-1-v1"]}
               for case, seeds in (("p2_s1_chain", (7, 19, 41)), ("p2_s1_cancel_in_flight", (7,)),
                                   ("p2_s1_restart", (7,))) for seed in seeds]
    value = {"status": "passed", "layer": "S1", "source_sha": SHA, "signer_key_id": "k",
             "certificate_fingerprints": {"ca": "1", "service": "2", "robot": "3"}, "results": results,
             "other_containers_before": {"count": 1}, "other_containers_after": {"count": 1}}
    value.update(overrides)
    return value


@pytest.fixture
def at_head(monkeypatch):
    """Read the suite from the working tree instead of a commit. / 从工作树读取场景集而不是某个提交。"""
    real = GATE["s1"].__globals__["subprocess"].check_output

    def check_output(argv, **kwargs):
        if argv[:2] == ["git", "show"] and argv[2].endswith(":configs/scenarios/p2_suite.yaml"):
            return (ROOT / "configs/scenarios/p2_suite.yaml").read_bytes()
        return real(argv, **kwargs)

    monkeypatch.setattr(GATE["s1"].__globals__["subprocess"], "check_output", check_output)


def test_s1_requires_every_case_on_this_revision_with_zero_counts(at_head):
    assert GATE["s1"]([], SHA)["status"] == "missing"
    assert GATE["s1"]([s1_receipt()], SHA)["status"] == "passed"
    assert GATE["s1"]([s1_receipt(source_sha="b" * 40)], SHA)["status"] == "failed"
    short = s1_receipt()
    short["results"] = short["results"][:-1]
    assert GATE["s1"]([short], SHA)["missing"] == [("p2_s1_restart", 7)]
    for count in GATE["COUNTS"]:
        unsafe = s1_receipt()
        unsafe["results"][0]["counts"][count] = 1
        assert GATE["s1"]([unsafe], SHA)["status"] == "failed", count
    assert GATE["s1"]([s1_receipt(other_containers_after={"count": 2})], SHA)["status"] == "failed"


def desk_receipts():
    activation = {"status": "verified", "source_sha": SHA,
                  "operations": {"catalog_id": "p1_s1_v1", "migration": {"status": "current"},
                                 "drill": {"status": "passed"}},
                  "workflows": {"catalog_id": "p2_s1_v1", "migration": {"status": "migrated"},
                                "drill": {"status": "passed"}},
                  "containers": {"desk-dock": {"docker_access": False}, "desk": {"docker_access": False}}}
    http = {"page": {"status": 200}, "script": {"matches_checkout": True},
            "health": {"ok": True, "console_source_sha": SHA, "service_source_sha": SHA}}
    probe = {"roles": ["admin", "approver", "operator", "reviewer"], "catalog": {"catalog_id": "p2_s1_v1"},
             "templates": [{"workflow_id": w, "version": 1, "triggers": triggers} for w, triggers in (
                 ("asset_check", [{"trigger_id": "manual", "kind": "manual", "state": None},
                                  {"trigger_id": "daily_0900", "kind": "schedule", "state": "disabled"}]),
                 ("campus_round", [{"trigger_id": "manual", "kind": "manual", "state": None}]),
                 ("asset_reinspection", [{"trigger_id": "reinspection", "kind": "internal", "state": None}]))],
             "draft": {"status": "planned", "active": False, "use": {"source": "live_model"}, "robots": ["uav_01"],
                       "nodes": [["inspect_1", "submit_mission"], ["report", "build_report"]]},
             "refused": [{"frame": f, "error": f"unknown type {f!r}"} for f in
                         ("inject", "docks.report", "arm", "flight", "simulator.inject", "workflow_activate",
                          "workflow_approve_all")]}
    return activation, http, probe


def test_the_desk_needs_both_catalogs_both_drills_an_inactive_draft_and_refused_frames():
    activation, http, probe = desk_receipts()
    assert GATE["desk"](None, http, probe, SHA)["status"] == "missing"
    assert GATE["desk"](activation, http, probe, SHA)["status"] == "passed"
    for mutate in (lambda a, h, p: a["workflows"].update(catalog_id="p1_s1_v1"),
                   lambda a, h, p: a["workflows"]["drill"].update(status="failed"),
                   lambda a, h, p: a["operations"]["migration"].update(status="refused"),
                   lambda a, h, p: h["health"].update(service_source_sha="b" * 40),
                   lambda a, h, p: p.update(roles=["operator", "approver"]),
                   lambda a, h, p: p["templates"].pop(),
                   lambda a, h, p: p["draft"].update(active=True),
                   lambda a, h, p: p["draft"].update(robots=["uav_h"]),
                   lambda a, h, p: p["refused"].pop(),
                   lambda a, h, p: p["refused"][0].update(error="ok")):
        values = copy.deepcopy(desk_receipts())
        mutate(*values)
        assert GATE["desk"](*values, SHA)["status"] == "failed"


def session_receipt():
    def mission(mission_id: str, epoch: int) -> tuple[dict, dict]:
        flight = {"version": 1, "status": "finished", "epoch": epoch, "started_at": "t0", "ended_at": "t1",
                  "manual_cleanup": False}
        value = {"status": "completed", "request": {"channel": "workflow", "requested_by": "workflow:wr-1"},
                 "binding": {"project_id": "campus_s1", "robot_id": "uav_01", "dock_id": "dock_s1"},
                 "dispatch": {"claims": [{"state": "claimed"}],
                              "reservations": [{"state": "released", "reason": "reconciled"}]},
                 "cloud": {"flights": [flight], "judge": {"passed": True, "replay_agrees": True,
                                                          "false_success_reports": 0, "problems": []}}}
        return value, {**flight, "mission_id": mission_id, "source_sha": SHA}

    first, first_flight = mission("m-000000000001", 3)
    second, second_flight = mission("m-000000000002", 4)
    runs = {"wr-1": {"run": {"state": "completed"}, "orders": [{"state": "reinspection_requested"}],
                     "reviews": [{"decision": "confirmed"}], "analyses": [{"source": "scripted"}]},
            "wr-2": {"run": {"state": "completed"}, "orders": [], "reviews": [], "analyses": [{"source": "scripted"}]}}
    receipt = {"root_run": "wr-1", "runs": runs, "errors": [],
               "missions": {"m-000000000001": first, "m-000000000002": second}}
    return receipt, [first_flight, second_flight]


def test_the_desk_session_must_reach_the_order_and_every_mission_must_be_bound_and_judged():
    receipt, flights = session_receipt()
    assert GATE["desk_session"](receipt, flights, SHA)["status"] == "passed"
    assert GATE["desk_session"](None, flights, SHA)["status"] == "missing"
    for mutate in (lambda r, f: r["runs"]["wr-1"]["run"].update(state="waiting"),
                   lambda r, f: r["runs"].pop("wr-2"),
                   lambda r, f: r["runs"]["wr-1"].update(orders=[]),
                   lambda r, f: r["runs"]["wr-1"].update(reviews=[{"decision": "dismissed"}]),
                   lambda r, f: r["runs"]["wr-2"]["analyses"][0].update(source="live_model"),
                   lambda r, f: r["missions"]["m-000000000001"]["request"].update(channel="console"),
                   lambda r, f: r["missions"]["m-000000000002"]["dispatch"]["claims"].clear(),
                   lambda r, f: r["missions"]["m-000000000002"]["cloud"]["judge"].update(passed=False),
                   lambda r, f: f[1].update(source_sha="b" * 40)):
        values = copy.deepcopy(session_receipt())
        mutate(*values)
        assert GATE["desk_session"](*values, SHA)["status"] == "failed"


def test_history_is_an_input_and_onboard_changes_require_m1(monkeypatch, tmp_path):
    # Checks images are git archives without .git; inject only the Git read boundary, not the gate decision.
    # 检查镜像来自 git archive，没有 .git；只替换 Git 读取边界，不替换门禁判定。
    paths = (GATE["M3_PATH"], GATE["P0_PATH"], GATE["P1_PATH"])
    records = {path: (ROOT / path).read_bytes() for path in paths}
    subprocess = GATE["historical"].__globals__["subprocess"]
    monkeypatch.setattr(subprocess, "check_output", lambda command, **_k: records[command[2].split(":", 1)[1]])
    assert GATE["historical"]()["status"] == "passed"
    for path in paths:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(records[path] + (b"\n" if path == GATE["P1_PATH"] else b""))
    monkeypatch.setitem(GATE["historical"].__globals__, "ROOT", tmp_path)
    assert GATE["historical"]()["status"] == "failed", "a rewritten P1 record never passes"

    scope = GATE["scope"]
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: type("Done", (), {"returncode": 0})())
    for changed, status in (("src/drone_agent/fleet/workflow.py\nconfigs/workflows/p2_s1_v1.yaml\nuv.lock", "passed"),
                            ("src/drone_agent/planner/workflow_draft.py\ndocs/roadmap.md", "passed"),
                            ("src/drone_agent/guardian/core.py", "missing"),
                            ("src/drone_agent/planner/engine.py", "failed"), ("Dockerfile.random", "failed")):
        monkeypatch.setitem(scope.__globals__, "git", lambda *args, value=changed: value)
        assert scope(GATE["P1_SHA"], [])["status"] == status, changed
