"""The D088 handoff tables: the migration and its drill, one open offer per task, compare-and-set transitions and
recheck links that close once.

D088 交接表：迁移及其演练、每个任务至多一个未结提议、比较后写入的状态转移，以及只关闭一次的复核关联。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from drone_agent.fleet.business_store import migrate as migrate_business
from drone_agent.fleet.handoff_store import (
    HANDOFF_SCHEMA,
    INDEXES,
    TABLES,
    HandoffStore,
    StaleHandoff,
    drill,
    handoff_schema,
    migrate,
)
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import MigrationError
from drone_agent.fleet.operations_store import migrate as migrate_operations
from drone_agent.fleet.scheduling_store import migrate as migrate_scheduling
from drone_agent.fleet.workflow_store import migrate as migrate_workflows
from tests.fleet.test_operations_store import populated

TARGET = {"predicate": "x1.target_point", "position": {"x": 1.0, "y": 2.0, "z": 0.0}}


async def prepared(tmp_path: Path) -> Path:
    """A ledger with a flown mission and every extension the handoff tables build on.

    带已飞行任务、并具备交接表所依赖的全部扩展的账本。
    """
    path = await populated(tmp_path)
    ledger = BusinessLedger(path)
    try:
        for step in (migrate_operations, migrate_workflows, migrate_scheduling, migrate_business):
            step(ledger, backups=path.parent / "backups")
    finally:
        ledger.close()
    return path


async def store(tmp_path: Path) -> tuple[BusinessLedger, HandoffStore]:
    path = await prepared(tmp_path)
    ledger = BusinessLedger(path)
    migrate(ledger, backups=path.parent / "backups")
    return ledger, HandoffStore(ledger)


def offer(handoffs: HandoffStore, handoff_id: str, task_id: str = "task-1") -> dict:
    return handoffs.insert_offer(handoff_id=handoff_id, task_id=task_id, project_id="p1", from_party="coordinator",
                                 to_robot_id="ugv_01", target=TARGET, evidence_refs=["ev-1"],
                                 expires_at="2026-10-14T00:05:00+00:00", actor="tester")


async def test_the_migration_adds_the_tables_once_and_backs_up_first(tmp_path):
    path = await prepared(tmp_path)
    ledger = BusinessLedger(path)
    try:
        report = migrate(ledger, backups=path.parent / "backups")
        assert report["status"] == "migrated" and handoff_schema(ledger) == HANDOFF_SCHEMA
        assert report["backup"]["path"].startswith("ledger-v2-ho-") and report["backup"]["rows"]["missions"] == 1
        names = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master")}
        assert set(TABLES) | set(INDEXES) <= names
        assert migrate(ledger, backups=None)["status"] == "current"
    finally:
        ledger.close()


async def test_a_ledger_without_the_business_or_scheduling_tables_is_refused(tmp_path):
    path = await populated(tmp_path)
    ledger = BusinessLedger(path)
    try:
        migrate_operations(ledger, backups=path.parent / "backups")
        with pytest.raises(MigrationError, match="business and scheduling"):
            migrate(ledger, backups=path.parent / "backups")
        with pytest.raises(MigrationError):
            HandoffStore(ledger)
    finally:
        ledger.close()


async def test_the_drill_proves_everything_else_unchanged(tmp_path):
    path = await prepared(tmp_path)
    copy = tmp_path / "copy" / "ledger.sqlite3"
    copy.parent.mkdir()
    shutil.copy(path, copy)
    report = drill(copy)
    assert report["status"] == "passed" and report["migration"] == "migrated"
    assert report["other_sha256_before"] == report["other_sha256_after"]
    assert (report["tables"], report["indexes"]) == (len(TABLES), len(INDEXES))


async def test_a_task_has_at_most_one_open_offer_and_sequences_grow(tmp_path):
    ledger, handoffs = await store(tmp_path)
    try:
        first = offer(handoffs, "h1")
        assert (first["state"], first["offer_seq"]) == ("offered", 1)
        with pytest.raises(sqlite3.IntegrityError):
            offer(handoffs, "h2")
        handoffs.transition("h1", ("offered",), "rejected", actor="ugv_01", body={"reason_code": "unreachable"},
                            reply={"accepted": False, "reason_code": "unreachable"})
        second = offer(handoffs, "h2")
        assert second["offer_seq"] == 2 and handoffs.open_offer("task-1")["handoff_id"] == "h2"
        assert [h["handoff_id"] for h in handoffs.for_task("task-1")] == ["h1", "h2"]
        assert [h["handoff_id"] for h in handoffs.open_offers(robot_id="ugv_01")] == ["h2"]
    finally:
        ledger.close()


async def test_transitions_compare_and_set_and_the_history_only_grows(tmp_path):
    ledger, handoffs = await store(tmp_path)
    try:
        offer(handoffs, "h1")
        handoffs.transition("h1", ("offered",), "accepted", actor="ugv_01", body={},
                            reply={"accepted": True, "path_length_m": 12.5})
        # A late duplicate reply finds the row no longer offered. / 迟到的重复回复发现该行已不是 offered。
        with pytest.raises(StaleHandoff):
            handoffs.transition("h1", ("offered",), "accepted", actor="ugv_01", body={})
        handoffs.note("h1", "reply_ignored", "ugv_01", {"reason": "duplicate"})
        acked = handoffs.transition("h1", ("accepted",), "acked", actor="coordinator", body={}, assignment_epoch=1)
        assert (acked["state"], acked["assignment_epoch"], acked["reply"]["path_length_m"]) == ("acked", 1, 12.5)
        assert [e["kind"] for e in handoffs.events("h1")] == ["offered", "accepted", "reply_ignored", "acked"]
        with pytest.raises(ValueError):
            handoffs.transition("h1", ("acked",), "lost", actor="coordinator", body={})
    finally:
        ledger.close()


async def test_open_offers_past_their_expiry_are_listed(tmp_path):
    ledger, handoffs = await store(tmp_path)
    try:
        offer(handoffs, "h1")
        assert handoffs.expired("2026-10-14T00:04:59+00:00") == []
        assert [h["handoff_id"] for h in handoffs.expired("2026-10-14T00:05:00+00:00")] == ["h1"]
    finally:
        ledger.close()


async def test_a_recheck_link_fills_in_and_closes_once(tmp_path):
    ledger, handoffs = await store(tmp_path)
    try:
        handoffs.insert_recheck(recheck_id="r1", project_id="p1", finding_id="f1", aerial_evidence=["ev-1"],
                                ground_task_id="task-1", detail={"kind": "point"})
        handoffs.update_recheck("r1", handoff_id="h1", ground_mission_id="m-ground")
        closed = handoffs.update_recheck("r1", ground_evidence=["ev-9"], outcome="relocated")
        assert (closed["outcome"], closed["ground_evidence"], closed["closed_at"] is not None) == (
            "relocated", ["ev-9"], True)
        assert [r["recheck_id"] for r in handoffs.rechecks(finding_id="f1")] == ["r1"]
        with pytest.raises(StaleHandoff):
            handoffs.update_recheck("r1", outcome="not_found")
        with pytest.raises(ValueError):
            handoffs.update_recheck("r1", outcome="confirmed")
    finally:
        ledger.close()
