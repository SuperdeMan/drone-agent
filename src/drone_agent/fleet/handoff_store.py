"""X1 handoff tables: offers to ground robots, their append-only transitions and the recheck links (D088).

Three tables join the ledger, added by a migration that backs up and verifies a ledger with data first, like
D056-D087. `ho_handoffs` holds one row per offer of a ground recheck task to a robot; a partial unique index lets a
task have at most one open offer (offered or accepted), and every state change is a compare-and-set on the current
state. `ho_events` is the append-only history of each offer. `bz_rechecks` links a finding and its aerial evidence
to the handoff, the ground task and mission and the ground evidence, with the recheck outcome. Nothing outside these
objects and the `handoff_` meta keys changes; the drill proves it on a copy with a streamed digest.

X1 交接表：发给地面车的提议、只追加的状态转移，以及复核关联（D088）。

三张表加入账本，由迁移新增；与 D056–D087 一样，有数据的账本先备份并校验。`ho_handoffs` 为地面复核任务的每次提议保存一行；
部分唯一索引保证一个任务至多一个未结提议（offered 或 accepted），每次状态变化都是针对当前状态的比较后写入。`ho_events` 是
每个提议只追加的历史。`bz_rechecks` 把发现及其空中证据与交接、地面任务与任务版本、地面证据以及复核结论关联起来。这些对象与
`handoff_` 元数据键之外的任何内容都不改变；演练在副本上用流式摘要证明这一点。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import SCHEMA_VERSION as OPERATIONS_SCHEMA
from drone_agent.fleet.operations_store import MigrationError, schema_version

HANDOFF_SCHEMA = "1"
OPEN = ("offered", "accepted")
STATES = ("offered", "accepted", "rejected", "acked", "completed", "timed_out", "withdrawn")
OUTCOMES = ("pending", "relocated", "not_found", "not_attempted", "unknown")
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS ho_handoffs (handoff_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
    "offer_seq INTEGER NOT NULL, project_id TEXT NOT NULL, from_party TEXT NOT NULL, to_robot_id TEXT NOT NULL, "
    "state TEXT NOT NULL, target TEXT NOT NULL, evidence_refs TEXT NOT NULL, expires_at TEXT NOT NULL, reply TEXT, "
    "assignment_epoch INTEGER, delivery_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "UNIQUE (task_id, offer_seq))",
    # At most one open offer per task (design §5). / 每个任务至多一个未结提议（设计 §5）。
    "CREATE UNIQUE INDEX IF NOT EXISTS ho_handoffs_open ON ho_handoffs (task_id) "
    "WHERE state IN ('offered', 'accepted')",
    "CREATE INDEX IF NOT EXISTS ho_handoffs_robot ON ho_handoffs (to_robot_id, state)",
    "CREATE TABLE IF NOT EXISTS ho_events (handoff_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, "
    "at TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (handoff_id, seq))",
    "CREATE TABLE IF NOT EXISTS bz_rechecks (recheck_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
    "finding_id TEXT NOT NULL, aerial_evidence TEXT NOT NULL, handoff_id TEXT, ground_task_id TEXT, "
    "ground_mission_id TEXT, ground_evidence TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)",
    "CREATE INDEX IF NOT EXISTS bz_rechecks_finding ON bz_rechecks (finding_id)",
    "CREATE INDEX IF NOT EXISTS bz_rechecks_handoff ON bz_rechecks (handoff_id)",
)
TABLES = ("ho_handoffs", "ho_events", "bz_rechecks")
INDEXES = ("ho_handoffs_open", "ho_handoffs_robot", "bz_rechecks_finding", "bz_rechecks_handoff")
REQUIRED = ("bz_findings", "sc_tasks", "sc_assignments")


class StaleHandoff(RuntimeError):
    """An offer changed since it was read; the caller re-reads and decides again. / 提议在读取后已变化；调用方重读后再决定。"""


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


def handoff_schema(ledger: BusinessLedger) -> str | None:
    row = ledger._one("SELECT value FROM meta WHERE key='handoff_schema'")
    return row["value"] if row else None


def _present(ledger: BusinessLedger, kind: str) -> set[str]:
    return {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def migrate(ledger: BusinessLedger, *, backups: Path | None, clock=utcnow) -> dict:
    """Add the handoff tables to a ledger with the business and scheduling tables; a ledger with data is backed up
    and verified first (D088). A migrated ledger is left as it is.

    为已有业务表与调度表的账本加上交接表；有数据的库先备份并校验（D088）。已迁移的账本保持原样。
    """
    with ledger._lock:
        if schema_version(ledger) != OPERATIONS_SCHEMA:
            raise MigrationError("the handoff tables need the P1 operations schema v2 first")
        missing = [name for name in REQUIRED if name not in _present(ledger, "table")]
        if missing:
            raise MigrationError(f"the handoff tables need the business and scheduling tables first (missing {missing})")
        version = handoff_schema(ledger)
        if version == HANDOFF_SCHEMA:
            gone = [name for name in (*TABLES, *INDEXES)
                    if name not in _present(ledger, "table") | _present(ledger, "index")]
            if gone:
                raise MigrationError(f"handoff schema {HANDOFF_SCHEMA} is missing {gone}")
            return {"status": "current", "handoff_schema": HANDOFF_SCHEMA}
        if version is not None:
            raise MigrationError(f"unexpected handoff schema version {version!r}")
        counts = _counts(ledger._db)
        backup = None
        has_data = any(counts.get(name, 0) for name in ("requests", "missions", "deliveries", "events", "op_events"))
        if ledger.path is not None and has_data:
            if backups is None:
                raise MigrationError("a ledger with data needs a backup directory before migrating")
            label = clock().strftime("%Y%m%dT%H%M%S%fZ")
            target = backups / f"ledger-v2-ho-{label}.sqlite3"
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
            with ledger.transaction() as db:
                for statement in DDL:
                    db.execute(statement)
                db.execute("INSERT INTO meta(key, value) VALUES ('handoff_schema', ?)", (HANDOFF_SCHEMA,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('handoff_migrated_at', ?)",
                           (clock().isoformat(),))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('handoff_backup', ?)",
                           (json.dumps(backup, sort_keys=True),))
        except sqlite3.Error as error:
            raise MigrationError(f"handoff migration rolled back: {error}") from error
        return {"status": "migrated", "handoff_schema": HANDOFF_SCHEMA, "backup": backup, "rows_before": counts}


def _ours(name: str) -> bool:
    return name.startswith("ho_") or name.startswith("bz_rechecks")


def other_digest(path: Path) -> str:
    """SHA-256 over the schema and every row outside the handoff objects, streamed (D085 lesson: no full dump in
    memory). / 交接对象以外的模式与每一行的 SHA-256，流式计算（D085 的教训：不把整库转储读入内存）。"""
    digest = hashlib.sha256()
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for row in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"):
            if not (_ours(row[1]) or _ours(row[2])):
                digest.update(repr(tuple(row)).encode())
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if not _ours(row[0])]
        for table in tables:
            digest.update(f"\x00{table}\x00".encode())
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                if table == "meta" and str(row[0]).startswith("handoff_"):
                    continue  # the migration's own markers / 迁移自己的标记
                digest.update(repr(row).encode())
    finally:
        db.close()
    return digest.hexdigest()


def drill(path: Path) -> dict:
    """D088 drill on a copy of a ledger: migrate it and prove everything else unchanged; counts only, never content.

    在账本副本上做 D088 演练：迁移并证明其余内容不变；只报告计数，从不报告内容。
    """
    before = other_digest(path)
    ledger = BusinessLedger(path)
    try:
        report = migrate(ledger, backups=path.parent / "backups")
        with sqlite3.connect(str(path)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                            if _ours(row[0]))
            indexes = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
                             if row[0] in INDEXES)
    finally:
        ledger.close()
    after = other_digest(path)
    return {"status": "passed" if report["status"] in ("migrated", "current") and integrity == "ok"
            and before == after and tables == sorted(TABLES) and indexes == sorted(INDEXES) else "failed",
            "migration": report["status"], "handoff_schema": HANDOFF_SCHEMA, "integrity": integrity,
            "other_sha256_before": before, "other_sha256_after": after, "tables": len(tables),
            "indexes": len(indexes), "rows_before": report.get("rows_before"), "backup": report.get("backup")}


class HandoffStore:
    """Reads and writes of the handoff tables; callers group writes with `transaction()`.

    交接表的读写；调用方用 `transaction()` 组合写入。
    """

    def __init__(self, ledger: BusinessLedger, *, clock=utcnow):
        if schema_version(ledger) != OPERATIONS_SCHEMA or handoff_schema(ledger) != HANDOFF_SCHEMA:
            raise MigrationError("run migrate() before opening the handoff store")
        self.ledger, self.clock = ledger, clock

    def transaction(self):
        return self.ledger.transaction()

    def _now(self) -> str:
        return self.clock().isoformat()

    @staticmethod
    def _handoff(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "target": _load(row["target"]), "evidence_refs": _load(row["evidence_refs"]),
                "reply": _load(row["reply"])}

    # ── offers / 提议 ──

    def handoff(self, handoff_id: str) -> dict | None:
        return self._handoff(self.ledger._one("SELECT * FROM ho_handoffs WHERE handoff_id=?", (handoff_id,)))

    def for_task(self, task_id: str) -> list[dict]:
        rows = self.ledger._rows("SELECT * FROM ho_handoffs WHERE task_id=? ORDER BY offer_seq", (task_id,))
        return [self._handoff(row) for row in rows]

    def open_offer(self, task_id: str) -> dict | None:
        return self._handoff(self.ledger._one("SELECT * FROM ho_handoffs WHERE task_id=? AND state IN "
                                              "('offered', 'accepted')", (task_id,)))

    def open_offers(self, *, robot_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM ho_handoffs WHERE state IN ('offered', 'accepted')", []
        if robot_id is not None:
            sql, args = sql + " AND to_robot_id=?", [robot_id]
        return [self._handoff(row) for row in self.ledger._rows(sql + " ORDER BY created_at, handoff_id", args)]

    def insert_offer(self, *, handoff_id: str, task_id: str, project_id: str, from_party: str, to_robot_id: str,
                     target: dict, evidence_refs: list[str], expires_at: str, actor: str) -> dict:
        """A new offer with the next sequence number; the open-offer index refuses a second open one.

        以下一个序号写入新提议；未结提议索引拒绝第二个未结提议。
        """
        at = self._now()
        row = self.ledger._one("SELECT COALESCE(MAX(offer_seq), 0) AS last FROM ho_handoffs WHERE task_id=?",
                               (task_id,))
        seq = int(row["last"]) + 1
        self.ledger._exec(
            "INSERT INTO ho_handoffs(handoff_id, task_id, offer_seq, project_id, from_party, to_robot_id, state, "
            "target, evidence_refs, expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (handoff_id, task_id, seq, project_id, from_party, to_robot_id, "offered", _dump(target),
             _dump(evidence_refs), expires_at, at, at))
        self._event(handoff_id, "offered", actor, {"offer_seq": seq, "to_robot_id": to_robot_id,
                                                    "expires_at": expires_at}, at)
        return self.handoff(handoff_id)

    def set_delivery(self, handoff_id: str, delivery_id: str) -> None:
        self.ledger._exec("UPDATE ho_handoffs SET delivery_id=? WHERE handoff_id=? AND delivery_id IS NULL",
                          (delivery_id, handoff_id))

    def transition(self, handoff_id: str, expected: tuple[str, ...], state: str, *, actor: str, body: dict,
                   reply: dict | None = None, assignment_epoch: int | None = None) -> dict:
        """Compare-and-set the state; a row no longer in an expected state raises `StaleHandoff`.

        针对当前状态的比较后写入；不再处于预期状态的行抛出 `StaleHandoff`。
        """
        if state not in STATES:
            raise ValueError(f"unknown handoff state {state!r}")
        at = self._now()
        sets, args = ["state=?", "updated_at=?"], [state, at]
        if reply is not None:
            sets.append("reply=?")
            args.append(_dump(reply))
        if assignment_epoch is not None:
            sets.append("assignment_epoch=?")
            args.append(assignment_epoch)
        marks = ",".join("?" * len(expected))
        cursor = self.ledger._exec(f"UPDATE ho_handoffs SET {', '.join(sets)} WHERE handoff_id=? AND state IN "
                                   f"({marks})", (*args, handoff_id, *expected))
        if cursor.rowcount != 1:
            raise StaleHandoff(handoff_id)
        self._event(handoff_id, state, actor, body, at)
        return self.handoff(handoff_id)

    def note(self, handoff_id: str, kind: str, actor: str, body: dict) -> None:
        """An event that changes no state, e.g. a late or duplicate reply. / 不改变状态的事件，例如迟到或重复的回复。"""
        self._event(handoff_id, kind, actor, body, self._now())

    def _event(self, handoff_id: str, kind: str, actor: str, body: dict, at: str) -> None:
        row = self.ledger._one("SELECT COALESCE(MAX(seq), 0) AS last FROM ho_events WHERE handoff_id=?",
                               (handoff_id,))
        self.ledger._exec("INSERT INTO ho_events(handoff_id, seq, kind, at, actor, body) VALUES (?,?,?,?,?,?)",
                          (handoff_id, int(row["last"]) + 1, kind, at, actor, _dump(body)))

    def events(self, handoff_id: str) -> list[dict]:
        rows = self.ledger._rows("SELECT * FROM ho_events WHERE handoff_id=? ORDER BY seq", (handoff_id,))
        return [{**row, "body": _load(row["body"])} for row in rows]

    def expired(self, now: str) -> list[dict]:
        rows = self.ledger._rows("SELECT * FROM ho_handoffs WHERE state IN ('offered', 'accepted') AND expires_at<=? "
                                 "ORDER BY expires_at, handoff_id", (now,))
        return [self._handoff(row) for row in rows]

    # ── recheck links / 复核关联 ──

    @staticmethod
    def _recheck(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "aerial_evidence": _load(row["aerial_evidence"]),
                "ground_evidence": _load(row["ground_evidence"]), "detail": _load(row["detail"])}

    def recheck(self, recheck_id: str) -> dict | None:
        return self._recheck(self.ledger._one("SELECT * FROM bz_rechecks WHERE recheck_id=?", (recheck_id,)))

    def rechecks(self, *, finding_id: str | None = None, handoff_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM bz_rechecks WHERE 1=1", []
        if finding_id is not None:
            sql, args = sql + " AND finding_id=?", [*args, finding_id]
        if handoff_id is not None:
            sql, args = sql + " AND handoff_id=?", [*args, handoff_id]
        return [self._recheck(row) for row in self.ledger._rows(sql + " ORDER BY created_at, recheck_id", args)]

    def insert_recheck(self, *, recheck_id: str, project_id: str, finding_id: str, aerial_evidence: list[str],
                       ground_task_id: str | None, detail: dict) -> dict:
        at = self._now()
        self.ledger._exec(
            "INSERT INTO bz_rechecks(recheck_id, project_id, finding_id, aerial_evidence, ground_task_id, "
            "ground_evidence, outcome, detail, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (recheck_id, project_id, finding_id, _dump(aerial_evidence), ground_task_id, _dump([]), "pending",
             _dump(detail), at, at))
        return self.recheck(recheck_id)

    def update_recheck(self, recheck_id: str, *, handoff_id: str | None = None, ground_mission_id: str | None = None,
                       ground_evidence: list[str] | None = None, outcome: str | None = None,
                       detail: dict | None = None) -> dict:
        """Fill in what the handoff and the ground mission produced; a closed recheck never changes again.

        填入交接与地面任务产生的内容；已关闭的复核不再改变。
        """
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError(f"unknown recheck outcome {outcome!r}")
        at = self._now()
        sets, args = ["updated_at=?"], [at]
        for column, value in (("handoff_id", handoff_id), ("ground_mission_id", ground_mission_id)):
            if value is not None:
                sets.append(f"{column}=?")
                args.append(value)
        if ground_evidence is not None:
            sets.append("ground_evidence=?")
            args.append(_dump(ground_evidence))
        if detail is not None:
            sets.append("detail=?")
            args.append(_dump(detail))
        if outcome is not None:
            sets.append("outcome=?")
            args.append(outcome)
            if outcome != "pending":
                sets.append("closed_at=?")
                args.append(at)
        cursor = self.ledger._exec(f"UPDATE bz_rechecks SET {', '.join(sets)} WHERE recheck_id=? AND closed_at IS "
                                   "NULL", (*args, recheck_id))
        if cursor.rowcount != 1:
            raise StaleHandoff(recheck_id)
        return self.recheck(recheck_id)
