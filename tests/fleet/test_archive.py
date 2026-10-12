"""The D085 status archive: only old `status.accepted` rows leave the ledger, as verifiable chunks.

D085 状态归档：只有旧的 `status.accepted` 行离开账本，成为可核验的块。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from drone_agent.fleet.archive import KIND, StatusArchive, read_chunk, verify
from drone_agent.fleet.capacity_store import migrate as migrate_capacity
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import migrate as migrate_operations

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def ledger_with_events(tmp_path: Path) -> tuple[BusinessLedger, dict[int, dict]]:
    """Status rows on three old days (one day split over several chunks) and one recent day, other kinds between.

    三个旧日与一个近日的状态行（其中一日分多块），其间夹着其他类型。
    """
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    migrate_operations(ledger, backups=None)
    migrate_capacity(ledger, backups=None)
    rows = []
    for days_ago, count in ((10, 2500), (9, 30), (8, 7), (1, 40)):
        start = NOW - timedelta(days=days_ago, hours=3)
        for i in range(count):
            at = (start + timedelta(seconds=i)).isoformat()
            rows.append(("dock:dock_a", KIND, "dock:sim", json.dumps({"seq": i, "days_ago": days_ago}), at))
            if i % 100 == 0:
                rows.append((f"mission:m-{days_ago}-{i}:v1", "reservation.reserved", "service",
                             json.dumps({"i": i}), at))
    with ledger.transaction() as db:
        db.executemany("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)", rows)
    original = {row["id"]: row for row in ledger._rows("SELECT * FROM op_events")}
    return ledger, original


def test_only_old_status_rows_move_in_day_bounded_chunks_and_the_record_stays_complete(tmp_path):
    ledger, original = ledger_with_events(tmp_path)
    archive = StatusArchive(ledger, tmp_path / "archive", retention_days=7, chunk_rows=1000, clock=lambda: NOW)
    assert archive.due() == 2500 + 30 + 7
    done = archive.run(max_chunks=100)
    assert [c["rows"] for c in done] == [1000, 1000, 500, 30, 7]
    assert all(c["first_at"][:10] == c["last_at"][:10] for c in done), "a chunk never crosses a day"
    assert len({c["file"] for c in done}) == 3 and archive.step() is None and archive.due() == 0
    left = {row["id"]: row for row in ledger._rows("SELECT * FROM op_events")}
    assert all(row["kind"] != KIND or row["created_at"] >= (NOW - timedelta(days=7)).isoformat()
               for row in left.values())
    assert all(row["kind"] == KIND for row in original.values() if row["id"] not in left), "only status rows left"
    archived = [row for chunk in ledger._rows("SELECT * FROM cap_event_archives ORDER BY chunk_id")
                for row in read_chunk(tmp_path / "archive", chunk)]
    assert len(left) + len(archived) == len(original)
    assert {row["id"]: row for row in archived} == {i: original[i] for i in original if i not in left}
    assert verify(ledger, tmp_path / "archive") == {"status": "passed", "rows": 2537, "problems": []}


def test_a_member_written_but_never_recorded_is_cut_off_before_the_next_chunk(tmp_path):
    ledger, _ = ledger_with_events(tmp_path)
    archive = StatusArchive(ledger, tmp_path / "archive", retention_days=7, chunk_rows=1000, clock=lambda: NOW)
    first = archive.step()
    path = tmp_path / "archive" / first["file"]
    with open(path, "ab") as stream:  # a crash after the write, before the transaction / 写入后、事务前崩溃
        stream.write(b"orphan member that was never recorded")
    record = archive.step()
    assert record["file"] == first["file"] and record["offset"] == first["offset"] + first["length"]
    assert path.stat().st_size == record["offset"] + record["length"]
    assert verify(ledger, tmp_path / "archive")["status"] == "passed"


def test_each_call_reads_a_bounded_range_however_many_newer_rows_there_are(tmp_path):
    """Nothing due among many newer rows costs a handful of rowid lookups, not a scan; then the archive rests.

    大量较新行中没有到期行时，只需少量 rowid 查找而非扫描；之后归档休息。
    """
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    migrate_operations(ledger, backups=None)
    migrate_capacity(ledger, backups=None)
    start = NOW - timedelta(days=7, minutes=30)
    with ledger.transaction() as db:
        db.executemany("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)",
                       [("dock:dock_a", KIND, "dock:sim", "{}", (start + timedelta(seconds=i / 4)).isoformat())
                        for i in range(200_000)])
    archive = StatusArchive(ledger, tmp_path / "archive", retention_days=7, chunk_rows=2000, clock=lambda: NOW)
    # 7,200 rows are due; the 1,200 after the last full chunk wait until their day is over.
    # 7200 行到期；最后一个满块之后的 1200 行等到该日结束。
    assert [c["rows"] for c in archive.run(max_chunks=10)] == [2000, 2000, 2000]
    fresh = StatusArchive(ledger, tmp_path / "archive", retention_days=7, chunk_rows=2000, clock=lambda: NOW)
    steps = []
    ledger._db.set_progress_handler(lambda: steps.append(1), 1000)
    try:
        assert fresh.step() is None
        assert len(steps) < 100, f"{len(steps)} thousand VM steps: the call scanned the newer rows"
        steps.clear()
        assert fresh.step() is None and not steps, "a resting archive does not touch the ledger"
    finally:
        ledger._db.set_progress_handler(None, 0)
    assert fresh.due() == 1200
    assert verify(ledger, tmp_path / "archive") == {"status": "passed", "rows": 6000, "problems": []}


def test_a_changed_archive_is_detected_and_no_retention_is_refused(tmp_path):
    ledger, _ = ledger_with_events(tmp_path)
    StatusArchive(ledger, tmp_path / "archive", retention_days=7, clock=lambda: NOW).run(max_chunks=100)
    path = sorted((tmp_path / "archive").glob("*.gz"))[0]
    data = bytearray(path.read_bytes())
    data[20] ^= 0xFF
    path.write_bytes(bytes(data))
    result = verify(ledger, tmp_path / "archive")
    assert result["status"] == "failed" and "does not match its digest" in result["problems"][0]
    with pytest.raises(ValueError):
        StatusArchive(ledger, tmp_path / "archive", retention_days=0)


def test_sparse_status_rows_never_make_one_call_read_far(tmp_path):
    """A long run of other rows between status rows is crossed a window per call; every due row is still archived.

    状态行之间的大段其他行逐窗口跨过，每次调用都不读很远；到期的行仍全部归档。
    """
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    migrate_operations(ledger, backups=None)
    migrate_capacity(ledger, backups=None)
    start = NOW - timedelta(days=9)
    rows = [("dock:dock_a", KIND, "dock:sim", "{}", (start + timedelta(seconds=i)).isoformat()) for i in range(300)]
    rows += [(f"workflow:wr-{i:020x}", "node.state", "worker", "{}", (start + timedelta(seconds=300 + i / 10))
              .isoformat()) for i in range(100_000)]
    rows += [("dock:dock_a", KIND, "dock:sim", "{}", (start + timedelta(hours=10, seconds=i)).isoformat())
             for i in range(3000)]
    with ledger.transaction() as db:
        db.executemany("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)", rows)
    archive = StatusArchive(ledger, tmp_path / "archive", retention_days=7, chunk_rows=2000, clock=lambda: NOW)
    steps, worst, calls = [], 0, 0
    ledger._db.set_progress_handler(lambda: steps.append(1), 1000)
    try:
        while calls < 30:
            steps.clear()
            record = archive.step()
            worst, calls = max(worst, len(steps)), calls + 1
            if record is None and archive._idle_until:
                break
    finally:
        ledger._db.set_progress_handler(None, 0)
    archived = ledger._one("SELECT COALESCE(SUM(rows), 0) AS n FROM cap_event_archives")["n"]
    assert archived == 3300 and archive.due() == 0
    assert worst < 400, f"{worst} thousand VM steps in one call: it read through the sparse run"
    assert verify(ledger, tmp_path / "archive")["status"] == "passed"
