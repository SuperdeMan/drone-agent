"""The desk's read-only capacity status (D085): archive chunks read back, overdue rows, and the startup reconciliation.

任务台的只读容量状态（D085）：读回核对的归档块、逾期行与启动核对用时。
"""

from __future__ import annotations

import json
import os
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from drone_agent.fleet.archive import KIND, StatusArchive
from drone_agent.fleet.capacity_store import migrate as migrate_capacity
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import migrate as migrate_operations

ROOT = Path(__file__).resolve().parents[2]
DESK = runpy.run_path(str(ROOT / "scripts/remote_desk.py"))


def desk_root(tmp_path: Path) -> Path:
    service = tmp_path / "desk/service"
    ledger = BusinessLedger(service / "ledger.sqlite3")
    migrate_operations(ledger, backups=None)
    migrate_capacity(ledger, backups=None)
    now = datetime.now(UTC)
    with ledger.transaction() as db:
        db.executemany("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)",
                       [("dock:dock_fa", KIND, "dock:sim", "{}", (now - timedelta(days=9) + timedelta(seconds=i))
                         .isoformat()) for i in range(3000)]
                       + [("dock:dock_fa", KIND, "dock:sim", "{}", (now - timedelta(hours=1)).isoformat())])
    StatusArchive(ledger, service / "archive/op_events", retention_days=7, chunk_rows=1000).run(max_chunks=2)
    ledger.close()
    (service / "ready.json").write_text(json.dumps({"source_sha": "a" * 40, "capacity": {
        "migration": {"status": "migrated"}, "status_retention_days": 7}}), encoding="utf-8")
    started = datetime.fromtimestamp((service / "ready.json").stat().st_mtime, UTC)
    (service / "capacity").mkdir()
    lines = [{"at": (started + timedelta(seconds=s)).isoformat(), "uptime_s": s, "series": {},
              "backlog": {"startup_backlog": left}} for s, left in ((1, 900), (61, 300), (121, 0), (181, 0))]
    (service / "capacity/capacity-20261012.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines),
                                                               encoding="utf-8")
    return tmp_path


def test_the_probe_reads_back_every_chunk_and_counts_what_is_overdue(tmp_path):
    root = desk_root(tmp_path)
    found = DESK["capacity"](root)
    assert found["meta"]["capacity_schema"] == "1" and found["source_sha"] == "a" * 40
    assert found["archive"]["chunks"] == 2 and found["archive"]["rows"] == 2000 and not found["archive"]["problems"]
    assert found["status_rows"] == 1001 and found["overdue_rows"] == 1000, "1,000 old rows are still to be archived"
    assert found["startup"] == {"first": {"startup_backlog": 900}, "reconciled_uptime_s": 121}
    assert found["latest"]["uptime_s"] == 181


def test_a_changed_chunk_is_reported(tmp_path):
    root = desk_root(tmp_path)
    path = next((root / "desk/service/archive/op_events").glob("*.gz"))
    data = bytearray(path.read_bytes())
    data[30] ^= 0xFF
    path.write_bytes(bytes(data))
    os.utime(root / "desk/service/ready.json")
    assert "does not match its digest" in DESK["capacity"](root)["archive"]["problems"][0]


def test_the_restore_drill_migrates_an_older_backup_before_the_switch_drill(tmp_path, monkeypatch):
    import hashlib
    import sqlite3
    from types import SimpleNamespace

    desk = runpy.run_path(str(ROOT / "scripts/desk_supervisor.py"))["Desk"](tmp_path)
    backup = desk.base / "backups" / "20261005T000000Z-00000000"
    backup.mkdir(parents=True)
    with sqlite3.connect(str(backup / "ledger.sqlite3")) as db:
        db.execute("CREATE TABLE missions (mission_id TEXT)")
        db.execute("INSERT INTO missions VALUES ('m-000000000001')")
    record = {"backup_id": backup.name, "catalogs": {"operations": {"catalog_sha256": "c" * 64}},
              "ledger": {"sha256": hashlib.sha256((backup / "ledger.sqlite3").read_bytes()).hexdigest(),
                         "tables": {"missions": 1}}}
    (backup / "backup.json").write_text(json.dumps(record), encoding="utf-8")
    (desk.base / "current.json").write_text(json.dumps({"images": {"ground": "ground:image"}}), encoding="utf-8")
    desk.secrets.mkdir(parents=True)
    (desk.secrets / "members.yaml").write_text("members: []\n", encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        module = argv[argv.index("-m") + 1]
        calls.append(module)
        body = ({"status": "passed", "migration": "migrated", "elapsed_s": 2.0}
                if module.endswith(("capacity_store", "asset_store"))
                else {"status": "passed", "catalogs": {"operations": ["p5_desk_v1", "c" * 64]}})
        return SimpleNamespace(stdout=(json.dumps(body) + "\n").encode(), returncode=0)

    monkeypatch.setattr(DESK["subprocess"], "run", run)
    monkeypatch.setattr(DESK["os"], "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(DESK["os"], "getgid", lambda: 1000, raising=False)
    receipt = DESK["restore_drill"](tmp_path, {"run_id": "20261012T000000Z-11111111"})
    # D085, then D087, then the switch drill, as the service would on a real restore. / 依次 D085、D087、目录切换演练。
    assert calls == ["drone_agent.fleet.capacity_store", "drone_agent.fleet.asset_store",
                     "drone_agent.fleet.switch_drill"]
    assert receipt["status"] == "passed" and receipt["capacity_drill"]["migration"] == "migrated"
    assert receipt["asset_drill"]["migration"] == "migrated"
