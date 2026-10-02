"""The soak harness's faults: injection, windows, deferral and the recovery it measures (D073).

长稳编排的故障：注入、窗口、延后，以及它测量的恢复（D073）。
"""

from __future__ import annotations

import json
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOAK = runpy.run_path(str(ROOT / "scripts/desk_soak.py"))
SUP = runpy.run_path(str(ROOT / "scripts/desk_supervisor.py"))
T0 = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = T0

    def __call__(self):
        return self.value


class FakeApi:
    """A service that is down until `ready_at`, with docks and robots fresh afterwards. / 在 ready_at 之前不可用的服务。"""

    def __init__(self, clock):
        self.clock, self.ready_at, self.calls = clock, T0, []

    def call(self, actor, method, trust="first_party", timeout=130.0, **params):
        self.calls.append((actor, method, params))
        if self.clock() < self.ready_at:
            return {"ok": False, "issue": {"code": "harness.unreachable"}, "latency_s": 0.0}
        result = {"health": {"status": "ready", "source_sha": "a" * 40},
                  "resources.list": {"sites": [{"docks": [{"dock_id": "dock_fa", "status": {"session": "active"}}]}]},
                  "robots": [{"robot_id": r, "status": {"comms": {"last_seen": self.clock().isoformat()}}}
                             for r in ("uav_fa", "uav_fb", "uav_01")],
                  "workflows.list": {"runs": []}, "list": []}.get(method)
        return {"ok": True, "result": result, "latency_s": 0.01}


class FakeContainers:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, *args))
            return {} if name in ("inspect", "stats") else True
        return call


def plan() -> dict:
    return {"format": "drone.soak-plan/v1", "duration_h": 1, "seed": 7,
            "identities": {k: f"harness:soak-{k}" for k in ("operator", "approver", "reviewer", "viewer", "other",
                                                             "outsider", "roads")},
            "schedules": [], "workload": {}, "cancel": {"layer": "S0", "permille": 0, "stages": ["before_approval"]},
            "faults": {"service_kill": {"every_h": 2, "offset_min": 0, "service": "desk-service", "mode": "kill"},
                       "fleet_uplink": {"every_h": 2, "offset_min": 1, "service": "desk-fleet",
                                        "network": "desk_uplink", "duration_min": 2},
                       "residents_restart": {"every_h": 2, "offset_min": 2, "services": ["desk-fleet"],
                                             "defer_max_min": 30},
                       "vendor_protocol": {"every_h": 2, "offset_min": 3, "duration_min": 5, "kinds": ["lost_ack"]},
                       "model_throttle": {"every_h": 2, "offset_min": 4, "duration_min": 15}},
            "sampling": {"every_s": 3600}, "criteria": {"cycle_timeout_h": 3}}


def soak(tmp_path, monkeypatch):
    desk = SUP["Desk"](tmp_path)
    for folder in desk.folders():
        folder.mkdir(parents=True, exist_ok=True)
    for name in ("model", "vendor"):
        (desk.faults / name).mkdir(parents=True, exist_ok=True)
    base = desk.base / "soak" / "soak-test"
    base.mkdir(parents=True)
    value = plan()
    (base / "manifest.json").write_text(json.dumps({"soak_id": "soak-test", "t0": T0.isoformat(),
                                                    "end": (T0 + timedelta(hours=1)).isoformat(), "plan": value}))
    (base / "occurrences.json").write_text(json.dumps(SOAK["expand"](value, T0)))
    monkeypatch.setitem(SOAK["HELPERS"], "capacity", lambda: {"load": [0, 0, 0], "memory": {}})
    clock = Clock()
    api, containers = FakeApi(clock), FakeContainers()
    harness = SOAK["Soak"](desk, base, api=api, containers=containers, clock=clock)
    harness.last_people = float("inf")  # no people pass in this test / 本测试不扮演人
    return harness, clock, api, containers, desk


def advance(harness, clock, seconds: float) -> None:
    clock.value += timedelta(seconds=seconds)
    harness.step()


