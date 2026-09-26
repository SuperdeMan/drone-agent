"""The D056 migration drill and the reservation invariants of the P1 operations store.

D056 迁移演练与 P1 运营存储的预约不变量。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.contracts import utcnow
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import (
    DDL,
    MigrationError,
    OperationsStore,
    drill,
    migrate,
    schema_version,
    v1_lines,
)
from drone_agent.fleet.resources import load_catalog
from tests.fleet.harness import build_loop, request

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "configs/sites/p1_campus_v1.yaml")


def v1_dump(path: Path) -> list[str]:
    """Every statement of the v1 tables, in order. / v1 表的全部语句，按序。"""
    lines = v1_lines(path)
    assert any(line.startswith('INSERT INTO "missions"') for line in lines), "the drill must see real v1 rows"
    return lines


async def populated(tmp_path: Path) -> Path:
    """A v1 ledger with a flown, reported M2 mission. / 带已飞行并出报告的 M2 任务的 v1 账本。"""
    loop = build_loop(tmp_path / "m2")
    view = await loop.service.submit(request())
    mission_id = view["mission"]["mission_id"]
    loop.service.approve(mission_id, 1, approver="tailnet:operator@example.test",
                         package_hash=view["versions"][0]["package_hash"])
    await loop.sync()
    result = await loop.fly(loop.inbox_package(), epoch=1)
    assert result["completed"]
    await loop.sync()
    assert loop.ledger.mission(mission_id)["status"] == "completed"
    loop.ledger.close()
    return tmp_path / "m2/service/ledger.sqlite3"


async def test_a_ledger_with_data_is_backed_up_verified_and_extended_without_touching_v1(tmp_path):
    path = await populated(tmp_path)
    before = v1_dump(path)
    ledger = BusinessLedger(path)
    assert schema_version(ledger) == "1"
    report = migrate(ledger, backups=tmp_path / "backups")
    assert report["status"] == "migrated" and report["backup"]["rows"]["missions"] == 1
    backup = tmp_path / "backups" / report["backup"]["path"]
    with sqlite3.connect(str(backup)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    assert schema_version(ledger) == "2"
    assert migrate(ledger, backups=tmp_path / "backups")["status"] == "current"
    assert len(list((tmp_path / "backups").iterdir())) == 1, "a rerun must not back up again"
    ledger.close()
    assert v1_dump(path) == before, "v1 tables, columns and rows are unchanged"


async def test_a_failing_migration_rolls_back_and_leaves_the_v1_database_as_it_was(tmp_path):
    path = await populated(tmp_path)
    before = v1_dump(path)
    ledger = BusinessLedger(path)
    with pytest.raises(MigrationError):
        migrate(ledger, backups=tmp_path / "backups", statements=(*DDL[:4], "CREATE TABLE broken (", *DDL[4:]))
    assert schema_version(ledger) == "1"
    names = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master")}
    assert not [name for name in names if name.startswith("op_")]
    ledger.close()
    assert v1_dump(path) == before


async def test_v1_code_keeps_working_on_a_migrated_file_and_the_backup_restores_v1(tmp_path):
    path = await populated(tmp_path)
    before = v1_dump(path)
    ledger = BusinessLedger(path)
    report = migrate(ledger, backups=tmp_path / "backups")
    ledger.close()
    # The unchanged v1 ledger and the M2-mode service ignore the new tables. / 未改的 v1 账本与 M2 模式服务忽略新表。
    loop = build_loop(tmp_path / "m2")
    [mission] = loop.ledger.missions()
    assert loop.service.view(mission["mission_id"])["mission"]["status"] == "completed"
    view = await loop.service.submit(request(key="idem-after-migration"))
    assert view["mission"]["status"] == "awaiting_approval"
    loop.ledger.close()
    restored = tmp_path / "restored.sqlite3"
    shutil.copyfile(tmp_path / "backups" / report["backup"]["path"], restored)
    assert v1_dump(restored) == before
    assert schema_version(BusinessLedger(restored)) == "1"


def store(path: Path, clock=utcnow) -> OperationsStore:
    ledger = BusinessLedger(path)
    migrate(ledger, backups=path.parent / "backups", clock=clock)
    return OperationsStore(ledger, CATALOG, clock=clock)


def test_one_holder_per_resource_and_a_repeat_returns_the_same_reservation(tmp_path):
    ops = store(tmp_path / "ledger.sqlite3")
    later = utcnow() + timedelta(minutes=5)
    first, conflicts = ops.reserve(activity="mission:m-1:v1", project_id="campus_ops", mission_id="m-1", version=1,
                                   robot_id="uav_a", expires_at=later)
    assert first is not None and not conflicts and first.state.value == "reserved"
    again, _ = ops.reserve(activity="mission:m-1:v1", project_id="campus_ops", mission_id="m-1", version=1,
                           robot_id="uav_a", expires_at=later)
    assert again.reservation_id == first.reservation_id
    other, conflicts = ops.reserve(activity="mission:m-2:v1", project_id="campus_ops", mission_id="m-2", version=1,
                                   robot_id="uav_a", expires_at=later)
    assert other is None and set(conflicts.values()) == {"mission:m-1:v1"}
    assert ops.transition("mission:m-1:v1", ("reserved",), "occupied", "claimed")
    assert ops.transition("mission:m-1:v1", ("occupied", "uncertain"), "released", "reconciled", {"x": 1})
    assert not ops.transition("mission:m-1:v1", ("occupied", "uncertain"), "released", "reconciled", {"x": 1}), \
        "a release happens exactly once"
    assert ops.reserve(activity="mission:m-2:v1", project_id="campus_ops", mission_id="m-2", version=1,
                       robot_id="uav_a", expires_at=later)[0] is not None


def test_only_an_expired_soft_hold_gives_way_and_its_activity_may_take_it_again(tmp_path):
    now = [utcnow()]
    ops = store(tmp_path / "ledger.sqlite3", clock=lambda: now[0])
    soon = now[0] + timedelta(seconds=10)
    ops.reserve(activity="mission:m-1:v1", project_id="campus_ops", mission_id="m-1", version=1, robot_id="uav_b",
                expires_at=soon)
    now[0] += timedelta(seconds=11)
    taken, _ = ops.reserve(activity="mission:m-2:v1", project_id="campus_ops", mission_id="m-2", version=1,
                           robot_id="uav_b", expires_at=now[0] + timedelta(minutes=5))
    assert taken is not None
    assert ops.reservation("mission:m-1:v1").reason == "soft_expired"
    assert ops.transition("mission:m-2:v1", ("reserved",), "occupied", "claimed")
    now[0] += timedelta(hours=1)
    blocked, conflicts = ops.reserve(activity="mission:m-1:v1", project_id="campus_ops", mission_id="m-1",
                                     version=1, robot_id="uav_b", expires_at=now[0] + timedelta(minutes=5))
    assert blocked is None and conflicts, "an occupied hold never expires"


def test_two_writers_racing_for_one_dock_leave_exactly_one_holder(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    store(path).ledger.close()
    stores = [OperationsStore(BusinessLedger(path), CATALOG) for _ in range(6)]
    barrier, outcomes = threading.Barrier(len(stores)), []

    def grab(index: int) -> None:
        barrier.wait()
        for attempt in range(20):
            try:
                reservation, _ = stores[index].reserve(
                    activity=f"mission:m-{index}:v1", project_id="campus_ops", mission_id=f"m-{index}", version=1,
                    robot_id="uav_c", expires_at=utcnow() + timedelta(minutes=5))
                outcomes.append(reservation is not None)
                return
            except sqlite3.OperationalError:  # database is locked: another writer's transaction / 其他写入方的事务
                threading.Event().wait(0.01 * (attempt + 1))
        outcomes.append(False)

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(True) == 1
    with sqlite3.connect(str(path)) as db:
        active = db.execute("SELECT COUNT(*) FROM op_holds WHERE active=1 AND resource_id='dock_c.pad'").fetchone()[0]
    assert active == 1


def test_the_store_refuses_an_unmigrated_ledger(tmp_path):
    with pytest.raises(MigrationError):
        OperationsStore(BusinessLedger(tmp_path / "ledger.sqlite3"), CATALOG)


async def test_the_drill_reports_counts_and_digests_but_no_content(tmp_path):
    path = await populated(tmp_path)
    copy = tmp_path / "drill/ledger.sqlite3"
    copy.parent.mkdir()
    shutil.copyfile(path, copy)
    result = drill(copy)
    assert result["status"] == "passed" and result["op_tables"] == 12
    assert result["v1_dump_sha256_before"] == result["v1_dump_sha256_after"]
    assert "Inspect" not in json.dumps(result), "the receipt carries no mission content"
