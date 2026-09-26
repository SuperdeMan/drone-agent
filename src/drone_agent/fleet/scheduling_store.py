"""P3 scheduling tables on the mission ledger: the scheduling extension migration and every scheduling read and write (D060).

The five `sc_` tables live in the same SQLite file as the v1 ledger, the P1 operations tables and the P2 workflow
tables, so a task, its assignment, the mission the assignment creates and the P1 holds of that mission's robot and
airspace cells commit in one transaction. The migration is additive and keeps `schema_version = 2` and
`workflow_schema`: the extension has its own `scheduling_schema` key, so the P2 build still starts on a migrated
ledger. A ledger with data is backed up and checked first; the DDL and the marker commit together or not at all.
Task changes are compare-and-set on `state_version`; `sc_live_assignment` keeps at most one active assignment per
task, whoever races.

任务账本上的 P3 调度表：调度扩展迁移与全部调度读写（D060）。

五张 `sc_` 表与 v1 账本、P1 运营表、P2 工作流表位于同一个 SQLite 文件，使任务、其分配、分配创建的任务，以及该任务的
机器人与空域单元的 P1 持有在一个事务中提交。迁移是增量的，保持 `schema_version = 2` 与 `workflow_schema`：扩展有自己的
`scheduling_schema` 键，因此 P2 版本仍能在迁移后的账本上启动。已有数据的账本先备份并校验；DDL 与标记一起提交或完全不
提交。任务变更对 `state_version` 做比较并交换；无论谁在竞争，`sc_live_assignment` 保证每个任务至多一个有效分配。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import SCHEMA_VERSION as OPERATIONS_SCHEMA
from drone_agent.fleet.operations_store import TABLES as OPERATIONS_TABLES
from drone_agent.fleet.operations_store import MigrationError, schema_version
from drone_agent.fleet.scheduling_models import CANCELLING_TASK, TERMINAL_TASK, SchedulingCatalog, TaskState

SCHEDULING_SCHEMA = "1"
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS sc_catalogs (catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, "
    "body TEXT NOT NULL, loaded_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sc_tasks (task_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, "
    "idempotency_key TEXT NOT NULL, project_id TEXT NOT NULL, asset_id TEXT NOT NULL, volume_id TEXT NOT NULL, "
    "candidates TEXT NOT NULL, priority INTEGER NOT NULL, not_after TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, "
    "source TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, epoch INTEGER NOT NULL, robot_id TEXT, "
    "mission_id TEXT, excluded TEXT NOT NULL, reason TEXT, detail TEXT, cancel TEXT, outcome TEXT, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (requested_by, idempotency_key))",
    "CREATE INDEX IF NOT EXISTS sc_tasks_state ON sc_tasks (state, priority, created_at)",
    "CREATE INDEX IF NOT EXISTS sc_tasks_project ON sc_tasks (project_id, created_at)",
    "CREATE TABLE IF NOT EXISTS sc_assignments (task_id TEXT NOT NULL, epoch INTEGER NOT NULL, robot_id TEXT NOT NULL, "
    "mission_id TEXT, decision_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, blocked_since TEXT, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (task_id, epoch))",
    "CREATE UNIQUE INDEX IF NOT EXISTS sc_live_assignment ON sc_assignments (task_id) WHERE state = 'active'",
    "CREATE UNIQUE INDEX IF NOT EXISTS sc_assignment_mission ON sc_assignments (mission_id) "
    "WHERE mission_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS sc_assignments_robot ON sc_assignments (robot_id, created_at)",
    "CREATE TABLE IF NOT EXISTS sc_decisions (decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
    "verdict TEXT NOT NULL, robot_id TEXT, epoch INTEGER NOT NULL, snapshot_sha256 TEXT NOT NULL, "
    "policy_version TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS sc_decisions_task ON sc_decisions (task_id, created_at)",
    "CREATE TABLE IF NOT EXISTS sc_footprints (activity_key TEXT PRIMARY KEY, mission_id TEXT NOT NULL, "
    "mission_version INTEGER NOT NULL, robot_id TEXT NOT NULL, frame TEXT NOT NULL, cells TEXT NOT NULL, "
    "box TEXT NOT NULL, volume_box TEXT NOT NULL, created_at TEXT NOT NULL)",
)
TABLES = ("sc_catalogs", "sc_tasks", "sc_assignments", "sc_decisions", "sc_footprints")
INDEXES = ("sc_tasks_state", "sc_tasks_project", "sc_live_assignment", "sc_assignment_mission",
           "sc_assignments_robot", "sc_decisions_task")


class StaleTask(RuntimeError):
    """A task changed since it was read; the caller re-reads and decides again. / 任务在读取后已变化；调用方重读后再决定。"""


def _dump(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(text):
    return None if text is None else json.loads(text)


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def scheduling_schema(ledger: BusinessLedger) -> str | None:
    row = ledger._one("SELECT value FROM meta WHERE key='scheduling_schema'")
    return row["value"] if row else None


def migrate(ledger: BusinessLedger, *, backups: Path | None, statements: tuple[str, ...] = DDL,
            clock=utcnow) -> dict:
    """Add the scheduling extension to a schema v2 ledger; back up and verify a database with data first (D060).

    为 schema v2 账本加上调度扩展；已有数据的库先备份并校验（D060）。
    """
    with ledger._lock:
        if schema_version(ledger) != OPERATIONS_SCHEMA:
            raise MigrationError("the scheduling extension needs the P1 operations schema v2 first")
        present = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in OPERATIONS_TABLES if name not in present]
        if missing:
            raise MigrationError(f"operations schema is missing tables {missing}")
        version = scheduling_schema(ledger)
        if version == SCHEDULING_SCHEMA:
            missing = [name for name in TABLES if name not in present]
            if missing:
                raise MigrationError(f"scheduling schema {SCHEDULING_SCHEMA} is missing tables {missing}")
            return {"status": "current", "scheduling_schema": SCHEDULING_SCHEMA}
        if version is not None:
            raise MigrationError(f"unexpected scheduling schema version {version!r}")
        counts = _counts(ledger._db)
        backup = None
        has_data = any(counts.get(name, 0) for name in ("requests", "missions", "deliveries", "events"))
        if ledger.path is not None and has_data:
            if backups is None:
                raise MigrationError("a ledger with data needs a backup directory before migrating")
            stamp = clock().strftime("%Y%m%dT%H%M%S%fZ")
            target = backups / f"ledger-v2-sc-{stamp}.sqlite3"
            ledger.backup(target)
            copy = sqlite3.connect(str(target))
            try:
                integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
                copied = _counts(copy)
            finally:
                copy.close()
            if integrity != "ok" or copied != counts:
                raise MigrationError("the ledger backup did not verify; nothing was migrated")
            backup = {"path": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                      "rows": counts}
        try:
            with ledger.transaction() as db:
                for statement in statements:
                    db.execute(statement)
                db.execute("INSERT INTO meta(key, value) VALUES ('scheduling_schema', ?)", (SCHEDULING_SCHEMA,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('scheduling_migrated_at', ?)",
                           (clock().isoformat(),))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('scheduling_backup', ?)",
                           (json.dumps(backup, sort_keys=True),))
        except sqlite3.Error as error:
            raise MigrationError(f"scheduling schema migration rolled back: {error}") from error
        return {"status": "migrated", "scheduling_schema": SCHEDULING_SCHEMA, "backup": backup, "rows_before": counts}


_SCHEDULING_LINE = re.compile(r'^(CREATE (TABLE|UNIQUE INDEX|INDEX) (IF NOT EXISTS )?"?sc_|INSERT INTO "sc_)')


def other_lines(path: Path) -> list[str]:
    """The SQL dump of everything but the scheduling extension. / 除调度扩展以外的 SQL 转储。"""
    with sqlite3.connect(str(path)) as db:
        dump = list(db.iterdump())
    return [line for line in dump if not _SCHEDULING_LINE.match(line)
            and not (line.startswith('INSERT INTO "meta"') and "'scheduling_" in line)]


def drill(path: Path) -> dict:
    """D060 drill on a copy of a ledger: migrate it and prove the other data unchanged; counts only, never content.

    在账本副本上做 D060 演练：迁移并证明其他数据不变；只报告计数，从不报告内容。
    """

    def dump_digest(target: Path) -> str:
        return hashlib.sha256("\n".join(other_lines(target)).encode()).hexdigest()

    before = dump_digest(path)
    ledger = BusinessLedger(path)
    try:
        report = migrate(ledger, backups=path.parent / "backups")
        with sqlite3.connect(str(path)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                            if row[0].startswith("sc_"))
            indexes = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
                             if row[0].startswith("sc_"))
    finally:
        ledger.close()
    after = dump_digest(path)
    return {"status": "passed" if report["status"] in ("migrated", "current") and integrity == "ok"
            and before == after and tables == sorted(TABLES) and indexes == sorted(INDEXES) else "failed",
            "migration": report["status"], "scheduling_schema": SCHEDULING_SCHEMA, "integrity": integrity,
            "other_dump_sha256_before": before, "other_dump_sha256_after": after, "sc_tables": len(tables),
            "sc_indexes": len(indexes), "rows_before": report.get("rows_before"), "backup": report.get("backup")}


class SchedulingStore:
    """Reads and writes of the scheduling tables; callers group writes with `transaction()`.

    调度表的读写；调用方用 `transaction()` 组合写入。
    """

    def __init__(self, ledger: BusinessLedger, catalog: SchedulingCatalog, *, clock=utcnow):
        if schema_version(ledger) != OPERATIONS_SCHEMA or scheduling_schema(ledger) != SCHEDULING_SCHEMA:
            raise MigrationError("run migrate() before opening the scheduling store")
        self.ledger, self.catalog, self.clock = ledger, catalog, clock
        self.ledger._exec("INSERT OR IGNORE INTO sc_catalogs VALUES (?,?,?,?)",
                          (catalog.sha256, catalog.catalog_id, _dump(catalog), self._now()))

    def transaction(self):
        return self.ledger.transaction()

    def _now(self) -> str:
        return self.clock().isoformat()

    def _rows(self, sql: str, args=()) -> list[dict]:
        return self.ledger._rows(sql, args)

    def _one(self, sql: str, args=()) -> dict | None:
        return self.ledger._one(sql, args)

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        return self.ledger._exec(sql, args)

    def event(self, subject: str, kind: str, actor: str, body: dict | None = None) -> None:
        """Scheduling audit rows share the P1 audit stream. / 调度审计行与 P1 审计流共用。"""
        self._exec("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)",
                   (subject, kind, actor, _dump(body or {}), self._now()))

    def events(self, subject: str, limit: int = 200) -> list[dict]:
        rows = self._rows("SELECT * FROM op_events WHERE subject=? ORDER BY id DESC LIMIT ?", (subject, limit))
        return [{**row, "body": _load(row["body"])} for row in reversed(rows)]

    # ── tasks / 任务 ──

    @staticmethod
    def _task(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "candidates": _load(row["candidates"]), "excluded": _load(row["excluded"]),
                "detail": _load(row["detail"]), "cancel": _load(row["cancel"]), "outcome": _load(row["outcome"])}

    def task(self, task_id: str) -> dict | None:
        return self._task(self._one("SELECT * FROM sc_tasks WHERE task_id=?", (task_id,)))

    def task_by_key(self, requested_by: str, key: str) -> dict | None:
        return self._task(self._one("SELECT * FROM sc_tasks WHERE requested_by=? AND idempotency_key=?",
                                    (requested_by, key)))

    def tasks(self, *, project_id: str | None = None, states: tuple[str, ...] | None = None,
              requested_by: str | None = None, limit: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM sc_tasks WHERE 1=1", []
        if project_id is not None:
            sql, args = sql + " AND project_id=?", [*args, project_id]
        if requested_by is not None:
            sql, args = sql + " AND requested_by=?", [*args, requested_by]
        if states is not None:
            sql, args = sql + f" AND state IN ({','.join('?' * len(states))})", [*args, *states]
        sql += " ORDER BY created_at, task_id"
        if limit is not None:
            sql, args = sql + " LIMIT ?", [*args, limit]
        return [self._task(row) for row in self._rows(sql, args)]

    def queue(self) -> list[dict]:
        """Queued tasks in the order the scheduler serves them. / 按调度顺序排列的排队任务。"""
        return [self._task(row) for row in self._rows(
            "SELECT * FROM sc_tasks WHERE state='queued' ORDER BY priority DESC, created_at, task_id")]

    def create_task(self, *, requested_by: str, key: str, project_id: str, asset_id: str, volume_id: str,
                    candidates: list[str], priority: int, not_after: str, source: str,
                    guard=None) -> tuple[dict | None, bool]:
        """One task per (requester, idempotency key); a repeat returns the first. `guard` may refuse inside the
        transaction (a workflow outbox claim that is no longer ours).

        每个（请求者，幂等键）一个任务；重复返回第一次的任务。`guard` 可在事务内拒绝（不再属于我方的工作流 outbox 领取）。
        """
        with self.transaction() as db:
            known = self.task_by_key(requested_by, key)
            if known is not None:
                return known, False
            if guard is not None and not guard(db):
                return None, False
            task_id = "tk-" + uuid.uuid4().hex[:20]
            now = self._now()
            self._exec("INSERT INTO sc_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (task_id, requested_by, key, project_id, asset_id, volume_id, _dump(list(candidates)),
                        int(priority), not_after, self.catalog.sha256, source, TaskState.QUEUED.value, 1, 0, None,
                        None, _dump([]), None, None, None, None, now, now))
            self.event(f"task:{task_id}", "task.created", requested_by,
                       {"project_id": project_id, "asset_id": asset_id, "volume_id": volume_id,
                        "candidates": list(candidates), "priority": int(priority), "not_after": not_after,
                        "source": source, "idempotency_key": key})
        return self.task(task_id), True

    def update_task(self, task_id: str, version: int, *, actor: str, **values) -> dict:
        """Compare-and-set one task; a persisted cancel is never undone by ordinary progress.

        对一个任务做比较并交换；普通推进从不撤销已持久化的取消。
        """
        with self.transaction():
            current = self.task(task_id)
            if current is None or current["state_version"] != version:
                raise StaleTask(f"{task_id} changed since version {version}")
            state = values.get("state")
            if state is not None:
                before = TaskState(current["state"])
                after = TaskState(state)
                if before in TERMINAL_TASK:
                    raise StaleTask(f"{task_id} is already {before.value}")
                if before in CANCELLING_TASK and after not in (TaskState.CANCELLING, TaskState.CANCELLED):
                    raise StaleTask(f"{task_id} is {before.value}; only the cancel may progress")
            columns, args = [], []
            for key, value in values.items():
                columns.append(f"{key}=?")
                args.append(_dump(value) if key in ("excluded", "detail", "cancel", "outcome") and value is not None
                            else value)
            columns += ["state_version=state_version+1", "updated_at=?"]
            args += [self._now(), task_id, version]
            changed = self._exec(f"UPDATE sc_tasks SET {', '.join(columns)} WHERE task_id=? AND state_version=?",
                                 args).rowcount
            if changed != 1:
                raise StaleTask(f"{task_id} changed since version {version}")
            if state is not None and state != current["state"]:
                self.event(f"task:{task_id}", "task.state", actor,
                           {"from": current["state"], "to": state, "epoch": values.get("epoch", current["epoch"]),
                            "reason": values.get("reason")})
        return self.task(task_id)

    # ── assignments / 分配 ──

    def assignment(self, task_id: str, epoch: int) -> dict | None:
        return self._one("SELECT * FROM sc_assignments WHERE task_id=? AND epoch=?", (task_id, epoch))

    def assignments(self, task_id: str | None = None, *, states: tuple[str, ...] | None = None,
                    robot_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM sc_assignments WHERE 1=1", []
        if task_id is not None:
            sql, args = sql + " AND task_id=?", [*args, task_id]
        if robot_id is not None:
            sql, args = sql + " AND robot_id=?", [*args, robot_id]
        if states is not None:
            sql, args = sql + f" AND state IN ({','.join('?' * len(states))})", [*args, *states]
        return self._rows(sql + " ORDER BY created_at, task_id, epoch", args)

    def assignment_of(self, mission_id: str) -> dict | None:
        return self._one("SELECT * FROM sc_assignments WHERE mission_id=?", (mission_id,))

    def live(self, task_id: str) -> dict | None:
        return self._one("SELECT * FROM sc_assignments WHERE task_id=? AND state='active'", (task_id,))

    def insert_assignment(self, *, task_id: str, epoch: int, robot_id: str, mission_id: str | None,
                          decision_id: str, state: str = "active", reason: str | None = None) -> None:
        """Insert inside the caller's transaction; the unique index refuses a second active one.

        在调用方事务内插入；唯一索引拒绝第二个有效分配。
        """
        now = self._now()
        self._exec("INSERT INTO sc_assignments VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (task_id, epoch, robot_id, mission_id, decision_id, state, reason, None, now, now))
        self.event(f"task:{task_id}", "assignment.created", "scheduler",
                   {"epoch": epoch, "robot_id": robot_id, "mission_id": mission_id, "state": state,
                    "reason": reason, "decision_id": decision_id})

    def end_assignment(self, task_id: str, epoch: int, *, state: str, reason: str) -> bool:
        changed = self._exec("UPDATE sc_assignments SET state=?, reason=?, updated_at=? WHERE task_id=? AND epoch=? "
                             "AND state='active'", (state, reason, self._now(), task_id, epoch)).rowcount == 1
        if changed:
            self.event(f"task:{task_id}", f"assignment.{state}", "scheduler", {"epoch": epoch, "reason": reason})
        return changed

    def mark_blocked(self, task_id: str, epoch: int, since: str | None) -> None:
        self._exec("UPDATE sc_assignments SET blocked_since=? WHERE task_id=? AND epoch=? AND state='active'",
                   (since, task_id, epoch))

    def usage(self, robot_id: str, since: str) -> int:
        """Assignments of the robot that created a mission since `since`. / 自 `since` 起该机器人创建了任务的分配数。"""
        row = self._one("SELECT COUNT(*) AS n FROM sc_assignments WHERE robot_id=? AND mission_id IS NOT NULL "
                        "AND created_at >= ?", (robot_id, since))
        return int(row["n"]) if row else 0

    # ── decisions / 判定 ──

    def record_decision(self, task_id: str, decision: dict, *, epoch: int) -> str:
        decision_id = "sd-" + uuid.uuid4().hex[:20]
        self._exec("INSERT INTO sc_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                   (decision_id, task_id, decision["verdict"], decision.get("robot_id"), epoch,
                    decision["snapshot_sha256"], decision["policy_version"], _dump(decision), self._now()))
        return decision_id

    def decisions(self, task_id: str, limit: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM sc_decisions WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                          (task_id, limit))
        return [{**row, "body": _load(row["body"])} for row in reversed(rows)]

    def last_decision(self, task_id: str) -> dict | None:
        row = self._one("SELECT * FROM sc_decisions WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                        (task_id,))
        return None if row is None else {**row, "body": _load(row["body"])}

    # ── footprints / 航迹覆盖 ──

    def record_footprint(self, activity: str, *, mission_id: str, version: int, robot_id: str, body: dict) -> bool:
        return self._exec("INSERT OR IGNORE INTO sc_footprints VALUES (?,?,?,?,?,?,?,?,?)",
                          (activity, mission_id, version, robot_id, body["frame"], _dump(body["cells"]),
                           _dump(body["box"]), _dump(body["volume_box"]), self._now())).rowcount == 1

    def footprint(self, activity: str) -> dict | None:
        row = self._one("SELECT * FROM sc_footprints WHERE activity_key=?", (activity,))
        if row is None:
            return None
        return {**row, "cells": _load(row["cells"]), "box": _load(row["box"]), "volume_box": _load(row["volume_box"])}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill", type=Path, required=True, help="a copy of the ledger to migrate (D060 drill)")
    args = parser.parse_args()
    result = drill(args.drill)
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
