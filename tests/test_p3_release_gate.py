"""Negative P3 gate cases: missing, mixed-revision or unsafe evidence, one-aircraft matrices, capacity outside D061,
a desk without the scheduling catalog and changed history cannot pass.

P3 门禁反例：缺失、混版本或不安全的证据、只飞一架的矩阵、超出 D061 的容量、没有调度目录的任务台以及被改写的历史，都不能通过。
"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p3_release.py"))
SHA = "a" * 40
ZERO = dict.fromkeys(GATE["COUNTS"], 0)
CASES = (("p3_s1_parallel", (7, 19, 41)), ("p3_s1_contention", (7,)), ("p3_s1_relay", (7,)))


def s1_receipt(**overrides) -> dict:
    flown = {"p3_s1_parallel": {"uav_01": ["m-1-v1"], "uav_02": ["m-2-v1"]},
             "p3_s1_contention": {"uav_01": ["m-3-v1"], "uav_02": ["m-4-v1"]},
             "p3_s1_relay": {"uav_01": [], "uav_02": ["m-6-v1"]}}
    results = [{"scenario": case, "seed": seed, "layer": "S1", "passed": True, "replay_agrees": True,
                "judge_exit_code": 0, "problems": [], "source_sha": SHA, "counts": dict(ZERO), "flown": flown[case],
                "capacity": {"verdict": {"sufficient": True, "real_time_factor_min": 0.8, "supervision_p99_max_s": 0.1},
                             "idle": {"sitl-p3": {}} if index == 0 else None}}
               for index, (case, seed) in enumerate((c, s) for c, seeds in CASES for s in seeds)]
    value = {"status": "passed", "layer": "S1", "aircraft": 2, "source_sha": SHA, "signer_key_id": "k",
             "certificate_fingerprints": {"ca": "1", "service": "2", "robot": "3", "robot-tls-uav_02": "4"},
             "budget": dict(GATE["FROZEN_BUDGET"]), "results": results,
             "other_containers_before": {"count": 1}, "other_containers_after": {"count": 1}}
    value.update(overrides)
    return value


@pytest.fixture
def at_head(monkeypatch):
    """Read the suite from the working tree instead of a commit. / 从工作树读取场景集而不是某个提交。"""
    subprocess = GATE["s1"].__globals__["subprocess"]
    real = subprocess.check_output

    def check_output(argv, **kwargs):
        if argv[:2] == ["git", "show"] and argv[2].endswith(":configs/scenarios/p3_suite.yaml"):
            return (ROOT / "configs/scenarios/p3_suite.yaml").read_bytes()
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", check_output)


def test_s1_requires_every_two_aircraft_case_on_this_revision_with_zero_counts(at_head):
    assert GATE["s1"]([], SHA)["status"] == "missing"
    passed = GATE["s1"]([s1_receipt()], SHA)
    assert passed["status"] == "passed" and passed["aircraft"] == ["uav_01", "uav_02"] and passed["cases"] == 5
    assert GATE["s1"]([s1_receipt(source_sha="b" * 40)], SHA)["status"] == "failed"
    assert GATE["s1"]([s1_receipt(aircraft=1)], SHA)["status"] == "failed"
    short = s1_receipt()
    short["results"] = short["results"][:-1]
    assert GATE["s1"]([short], SHA)["missing"] == [("p3_s1_relay", 7)]
    for count in GATE["COUNTS"]:
        unsafe = s1_receipt()
        unsafe["results"][0]["counts"][count] = 1
        assert GATE["s1"]([unsafe], SHA)["status"] == "failed", count
    assert GATE["s1"]([s1_receipt(other_containers_after={"count": 2})], SHA)["status"] == "failed"
    single = s1_receipt(certificate_fingerprints={"ca": "1", "service": "2", "robot": "3"})
    assert GATE["s1"]([single], SHA)["status"] == "failed", "the second robot's certificate is required"
    one = s1_receipt()
    for row in one["results"]:
        row["flown"] = {"uav_01": ["m-1-v1"], "uav_02": []}
    assert GATE["s1"]([one], SHA)["status"] == "failed", "a matrix that flew one aircraft is not two-aircraft S1"


def test_capacity_needs_the_frozen_budget_the_idle_probe_and_both_limits_in_every_case():
    assert GATE["capacity"]([])["status"] == "missing"
    assert GATE["capacity"]([s1_receipt()])["status"] == "passed"
    assert GATE["capacity"]([s1_receipt(budget={"sitl_cpus": "3.5", "sitl_mem": "3g"})])["status"] == "failed"
    no_probe = s1_receipt()
    no_probe["results"][0]["capacity"]["idle"] = None
    assert GATE["capacity"]([no_probe])["status"] == "failed"
    slow = s1_receipt()
    slow["results"][2]["capacity"]["verdict"]["sufficient"] = False
    assert GATE["capacity"]([slow])["status"] == "failed"
    unmeasured = s1_receipt()
    unmeasured["results"][3]["capacity"] = {}
    assert GATE["capacity"]([unmeasured])["status"] == "failed"


def desk_receipts():
    activation = {"status": "verified", "source_sha": SHA,
                  "operations": {"catalog_id": "p1_s1_v1", "migration": {"status": "current"},
                                 "drill": {"status": "passed"}},
                  "workflows": {"catalog_id": "p2_s1_v1", "migration": {"status": "current"},
                                "drill": {"status": "passed"}},
                  "scheduling": {"catalog_id": "p3_desk_v1", "migration": {"status": "migrated"},
                                 "drill": {"status": "passed"}},
                  "containers": {"desk-dock": {"docker_access": False}, "desk": {"docker_access": False}}}
    http = {"page": {"status": 200}, "script": {"matches_checkout": True},
            "health": {"ok": True, "console_source_sha": SHA, "service_source_sha": SHA}}
    probe = {"project": "campus_s1",
             "projects": [{"project_id": "campus_s1", "scheduling": True}, {"project_id": "legacy_m2",
                                                                            "scheduling": False}],
             "queue": {"error": None, "roles": ["approver", "operator"], "catalog": {"catalog_id": "p3_desk_v1"},
                       "assets": {"asset_red": {"volume_id": "campus_training", "robots": ["uav_01"]}},
                       "robots": [{"robot_id": "uav_01", "eligibility": {"verdict": "blocked",
                                                                         "reasons": ["energy.charging"]}}]},
             "refused": [{"frame": f, "error": f"unknown type {f!r}"} for f in
                         ("inject", "docks.report", "arm", "flight", "simulator.inject", "task_assign",
                          "task_approve_all", "scheduler_tick")]}
    return activation, http, probe


def test_the_desk_needs_three_catalogs_three_drills_the_queue_with_reasons_and_refused_frames():
    activation, http, probe = desk_receipts()
    assert GATE["desk"](None, http, probe, SHA)["status"] == "missing"
    assert GATE["desk"](activation, http, probe, SHA)["status"] == "passed"
    for mutate in (lambda a, h, p: a.pop("scheduling"),
                   lambda a, h, p: a["scheduling"].update(catalog_id="p3_s1_v1"),
                   lambda a, h, p: a["scheduling"]["drill"].update(status="failed"),
                   lambda a, h, p: a["scheduling"]["migration"].update(status="refused"),
                   lambda a, h, p: a["workflows"].update(catalog_id="p1_s1_v1"),
                   lambda a, h, p: a["containers"]["desk-dock"].update(docker_access=True),
                   lambda a, h, p: h["health"].update(service_source_sha="b" * 40),
                   lambda a, h, p: p["projects"][0].update(scheduling=False),
                   lambda a, h, p: p["queue"].update(roles=["viewer"]),
                   lambda a, h, p: p["queue"]["robots"][0]["eligibility"].pop("reasons"),
                   lambda a, h, p: p["queue"].update(assets={}),
                   lambda a, h, p: p["refused"].pop(),
                   lambda a, h, p: p["refused"][0].update(error="ok")):
        values = copy.deepcopy(desk_receipts())
        mutate(*values)
        assert GATE["desk"](*values, SHA)["status"] == "failed"


def session_receipt():
    flight = {"version": 1, "status": "finished", "epoch": 5, "started_at": "t0", "ended_at": "t1",
              "manual_cleanup": False}
    mission = {"status": "completed", "request": {"channel": "scheduler", "requested_by": "task:tk-1"},
               "binding": {"project_id": "campus_s1", "robot_id": "uav_01", "dock_id": "dock_s1"},
               "dispatch": {"claims": [{"state": "claimed"}],
                            "reservations": [{"state": "released", "reason": "reconciled"}]},
               "cloud": {"flights": [flight], "judge": {"passed": True, "replay_agrees": True,
                                                        "false_success_reports": 0, "problems": []}}}
    receipt = {"errors": [], "sent": [{"action": "approve", "mission_id": "m-000000000001", "version": 1}],
               "task": {"task": {"task_id": "tk-1", "state": "completed", "robot_id": "uav_01", "epoch": 1,
                                 "source": "api", "requested_by": "tailnet:<operator>"},
                        "decisions": [{"verdict": "wait", "robot_id": None},
                                      {"verdict": "assign", "robot_id": "uav_01"}]},
               "missions": {"m-000000000001": mission}}
    return receipt, [{**flight, "mission_id": "m-000000000001", "source_sha": SHA}]


def test_the_desk_task_must_be_scheduled_approved_claimed_reconciled_and_judged():
    receipt, flights = session_receipt()
    assert GATE["desk_session"](receipt, flights, SHA)["status"] == "passed"
    assert GATE["desk_session"](None, flights, SHA)["status"] == "missing"
    for mutate in (lambda r, f: r["task"]["task"].update(state="assigned"),
                   lambda r, f: r["task"]["task"].update(source="workflow"),
                   lambda r, f: r["task"]["task"].update(requested_by="harness:p3-operator"),
                   lambda r, f: r["task"].update(decisions=[{"verdict": "wait", "robot_id": None}]),
                   lambda r, f: r.update(sent=[]),
                   lambda r, f: r["missions"]["m-000000000001"]["request"].update(channel="console"),
                   lambda r, f: r["missions"]["m-000000000001"]["binding"].update(robot_id="uav_02"),
                   lambda r, f: r["missions"]["m-000000000001"]["dispatch"]["claims"].clear(),
                   lambda r, f: r["missions"]["m-000000000001"]["dispatch"]["reservations"][0].update(reason="soft"),
                   lambda r, f: r["missions"]["m-000000000001"]["cloud"]["judge"].update(replay_agrees=False),
                   lambda r, f: r.update(missions={}),
                   lambda r, f: f[0].update(source_sha="b" * 40),
                   lambda r, f: r.update(errors=[{"error": "TimeoutError"}])):
        values = copy.deepcopy(session_receipt())
        mutate(*values)
        assert GATE["desk_session"](*values, SHA)["status"] == "failed"


def test_history_is_an_input_and_onboard_changes_require_m1(monkeypatch, tmp_path):
    # Checks images are git archives without .git; inject only the Git read boundary, not the gate decision.
    # 检查镜像来自 git archive，没有 .git；只替换 Git 读取边界，不替换门禁判定。
    paths = (GATE["M3_PATH"], GATE["P0_PATH"], GATE["P1_PATH"], GATE["P2_PATH"])
    records = {path: (ROOT / path).read_bytes() for path in paths}
    subprocess = GATE["historical"].__globals__["subprocess"]
    monkeypatch.setattr(subprocess, "check_output", lambda command, **_k: records[command[2].split(":", 1)[1]])
    assert GATE["historical"]()["status"] == "passed"
    for path in paths:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(records[path] + (b"\n" if path == GATE["P2_PATH"] else b""))
    monkeypatch.setitem(GATE["historical"].__globals__, "ROOT", tmp_path)
    assert GATE["historical"]()["status"] == "failed", "a rewritten P2 record never passes"

    scope = GATE["scope"]
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: type("Done", (), {"returncode": 0})())
    for changed, status in (("src/drone_agent/fleet/scheduler.py\nconfigs/scheduling/p3_desk_v1.yaml\nuv.lock", "passed"),
                            ("sim/p3_setup.py\nsim/compose.p3.yaml\ndocs/roadmap.md", "passed"),
                            ("src/drone_agent/guardian/core.py", "missing"),
                            ("src/drone_agent/planner/engine.py", "failed"), ("sim/m1_setup.py", "failed")):
        monkeypatch.setitem(scope.__globals__, "git", lambda *args, value=changed: value)
        assert scope(GATE["P2_SHA"], [])["status"] == status, changed
