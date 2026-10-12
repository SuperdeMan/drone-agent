"""Capacity metrics of the mission service: pass and part times, transactions, loop lag, API latency, backlogs (D085).

Durations are kept in memory for a rolling window and reported as percentiles; backlogs are cheap counts refreshed
at most every few seconds. Nothing here writes the ledger or feeds a decision: `capacity.get` reads a snapshot, and
the service appends one snapshot a minute to a per-day JSONL file it keeps for two weeks.

任务服务的容量指标：每轮处理与各部分耗时、事务、事件循环延迟、接口延迟与积压（D085）。耗时在内存中按滚动窗口保存并
报告分位数；积压是便宜的计数，至多每几秒刷新一次。这里不写账本，也不参与任何判定：`capacity.get` 读取一份快照，服务每分钟
把一份快照追加到按日的 JSONL 文件，保留两周。
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from drone_agent.contracts import utcnow

WINDOW_S = 300.0
BACKLOG_EVERY_S = 10.0
LOG_EVERY_S = 60.0
LOG_KEEP_DAYS = 14


def _percentiles(values: list[float]) -> dict:
    ordered = sorted(values)

    def pick(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)], 3)

    return {"n": len(ordered), "p50": pick(0.5), "p95": pick(0.95), "p99": pick(0.99), "max": round(ordered[-1], 3)}


class Capacity:
    """Rolling duration series and the latest backlog counts. / 滚动的耗时序列与最新积压计数。"""

    def __init__(self, *, window_s: float = WINDOW_S, monotonic=time.monotonic):
        self.window_s, self.monotonic = window_s, monotonic
        self.series: dict[str, deque] = defaultdict(deque)
        self.backlog: dict[str, int | float | None] = {}
        self.backlog_at: float | None = None
        self.started = monotonic()
        self.logged_at: float | None = None

    def record(self, name: str, milliseconds: float) -> None:
        now = self.monotonic()
        series = self.series[name]
        series.append((now, milliseconds))
        while series and now - series[0][0] > self.window_s:
            series.popleft()

    def set_backlog(self, counts: dict) -> None:
        self.backlog, self.backlog_at = dict(counts), self.monotonic()

    def backlog_due(self) -> bool:
        return self.backlog_at is None or self.monotonic() - self.backlog_at >= BACKLOG_EVERY_S

    def snapshot(self) -> dict:
        now = self.monotonic()
        series = {}
        for name, values in sorted(self.series.items()):
            kept = [value for at, value in values if now - at <= self.window_s]
            if kept:
                series[name] = _percentiles(kept)
        return {"schema_version": "0.1.0", "at": utcnow().isoformat(), "window_s": self.window_s,
                "uptime_s": round(now - self.started, 1), "series": series, "backlog": dict(self.backlog)}

    def log_due(self) -> bool:
        return self.logged_at is None or self.monotonic() - self.logged_at >= LOG_EVERY_S

    def append_log(self, directory: Path) -> None:
        """Append one snapshot to today's file and drop files older than two weeks. / 追加快照并删除两周前的文件。"""
        self.logged_at = self.monotonic()
        directory.mkdir(parents=True, exist_ok=True)
        today = utcnow()
        with open(directory / f"capacity-{today:%Y%m%d}.jsonl", "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(self.snapshot(), sort_keys=True) + "\n")
        oldest = (today - timedelta(days=LOG_KEEP_DAYS)).strftime("%Y%m%d")
        for path in directory.glob("capacity-*.jsonl"):
            stamp = path.stem.removeprefix("capacity-")
            if stamp.isdigit() and stamp < oldest:
                path.unlink(missing_ok=True)


def backlog_counts(service) -> dict:
    """Cheap queue counts; every query is served by an index or a small table. / 便宜的队列计数，均由索引或小表服务。"""
    ledger = service.ledger

    def count(sql: str, args=()) -> int | None:
        try:
            row = ledger._one(sql, args)
        except Exception:  # a table this deployment does not have / 本部署没有的表
            return None
        value = next(iter(row.values())) if row else None
        return int(value) if value is not None else None

    found = {"refresh_queue": len(service.dirty), "startup_backlog": len(service.backlog),
             "deliveries_open": count("SELECT COUNT(*) AS n FROM deliveries WHERE acked_at IS NULL")}
    if service.ops is not None:
        docks = sorted(service.ops.catalog.docks)
        found["dock_actions_open"] = count(
            f"SELECT COUNT(*) AS n FROM op_dock_actions WHERE dock_id IN ({','.join('?' * len(docks))}) "
            "AND state IN ('requested','acked')", tuple(docks)) if docks else 0
        found["op_events_last_id"] = count("SELECT MAX(id) AS n FROM op_events")
        found["status_archived_rows"] = count("SELECT COALESCE(SUM(rows), 0) AS n FROM cap_event_archives")
    if service.workflows is not None:
        found["outbox_pending"] = count("SELECT COUNT(*) AS n FROM wf_outbox WHERE state='pending'")
    if service.scheduler is not None:
        found["tasks_queued"] = count("SELECT COUNT(*) AS n FROM sc_tasks WHERE state='queued'")
    if service.business is not None:
        found["jobs_runnable"] = count("SELECT COUNT(*) AS n FROM bz_jobs WHERE state IN ('queued','running')")
    if ledger.path is not None:
        found["ledger_bytes"] = sum(p.stat().st_size for p in ledger.path.parent.glob(ledger.path.name + "*")
                                    if p.is_file())
    return found
