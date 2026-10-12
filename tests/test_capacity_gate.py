"""The D085 gate's desk criteria on hand-made receipts. / D085 门禁的任务台判据，用手工回执检验。"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_capacity.py"))
SHA = "a" * 40


def capacity_receipt(**changes) -> dict:
    body = {"status": "ok", "checked_at": "2026-10-12T12:10:00+00:00", "source_sha": SHA,
            "meta": {"capacity_schema": "1"},
            "archive": {"chunks": 800, "rows": 1_600_000, "bytes": 90_000_000, "files": 9, "problems": []},
            "status_rows": 2_200_000, "overdue_rows": 0,
            "startup": {"first": {"startup_backlog": 1300}, "reconciled_uptime_s": 61.2},
            "latest": {"at": "2026-10-12T12:09:00+00:00", "series": {"pass": {"p95": 18.0},
                                                                    "api.docks.report": {"p99": 9.0}},
                       "backlog": {"startup_backlog": 0}}}
    for key, value in changes.items():
        body[key] = value
    return body


def test_the_desk_capacity_receipt_must_show_this_revision_a_readable_archive_and_nothing_overdue():
    assert GATE["desk_capacity"](capacity_receipt(), SHA)["status"] == "passed"
    stale = capacity_receipt()
    stale["latest"] = {**stale["latest"], "at": "2026-10-12T11:40:00+00:00"}
    slow = capacity_receipt()
    slow["latest"] = copy.deepcopy(slow["latest"])
    slow["latest"]["series"]["pass"]["p95"] = 140.0
    for bad in (capacity_receipt(source_sha="b" * 40), capacity_receipt(meta={}), capacity_receipt(overdue_rows=12),
                capacity_receipt(archive={"chunks": 3, "problems": ["chunk 2 does not match its digest"]}),
                capacity_receipt(archive={"chunks": 0, "problems": []}),
                capacity_receipt(startup={"first": None, "reconciled_uptime_s": None}), stale, slow):
        assert GATE["desk_capacity"](bad, SHA)["status"] == "failed"
    assert GATE["desk_capacity"](None, SHA)["status"] == "missing"


def test_the_desk_criterion_adds_the_capacity_migration_its_drill_and_the_retention(monkeypatch):
    base = {"status": "passed", "problems": [], "catalogs": {}, "switch_drill": "passed"}
    monkeypatch.setitem(GATE["desk"].__globals__, "P6R", {"desk": lambda *_: copy.deepcopy(base)})
    activation = {"capacity": {"migration": {"status": "migrated"}, "drill": {"status": "passed"},
                               "status_retention_days": 7}}
    assert GATE["desk"](activation, {}, {}, SHA)["status"] == "passed"
    for capacity in ({"migration": {"status": "skipped"}, "drill": {"status": "passed"}, "status_retention_days": 7},
                     {"migration": {"status": "current"}, "drill": None, "status_retention_days": 7},
                     {"migration": {"status": "current"}, "drill": {"status": "passed"}, "status_retention_days": None}):
        assert GATE["desk"]({"capacity": capacity}, {}, {}, SHA)["status"] == "failed"
