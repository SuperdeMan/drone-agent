"""D085: the audit trail filters the noise in SQL and still pages exactly like a full scan of the rows.

D085：审计记录在 SQL 中过滤噪声，分页结果仍与逐行全扫描完全一致。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from drone_agent.fleet import audit as audit_module
from drone_agent.fleet.audit import FINAL_RUN, NOISE, ProjectAudit
from drone_agent.fleet.capacity_store import migrate as migrate_capacity
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import migrate as migrate_operations


@pytest.fixture
def trail(tmp_path):
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    migrate_operations(ledger, backups=None)
    migrate_capacity(ledger, backups=None)
    rows = []
    for i in range(600):
        at = f"2026-10-12T00:{i // 60:02d}:{i % 60:02d}+00:00"
        rows += [("dock:dock_a", "status.accepted", "dock:sim", "{}", at)] * 5  # the noise / 噪声
        project = "proj_a" if i % 3 else "proj_b"
        if i % 10 == 0:
            rows.append((f"audit:{project}:run-{i}", "run.state", "worker", json.dumps({"to": "running"}), at))
        elif i % 10 == 5:
            rows.append((f"audit:{project}:run-{i}", "run.state", "worker", json.dumps({"to": "completed"}), at))
        else:
            rows.append((f"audit:{project}:x-{i}", "action.requested", "operator", json.dumps({"i": i}), at))
    with ledger.transaction() as db:
        db.executemany("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)", rows)
    service = SimpleNamespace(ledger=ledger, ops=SimpleNamespace(catalog=SimpleNamespace(docks={}, sites={})))
    yield ProjectAudit(service), ledger
    ledger.close()


def full_scan(ledger, project: str) -> list[int]:
    found = []
    for row in ledger._rows("SELECT * FROM op_events ORDER BY id DESC"):
        if row["kind"] in NOISE:
            continue
        if row["kind"] == "run.state" and json.loads(row["body"]).get("to") not in FINAL_RUN:
            continue
        if row["subject"].split(":")[1] == project:
            found.append(row["id"])
    return found


def pages(audit: ProjectAudit, project: str, limit: int) -> list[int]:
    collected, before = [], None
    for _ in range(10_000):
        found, before = audit._events(project, before, limit)
        collected += [entry["event_id"] for entry in found]
        if before is None:
            return collected
    raise AssertionError("paging did not end")


@pytest.mark.parametrize("window", [20_000, 37])
@pytest.mark.parametrize("limit", [1, 7, 50])
def test_pages_equal_a_full_scan_whatever_the_page_size_and_window(trail, monkeypatch, limit, window):
    audit, ledger = trail
    monkeypatch.setattr(audit_module, "EVENT_WINDOW", window)
    for project in ("proj_a", "proj_b"):
        expected = full_scan(ledger, project)
        assert expected and pages(audit, project, limit) == expected
