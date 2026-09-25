"""The logical dock simulator: ACK apart from the physical result, charging physics and report faults.

逻辑机场模拟器：ACK 与物理结果分离、充电过程与报告故障。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from drone_agent.contracts import utcnow
from drone_agent.eval.dock_simulator import DockSimulator
from drone_agent.eval.logical_flight import Battery


def sim(position=(0.0, 0.0, 0.0), in_air=False, battery=1.0, **kwargs):
    world = {"position": list(position), "in_air": in_air}
    dock = DockSimulator("dock_a", battery=Battery(battery), presence=lambda: (world["position"], world["in_air"]),
                         seed=7, **kwargs)
    return dock, world


def run(dock, seconds, step=0.1):
    for _ in range(int(round(seconds / step))):
        dock.advance(step)


def test_the_open_ack_is_immediate_but_completion_follows_the_lid():
    dock, _ = sim(lid_time_s=0.6)
    assert dock.handle({"action_id": "act-0000000001", "kind": "open_lid"}) == (True, "")
    assert dock.lid == "opening" and dock.actions["act-0000000001"]["result"] == "in_progress"
    run(dock, 0.3)
    assert dock.actions["act-0000000001"]["result"] == "in_progress"
    run(dock, 0.4)
    assert dock.lid == "open" and dock.actions["act-0000000001"]["result"] == "completed"


def test_a_jammed_lid_fails_the_action_and_stays_jammed():
    dock, _ = sim(lid_time_s=0.6)
    dock.faults.lid_jam = True
    dock.handle({"action_id": "act-0000000001", "kind": "open_lid"})
    run(dock, 1.0)
    assert dock.lid == "jammed" and dock.actions["act-0000000001"]["result"] == "failed"
    dock.handle({"action_id": "act-0000000002", "kind": "open_lid"})
    assert dock.actions["act-0000000002"]["result"] == "failed"


def test_charging_needs_the_aircraft_and_is_measured_not_timed():
    dock, world = sim(battery=0.5, charge_rate=0.5, cooling_s=0.4)
    assert dock.energy() == ("idle", 0.5)
    assert dock.handle({"action_id": "act-0000000001", "kind": "start_charge"}) == (True, "")
    run(dock, 0.6)
    assert dock.energy()[0] == "charging" and 0.5 < dock.battery.fraction < 1.0
    run(dock, 0.6)
    assert dock.energy()[0] == "cooling" and dock.actions["act-0000000001"]["result"] == "completed"
    run(dock, 0.5)
    assert dock.energy() == ("ready", 1.0)
    world["position"], world["in_air"] = [0.0, 0.0, 4.0], True
    dock.advance(0.1)
    assert dock.energy() == ("unknown", None), "an absent aircraft cannot be measured"
    assert dock.handle({"action_id": "act-0000000002", "kind": "start_charge"}) == (False, "no_aircraft")


def test_a_stalled_charger_acknowledges_but_never_becomes_ready():
    dock, _ = sim(battery=0.6, charge_rate=0.5)
    dock.faults.charge_stall = True
    assert dock.handle({"action_id": "act-0000000001", "kind": "start_charge"}) == (True, "")
    run(dock, 5.0)
    assert dock.energy() == ("charging", 0.6)
    assert dock.actions["act-0000000001"]["result"] == "in_progress"


def test_report_faults_shape_what_is_sent_but_never_the_truth_log():
    dock, _ = sim()
    now = utcnow()
    first, second = dock.report(now), dock.report(now)
    assert (first["seq"], second["seq"]) == (1, 2)
    dock.faults.reorder = True
    assert dock.report(now)["seq"] == 1
    dock.faults.stale_by_s, dock.faults.future_by_s = 5.0, 0.0
    assert datetime.fromisoformat(dock.report(now)["observed_at"]) == now - timedelta(seconds=5)
    dock.faults.stale_by_s = 0.0
    old = dock.boot_id
    dock.reboot()
    dock.faults.replay_boot = old
    replayed = dock.report(now)
    assert replayed["boot_id"] == old and dock.report(now)["boot_id"] == dock.boot_id
    dock.faults.false_presence = True
    dock.presence = lambda: ([0.0, 0.0, 5.0], True)
    dock.advance(0.1)
    sent = dock.report(now)
    assert sent["aircraft"] == "present" and dock.truth[-1]["present"] == "absent"
    dock.faults.offline = True
    assert dock.report(now) is None
    assert [row["sent"]["seq"] for row in dock.truth if row["kind"] == "report"][:3] == [1, 2, 1]
