"""Business ledger of the mission service (SQLite, WP-M2-12). It mirrors; it never commands.

The authoritative flight state stays in the aircraft's hash-chained journals. This ledger stores requests,
mission versions with their planner, admission and approval records, deliveries, the events and evidence
the uplink forwards, service-side verifications, facts and reports. It is eventually consistent: a robot
that flew while the service was down is synchronised afterwards. Active operations are unique per
`(mission_id, idempotency_key)`, the lesson car-agent learned against double execution.

任务服务的业务账本（SQLite，WP-M2-12）。它只做镜像，从不下达命令。

权威飞行状态仍在飞行器的哈希链账本中。本账本保存请求、带规划 / 准入 / 审批记录的任务版本、投递、
uplink 转发的事件与证据、服务侧验证、事实与报告。它是最终一致的：服务停机期间飞过的机器人事后同步。
进行中的操作按 `(mission_id, idempotency_key)` 唯一，这是 car-agent 防重复执行得到的教训。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from drone_agent.contracts import ExecutionEvent, utcnow

SCHEMA_VERSION = "1"
ACTIVE_OPERATION_STATES = ("pending", "in_flight")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    mission_id TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL,
    UNIQUE (requested_by, idempotency_key));
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, robot_id TEXT, status TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0, replans INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
    mission_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL,
    spec TEXT, planner TEXT, compile TEXT, admission TEXT, package TEXT, package_hash TEXT,
    approval TEXT, decision TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (mission_id, version));
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, kind TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_operation ON operations (mission_id, idempotency_key)
    WHERE status IN ('pending', 'in_flight');
CREATE TABLE IF NOT EXISTS deliveries (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT UNIQUE NOT NULL, robot_id TEXT NOT NULL,
    kind TEXT NOT NULL, mission_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL, acked_at TEXT, ack_accepted INTEGER, ack_reason TEXT);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL,
    journal TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL, previous TEXT NOT NULL,
    timestamp TEXT NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL,
    UNIQUE (robot_id, mission_id, mission_version, journal, seq));
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL,
    step_id TEXT NOT NULL, sha256 TEXT NOT NULL, media_path TEXT, media_type TEXT, width INTEGER, height INTEGER,
    body TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications (
    evidence_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, step_id TEXT NOT NULL,
    onboard_verdict TEXT, service_verdict TEXT NOT NULL, agrees INTEGER NOT NULL, body TEXT NOT NULL,
    verified_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts (fact_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT, request_id TEXT,
    code TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS robots (robot_id TEXT PRIMARY KEY, capability TEXT, status TEXT, status_at TEXT);
CREATE TABLE IF NOT EXISTS reports (mission_id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class DuplicateOperation(RuntimeError):
    """An operation with the same idempotency key is already active. / 相同幂等键的操作仍在进行中。"""


def _now() -> str:
    return utcnow().isoformat()


def _dump(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(text):
    return None if text is None else json.loads(text)


class BusinessLedger:
    """Thread-safe SQLite ledger; one connection guarded by a lock. / 线程安全的 SQLite 账本；单连接加锁。"""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # ── requests and missions / 请求与任务 ──

    def record_request(self, request, mission_id: str) -> tuple[str, bool]:
        """Store a request once per (requester, idempotency key); a repeat returns the original mission.

        每个 (请求者, 幂等键) 只存一次请求；重复提交返回原任务。
        """
        with self._lock:
            known = self._one("SELECT mission_id FROM requests WHERE requested_by=? AND idempotency_key=?",
                              (request.requested_by, request.idempotency_key))
            if known:
                return known["mission_id"], False
            now = _now()
            self._db.execute("BEGIN")
            try:
                self._db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?)",
                                 (request.request_id, request.requested_by, request.idempotency_key, mission_id,
                                  _dump(request), request.received_at.isoformat()))
                self._db.execute("INSERT INTO missions(mission_id, request_id, status, created_at, updated_at) "
                                 "VALUES (?,?,?,?,?)", (mission_id, request.request_id, "planning", now, now))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return mission_id, True

    def request(self, request_id: str) -> dict | None:
        row = self._one("SELECT * FROM requests WHERE request_id=?", (request_id,))
        return None if row is None else {**row, "body": _load(row["body"])}

    def mission(self, mission_id: str) -> dict | None:
        return self._one("SELECT * FROM missions WHERE mission_id=?", (mission_id,))

    def missions(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM missions ORDER BY created_at DESC LIMIT ?", (limit,))

    def update_mission(self, mission_id: str, **fields) -> None:
        allowed = {"status", "robot_id", "current_version", "replans"}
        if set(fields) - allowed:
            raise ValueError("unknown mission fields")
        assignments = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        self._exec(f"UPDATE missions SET {assignments} WHERE mission_id=?", (*fields.values(), _now(), mission_id))

    # ── versions / 版本 ──

    def record_version(self, mission_id: str, version: int, *, status: str, origin: str, **records) -> None:
        now = _now()
        columns = ["spec", "planner", "compile", "admission", "package", "package_hash", "approval", "decision"]
        values = [records.get(c) if c == "package_hash" else _dump(records.get(c)) for c in columns]
        self._exec("INSERT INTO versions(mission_id, version, status, origin, " + ", ".join(columns)
                   + ", created_at, updated_at) VALUES (?,?,?,?," + ",".join("?" * len(columns)) + ",?,?)",
                   (mission_id, version, status, origin, *values, now, now))

    def update_version(self, mission_id: str, version: int, **records) -> None:
        allowed = {"status", "package", "package_hash", "approval", "decision", "admission"}
        if set(records) - allowed:
            raise ValueError("unknown version fields")
        values = [v if k in ("status", "package_hash") else _dump(v) for k, v in records.items()]
        assignments = ", ".join(f"{k}=?" for k in records) + ", updated_at=?"
        self._exec(f"UPDATE versions SET {assignments} WHERE mission_id=? AND version=?",
                   (*values, _now(), mission_id, version))

    def version(self, mission_id: str, version: int) -> dict | None:
        row = self._one("SELECT * FROM versions WHERE mission_id=? AND version=?", (mission_id, version))
        return self._decode_version(row)

    def versions(self, mission_id: str) -> list[dict]:
        rows = self._rows("SELECT * FROM versions WHERE mission_id=? ORDER BY version", (mission_id,))
        return [self._decode_version(row) for row in rows]

    @staticmethod
    def _decode_version(row):
        if row is None:
            return None
        for key in ("spec", "planner", "compile", "admission", "package", "approval", "decision"):
            row[key] = _load(row[key])
        return row

    # ── operations with active-state uniqueness / 带进行中唯一性的操作 ──

    def begin_operation(self, mission_id: str, kind: str, idempotency_key: str, detail: dict | None = None) -> str:
        operation_id = uuid.uuid4().hex
        now = _now()
        try:
            self._exec("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
                       (operation_id, mission_id, kind, idempotency_key, "pending", _dump(detail or {}), now, now))
        except sqlite3.IntegrityError as error:
            raise DuplicateOperation(f"{kind} {idempotency_key} is already active for {mission_id}") from error
        return operation_id

    def finish_operation(self, operation_id: str, status: str, detail: dict | None = None) -> None:
        if status in ACTIVE_OPERATION_STATES:
            raise ValueError("finish_operation needs a terminal status")
        self._exec("UPDATE operations SET status=?, detail=?, updated_at=? WHERE operation_id=?",
                   (status, _dump(detail or {}), _now(), operation_id))

    def operations(self, mission_id: str) -> list[dict]:
        return self._rows("SELECT * FROM operations WHERE mission_id=? ORDER BY created_at", (mission_id,))

    # ── deliveries / 投递 ──

    def queue_delivery(self, robot_id: str, kind: str, mission_id: str, version: int, payload) -> dict:
        delivery_id = uuid.uuid4().hex
        self._exec("INSERT INTO deliveries(delivery_id, robot_id, kind, mission_id, version, payload, created_at) "
                   "VALUES (?,?,?,?,?,?,?)", (delivery_id, robot_id, kind, mission_id, version, _dump(payload), _now()))
        return self.delivery(delivery_id)

    def delivery(self, delivery_id: str) -> dict | None:
        row = self._one("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,))
        return None if row is None else {**row, "payload": _load(row["payload"])}

    def deliveries_after(self, robot_id: str, cursor: int, limit: int = 10) -> list[dict]:
        rows = self._rows("SELECT * FROM deliveries WHERE robot_id=? AND cursor>? AND acked_at IS NULL "
                          "ORDER BY cursor LIMIT ?", (robot_id, cursor, limit))
        return [{**row, "payload": _load(row["payload"])} for row in rows]

    def deliveries(self, mission_id: str) -> list[dict]:
        rows = self._rows("SELECT * FROM deliveries WHERE mission_id=? ORDER BY cursor", (mission_id,))
        return [{**row, "payload": _load(row["payload"])} for row in rows]

    def ack_delivery(self, robot_id: str, delivery_id: str, accepted: bool, reason: str) -> bool:
        cursor = self._exec("UPDATE deliveries SET acked_at=?, ack_accepted=?, ack_reason=? "
                            "WHERE delivery_id=? AND robot_id=? AND acked_at IS NULL",
                            (_now(), int(accepted), reason[:300], delivery_id, robot_id))
        return cursor.rowcount == 1

    # ── events, evidence, verifications, facts / 事件、证据、验证、事实 ──

    def record_event(self, robot_id: str, event: ExecutionEvent) -> bool:
        """Insert once; duplicates from at-least-once delivery are ignored. / 只插入一次；至少一次投递的重复被忽略。"""
        data = event.data
        cursor = self._exec(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.event_id, robot_id, event.mission_id, event.mission_version, data.get("journal", ""),
             int(data.get("seq", -1)), data.get("kind", event.event_type.value), data.get("sha256", ""),
             data.get("previous", ""), event.timestamp.isoformat(), _now(), _dump(event)))
        return cursor.rowcount == 1

    def events(self, mission_id: str, version: int | None = None) -> list[dict]:
        if version is None:
            rows = self._rows("SELECT * FROM events WHERE mission_id=? ORDER BY mission_version, journal, seq",
                              (mission_id,))
        else:
            rows = self._rows("SELECT * FROM events WHERE mission_id=? AND mission_version=? ORDER BY journal, seq",
                              (mission_id, version))
        return [{**row, "body": _load(row["body"])} for row in rows]

    def record_evidence(self, robot_id: str, evidence, *, media_path: str | None = None, media_type: str | None = None,
                        width: int | None = None, height: int | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (evidence.evidence_id, robot_id, evidence.mission_id or "", evidence.mission_version or 0,
                    evidence.produced_by_skill_instance, evidence.sha256, media_path, media_type, width, height,
                    _dump(evidence), _now()))

    def attach_media(self, evidence_id: str, media_path: str, media_type: str, width: int, height: int) -> bool:
        cursor = self._exec("UPDATE evidence SET media_path=?, media_type=?, width=?, height=? WHERE evidence_id=?",
                            (media_path, media_type, width, height, evidence_id))
        return cursor.rowcount == 1

    def evidence(self, mission_id: str, version: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM evidence WHERE mission_id=?", [mission_id]
        if version is not None:
            sql, args = sql + " AND mission_version=?", [mission_id, version]
        return [{**row, "body": _load(row["body"])} for row in self._rows(sql + " ORDER BY received_at", args)]

    def record_verification(self, record: dict) -> None:
        self._exec("INSERT OR REPLACE INTO verifications VALUES (?,?,?,?,?,?,?,?,?)",
                   (record["evidence_id"], record["mission_id"], record["mission_version"], record["step_id"],
                    record.get("onboard_verdict"), record["service_verdict"], int(record["agrees"]), _dump(record),
                    _now()))

    def verifications(self, mission_id: str) -> list[dict]:
        return [{**row, "body": _load(row["body"])}
                for row in self._rows("SELECT * FROM verifications WHERE mission_id=? ORDER BY verified_at",
                                      (mission_id,))]

    def record_fact(self, mission_id: str, fact) -> None:
        self._exec("INSERT OR REPLACE INTO facts VALUES (?,?,?,?)", (fact.fact_id, mission_id, _dump(fact), _now()))

    def facts(self, mission_id: str) -> list[dict]:
        return [_load(row["body"]) for row in self._rows("SELECT body FROM facts WHERE mission_id=?", (mission_id,))]

    def record_issue(self, issue, *, mission_id: str | None = None, request_id: str | None = None) -> None:
        self._exec("INSERT INTO issues(mission_id, request_id, code, body, created_at) VALUES (?,?,?,?,?)",
                   (mission_id, request_id, issue.code, _dump(issue), _now()))

    def issues(self, mission_id: str) -> list[dict]:
        return [_load(row["body"]) for row in self._rows("SELECT body FROM issues WHERE mission_id=? ORDER BY id",
                                                         (mission_id,))]

    # ── robots and reports / 机器人与报告 ──

    def record_capability(self, capability) -> None:
        self._exec("INSERT INTO robots(robot_id, capability) VALUES (?,?) ON CONFLICT(robot_id) "
                   "DO UPDATE SET capability=excluded.capability", (capability.robot_id, _dump(capability)))

    def record_status(self, status) -> None:
        self._exec("INSERT INTO robots(robot_id, status, status_at) VALUES (?,?,?) ON CONFLICT(robot_id) "
                   "DO UPDATE SET status=excluded.status, status_at=excluded.status_at",
                   (status.robot_id, _dump(status), status.timestamp.isoformat()))

    def robot(self, robot_id: str) -> dict | None:
        row = self._one("SELECT * FROM robots WHERE robot_id=?", (robot_id,))
        return None if row is None else {**row, "capability": _load(row["capability"]), "status": _load(row["status"])}

    def robots(self) -> list[dict]:
        return [{**row, "capability": _load(row["capability"]), "status": _load(row["status"])}
                for row in self._rows("SELECT * FROM robots ORDER BY robot_id")]

    def record_report(self, mission_id: str, report) -> None:
        self._exec("INSERT OR REPLACE INTO reports VALUES (?,?,?)", (mission_id, _dump(report), _now()))

    def report(self, mission_id: str) -> dict | None:
        row = self._one("SELECT body FROM reports WHERE mission_id=?", (mission_id,))
        return None if row is None else _load(row["body"])
