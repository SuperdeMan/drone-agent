"""The D085 capacity migration: indexes and the archive manifest, its drill, and the hot-path query plans.

D085 容量迁移：索引与归档清单、其演练，以及热路径查询计划。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from drone_agent.fleet.capacity_store import (
    AUDIT_NOISE,
    CAPACITY_SCHEMA,
    INDEXES,
    TABLES,
    capacity_schema,
    drill,
    migrate,
)
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import drill as operations_drill
from drone_agent.fleet.operations_store import migrate as migrate_operations
from tests.fleet.test_operations_store import populated


async def migrated(tmp_path: Path) -> Path:
    """A ledger with a flown mission, at operations schema v2 but without the capacity step.

    带已飞行任务、处于运营 schema v2 但未做容量步骤的账本。
    """
    path = await populated(tmp_path)
    ledger = BusinessLedger(path)
    try:
        migrate_operations(ledger, backups=path.parent / "backups")
    finally:
        ledger.close()
    return path


def plan(db: sqlite3.Connection, sql: str, args=()) -> str:
    return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args))


async def test_the_migration_adds_the_indexes_and_the_manifest_once_and_backs_up_first(tmp_path):
    path = await migrated(tmp_path)
    ledger = BusinessLedger(path)
    try:
        report = migrate(ledger, backups=path.parent / "backups")
        assert report["status"] == "migrated" and capacity_schema(ledger) == CAPACITY_SCHEMA
        assert report["backup"]["path"].startswith("ledger-v2-cap-") and report["backup"]["rows"]["missions"] == 1
        assert (path.parent / "backups" / report["backup"]["path"]).is_file()
        names = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master")}
        assert set(TABLES) | set(INDEXES) <= names
        assert migrate(ledger, backups=None)["status"] == "current"
    finally:
        ledger.close()


async def test_the_drill_proves_the_rest_unchanged_and_the_other_drills_still_pass_after_it(tmp_path):
    path = await migrated(tmp_path)
    copy = tmp_path / "drill/ledger.sqlite3"
    copy.parent.mkdir()
    shutil.copyfile(path, copy)
    result = drill(copy)
    assert result["status"] == "passed" and result["cap_indexes"] == len(INDEXES) and result["cap_tables"] == 1
    assert result["other_sha256_before"] == result["other_sha256_after"] and result["elapsed_s"] >= 0
    assert "Inspect" not in json.dumps(result), "the receipt carries no mission content"
    # Each drill checks only its own prefix, so the D056 drill passes on the capacity-migrated copy.
    # 每个演练只核对自己的前缀，因此 D056 演练在已做容量迁移的副本上照样通过。
    assert operations_drill(copy)["status"] == "passed"
    assert drill(copy)["migration"] == "current"


async def test_the_hot_path_queries_use_the_new_indexes(tmp_path):
    path = await migrated(tmp_path)
    ledger = BusinessLedger(path)
    try:
        migrate(ledger, backups=path.parent / "backups")
    finally:
        ledger.close()
    noise = ", ".join(f"'{kind}'" for kind in AUDIT_NOISE)
    with sqlite3.connect(str(path)) as db:
        expected = [
            ("cap_events_mission", "SELECT * FROM events WHERE mission_id=? ORDER BY mission_version, journal, seq",
             ("m",)),
            ("cap_missions_created", "SELECT * FROM missions ORDER BY created_at DESC LIMIT 50", ()),
            ("cap_deliveries_mission", "SELECT * FROM deliveries WHERE mission_id=? ORDER BY cursor", ("m",)),
            ("cap_deliveries_open", "SELECT * FROM deliveries WHERE robot_id=? AND cursor>? AND acked_at IS NULL "
                                    "ORDER BY cursor LIMIT 10", ("r", 0)),
            ("cap_deliveries_open", "SELECT * FROM deliveries WHERE kind='mission_package' AND acked_at IS NULL "
                                    "ORDER BY cursor", ()),
            ("cap_op_events_audit", f"SELECT * FROM op_events WHERE kind NOT IN ({noise}) AND id < ? "
                                    "ORDER BY id DESC LIMIT 500", (10**9,)),
            ("cap_op_reservations_dock", "SELECT activity_key FROM op_reservations WHERE dock_id=? AND state=? "
                                         "ORDER BY updated_at DESC, created_at ASC LIMIT 1", ("d", "released")),
            ("cap_op_holds_reservation", "SELECT resource_id FROM op_holds WHERE reservation_id=? "
                                         "ORDER BY resource_id", ("res",)),
            ("cap_op_claims_mission", "SELECT * FROM op_claims WHERE mission_id=? ORDER BY decided_at", ("m",)),
            ("cap_op_reservations_state", "SELECT * FROM op_reservations WHERE 1=1 AND state IN (?,?) "
                                          "ORDER BY created_at", ("reserved", "occupied")),
            ("cap_evidence_mission", "SELECT * FROM evidence WHERE mission_id=? ORDER BY received_at", ("m",)),
            ("cap_evidence_mission", "SELECT * FROM evidence WHERE mission_id=? AND mission_version=? "
                                     "ORDER BY received_at", ("m", 1)),
            ("cap_verifications_mission", "SELECT * FROM verifications WHERE mission_id=? ORDER BY verified_at",
             ("m",)),
            ("cap_facts_mission", "SELECT body FROM facts WHERE mission_id=?", ("m",)),
            ("cap_issues_mission", "SELECT body FROM issues WHERE mission_id=? ORDER BY id", ("m",)),
            ("cap_op_dock_actions_open", "SELECT * FROM op_dock_actions WHERE dock_id=? AND state IN (?) "
                                         "ORDER BY requested_at", ("d", "requested")),
            ("cap_op_dock_actions_dock", "SELECT * FROM op_dock_actions WHERE dock_id=? "
                                         "ORDER BY requested_at DESC LIMIT ?", ("d", 20)),
            ("(activity_key>? AND activity_key<?)", "SELECT * FROM op_dock_actions WHERE +dock_id=? "
             "AND activity_key >= ? AND activity_key < ? ORDER BY requested_at", ("d", "mission:m:", "mission:m;")),
        ]
        for index, sql, args in expected:
            assert index in plan(db, sql, args), (index, plan(db, sql, args))


async def test_the_drill_fails_when_the_step_touches_other_data(tmp_path, monkeypatch):
    import drone_agent.fleet.capacity_store as store

    path = await migrated(tmp_path)
    real = store.migrate

    def touching(ledger, **kwargs):
        report = real(ledger, **kwargs)
        ledger._exec("UPDATE missions SET updated_at = updated_at || 'x'")
        return report

    monkeypatch.setattr(store, "migrate", touching)
    assert store.drill(path)["status"] == "failed"
