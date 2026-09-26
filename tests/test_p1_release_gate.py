"""Negative P1 gate cases: missing, mixed-revision or unsafe evidence and changed history cannot pass.

P1 门禁反例：缺失、混版本或不安全的证据，以及被改写的历史，都不能通过。
"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p1_release.py"))
SHA = "a" * 40
ZERO = dict.fromkeys(GATE["COUNTS"], 0)


def s1_receipt(**overrides) -> dict:
    results = [{"scenario": case, "seed": seed, "passed": True, "replay_agrees": True, "judge_exit_code": 0,
                "problems": [], "source_sha": SHA, "counts": dict(ZERO), "flown_versions": [1]}
               for case, seeds in (("p1_s1_nominal", (7, 19, 41)), ("p1_s1_blocked_before_claim", (7,)),
                                   ("p1_s1_restart", (7,))) for seed in seeds]
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
        if argv[:2] == ["git", "show"] and argv[2].endswith(":configs/scenarios/p1_suite.yaml"):
            return (ROOT / "configs/scenarios/p1_suite.yaml").read_bytes()
        return real(argv, **kwargs)

    monkeypatch.setattr(GATE["s1"].__globals__["subprocess"], "check_output", check_output)


def test_s1_requires_every_case_on_this_revision_with_zero_counts(at_head):
    assert GATE["s1"]([], SHA)["status"] == "missing"
    assert GATE["s1"]([s1_receipt()], SHA)["status"] == "passed"
    other = s1_receipt(source_sha="b" * 40)
    assert GATE["s1"]([other], SHA)["status"] == "failed"
    short = s1_receipt()
    short["results"] = short["results"][:-1]
    assert GATE["s1"]([short], SHA)["missing"] == [("p1_s1_restart", 7)]
    unsafe = s1_receipt()
    unsafe["results"][0]["counts"]["wrong_release"] = 1
    assert GATE["s1"]([unsafe], SHA)["status"] == "failed"
    shared = s1_receipt(other_containers_after={"count": 2})
    assert GATE["s1"]([shared], SHA)["status"] == "failed"


def desk_receipts():
    activation = {"status": "verified", "source_sha": SHA,
                  "operations": {"catalog_id": "p1_s1_v1", "migration": {"status": "migrated"},
                                 "drill": {"status": "passed"}},
                  "containers": {"desk-dock": {"docker_access": False}, "desk": {"docker_access": False}}}
    http = {"page": {"status": 200}, "script": {"matches_checkout": True},
            "health": {"ok": True, "console_source_sha": SHA, "service_source_sha": SHA}}
    probe = {"projects": [{"project_id": "campus_s1", "roles": ["admin", "approver", "operator"],
                           "robots": ["uav_01"], "legacy": False}],
             "resources": {"campus_s1": {
                 "docks": [{"dock_id": "dock_s1", "source": "logical_sim", "pad": "free",
                            "status": {"age_s": 0.4, "session": "active", "link": "online", "fresh": True}}],
                 "robots": [{"robot_id": "uav_01", "eligibility": {"verdict": "blocked",
                                                                   "reasons": ["dock.lid_not_open"]}}]}},
             "refused": [{"frame": f, "error": f"unknown type {f!r}"} for f in
                         ("inject", "docks.report", "arm", "flight", "simulator.inject")],
             "script": {"control_or_injection_frames": []}}
    return activation, http, probe


def test_the_desk_needs_the_catalog_migration_resource_entry_and_refused_frames():
    activation, http, probe = desk_receipts()
    assert GATE["desk"](None, http, probe, SHA)["status"] == "missing"
    assert GATE["desk"](activation, http, probe, SHA)["status"] == "passed"
    for mutate in (lambda a, h, p: a["operations"].update(catalog_id="m2"),
                   lambda a, h, p: a["operations"]["drill"].update(status="failed"),
                   lambda a, h, p: a["containers"].pop("desk-dock"),
                   lambda a, h, p: h["health"].update(service_source_sha="b" * 40),
                   lambda a, h, p: p["projects"][0].update(roles=["viewer"]),
                   lambda a, h, p: p["resources"]["campus_s1"]["docks"][0]["status"].update(age_s=None),
                   lambda a, h, p: p["refused"][0].update(error="ok"),
                   lambda a, h, p: p["script"].update(control_or_injection_frames=["inject"])):
        values = copy.deepcopy(desk_receipts())
        mutate(*values)
        assert GATE["desk"](*values, SHA)["status"] == "failed"


def session_receipt():
    flight = {"version": 1, "status": "finished", "epoch": 3, "started_at": "t0", "ended_at": "t1",
              "manual_cleanup": False}
    mission = {"mission_id": "m-000000000001", "status": "completed",
               "binding": {"project_id": "campus_s1", "robot_id": "uav_01", "dock_id": "dock_s1"},
               "dispatch": {"claims": [{"state": "claimed"}],
                            "reservations": [{"state": "released", "reason": "reconciled"}]},
               "cloud": {"flights": [flight], "judge": {"passed": True, "replay_agrees": True,
                                                        "false_success_reports": 0, "problems": []}}}
    recorded = [{**flight, "mission_id": mission["mission_id"], "source_sha": SHA}]
    return {"mission": mission, "errors": []}, recorded


def test_the_desk_session_must_be_bound_claimed_reconciled_and_judged():
    receipt, flights = session_receipt()
    assert GATE["desk_session"](receipt, flights, SHA)["status"] == "passed"
    assert GATE["desk_session"](None, flights, SHA)["status"] == "missing"
    for mutate in (lambda r, f: r["mission"]["binding"].update(project_id="legacy_m2"),
                   lambda r, f: r["mission"]["dispatch"]["claims"].clear(),
                   lambda r, f: r["mission"]["dispatch"]["reservations"][0].update(reason="soft_expired"),
                   lambda r, f: r["mission"]["cloud"]["judge"].update(passed=False),
                   lambda r, f: f[0].update(source_sha="b" * 40)):
        values = copy.deepcopy(session_receipt())
        mutate(*values)
        assert GATE["desk_session"](*values, SHA)["status"] == "failed"


def test_history_is_an_input_and_onboard_changes_require_m1(monkeypatch, tmp_path):
    # Checks images are git archives without .git; inject only the Git read boundary, not the gate decision.
    # 检查镜像来自 git archive，没有 .git；只替换 Git 读取边界，不替换门禁判定。
    records = {path: (ROOT / path).read_bytes() for path in (GATE["M3_PATH"], GATE["P0_PATH"])}
    subprocess = GATE["historical"].__globals__["subprocess"]
    monkeypatch.setattr(subprocess, "check_output",
                        lambda command, **_k: records[command[2].split(":", 1)[1]])
    assert GATE["historical"]()["status"] == "passed"
    altered = tmp_path / GATE["P0_PATH"]
    altered.parent.mkdir(parents=True)
    altered.write_bytes(records[GATE["P0_PATH"]] + b"\n")
    (tmp_path / GATE["M3_PATH"]).parent.mkdir(parents=True)
    (tmp_path / GATE["M3_PATH"]).write_bytes(records[GATE["M3_PATH"]])
    monkeypatch.setitem(GATE["historical"].__globals__, "ROOT", tmp_path)
    assert GATE["historical"]()["status"] == "failed", "a rewritten P0 record never passes"

    scope = GATE["scope"]
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: type("Done", (), {"returncode": 0})())
    for changed, status in (("src/drone_agent/fleet/service.py\ndocs/roadmap.md", "passed"),
                            ("src/drone_agent/guardian/core.py", "missing"), ("Dockerfile.random", "failed")):
        monkeypatch.setitem(scope.__globals__, "git", lambda *args, value=changed: value)
        assert scope(GATE["P0_SHA"], [])["status"] == status, changed