def test_a_killed_service_is_measured_until_ready_and_its_docks_active(tmp_path, monkeypatch):
    harness, clock, api, containers, _ = soak(tmp_path, monkeypatch)
    api.ready_at = T0 + timedelta(seconds=40)
    advance(harness, clock, 1)
    status = harness.state["occurrences"]["service_kill-000"]
    assert ("kill_main", "desk-service") in containers.calls and status["status"] == "recovering"
    advance(harness, clock, 20)
    assert "service_ready_s" not in harness.state["occurrences"]["service_kill-000"]
    advance(harness, clock, 30)
    advance(harness, clock, 2)
    status = harness.state["occurrences"]["service_kill-000"]
    assert status["status"] == "done" and 40 <= status["service_ready_s"] <= 60 and status["docks_active_s"] >= 40


def test_window_faults_end_on_time_and_deferred_restarts_wait_for_an_idle_fleet(tmp_path, monkeypatch):
    harness, clock, api, containers, desk = soak(tmp_path, monkeypatch)
    (desk.fleet / "heartbeat.json").write_text(json.dumps({"robots": {"uav_fa": {"flying": True}}}))
    advance(harness, clock, 61)
    assert ("disconnect", "desk-fleet", "desk_uplink") in containers.calls
    advance(harness, clock, 60)
    assert harness.state["occurrences"]["fleet_uplink-000"]["status"] == "fault_active"
    advance(harness, clock, 61)
    advance(harness, clock, 1)
    uplink = harness.state["occurrences"]["fleet_uplink-000"]
    assert ("connect", "desk-fleet", "desk_uplink") in containers.calls and uplink["status"] == "done"
    assert uplink["robots_fresh_s"] is not None
    assert harness.state["occurrences"]["residents_restart-000"]["status"] == "deferred"
    assert not any(c[0] == "restart" for c in containers.calls)
    (desk.fleet / "heartbeat.json").write_text(json.dumps({"robots": {"uav_fa": {"flying": False}}}))
    advance(harness, clock, 2)
    assert ("restart", "desk-fleet") in containers.calls
    advance(harness, clock, 60)
    faults = json.loads((desk.faults / "vendor" / "vendor.json").read_text())
    assert faults["faults"]["drop_replies"] == {"flighttask_prepare": 1, "flighttask_execute": 1}
    advance(harness, clock, 60)
    throttle = json.loads((desk.faults / "model" / "throttle.json").read_text())
    assert datetime.fromisoformat(throttle["until"]) == T0 + timedelta(minutes=19)
    advance(harness, clock, 5 * 60)
    faults = json.loads((desk.faults / "vendor" / "vendor.json").read_text())
    assert faults["faults"]["drop_replies"] == {} and faults["faults"]["duplicate"] is False


def test_the_end_disables_schedules_clears_switches_and_restores_the_world(tmp_path, monkeypatch):
    harness, clock, api, containers, desk = soak(tmp_path, monkeypatch)
    harness.plan["schedules"] = [{"project_id": "fleet_s0", "workflow_id": "fleet_watch", "trigger_id": "rounds_fa"}]
    harness.set_world("S0", "asset_blue", "damaged", "test")
    advance(harness, clock, 61)  # the uplink window is open / 断网窗口打开
    clock.value = T0 + timedelta(hours=1, seconds=1)
    assert harness.step() is False
    assert ("connect", "desk-fleet", "desk_uplink") in containers.calls
    assert any(m == "workflows.schedule" and p["action"] == "disable" for _, m, p in api.calls)
    world = json.loads(desk.world.read_text())
    assert world["s0"]["asset_blue"] == "normal"
    assert (harness.base / "finished.json").is_file()
    throttle = json.loads((desk.faults / "model" / "throttle.json").read_text())
    assert datetime.fromisoformat(throttle["until"]) < clock.value


