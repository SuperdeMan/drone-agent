"""D085 capacity metrics: rolling windows, the snapshot log, `capacity.get` for project admins, and the status archive
inside a running pass.

D085 容量指标：滚动窗口、快照日志、仅项目管理员可读的 `capacity.get`，以及运行中每轮处理里的状态归档。
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

from drone_agent.eval.p3_world import ADMIN, VIEWER, P3World
from drone_agent.fleet.archive import StatusArchive, verify
from drone_agent.fleet.capacity import Capacity

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_window_keeps_recent_durations_and_reports_percentiles():
    clock = Clock()
    capacity = Capacity(window_s=60, monotonic=clock)
    for value in range(1, 101):
        capacity.record("pass", float(value))
    series = capacity.snapshot()["series"]["pass"]
    assert series == {"n": 100, "p50": 50.0, "p95": 95.0, "p99": 99.0, "max": 100.0}
    clock.now += 61
    capacity.record("pass", 7.0)
    assert capacity.snapshot()["series"]["pass"] == {"n": 1, "p50": 7.0, "p95": 7.0, "p99": 7.0, "max": 7.0}


def test_the_snapshot_log_appends_a_line_and_keeps_two_weeks(tmp_path):
    capacity = Capacity()
    capacity.record("pass", 3.0)
    (tmp_path / "capacity-20000101.jsonl").write_text("{}\n", encoding="utf-8")
    capacity.append_log(tmp_path)
    files = sorted(p.name for p in tmp_path.glob("capacity-*.jsonl"))
    assert len(files) == 1 and files[0] != "capacity-20000101.jsonl"
    line = json.loads((tmp_path / files[0]).read_text(encoding="utf-8").splitlines()[-1])
    assert line["series"]["pass"]["n"] == 1 and not capacity.log_due()


def test_admins_read_the_capacity_and_the_pass_archives_status_rows_while_docks_report(tmp_path):
    w = P3World(tmp_path / "case", ROOT, seed=7)

    async def scenario():
        await w.settle()
        # A retention of under a tenth of a second archives while the docks keep reporting; the fake monotonic clock
        # keeps the archive from resting between passes. / 不到十分之一秒的保留期，使机场持续报告期间就开始归档；假的单调
        # 时钟使归档在两轮之间不休息。
        ticks = itertools.count(0, 60)
        w.service.status_archive = StatusArchive(w.ledger, tmp_path / "archive", retention_days=1e-6, chunk_rows=5,
                                                 monotonic=lambda: next(ticks))
        stop = asyncio.Event()
        probe = asyncio.create_task(w.service._probe_loop(stop, interval_s=0.05))
        for _ in range(8):
            await w.step()
            await w.service._pass(stop)
        stop.set()
        await probe
        first = await w.api(ADMIN, "capacity.get", project_id="campus_ops")
        second = await w.api(ADMIN, "capacity.get", project_id="campus_ops")
        denied = await w.api(VIEWER, "capacity.get", project_id="campus_ops")
        return first, second, denied

    try:
        first, second, denied = asyncio.run(scenario())
        assert first["ok"] and denied["issue"]["code"] == "auth.project_denied"
        series = second["result"]["series"]
        assert {"pass", "pass.operations", "pass.scheduler", "pass.archive", "transaction", "loop_lag",
                "api.capacity.get"} <= set(series)
        backlog = second["result"]["backlog"]
        assert backlog["deliveries_open"] is not None and backlog["status_archived_rows"] > 0
        assert second["result"]["status_retention"]["days"] == 1e-6
        result = verify(w.ledger, tmp_path / "archive")
        # The backlog counts refresh at most every ten seconds; the archive kept going. / 积压计数至多每十秒刷新；归档仍在继续。
        assert result["status"] == "passed" and result["rows"] >= backlog["status_archived_rows"]
        archived = {row["first_id"] for row in w.ledger._rows("SELECT first_id FROM cap_event_archives")}
        kept = {row["id"] for row in w.ledger._rows("SELECT id FROM op_events WHERE kind='status.accepted'")}
        assert archived and not archived & kept
    finally:
        w.ledger.close()
