"""P1 operations tables on the mission ledger: the schema v2 migration and every operations read and write (D056).

The tables live in the same SQLite file as the v1 ledger so that reservations, delivery claims, cancellations and
the existing deliveries share one transaction boundary. The migration is additive: v1 tables, columns and rows are
never touched, a database that already holds data is backed up and checked first, and the DDL plus the version
bump commit together or not at all. Multi-statement writes run inside `ledger.transaction()`; the exclusive-hold
index makes a double reservation impossible even if two callers race.

任务账本上的 P1 运营表：schema v2 迁移与全部运营读写（D056）。

这些表与 v1 账本位于同一个 SQLite 文件，使预约、交付领取、取消与既有投递共用一个事务边界。迁移是增量的：
从不改动 v1 的表、列与行；已有数据的库先备份并校验；DDL 与版本号一起提交或完全不提交。多语句写入在
`ledger.transaction()` 内执行；独占持有索引使两个并发调用方也不可能重复预约。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.resources import (
    DockLock,
    DockStatus,
    OperationsCatalog,
    ReservationState,
    ResourceReservation,
)

SCHEMA_VERSION = "2"
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS op_catalogs (catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, "
    "body TEXT NOT NULL, loaded_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS op_bindings (mission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
    "site_id TEXT NOT NULL, dock_id TEXT NOT NULL, robot_id TEXT NOT NULL, execution_backend TEXT NOT NULL, "
    "catalog_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS op_bindings_project ON op_bindings (project_id, created_at)",
    "CREATE TABLE IF NOT EXISTS op_dock_state (dock_id TEXT PRIMARY KEY, boot_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "session TEXT NOT NULL, complete_reports INTEGER NOT NULL, source TEXT NOT NULL, report TEXT NOT NULL, "
    "observed_at TEXT NOT NULL, received_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS op_dock_sessions (dock_id TEXT NOT NULL, boot_id TEXT NOT NULL, "
    "first_seen TEXT NOT NULL, PRIMARY KEY (dock_id, boot_id))",
    "CREATE TABLE IF NOT EXISTS op_dock_locks (dock_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, "
    "set_by TEXT NOT NULL, set_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS op_dock_actions (action_id TEXT PRIMARY KEY, dock_id TEXT NOT NULL, "
    "kind TEXT NOT NULL, activity_key TEXT NOT NULL, state TEXT NOT NULL, requested_at TEXT NOT NULL, "
    "acked_at TEXT, ack_accepted INTEGER, ack_reason TEXT, result_at TEXT, UNIQUE (activity_key, kind))",
    "CREATE TABLE IF NOT EXISTS op_reservations (reservation_id TEXT PRIMARY KEY, activity_key TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, robot_id TEXT NOT NULL, "
    "dock_id TEXT NOT NULL, state TEXT NOT NULL, expires_at TEXT, reason TEXT NOT NULL, evidence TEXT, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS op_reservations_mission ON op_reservations (mission_id, mission_version)",
    "CREATE TABLE IF NOT EXISTS op_holds (resource_id TEXT NOT NULL, reservation_id TEXT NOT NULL, "
    "active INTEGER NOT NULL, PRIMARY KEY (resource_id, reservation_id))",
    "CREATE UNIQUE INDEX IF NOT EXISTS op_exclusive_hold ON op_holds (resource_id) WHERE active = 1",
    "CREATE TABLE IF NOT EXISTS op_claims (delivery_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, "
    "mission_version INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, decision_id TEXT, "
    "decided_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS op_decisions (decision_id TEXT PRIMARY KEY, mission_id TEXT, mission_version INTEGER, "
    "robot_id TEXT NOT NULL, stage TEXT NOT NULL, verdict TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS op_decisions_mission ON op_decisions (mission_id, created_at)",
    "CREATE TABLE IF NOT EXISTS op_cancellations (mission_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, "
    "request_id TEXT NOT NULL, reason TEXT NOT NULL, requested_at TEXT NOT NULL, relayed_request_id TEXT)",
    "CREATE TABLE IF NOT EXISTS op_events (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, "
    "kind TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS op_events_subject ON op_events (subject, id)",
)
TABLES = ("op_catalogs", "op_bindings", "op_dock_state", "op_dock_sessions", "op_dock_locks", "op_dock_actions",
          "op_reservations", "op_holds", "op_claims", "op_decisions", "op_cancellations", "op_events")
ACTIVE = (ReservationState.RESERVED.value, ReservationState.OCCUPIED.value, ReservationState.UNCERTAIN.value)


class MigrationError(RuntimeError):
    """The operations schema could not be put in place; the v1 database is unchanged. / 运营 schema 未就位；v1 库未变。"""


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


def schema_version(ledger: BusinessLedger) -> str:
    row = ledger._one("SELECT value FROM meta WHERE key='schema_version'")
    return row["value"] if row else ""


def migrate(ledger: BusinessLedger, *, backups: Path | None, statements: tuple[str, ...] = DDL,
            clock=utcnow) -> dict:
    """Bring the ledger to schema v2; back up and verify a database with data first (D056).

    把账本升到 schema v2；已有数据的库先备份并校验（D056）。
    """
    with ledger._lock:
        version = schema_version(ledger)
        if version == SCHEMA_VERSION:
            present = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [name for name in TABLES if name not in present]
            if missing:
                raise MigrationError(f"schema {SCHEMA_VERSION} is missing tables {missing}")
            return {"status": "current", "schema_version": SCHEMA_VERSION}
        if version != "1":
            raise MigrationError(f"unexpected ledger schema version {version!r}")
        counts = _counts(ledger._db)
        backup = None
        has_data = any(counts.get(name, 0) for name in ("requests", "missions", "deliveries", "events"))
        if ledger.path is not None and has_data:
            if backups is None:
                raise MigrationError("a ledger with data needs a backup directory before migrating")
            stamp = clock().strftime("%Y%m%dT%H%M%S%fZ")
            target = backups / f"ledger-v1-{stamp}.sqlite3"
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
                db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('operations_migrated_at', ?)",
                           (clock().isoformat(),))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('operations_backup', ?)",
                           (json.dumps(backup, sort_keys=True),))
        except sqlite3.Error as error:
            raise MigrationError(f"operations schema migration rolled back: {error}") from error
        return {"status": "migrated", "schema_version": SCHEMA_VERSION, "backup": backup, "rows_before": counts}


class OperationsStore:
    """Reads and writes of the P1 tables; callers group writes with `transaction()`. / P1 表的读写；调用方用 `transaction()` 组合写入。"""

    def __init__(self, ledger: BusinessLedger, catalog: OperationsCatalog, *, clock=utcnow):
        if schema_version(ledger) != SCHEMA_VERSION:
            raise MigrationError("run migrate() before opening the operations store")
        self.ledger, self.catalog, self.clock = ledger, catalog, clock
        self.ledger._exec("INSERT OR IGNORE INTO op_catalogs VALUES (?,?,?,?)",
                          (catalog.sha256, catalog.catalog_id, _dump(catalog), self.clock().isoformat()))

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

    # ── audit / 审计 ──

    def event(self, subject: str, kind: str, actor: str, body: dict | None = None) -> None:
        self._exec("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)",
                   (subject, kind, actor, _dump(body or {}), self._now()))

    def events(self, subject: str | None = None, *, prefix: str | None = None, limit: int = 200) -> list[dict]:
        if subject is not None:
            rows = self._rows("SELECT * FROM op_events WHERE subject=? ORDER BY id DESC LIMIT ?", (subject, limit))
        elif prefix is not None:
            rows = self._rows("SELECT * FROM op_events WHERE subject LIKE ? ORDER BY id DESC LIMIT ?",
                              (prefix + "%", limit))
        else:
            rows = self._rows("SELECT * FROM op_events ORDER BY id DESC LIMIT ?", (limit,))
        return [{**row, "body": _load(row["body"])} for row in reversed(rows)]

    # ── bindings / 绑定 ──

    def binding_writer(self, mission_id: str, *, project_id: str, robot_id: str):
        """A writer for `BusinessLedger.record_request(also=...)`: the binding joins the mission's transaction.

        供 `BusinessLedger.record_request(also=...)` 使用：绑定与任务在同一事务写入。
        """
        robot = self.catalog.robots[robot_id]

        def write(db: sqlite3.Connection) -> None:
            db.execute("INSERT INTO op_bindings VALUES (?,?,?,?,?,?,?,?)",
                       (mission_id, project_id, robot.site_id, robot.dock_id, robot_id, robot.execution_backend,
                        self.catalog.sha256, self._now()))
        return write

    def binding(self, mission_id: str) -> dict | None:
        return self._one("SELECT * FROM op_bindings WHERE mission_id=?", (mission_id,))

    def bound_missions(self, project_ids: list[str], limit: int = 50) -> set[str]:
        if not project_ids:
            return set()
        marks = ",".join("?" * len(project_ids))
        rows = self._rows(f"SELECT mission_id FROM op_bindings WHERE project_id IN ({marks}) "
                          "ORDER BY created_at DESC LIMIT ?", (*project_ids, limit))
        return {row["mission_id"] for row in rows}

    def all_bound(self) -> set[str]:
        return {row["mission_id"] for row in self._rows("SELECT mission_id FROM op_bindings")}

    # ── dock state / 机场状态 ──

    def dock_row(self, dock_id: str) -> dict | None:
        return self._one("SELECT * FROM op_dock_state WHERE dock_id=?", (dock_id,))

    def dock_status(self, dock_id: str) -> DockStatus | None:
        row = self.dock_row(dock_id)
        if row is None:
            return None
        lock = self.lock(dock_id)
        return DockStatus(dock_id=dock_id, source=row["source"], session=row["session"],
                          complete_reports=row["complete_reports"], report=_load(row["report"]),
                          received_at=datetime.fromisoformat(row["received_at"]), lock=lock)

    def known_boots(self, dock_id: str) -> set[str]:
        return {row["boot_id"] for row in self._rows("SELECT boot_id FROM op_dock_sessions WHERE dock_id=?",
                                                     (dock_id,))}

    def put_dock_state(self, dock_id: str, *, boot_id: str, seq: int, session: str, complete_reports: int,
                       source: str, report, observed_at: str) -> None:
        self._exec("INSERT OR IGNORE INTO op_dock_sessions VALUES (?,?,?)", (dock_id, boot_id, self._now()))
        self._exec("INSERT INTO op_dock_state VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(dock_id) DO UPDATE SET "
                   "boot_id=excluded.boot_id, seq=excluded.seq, session=excluded.session, "
                   "complete_reports=excluded.complete_reports, source=excluded.source, report=excluded.report, "
                   "observed_at=excluded.observed_at, received_at=excluded.received_at",
                   (dock_id, boot_id, seq, session, complete_reports, source, _dump(report), observed_at,
                    self._now()))

    def lock(self, dock_id: str) -> DockLock | None:
        row = self._one("SELECT * FROM op_dock_locks WHERE dock_id=?", (dock_id,))
        return None if row is None else DockLock(state=row["state"], reason=row["reason"], set_by=row["set_by"],
                                                 set_at=datetime.fromisoformat(row["set_at"]))

    def set_lock(self, dock_id: str, state: str, reason: str, set_by: str) -> bool:
        """Set a lock unless one is already active; a fault replaces maintenance. / 设锁；故障可替换维护锁。"""
        current = self.lock(dock_id)
        if current is not None and (current.state == state or current.state == "fault"):
            return False
        self._exec("INSERT OR REPLACE INTO op_dock_locks VALUES (?,?,?,?,?)",
                   (dock_id, state, reason[:300], set_by, self._now()))
        return True

    def clear_lock(self, dock_id: str) -> bool:
        return self._exec("DELETE FROM op_dock_locks WHERE dock_id=?", (dock_id,)).rowcount == 1

    # ── dock actions / 机场动作 ──

    def request_action(self, dock_id: str, kind: str, activity: str) -> dict:
        """Request once per (activity, kind); a repeat returns the first request. / 每个 (活动, 动作) 只请求一次。"""
        known = self._one("SELECT * FROM op_dock_actions WHERE activity_key=? AND kind=?", (activity, kind))
        if known is not None:
            return known
        action_id = "act-" + uuid.uuid4().hex[:20]
        self._exec("INSERT INTO op_dock_actions(action_id, dock_id, kind, activity_key, state, requested_at) "
                   "VALUES (?,?,?,?,?,?)", (action_id, dock_id, kind, activity, "requested", self._now()))
        return self.action(action_id)

    def action(self, action_id: str) -> dict | None:
        return self._one("SELECT * FROM op_dock_actions WHERE action_id=?", (action_id,))

    def action_for(self, activity: str, kind: str) -> dict | None:
        return self._one("SELECT * FROM op_dock_actions WHERE activity_key=? AND kind=?", (activity, kind))

    def actions(self, dock_id: str, states: tuple[str, ...] | None = None) -> list[dict]:
        if states is None:
            return self._rows("SELECT * FROM op_dock_actions WHERE dock_id=? ORDER BY requested_at", (dock_id,))
        marks = ",".join("?" * len(states))
        return self._rows(f"SELECT * FROM op_dock_actions WHERE dock_id=? AND state IN ({marks}) "
                          "ORDER BY requested_at", (dock_id, *states))

    def ack_action(self, action_id: str, accepted: bool, reason: str) -> bool:
        """The first ACK wins; a duplicate or late ACK changes nothing. / 首个 ACK 生效；重复或迟到的 ACK 不改变任何东西。"""
        return self._exec("UPDATE op_dock_actions SET state=?, acked_at=?, ack_accepted=?, ack_reason=? "
                          "WHERE action_id=? AND state='requested'",
                          ("acked" if accepted else "rejected", self._now(), int(accepted), reason[:300],
                           action_id)).rowcount == 1

    def finish_action(self, action_id: str, result: str) -> bool:
        """Only a status report finishes an action, and only once. / 只有状态报告能结束动作，且只结束一次。"""
        return self._exec("UPDATE op_dock_actions SET state=?, result_at=? WHERE action_id=? "
                          "AND state IN ('requested', 'acked')", (result, self._now(), action_id)).rowcount == 1

    # ── reservations / 预约 ──

    def _reservation(self, row: dict | None) -> ResourceReservation | None:
        if row is None:
            return None
        resources = tuple(r["resource_id"] for r in self._rows(
            "SELECT resource_id FROM op_holds WHERE reservation_id=? ORDER BY resource_id", (row["reservation_id"],)))
        return ResourceReservation(
            reservation_id=row["reservation_id"], activity_key=row["activity_key"], project_id=row["project_id"],
            mission_id=row["mission_id"], mission_version=row["mission_version"], robot_id=row["robot_id"],
            dock_id=row["dock_id"], resources=resources, state=row["state"],
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            reason=row["reason"], evidence=_load(row["evidence"]),
            created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]))

    def reservation(self, activity: str) -> ResourceReservation | None:
        return self._reservation(self._one("SELECT * FROM op_reservations WHERE activity_key=?", (activity,)))

    def reservations(self, *, mission_id: str | None = None, states: tuple[str, ...] | None = None,
                     dock_id: str | None = None) -> list[ResourceReservation]:
        sql, args = "SELECT * FROM op_reservations WHERE 1=1", []
        if mission_id is not None:
            sql, args = sql + " AND mission_id=?", [*args, mission_id]
        if dock_id is not None:
            sql, args = sql + " AND dock_id=?", [*args, dock_id]
        if states is not None:
            sql, args = sql + f" AND state IN ({','.join('?' * len(states))})", [*args, *states]
        return [self._reservation(row) for row in self._rows(sql + " ORDER BY created_at", args)]

    def holders(self, resources) -> dict[str, str]:
        """Resource -> activity key of its active holder. / 资源 -> 其有效持有者的活动键。"""
        resources = list(resources)
        if not resources:
            return {}
        marks = ",".join("?" * len(resources))
        rows = self._rows("SELECT h.resource_id, r.activity_key FROM op_holds h JOIN op_reservations r "
                          f"ON r.reservation_id = h.reservation_id WHERE h.active = 1 AND h.resource_id IN ({marks})",
                          resources)
        return {row["resource_id"]: row["activity_key"] for row in rows}

    def expire_soft(self, resources, now: datetime) -> list[str]:
        """Release soft reservations past their expiry on these resources. / 释放这些资源上已到期的软预约。"""
        resources = list(resources)
        marks = ",".join("?" * len(resources))
        rows = self._rows("SELECT DISTINCT r.reservation_id, r.expires_at FROM op_holds h JOIN op_reservations r "
                          "ON r.reservation_id = h.reservation_id WHERE h.active = 1 AND r.state = 'reserved' "
                          f"AND h.resource_id IN ({marks})", resources)
        expired = [row["reservation_id"] for row in rows
                   if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= now]
        for reservation_id in expired:
            self._set_state(reservation_id, ("reserved",), "released", "soft_expired", None)
        return expired

    def reserve(self, *, activity: str, project_id: str, mission_id: str, version: int, robot_id: str,
                expires_at: datetime, extra: tuple[str, ...] = (),
                blocked: dict[str, str] | None = None) -> tuple[ResourceReservation | None, dict[str, str]]:
        """Reserve the robot's resources for `activity`; returns (reservation, conflicting holders).

        A repeat of the same activity returns its reservation (a soft one gets the new expiry); a reservation that
        was only released by soft expiry may be taken again. The exclusive-hold index is the final arbiter. `extra`
        adds the flight's airspace cells (P3, D059) to the same exclusive holds; a new hold on a cell that `blocked`
        maps to another activity (a silent robot's envelope) is refused.

        为 `activity` 预约机器人的资源；返回（预约，冲突持有者）。同一活动重复预约返回原预约（软预约刷新到期时间）；
        仅因软到期而释放的预约可以重新获取。独占持有索引是最终裁决。`extra` 把该飞行的空域单元（P3，D059）加入同一组
        独占持有；`blocked` 映射到其他活动的单元（失联机器人的包络）上不接受新持有。
        """
        resources = (*self.catalog.resources(robot_id), *extra)
        dock_id = self.catalog.robots[robot_id].dock_id
        now = self.clock()
        try:
            with self.transaction():
                self.expire_soft(resources, now)
                current = self._one("SELECT * FROM op_reservations WHERE activity_key=?", (activity,))
                if current is not None and current["state"] in ACTIVE:
                    if current["state"] == "reserved":
                        self._exec("UPDATE op_reservations SET expires_at=?, updated_at=? WHERE reservation_id=?",
                                   (expires_at.isoformat(), self._now(), current["reservation_id"]))
                    return self.reservation(activity), {}
                if current is not None and current["reason"] != "soft_expired":
                    return self.reservation(activity), {}
                others = {r: a for r, a in self.holders(resources).items() if a != activity}
                others |= {r: f"envelope:{a}" for r, a in (blocked or {}).items()
                           if r in resources and a != activity and r not in others}
                if others:
                    return None, others
                if current is None:
                    reservation_id = "res-" + uuid.uuid4().hex[:20]
                    self._exec("INSERT INTO op_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (reservation_id, activity, project_id, mission_id, version, robot_id, dock_id,
                                "reserved", expires_at.isoformat(), "reserved", None, self._now(), self._now()))
                else:
                    reservation_id = current["reservation_id"]
                    self._exec("UPDATE op_reservations SET state='reserved', expires_at=?, reason='re_reserved', "
                               "updated_at=? WHERE reservation_id=?", (expires_at.isoformat(), self._now(),
                                                                       reservation_id))
                for resource in resources:
                    self._exec("INSERT INTO op_holds VALUES (?,?,1) ON CONFLICT(resource_id, reservation_id) "
                               "DO UPDATE SET active=1", (resource, reservation_id))
                self.event(activity, "reservation.reserved", "service",
                           {"reservation_id": reservation_id, "resources": list(resources),
                            "expires_at": expires_at.isoformat()})
        except sqlite3.IntegrityError:
            # Another writer took a resource between our read and insert. / 另一写入方在读取与插入之间占用了资源。
            return None, {r: a for r, a in self.holders(resources).items() if a != activity}
        return self.reservation(activity), {}

    def _set_state(self, reservation_id: str, before: tuple[str, ...], after: str, reason: str,
                   evidence: dict | None) -> bool:
        marks = ",".join("?" * len(before))
        changed = self._exec(f"UPDATE op_reservations SET state=?, reason=?, evidence=?, updated_at=? "
                             f"WHERE reservation_id=? AND state IN ({marks})",
                             (after, reason, _dump(evidence) if evidence is not None else None, self._now(),
                              reservation_id, *before)).rowcount == 1
        if changed:
            if after == "released":
                self._exec("UPDATE op_holds SET active=0 WHERE reservation_id=?", (reservation_id,))
            activity = self._one("SELECT activity_key FROM op_reservations WHERE reservation_id=?",
                                 (reservation_id,))["activity_key"]
            self.event(activity, f"reservation.{after}", "service",
                       {"reservation_id": reservation_id, "from": list(before), "reason": reason,
                        "evidence": evidence})
        return changed

    def transition(self, activity: str, before: tuple[str, ...], after: str, reason: str,
                   evidence: dict | None = None) -> bool:
        """Compare-and-set one reservation; releasing drops its holds exactly once. / 对一个预约做 CAS；释放恰好一次。"""
        row = self._one("SELECT reservation_id FROM op_reservations WHERE activity_key=?", (activity,))
        if row is None:
            return False
        with self.transaction():
            return self._set_state(row["reservation_id"], before, after, reason, evidence)

    # ── claims, decisions, cancellations / 领取、判定、取消 ──

    def claim(self, delivery_id: str) -> dict | None:
        return self._one("SELECT * FROM op_claims WHERE delivery_id=?", (delivery_id,))

    def claims(self, mission_id: str) -> list[dict]:
        return self._rows("SELECT * FROM op_claims WHERE mission_id=? ORDER BY decided_at", (mission_id,))

    def record_claim(self, delivery_id: str, mission_id: str, version: int, state: str, reason: str,
                     decision_id: str | None) -> bool:
        return self._exec("INSERT OR IGNORE INTO op_claims VALUES (?,?,?,?,?,?,?)",
                          (delivery_id, mission_id, version, state, reason, decision_id, self._now())).rowcount == 1

    def record_decision(self, eligibility, *, mission_id: str | None, version: int | None) -> str:
        decision_id = "dec-" + uuid.uuid4().hex[:20]
        self._exec("INSERT INTO op_decisions VALUES (?,?,?,?,?,?,?,?)",
                   (decision_id, mission_id, version, eligibility.robot_id, eligibility.stage.value,
                    eligibility.verdict.value, _dump(eligibility), self._now()))
        return decision_id

    def decisions(self, mission_id: str, limit: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM op_decisions WHERE mission_id=? ORDER BY created_at DESC LIMIT ?",
                          (mission_id, limit))
        return [{**row, "body": _load(row["body"])} for row in reversed(rows)]

    def last_decision(self, mission_id: str, version: int, stage: str) -> dict | None:
        row = self._one("SELECT * FROM op_decisions WHERE mission_id=? AND mission_version=? AND stage=? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (mission_id, version, stage))
        return None if row is None else {**row, "body": _load(row["body"])}

    def cancel_intent(self, mission_id: str) -> dict | None:
        return self._one("SELECT * FROM op_cancellations WHERE mission_id=?", (mission_id,))

    def record_cancel(self, mission_id: str, *, requested_by: str, request_id: str, reason: str) -> bool:
        """Persist the first cancel intent of a mission; later ones keep the first. / 持久化任务的首个取消意图。"""
        created = self._exec("INSERT OR IGNORE INTO op_cancellations VALUES (?,?,?,?,?,?)",
                             (mission_id, requested_by, request_id, reason[:300], self._now(), None)).rowcount == 1
        if created:
            self.event(f"mission:{mission_id}", "cancel.requested", requested_by, {"request_id": request_id})
        return created

    def mark_cancel_relayed(self, mission_id: str, request_id: str) -> bool:
        return self._exec("UPDATE op_cancellations SET relayed_request_id=? WHERE mission_id=? "
                          "AND relayed_request_id IS NULL", (request_id, mission_id)).rowcount == 1


def soft_expiry(catalog: OperationsCatalog, now: datetime, not_after: datetime | None) -> datetime:
    """Soft reservation expiry, never beyond the approval or package window. / 软预约到期时间，不超过审批或任务窗口。"""
    expiry = now + timedelta(seconds=catalog.policy.soft_reservation_s)
    return min(expiry, not_after) if not_after is not None else expiry


_OPERATIONS_LINE = re.compile(r'^(CREATE (TABLE|UNIQUE INDEX|INDEX) (IF NOT EXISTS )?"?op_|INSERT INTO "op_)')


def v1_lines(path: Path) -> list[str]:
    """The SQL dump of the v1 tables only: op_* objects, their sequence rows and migration meta keys are left out.

    只含 v1 表的 SQL 转储：去掉 op_* 对象、其序列行与迁移 meta 键。
    """
    with sqlite3.connect(str(path)) as db:
        dump = list(db.iterdump())
    kept = []
    for line in dump:
        if _OPERATIONS_LINE.match(line) or line.startswith("INSERT INTO \"sqlite_sequence\" VALUES('op_"):
            continue
        if line.startswith('INSERT INTO "meta"') and ("'schema_version'" in line or "'operations_" in line):
            continue
        kept.append(line)
    return kept


def drill(path: Path) -> dict:
    """D056 drill on a copy of a ledger: migrate it and prove the v1 data unchanged; reports counts, never content.

    在账本副本上做 D056 演练：迁移并证明 v1 数据不变；只报告计数，从不报告内容。
    """

    def v1_dump(target: Path) -> str:
        return hashlib.sha256("\n".join(v1_lines(target)).encode()).hexdigest()

    before = v1_dump(path)
    ledger = BusinessLedger(path)
    try:
        report = migrate(ledger, backups=path.parent / "backups")
        with sqlite3.connect(str(path)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                            if row[0].startswith("op_"))
    finally:
        ledger.close()
    after = v1_dump(path)
    return {"status": "passed" if report["status"] in ("migrated", "current") and integrity == "ok"
            and before == after and tables == sorted(TABLES) else "failed",
            "migration": report["status"], "schema_version": SCHEMA_VERSION, "integrity": integrity,
            "v1_dump_sha256_before": before, "v1_dump_sha256_after": after, "op_tables": len(tables),
            "rows_before": report.get("rows_before"), "backup": report.get("backup")}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill", type=Path, required=True, help="a copy of the ledger to migrate (D056 drill)")
    args = parser.parse_args()
    result = drill(args.drill)
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
