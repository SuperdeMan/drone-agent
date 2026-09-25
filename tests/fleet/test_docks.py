"""Dock report ingestion and the action exchange: bound backends, ordering, sessions, sticky locks, ACK vs result.

机场报告入账与动作交换：绑定后端、顺序、会话、粘滞锁、ACK 与结果分离。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.contracts import utcnow
from drone_agent.fleet import docks
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import OperationsStore, migrate
from drone_agent.fleet.resources import SessionState, load_catalog

ROOT = Path(__file__).resolve().parents[2]
PRINCIPAL = "dock:p1-s0-sim"


@pytest.fixture
def store(tmp_path) -> OperationsStore:
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    migrate(ledger, backups=tmp_path / "backups")
    return OperationsStore(ledger, load_catalog(ROOT / "configs/sites/p1_campus_v1.yaml"))


def report(seq: int, *, boot: str = "boot-dock_a-0001", at=None, **overrides) -> dict:
    value = {"dock_id": "dock_a", "boot_id": boot, "seq": seq, "observed_at": (at or utcnow()).isoformat(),
             "link": "online", "lid": "closed", "aircraft": "present",
             "energy": {"state": "ready", "charge_fraction": 1.0},
             "environment": {"state": "permitted", "wind_mps": 2.0}, "upkeep": "normal", "actions": []}
    value.update(overrides)
    return value


def ingest(store, data, principal=PRINCIPAL):
    return docks.ingest(store, principal, data, utcnow())


def rejected(store) -> list[str]:
    return [e["body"]["reason"] for e in store.events("dock:dock_a") if e["kind"] == "status.rejected"]


def test_a_new_session_reconciles_before_it_counts(store):
    assert ingest(store, report(1)) == {"accepted": True, "session": "reconciling", "finished": []}
    assert store.dock_status("dock_a").session is SessionState.RECONCILING
    assert ingest(store, report(2))["session"] == "active"
    assert store.dock_status("dock_a").source == "logical_sim"


def test_reports_from_the_wrong_backend_or_for_unknown_docks_are_refused(store):
    assert not ingest(store, report(1), principal="dock:p1-s0-harbor")["accepted"]
    assert not ingest(store, {**report(1), "dock_id": "dock_real_01"})["accepted"]
    assert not ingest(store, {**report(1), "source": "real_device"})["accepted"]
    assert store.dock_status("dock_a") is None
    assert rejected(store) == ["backend_mismatch", "invalid_report"]
    assert [e["body"]["reason"] for e in store.events("dock:dock_real_01")] == ["unknown_dock"]


def test_future_stale_reordered_and_replaced_boot_reports_never_extend_validity(store):
    ingest(store, report(1))
    ingest(store, report(2))
    before = store.dock_status("dock_a").report.observed_at
    assert not ingest(store, report(3, at=utcnow() + timedelta(seconds=1.5)))["accepted"]
    assert not ingest(store, report(4, at=utcnow() - timedelta(seconds=3.5)))["accepted"]
    assert not ingest(store, report(2))["accepted"]
    assert store.dock_status("dock_a").report.observed_at == before
    assert ingest(store, report(1, boot="boot-dock_a-0002"))["session"] == "reconciling"
    assert not ingest(store, report(9, boot="boot-dock_a-0001"))["accepted"]
    assert rejected(store) == ["future_timestamp", "stale_on_arrival", "reordered", "replaced_boot"]
    assert [e["kind"] for e in store.events("dock:dock_a") if e["kind"] == "session.new"] == ["session.new"]


def test_telemetry_sets_a_maintenance_lock_but_only_an_admin_clears_it(store):
    ingest(store, report(1, upkeep="maintenance"))
    ingest(store, report(2))
    ingest(store, report(3))
    status = store.dock_status("dock_a")
    assert status.report.upkeep.value == "normal" and status.lock.state == "maintenance"
    assert docks.release_lock(store, "dock_a", "harness:p1-admin", "inspected")
    assert store.dock_status("dock_a").lock is None
    assert not docks.release_lock(store, "dock_a", "harness:p1-admin", "again")
    ingest(store, report(4, upkeep="fault"))
    assert store.dock_status("dock_a").lock.state == "fault"


def test_an_ack_records_receipt_once_and_only_a_report_finishes_an_action(store):
    action = store.request_action("dock_a", "open_lid", "mission:m-1:v1")
    assert store.request_action("dock_a", "open_lid", "mission:m-1:v1")["action_id"] == action["action_id"]
    assert [a["action_id"] for a in docks.pending_actions(store, PRINCIPAL, "dock_a")] == [action["action_id"]]
    with pytest.raises(docks.ReportRejected):
        docks.pending_actions(store, "dock:p1-s0-harbor", "dock_a")
    assert docks.acknowledge(store, PRINCIPAL, action["action_id"], True, "")["applied"]
    assert store.action(action["action_id"])["state"] == "acked", "an ACK is receipt, not completion"
    assert not docks.acknowledge(store, PRINCIPAL, action["action_id"], False, "late")["applied"]
    ingest(store, report(1, lid="opening", actions=[{"action_id": action["action_id"], "result": "in_progress"}]))
    assert store.action(action["action_id"])["state"] == "acked"
    ingest(store, report(2, lid="open", actions=[{"action_id": action["action_id"], "result": "completed"}]))
    assert store.action(action["action_id"])["state"] == "completed"
    ingest(store, report(3, lid="jammed", actions=[{"action_id": action["action_id"], "result": "failed"}]))
    assert store.action(action["action_id"])["state"] == "completed", "a finished action never changes"
    kinds = [e["kind"] for e in store.events("dock:dock_a") if e["kind"].startswith("action")]
    assert kinds == ["action.ack", "action.ack_ignored"]
