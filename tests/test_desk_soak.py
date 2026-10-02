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
                  "resources.list": {"docks": [{"dock_id": "dock_fa", "status": {"session": "active"}}]},
                  "robots": [{"robot_id": r, "status_at": self.clock().isoformat()} for r in ("uav_fa", "uav_fb",
                                                                                           "uav_01")],
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
