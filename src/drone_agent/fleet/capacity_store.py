"""WP-P6-07 capacity migration: indexes for the service's hot-path queries and the status archive manifest (D085).

The schema v2 ledger grows with every mission and every dock report, and its hot paths scanned whole tables: the
journal rows of one mission, the newest missions on every dock report, a mission's deliveries, the open deliveries,
the audit rows, a dock's latest released activity, a reservation's holds and a mission's claims. This step adds one
index for each, and the manifest table of the archived `status.accepted` rows. Every object it owns starts with
`cap_`, so the D056-D064 drills, which check their own prefixes, are not disturbed. No existing table or column
changes; a ledger with data is backed up and verified first, and the DDL and its marker commit together.

WP-P6-07 容量迁移：服务热路径查询的索引与状态归档清单（D085）。schema v2 账本随每个任务与每条机场报告增长，其热路径
曾扫描整表：一个任务的账本行、每条机场报告时的最新任务、一个任务的投递、未确认投递、审计行、一个机场最近释放的活动、
一个预约的持有与一个任务的领取。本步骤为每项加一个索引，并加已归档 `status.accepted` 行的清单表。它拥有的每个对象都以
`cap_` 开头，因此按各自前缀核对的 D056–D064 演练不受干扰。不改任何既有表与列；有数据的库先备份并校验，DDL 与其标记一起
提交。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import SCHEMA_VERSION as OPERATIONS_SCHEMA
from drone_agent.fleet.operations_store import TABLES as OPERATIONS_TABLES
from drone_agent.fleet.operations_store import MigrationError, schema_version

CAPACITY_SCHEMA = "1"
# Noise the audit trail skips; the partial index keeps the other rows only. / 审计跳过的噪声；部分索引只保留其余行。
AUDIT_NOISE = ("status.accepted", "lease.taken", "node.state")
_NOISE_SQL = ", ".join(f"'{kind}'" for kind in AUDIT_NOISE)
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS cap_event_archives (chunk_id INTEGER PRIMARY KEY,"
    "kind TEXT NOT NULL, file TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, "
    "first_id INTEGER NOT NULL, last_id INTEGER NOT NULL, rows INTEGER NOT NULL, first_at TEXT NOT NULL, "
    "last_at TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (file, offset))",
    "CREATE INDEX IF NOT EXISTS cap_events_mission ON events (mission_id, mission_version, journal, seq)",
    "CREATE INDEX IF NOT EXISTS cap_missions_created ON missions (created_at)",
    "CREATE INDEX IF NOT EXISTS cap_deliveries_mission ON deliveries (mission_id, cursor)",
    # Only unacknowledged deliveries, in cursor order: serves both the robot's pull and the per-pass package scan.
    # 只含未确认的投递、按游标排序：同时服务机器人拉取与每轮的任务包扫描。
    "CREATE INDEX IF NOT EXISTS cap_deliveries_open ON deliveries (cursor) WHERE acked_at IS NULL",
    f"CREATE INDEX IF NOT EXISTS cap_op_events_audit ON op_events (id) WHERE kind NOT IN ({_NOISE_SQL})",
    "CREATE INDEX IF NOT EXISTS cap_op_reservations_dock ON op_reservations (dock_id, state, updated_at)",
    "CREATE INDEX IF NOT EXISTS cap_op_holds_reservation ON op_holds (reservation_id)",
    "CREATE INDEX IF NOT EXISTS cap_op_claims_mission ON op_claims (mission_id)",
    # Found by the steady-state statement inventory (D085): the active reservations each pass, a mission's evidence,
    # verdicts, facts and issues on each refresh, and a dock's pending and newest actions.
    # 由稳态语句清单找出（D085）：每轮的活动预约，每次刷新读取的任务证据、判定、事实与问题，以及机场待处理与最新的动作。
    "CREATE INDEX IF NOT EXISTS cap_op_reservations_state ON op_reservations (state, created_at)",
    "CREATE INDEX IF NOT EXISTS cap_evidence_mission ON evidence (mission_id, mission_version)",
    "CREATE INDEX IF NOT EXISTS cap_verifications_mission ON verifications (mission_id)",
    "CREATE INDEX IF NOT EXISTS cap_facts_mission ON facts (mission_id)",
    "CREATE INDEX IF NOT EXISTS cap_issues_mission ON issues (mission_id)",
    "CREATE INDEX IF NOT EXISTS cap_op_dock_actions_open ON op_dock_actions (dock_id, state, requested_at)",
    "CREATE INDEX IF NOT EXISTS cap_op_dock_actions_dock ON op_dock_actions (dock_id, requested_at)",
    # Found by replaying every read of a steady window on the 16x ledger (D085): the audit trail's requests,
    # approvals and operator requests read newest first, and a dock's newest reservations.
    # 由在 16 倍账本上重放稳态窗口的全部读取找出（D085）：审计记录按时间倒序读请求、审批与操作请求，以及机场最新的预约。
    "CREATE INDEX IF NOT EXISTS cap_requests_received ON requests (received_at)",
    "CREATE INDEX IF NOT EXISTS cap_versions_updated ON versions (updated_at)",
    # Operator requests only, so the per-pass package scan stays on `cap_deliveries_open`.
    # 只含操作请求，使每轮的任务包扫描仍走 `cap_deliveries_open`。
    "CREATE INDEX IF NOT EXISTS cap_deliveries_requests ON deliveries (created_at) WHERE kind = 'operator_request'",
    "CREATE INDEX IF NOT EXISTS cap_op_reservations_recent ON op_reservations (dock_id, created_at)",
)
TABLES = ("cap_event_archives",)
INDEXES = ("cap_events_mission", "cap_missions_created", "cap_deliveries_mission", "cap_deliveries_open",
           "cap_op_events_audit", "cap_op_reservations_dock", "cap_op_holds_reservation", "cap_op_claims_mission",
           "cap_op_reservations_state", "cap_evidence_mission", "cap_verifications_mission", "cap_facts_mission",
           "cap_issues_mission", "cap_op_dock_actions_open", "cap_op_dock_actions_dock", "cap_requests_received",
           "cap_versions_updated", "cap_deliveries_requests", "cap_op_reservations_recent")
# Indexes on the tables of the workflow, scheduling and business stores: a run's children, outbox and analysis jobs,
# a project's or a finding's jobs, and a robot's assignments by state. On a fresh ledger those tables appear after
# this step, so the step builds the ones whose table exists and `converge` adds the rest at a later start.
# 工作流、调度与业务存储各表上的索引：运行的子运行、出站消息与分析作业，项目或发现的作业，以及机器人按状态的分配。全新账本
# 上这些表在本步骤之后才出现，因此本步骤只建表已存在的，其余由之后的启动经 `converge` 补建。
LATE: dict[str, tuple[str, str]] = {
    "cap_wf_runs_started": ("wf_runs", "CREATE INDEX IF NOT EXISTS cap_wf_runs_started ON wf_runs (started_by, created_at)"),
    "cap_wf_outbox_run": ("wf_outbox", "CREATE INDEX IF NOT EXISTS cap_wf_outbox_run ON wf_outbox (run_id, created_at)"),
    "cap_sc_assignments_state": ("sc_assignments",
                                 "CREATE INDEX IF NOT EXISTS cap_sc_assignments_state ON sc_assignments (state, robot_id)"),
    "cap_bz_jobs_project": ("bz_jobs", "CREATE INDEX IF NOT EXISTS cap_bz_jobs_project ON bz_jobs (project_id, created_at)"),
    "cap_bz_jobs_requester": ("bz_jobs",
                              "CREATE INDEX IF NOT EXISTS cap_bz_jobs_requester ON bz_jobs (requested_by, created_at)"),
    "cap_bz_jobs_finding": ("bz_jobs", "CREATE INDEX IF NOT EXISTS cap_bz_jobs_finding ON bz_jobs (finding_id, created_at)"),
}


def capacity_schema(ledger: BusinessLedger) -> str | None:
    row = ledger._one("SELECT value FROM meta WHERE key='capacity_schema'")
    return row["value"] if row else None


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def _present(ledger: BusinessLedger, kind: str) -> set[str]:
    return {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def late_due(tables: set[str], indexes: set[str]) -> list[str]:
    """The late indexes whose table exists but which are not built yet. / 表已存在但尚未建立的后置索引。"""
    return [name for name, (table, _) in LATE.items() if table in tables and name not in indexes]


def converge(ledger: BusinessLedger) -> list[str]:
    """Build the late indexes of tables that appeared after the capacity step; only indexes, never data (D085).

    为容量步骤之后才出现的表补建后置索引；只建索引，从不改数据（D085）。
    """
    with ledger._lock:
        if capacity_schema(ledger) != CAPACITY_SCHEMA:
            return []
        due = late_due(_present(ledger, "table"), _present(ledger, "index"))
        if due:
            with ledger.transaction() as db:
                for name in due:
                    db.execute(LATE[name][1])
        return due


def migrate(ledger: BusinessLedger, *, backups: Path | None, clock=utcnow) -> dict:
    """Add the capacity indexes and the archive manifest to a schema v2 ledger; a ledger with data is backed up and
    verified first (D085).

    为 schema v2 账本加上容量索引与归档清单；有数据的库先备份并校验（D085）。
    """
    with ledger._lock:
        if schema_version(ledger) != OPERATIONS_SCHEMA:
            raise MigrationError("the capacity step needs the P1 operations schema v2 first")
        missing = [name for name in OPERATIONS_TABLES if name not in _present(ledger, "table")]
        if missing:
            raise MigrationError(f"operations schema is missing tables {missing}")
        version = capacity_schema(ledger)
        if version == CAPACITY_SCHEMA:
            gone = [name for name in (*TABLES, *INDEXES) if name not in _present(ledger, "table") | _present(
                ledger, "index")]
            if gone:
                raise MigrationError(f"capacity schema {CAPACITY_SCHEMA} is missing {gone}")
            return {"status": "current", "capacity_schema": CAPACITY_SCHEMA}
        if version is not None:
            raise MigrationError(f"unexpected capacity schema version {version!r}")
        counts = _counts(ledger._db)
        backup = None
        has_data = any(counts.get(name, 0) for name in ("requests", "missions", "deliveries", "events", "op_events"))
        if ledger.path is not None and has_data:
            if backups is None:
                raise MigrationError("a ledger with data needs a backup directory before migrating")
            stamp = clock().strftime("%Y%m%dT%H%M%S%fZ")
            target = backups / f"ledger-v2-cap-{stamp}.sqlite3"
            ledger.backup(target)
            copy = sqlite3.connect(str(target))
            try:
                integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
                copied = _counts(copy)
            finally:
                copy.close()
            if integrity != "ok" or copied != counts:
                raise MigrationError("the ledger backup did not verify; nothing was migrated")
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            backup = {"path": target.name, "sha256": digest.hexdigest(), "rows": counts}
        try:
            late = late_due(_present(ledger, "table"), _present(ledger, "index"))
            with ledger.transaction() as db:
                for statement in DDL:
                    db.execute(statement)
                for name in late:
                    db.execute(LATE[name][1])
                db.execute("INSERT INTO meta(key, value) VALUES ('capacity_schema', ?)", (CAPACITY_SCHEMA,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('capacity_migrated_at', ?)",
                           (clock().isoformat(),))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('capacity_backup', ?)",
                           (json.dumps(backup, sort_keys=True),))
        except sqlite3.Error as error:
            raise MigrationError(f"capacity schema migration rolled back: {error}") from error
        return {"status": "migrated", "capacity_schema": CAPACITY_SCHEMA, "backup": backup, "rows_before": counts}


def other_digest(path: Path) -> str:
    """SHA-256 over the schema and every row outside the capacity step, streamed: the ledger is never held in memory
    (a dump of the resident desk's ledger would take gigabytes).

    容量步骤以外的模式与每一行的 SHA-256，流式计算：从不把账本整个读入内存（常驻任务台账本的转储要占数 GB）。
    """
    digest = hashlib.sha256()
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        # The step's own objects, including the automatic index of its table. / 本步骤自己的对象，包括其表的自动索引。
        for row in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE substr(name, 1, 4) != 'cap_' "
                              "AND substr(tbl_name, 1, 4) != 'cap_' ORDER BY type, name"):
            digest.update(repr(tuple(row)).encode())
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                               "AND substr(name, 1, 4) != 'cap_' ORDER BY name")]
        for table in tables:
            digest.update(f"\x00{table}\x00".encode())
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                if table == "meta" and str(row[0]).startswith("capacity_"):
                    continue  # the step's own markers / 本步骤自己的标记
                digest.update(repr(row).encode())
    finally:
        db.close()
    return digest.hexdigest()


def drill(path: Path) -> dict:
    """D085 drill on a copy of a ledger: migrate it and prove the other data unchanged; counts only, never content.
    The elapsed time bounds how long the service's own first start will migrate the live ledger.

    在账本副本上做 D085 演练：迁移并证明其他数据不变；只报告计数，从不报告内容。用时给出服务首次启动迁移在用账本所需
    时间的上限依据。
    """
    started = time.monotonic()
    before = other_digest(path)
    ledger = BusinessLedger(path)
    try:
        # What the service does at start: the marked step, then the late indexes. / 与服务启动时相同：带标记的步骤，再补后置索引。
        report = migrate(ledger, backups=path.parent / "backups")
        converged = converge(ledger)
        expected = sorted([*INDEXES, *(name for name, (table, _) in LATE.items() if table in _present(ledger, "table"))])
        with sqlite3.connect(str(path)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                            if row[0].startswith("cap_"))
            indexes = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
                             if row[0].startswith("cap_"))
    finally:
        ledger.close()
    after = other_digest(path)
    return {"status": "passed" if report["status"] in ("migrated", "current") and integrity == "ok"
            and before == after and tables == sorted(TABLES) and indexes == expected else "failed",
            "migration": report["status"], "converged": converged, "capacity_schema": CAPACITY_SCHEMA,
            "integrity": integrity, "other_sha256_before": before, "other_sha256_after": after,
            "cap_tables": len(tables), "cap_indexes": len(indexes), "rows_before": report.get("rows_before"),
            "backup": report.get("backup"), "elapsed_s": round(time.monotonic() - started, 1)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill", type=Path, required=True, help="a copy of the ledger to migrate (D085 drill)")
    args = parser.parse_args()
    print(json.dumps(drill(args.drill)))


if __name__ == "__main__":
    main()
