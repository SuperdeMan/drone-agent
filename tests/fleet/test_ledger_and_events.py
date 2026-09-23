"""Business ledger and journal-row events: exact rebuild, chain checks, dedup, active-operation uniqueness.

业务账本与账本行事件：精确重建、链检查、去重、进行中操作唯一性。
"""

from __future__ import annotations

import pytest

from drone_agent.fleet.events import event_to_row, journal_row_to_event, outcomes_from_rows, verify_chain
from drone_agent.fleet.ledger import BusinessLedger, DuplicateOperation
from drone_agent.runtime.ledger import Journal, read_log
from tests.fleet.harness import request


def journal_rows(tmp_path) -> list[dict]:
    journal = Journal(tmp_path / "executive.jsonl")
    journal.append("mission_accepted", {"mission_id": "m-1", "package_hash": "a" * 64})
    journal.append("skill_state", {"mission_id": "m-1", "step_id": "takeoff", "previous": "accepted",
                                   "state": "running"})
    journal.append("step_outcome", {"mission_id": "m-1", "outcome": {
        "mission_id": "m-1", "mission_version": 1, "step_id": "takeoff", "robot_id": "uav_01",
        "execution_status": "succeeded", "effect_verdict": "verified", "safety_verdict": "proceed"}})
    journal.close()
    return read_log(tmp_path / "executive.jsonl")


def test_events_rebuild_the_exact_rows_and_the_chain_verifies(tmp_path):
    rows = journal_rows(tmp_path)
    events = [journal_row_to_event(r, journal="executive", robot_id="uav_01", mission_id="m-1", version=1)
              for r in rows]
    assert [e.event_type.value for e in events] == ["mission_accepted", "skill_started", "skill_completed"]
    rebuilt = [event_to_row(e.model_dump(mode="json")) for e in events]
    assert rebuilt == rows and verify_chain(rebuilt) == (True, "")
    assert outcomes_from_rows(rebuilt)["takeoff"].counts_as_completed
    assert verify_chain(rebuilt[1:])[1] == "gap before seq 1"
    tampered = [dict(r) for r in rebuilt]
    tampered[1] = {**tampered[1], "data": {**tampered[1]["data"], "state": "paused"}}
    assert verify_chain(tampered) == (False, "hash chain broken at seq 1")


def test_ledger_deduplicates_events_and_requests(tmp_path):
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    rows = journal_rows(tmp_path)
    event = journal_row_to_event(rows[0], journal="executive", robot_id="uav_01", mission_id="m-1", version=1)
    assert ledger.record_event("uav_01", event) and not ledger.record_event("uav_01", event)
    first, created = ledger.record_request(request(), "m-first")
    again, created_again = ledger.record_request(request(), "m-second")
    assert (first, created, again, created_again) == ("m-first", True, "m-first", False)
    ledger.close()
    reopened = BusinessLedger(tmp_path / "ledger.sqlite3")
    assert len(reopened.events("m-1")) == 1 and reopened.mission("m-first")["status"] == "planning"


def test_one_active_operation_per_mission_and_idempotency_key(tmp_path):
    ledger = BusinessLedger(":memory:")
    operation = ledger.begin_operation("m-1", "plan", "idem-1")
    with pytest.raises(DuplicateOperation):
        ledger.begin_operation("m-1", "plan", "idem-1")
    ledger.begin_operation("m-2", "plan", "idem-1")
    ledger.finish_operation(operation, "done")
    ledger.begin_operation("m-1", "plan", "idem-1")
    with pytest.raises(ValueError):
        ledger.finish_operation(operation, "pending")