def test_the_judge_explains_each_container_start_by_an_injected_fault_only():
    from types import SimpleNamespace

    from drone_agent.eval.judge_p5_soak import unexplained_restarts

    def sample(service_start, dock_start):
        return {"residents": {"desk-service": {"started_at": service_start}, "desk-dock": {"started_at": dock_start}}}

    r = SimpleNamespace(
        plan={"faults": {"service_kill": {"service": "desk-service"}}},
        occurrences=[{"id": "service_kill-000", "kind": "service_kill"},
                     {"id": "residents_restart-000", "kind": "residents_restart"}],
        state={"occurrences": {
            "service_kill-000": {"injected": True, "started_at": "2026-10-03T01:00:00+00:00"},
            "residents_restart-000": {"restarted": [{"service": "desk-dock", "at": "2026-10-03T02:00:00+00:00",
                                                     "ok": True}]}}},
        samples=[sample("2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
                 sample("2026-10-03T01:00:20+00:00", "2026-10-03T00:00:00+00:00"),
                 sample("2026-10-03T01:00:20+00:00", "2026-10-03T02:00:03+00:00"),
                 sample("2026-10-03T03:30:00+00:00", "2026-10-03T02:00:03+00:00")])
    assert unexplained_restarts(r) == {"desk-service": ["2026-10-03T03:30:00+00:00"]}


class StartingApi(FakeApi):
    """Refuses every start until `accept_at`; then starts one run per request id. / 在 accept_at 之前拒绝启动。"""

    def __init__(self, clock, accept_at):
        super().__init__(clock)
        self.accept_at, self.started = accept_at, {}

    def call(self, actor, method, trust="first_party", timeout=130.0, **params):
        if method == "workflows.start":
            self.calls.append((actor, method, params))
            if self.clock() < self.accept_at:
                return {"ok": False, "issue": {"code": "service.degraded"}, "latency_s": 0.0}
            run = self.started.setdefault(params["request_id"], f"wr-{len(self.started):04d}")
            return {"ok": True, "result": {"run": {"run_id": run}}, "latency_s": 0.0}
        if method == "workflows.get":
            return {"ok": True, "result": {"run": {"state": "completed"}}, "latency_s": 0.0}
        return super().call(actor, method, trust, timeout, **params)


def watch_plan() -> dict:
    value = plan()
    value["faults"] = {}
    value["workload"] = {"s1_watch": {"layer": "S1", "every_h": 2, "offset_min": 0, "project_id": "campus_s1",
                                      "workflow_id": "appearance_watch", "assets": ["asset_red"]}}
    return value


def harness_with(tmp_path, monkeypatch, accept_after_s):
    desk = SUP["Desk"](tmp_path)
    for folder in desk.folders():
        folder.mkdir(parents=True, exist_ok=True)
    base = desk.base / "soak" / "soak-test"
    base.mkdir(parents=True)
    value = watch_plan()
    (base / "manifest.json").write_text(json.dumps({"soak_id": "soak-test", "t0": T0.isoformat(),
                                                    "end": (T0 + timedelta(hours=1)).isoformat(), "plan": value}))
    (base / "occurrences.json").write_text(json.dumps(SOAK["expand"](value, T0)))
    monkeypatch.setitem(SOAK["HELPERS"], "capacity", lambda: {"load": [0, 0, 0], "memory": {}})
    clock = Clock()
    api = StartingApi(clock, T0 + timedelta(seconds=accept_after_s))
    harness = SOAK["Soak"](desk, base, api=api, containers=FakeContainers(), clock=clock)
    harness.last_people = float("inf")
    return harness, clock, api


def test_a_refused_start_is_retried_with_the_same_request_id_until_it_starts(tmp_path, monkeypatch):
    harness, clock, api = harness_with(tmp_path, monkeypatch, accept_after_s=120)
    advance(harness, clock, 1)
    assert harness.state["occurrences"]["s1_watch-000"]["status"] == "watching"
    assert not harness.state["occurrences"]["s1_watch-000"].get("runs")
    for _ in range(5):
        advance(harness, clock, 30)
    status = harness.state["occurrences"]["s1_watch-000"]
    assert status["status"] == "done" and status["runs"] == ["wr-0000"]
    requests = {p["request_id"] for _, m, p in api.calls if m == "workflows.start"}
    assert requests == {"soak-test-s1_watch-000"} and len(api.started) == 1


def test_a_start_still_refused_after_the_grace_period_fails_the_occurrence(tmp_path, monkeypatch):
    harness, clock, api = harness_with(tmp_path, monkeypatch, accept_after_s=10 ** 6)
    advance(harness, clock, 1)
    for _ in range(25):
        advance(harness, clock, 30)
    status = harness.state["occurrences"]["s1_watch-000"]
    assert status["status"] == "failed" and status["reason"] == "start_refused" and status["start_failures"] == 1
