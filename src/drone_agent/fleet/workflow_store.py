"""P2 workflow tables on the mission ledger: the workflow extension migration and every workflow read and write (D058).

The nine `wf_` tables live in the same SQLite file as the v1 ledger and the P1 operations tables, so a trigger, a run
and its nodes, an outbox row, a cancel and the P1 cancel intents of its missions commit in one transaction. The
migration is additive and keeps `schema_version = 2`: the extension has its own `workflow_schema` key, so the P1
build still starts on a migrated ledger. A ledger with data is backed up and checked first; the DDL and the marker
commit together or not at all. Every write of a run's progress checks the run's lease owner and fencing epoch in the
same transaction, and node changes are compare-and-set on `state_version`, so a stale worker writes nothing.

任务账本上的 P2 工作流表：工作流扩展迁移与全部工作流读写（D058）。

九张 `wf_` 表与 v1 账本、P1 运营表位于同一个 SQLite 文件，使触发、运行及其节点、outbox 行、取消与其任务的 P1 取消
意图在一个事务中提交。迁移是增量的并保持 `schema_version = 2`：扩展有自己的 `workflow_schema` 键，因此 P1 版本仍能
在迁移后的账本上启动。已有数据的账本先备份并校验；DDL 与标记一起提交或完全不提交。运行进度的每次写入都在同一事务内
核对运行的租约持有者与 fencing 代次，节点变更对 `state_version` 做比较并交换，因此过期 worker 什么也写不进去。
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
from drone_agent.fleet.operations_store import SCHEMA_VERSION as OPERATIONS_SCHEMA
from drone_agent.fleet.operations_store import TABLES as OPERATIONS_TABLES
from drone_agent.fleet.operations_store import MigrationError, schema_version
from drone_agent.fleet.workflow_models import (
    CANCELLING_RUN,
    TERMINAL_RUN,
    NodeState,
    RunState,
    WorkflowCatalog,
    WorkflowSpec,
    run_principal,
)

WORKFLOW_SCHEMA = "1"
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS wf_catalogs (catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, "
    "body TEXT NOT NULL, fixtures TEXT NOT NULL, loaded_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS wf_schedules (project_id TEXT NOT NULL, workflow_id TEXT NOT NULL, "
    "trigger_id TEXT NOT NULL, version INTEGER NOT NULL, catalog_sha256 TEXT NOT NULL, state TEXT NOT NULL, "
    "cursor_at TEXT, changed_by TEXT NOT NULL, changed_at TEXT NOT NULL, reason TEXT NOT NULL, "
    "PRIMARY KEY (project_id, workflow_id, trigger_id))",
    "CREATE TABLE IF NOT EXISTS wf_triggers (project_id TEXT NOT NULL, workflow_id TEXT NOT NULL, "
    "version INTEGER NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL, disposition TEXT NOT NULL, run_id TEXT, "
    "actor TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, "
    "PRIMARY KEY (project_id, workflow_id, version, source, event_id))",
    "CREATE TABLE IF NOT EXISTS wf_runs (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workflow_id TEXT NOT NULL, "
    "version INTEGER NOT NULL, spec_sha256 TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, trigger_source TEXT NOT NULL, "
    "trigger_event TEXT NOT NULL, started_by TEXT NOT NULL, inputs TEXT NOT NULL, state TEXT NOT NULL, "
    "state_version INTEGER NOT NULL, cancel_epoch INTEGER NOT NULL, cancel TEXT, owner TEXT, "
    "owner_epoch INTEGER NOT NULL, lease_until TEXT, outcome TEXT, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS wf_runs_project ON wf_runs (project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS wf_runs_state ON wf_runs (state)",
    "CREATE TABLE IF NOT EXISTS wf_nodes (run_id TEXT NOT NULL, node_id TEXT NOT NULL, occurrence INTEGER NOT NULL, "
    "activity TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, reason TEXT, detail TEXT, "
    "result TEXT, deadline_at TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL, "
    "PRIMARY KEY (run_id, node_id, occurrence))",
    "CREATE TABLE IF NOT EXISTS wf_outbox (outbox_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
    "run_id TEXT NOT NULL, node_id TEXT NOT NULL, occurrence INTEGER NOT NULL, kind TEXT NOT NULL, "
    "payload TEXT NOT NULL, state TEXT NOT NULL, cancel_epoch INTEGER NOT NULL, claimed_by TEXT, claim_epoch INTEGER, "
    "attempts INTEGER NOT NULL, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS wf_outbox_state ON wf_outbox (state, created_at)",
    "CREATE TABLE IF NOT EXISTS wf_analyses (analysis_id TEXT PRIMARY KEY, activity_key TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, run_id TEXT NOT NULL, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, "
    "evidence_id TEXT NOT NULL, asset_id TEXT NOT NULL, analyzer TEXT NOT NULL, source TEXT NOT NULL, "
    "verdict TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS wf_reviews (review_id TEXT PRIMARY KEY, activity_key TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, run_id TEXT NOT NULL, analysis_id TEXT NOT NULL, reviewer TEXT NOT NULL, "
    "request_id TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS wf_work_orders (order_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, run_id TEXT NOT NULL, asset_id TEXT NOT NULL, review_id TEXT NOT NULL, "
    "analysis_id TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL, feedback TEXT, reinspection_run TEXT, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS wf_work_orders_project ON wf_work_orders (project_id, created_at)",
)
TABLES = ("wf_catalogs", "wf_schedules", "wf_triggers", "wf_runs", "wf_nodes", "wf_outbox", "wf_analyses",
          "wf_reviews", "wf_work_orders")
ACTIVE_OUTBOX = ("pending", "claimed")
# Mission statuses after which a mission is settled for a workflow. / 对工作流而言任务已了结的状态。
MISSION_TERMINAL = ("completed", "incomplete", "declined", "rejected", "refused", "planning_failed",
                    "delivery_rejected", "cancelled", "dispatch_expired")


class Fenced(RuntimeError):
    """A write from a worker that no longer owns the run; nothing was written. / 不再持有运行的 worker 的写入；未写入任何内容。"""


class StaleNode(RuntimeError):
    """A node changed since it was read; the caller re-reads and decides again. / 节点在读取后已变化；调用方重读后再决定。"""


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


def workflow_schema(ledger: BusinessLedger) -> str | None:
    row = ledger._one("SELECT value FROM meta WHERE key='workflow_schema'")
    return row["value"] if row else None


def migrate(ledger: BusinessLedger, *, backups: Path | None, statements: tuple[str, ...] = DDL,
            clock=utcnow) -> dict:
    """Add the workflow extension to a schema v2 ledger; back up and verify a database with data first (D058).

    为 schema v2 账本加上工作流扩展；已有数据的库先备份并校验（D058）。
    """
    with ledger._lock:
        if schema_version(ledger) != OPERATIONS_SCHEMA:
            raise MigrationError("the workflow extension needs the P1 operations schema v2 first")
        present = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in OPERATIONS_TABLES if name not in present]
        if missing:
            raise MigrationError(f"operations schema is missing tables {missing}")
        version = workflow_schema(ledger)
        if version == WORKFLOW_SCHEMA:
            missing = [name for name in TABLES if name not in present]
            if missing:
                raise MigrationError(f"workflow schema {WORKFLOW_SCHEMA} is missing tables {missing}")
            return {"status": "current", "workflow_schema": WORKFLOW_SCHEMA}
        if version is not None:
            raise MigrationError(f"unexpected workflow schema version {version!r}")
        counts = _counts(ledger._db)
        backup = None
        has_data = any(counts.get(name, 0) for name in ("requests", "missions", "deliveries", "events"))
        if ledger.path is not None and has_data:
            if backups is None:
                raise MigrationError("a ledger with data needs a backup directory before migrating")
            stamp = clock().strftime("%Y%m%dT%H%M%S%fZ")
            target = backups / f"ledger-v2-{stamp}.sqlite3"
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
                db.execute("INSERT INTO meta(key, value) VALUES ('workflow_schema', ?)", (WORKFLOW_SCHEMA,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('workflow_migrated_at', ?)",
                           (clock().isoformat(),))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('workflow_backup', ?)",
                           (json.dumps(backup, sort_keys=True),))
        except sqlite3.Error as error:
            raise MigrationError(f"workflow schema migration rolled back: {error}") from error
        return {"status": "migrated", "workflow_schema": WORKFLOW_SCHEMA, "backup": backup, "rows_before": counts}


_WORKFLOW_LINE = re.compile(r'^(CREATE (TABLE|UNIQUE INDEX|INDEX) (IF NOT EXISTS )?"?wf_|INSERT INTO "wf_)')


def other_lines(path: Path) -> list[str]:
    """The SQL dump of everything but the workflow extension: wf_* objects and workflow meta keys are left out.

    除工作流扩展以外的 SQL 转储：去掉 wf_* 对象与工作流 meta 键。
    """
    with sqlite3.connect(str(path)) as db:
        dump = list(db.iterdump())
    return [line for line in dump if not _WORKFLOW_LINE.match(line)
            and not (line.startswith('INSERT INTO "meta"') and "'workflow_" in line)]


def drill(path: Path) -> dict:
    """D058 drill on a copy of a ledger: migrate it and prove the v1 and P1 data unchanged; counts only, never content.

    在账本副本上做 D058 演练：迁移并证明 v1 与 P1 数据不变；只报告计数，从不报告内容。
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
                            if row[0].startswith("wf_"))
    finally:
        ledger.close()
    after = dump_digest(path)
    return {"status": "passed" if report["status"] in ("migrated", "current") and integrity == "ok"
            and before == after and tables == sorted(TABLES) else "failed",
            "migration": report["status"], "workflow_schema": WORKFLOW_SCHEMA, "integrity": integrity,
            "other_dump_sha256_before": before, "other_dump_sha256_after": after, "wf_tables": len(tables),
            "rows_before": report.get("rows_before"), "backup": report.get("backup")}


class WorkflowStore:
    """Reads and writes of the workflow tables; progress writes are fenced by the run's lease.

    工作流表的读写；运行进度的写入受运行租约的 fencing 约束。
    """

    def __init__(self, ledger: BusinessLedger, catalog: WorkflowCatalog, fixtures: dict[str, str], *, clock=utcnow):
        if schema_version(ledger) != OPERATIONS_SCHEMA or workflow_schema(ledger) != WORKFLOW_SCHEMA:
            raise MigrationError("run migrate() before opening the workflow store")
        self.ledger, self.catalog, self.fixtures, self.clock = ledger, catalog, dict(fixtures), clock
        self.catalogs: dict[str, WorkflowCatalog] = {}
        self._record_catalog(catalog, fixtures)

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
        """Workflow audit rows share the P1 audit stream. / 工作流审计行与 P1 审计流共用。"""
        self._exec("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)",
                   (subject, kind, actor, _dump(body or {}), self._now()))

    def events(self, subject: str, limit: int = 200) -> list[dict]:
        rows = self._rows("SELECT * FROM op_events WHERE subject=? ORDER BY id DESC LIMIT ?", (subject, limit))
        return [{**row, "body": _load(row["body"])} for row in reversed(rows)]

    # ── catalogs / 目录 ──

    def _record_catalog(self, catalog: WorkflowCatalog, fixtures: dict[str, str]) -> None:
        """Record the loaded catalog; a version whose content changed is refused (runs pin versions).

        记录加载的目录；内容改变的版本被拒绝（运行固定版本）。
        """
        with self.transaction():
            for row in self._rows("SELECT catalog_sha256, body FROM wf_catalogs"):
                known = WorkflowCatalog.model_validate(_load(row["body"]))
                self.catalogs[row["catalog_sha256"]] = known
                for old in known.workflows:
                    new = catalog.spec(old.project_id, old.workflow_id, old.version)
                    if new is not None and new.sha256 != old.sha256:
                        raise MigrationError(f"{old.workflow_id} v{old.version} changed; publish a new version")
            self._exec("INSERT OR IGNORE INTO wf_catalogs VALUES (?,?,?,?,?)",
                       (catalog.sha256, catalog.catalog_id, _dump(catalog), _dump(fixtures), self._now()))
        self.catalogs[catalog.sha256] = catalog

    def pinned(self, catalog_sha256: str) -> WorkflowCatalog:
        found = self.catalogs.get(catalog_sha256)
        if found is None:
            row = self._one("SELECT body FROM wf_catalogs WHERE catalog_sha256=?", (catalog_sha256,))
            if row is None:
                raise MigrationError(f"catalog {catalog_sha256[:12]} is not recorded")
            found = self.catalogs[catalog_sha256] = WorkflowCatalog.model_validate(_load(row["body"]))
        return found

    def fixtures_of(self, catalog_sha256: str) -> dict[str, str]:
        row = self._one("SELECT fixtures FROM wf_catalogs WHERE catalog_sha256=?", (catalog_sha256,))
        return _load(row["fixtures"]) if row else {}

    def spec_of(self, run: dict) -> WorkflowSpec:
        spec = self.pinned(run["catalog_sha256"]).spec(run["project_id"], run["workflow_id"], run["version"])
        if spec is None or spec.sha256 != run["spec_sha256"]:
            raise MigrationError(f"run {run['run_id']} does not match its pinned template")
        return spec

    # ── triggers and runs / 触发与运行 ──

    def trigger(self, project_id: str, workflow_id: str, version: int, source: str, event_id: str) -> dict | None:
        row = self._one("SELECT * FROM wf_triggers WHERE project_id=? AND workflow_id=? AND version=? AND source=? "
                        "AND event_id=?", (project_id, workflow_id, version, source, event_id))
        return None if row is None else {**row, "body": _load(row["body"])}

    def triggers(self, project_id: str, workflow_id: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM wf_triggers WHERE project_id=?", [project_id]
        if workflow_id is not None:
            sql, args = sql + " AND workflow_id=?", [*args, workflow_id]
        rows = self._rows(sql + " ORDER BY received_at DESC LIMIT ?", [*args, limit])
        return [{**row, "body": _load(row["body"])} for row in rows]

    def record_trigger(self, spec: WorkflowSpec, *, source: str, event_id: str, disposition: str, actor: str,
                       body: dict) -> bool:
        """A trigger that started nothing (missed or refused), once per key. / 未启动运行的触发（错过或拒绝），每键一次。"""
        return self._exec("INSERT OR IGNORE INTO wf_triggers VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (spec.project_id, spec.workflow_id, spec.version, source, event_id, disposition, None,
                           actor, _dump(body), self._now())).rowcount == 1

    def start_run(self, spec: WorkflowSpec, *, catalog_sha256: str, source: str, event_id: str, actor: str,
                  started_by: str, inputs: dict, body: dict) -> tuple[str, bool]:
        """Create the run and all its nodes for a new trigger key; a repeated key returns the first run.

        为新的触发键创建运行及其全部节点；重复的键返回第一次的运行。
        """
        with self.transaction():
            known = self.trigger(spec.project_id, spec.workflow_id, spec.version, source, event_id)
            if known is not None:
                return known["run_id"], False
            run_id = "wr-" + uuid.uuid4().hex[:20]
            now = self._now()
            self._exec("INSERT INTO wf_triggers VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (spec.project_id, spec.workflow_id, spec.version, source, event_id, "started", run_id, actor,
                        _dump(body), now))
            self._exec("INSERT INTO wf_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, spec.project_id, spec.workflow_id, spec.version, spec.sha256, catalog_sha256, source,
                        event_id, started_by, _dump(inputs), RunState.PENDING.value, 1, 0, None, None, 0, None, None,
                        now, now))
            for node in spec.nodes:
                self._exec("INSERT INTO wf_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, node.node_id, 1, node.activity, NodeState.PENDING.value, 1, None, None, None,
                            None, None, None, now))
            self.event(f"workflow:{run_id}", "run.started", actor,
                       {"workflow_id": spec.workflow_id, "version": spec.version, "source": source,
                        "event_id": event_id, "inputs": inputs, "started_by": started_by})
        return run_id, True

    @staticmethod
    def _decode_run(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "inputs": _load(row["inputs"]), "cancel": _load(row["cancel"]), "outcome": _load(row["outcome"])}

    def run(self, run_id: str) -> dict | None:
        return self._decode_run(self._one("SELECT * FROM wf_runs WHERE run_id=?", (run_id,)))

    def runs(self, project_id: str, limit: int = 50) -> list[dict]:
        return [self._decode_run(r) for r in self._rows("SELECT * FROM wf_runs WHERE project_id=? "
                                                         "ORDER BY created_at DESC LIMIT ?", (project_id, limit))]

    def active_runs(self) -> list[dict]:
        marks = ",".join("?" * len(TERMINAL_RUN))
        return [self._decode_run(r) for r in self._rows(
            f"SELECT * FROM wf_runs WHERE state NOT IN ({marks}) ORDER BY created_at",
            tuple(s.value for s in TERMINAL_RUN))]

    def children(self, run_id: str) -> list[dict]:
        """Runs a run started (reinspection). / 由本运行启动的运行（复检）。"""
        return [self._decode_run(r) for r in self._rows("SELECT * FROM wf_runs WHERE started_by=? ORDER BY created_at",
                                                         (run_principal(run_id),))]

    # ── lease and fencing / 租约与 fencing ──

    def acquire(self, run_id: str, worker: str, lease_s: float) -> int | None:
        """Take or renew the run's lease; a takeover increments the fencing epoch. None when held elsewhere.

        获取或续租运行的租约；接管时 fencing 代次加一。被他人持有时返回 None。
        """
        now = self.clock()
        until = (now + timedelta(seconds=lease_s)).isoformat()
        with self.transaction():
            row = self._one("SELECT owner, owner_epoch, lease_until, state FROM wf_runs WHERE run_id=?", (run_id,))
            if row is None or RunState(row["state"]) in TERMINAL_RUN:
                return None
            expires = datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None
            alive = expires is not None and expires > now
            if row["owner"] == worker and alive:
                # Renew only in the second half of the lease, to keep the write rate low. / 只在租约后半段续租，降低写频率。
                if (expires - now).total_seconds() < lease_s / 2:
                    self._exec("UPDATE wf_runs SET lease_until=? WHERE run_id=? AND owner=? AND owner_epoch=?",
                               (until, run_id, worker, row["owner_epoch"]))
                return row["owner_epoch"]
            if row["owner"] is not None and alive:
                return None
            epoch = row["owner_epoch"] + 1
            self._exec("UPDATE wf_runs SET owner=?, owner_epoch=?, lease_until=? WHERE run_id=? AND owner_epoch=?",
                       (worker, epoch, until, run_id, row["owner_epoch"]))
            self.event(f"workflow:{run_id}", "lease.taken", worker,
                       {"epoch": epoch, "previous_owner": row["owner"], "previous_epoch": row["owner_epoch"]})
            return epoch

    def fence(self, run_id: str, worker: str, epoch: int) -> dict:
        """Inside a transaction: the run as its current owner sees it, or Fenced. / 事务内：以当前持有者身份读取运行，否则 Fenced。"""
        row = self._one("SELECT * FROM wf_runs WHERE run_id=?", (run_id,))
        if row is None or row["owner"] != worker or row["owner_epoch"] != epoch:
            raise Fenced(f"{worker} epoch {epoch} no longer owns {run_id}")
        return self._decode_run(row)

    def set_run_state(self, run_id: str, *, worker: str, epoch: int, state: RunState, outcome: dict | None = None,
                      allowed_from: tuple[RunState, ...] | None = None) -> bool:
        with self.transaction():
            run = self.fence(run_id, worker, epoch)
            current = RunState(run["state"])
            if current is state and outcome is None:
                return False
            if current in TERMINAL_RUN or (allowed_from is not None and current not in allowed_from):
                return False
            if current in CANCELLING_RUN and state not in (RunState.CANCELLING, RunState.CANCELLED):
                # A persisted cancel is never undone by ordinary progress. / 普通推进从不撤销已持久化的取消。
                return False
            self._exec("UPDATE wf_runs SET state=?, state_version=state_version+1, outcome=COALESCE(?, outcome), "
                       "updated_at=? WHERE run_id=? AND state_version=?",
                       (state.value, _dump(outcome) if outcome is not None else None, self._now(), run_id,
                        run["state_version"]))
            if current is not state:
                self.event(f"workflow:{run_id}", "run.state", worker, {"from": current.value, "to": state.value,
                                                                      "outcome": outcome})
            return True

    # ── nodes / 节点 ──

    @staticmethod
    def _decode_node(row: dict) -> dict:
        return {**row, "detail": _load(row["detail"]), "result": _load(row["result"])}

    def nodes(self, run_id: str) -> dict[str, dict]:
        return {row["node_id"]: self._decode_node(row) for row in self._rows(
            "SELECT * FROM wf_nodes WHERE run_id=? AND occurrence=1 ORDER BY rowid", (run_id,))}

    def transition(self, run_id: str, node_id: str, *, worker: str, epoch: int, version: int, state: NodeState,
                   reason: str | None = None, detail: dict | None = None, result: dict | None = None,
                   deadline: datetime | None = None, outbox: dict | None = None) -> int:
        """Compare-and-set one node, fenced by the run's lease; an effect's outbox row commits with it.

        对一个节点做比较并交换，受运行租约 fencing 约束；效果的 outbox 行与之一起提交。
        """
        now = self._now()
        terminal = state in (NodeState.COMPLETED, NodeState.SKIPPED, NodeState.FAILED, NodeState.OUTCOME_UNKNOWN,
                             NodeState.CANCELLED)
        # A node counts as started once it runs, waits or ends by itself; skipped and cancelled nodes never started.
        # 节点一旦运行、等待或自行结束即算已开始；被跳过与被取消的节点从未开始。
        started = state in (NodeState.RUNNING, NodeState.WAITING, NodeState.COMPLETED, NodeState.FAILED,
                            NodeState.OUTCOME_UNKNOWN)
        with self.transaction():
            run = self.fence(run_id, worker, epoch)
            if outbox is not None and (RunState(run["state"]) in CANCELLING_RUN or RunState(run["state"]) in
                                       TERMINAL_RUN):
                raise StaleNode(f"{run_id} is {run['state']}; no new effect may start")
            changed = self._exec(
                "UPDATE wf_nodes SET state=?, state_version=state_version+1, reason=?, detail=?, result=?, "
                "deadline_at=?, started_at=COALESCE(started_at, ?), finished_at=?, updated_at=? "
                "WHERE run_id=? AND node_id=? AND occurrence=1 AND state_version=?",
                (state.value, reason, _dump(detail) if detail is not None else None,
                 _dump(result) if result is not None else None, deadline.isoformat() if deadline else None,
                 now if started else None, now if terminal else None, now, run_id, node_id, version)).rowcount
            if changed != 1:
                raise StaleNode(f"{run_id}/{node_id} changed since version {version}")
            if outbox is not None:
                self._exec("INSERT INTO wf_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("ob-" + uuid.uuid4().hex[:20], outbox["key"], run_id, node_id, 1, outbox["kind"],
                            _dump(outbox["payload"]), "pending", run["cancel_epoch"], None, None, 0, None, now, now))
            self.event(f"workflow:{run_id}", "node.state", worker,
                       {"node_id": node_id, "state": state.value, "reason": reason,
                        "outbox": outbox["kind"] if outbox else None})
        return version + 1

    # ── outbox / outbox ──

    @staticmethod
    def _decode_outbox(row: dict | None) -> dict | None:
        return None if row is None else {**row, "payload": _load(row["payload"]), "result": _load(row["result"])}

    def outbox(self, run_id: str, states: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM wf_outbox WHERE run_id=?", [run_id]
        if states is not None:
            sql, args = sql + f" AND state IN ({','.join('?' * len(states))})", [*args, *states]
        return [self._decode_outbox(r) for r in self._rows(sql + " ORDER BY created_at", args)]

    def outbox_row(self, outbox_id: str) -> dict | None:
        return self._decode_outbox(self._one("SELECT * FROM wf_outbox WHERE outbox_id=?", (outbox_id,)))

    def claim(self, outbox_id: str, *, worker: str, epoch: int) -> dict | None:
        """Claim a row for delivery; a cancel that committed first voids it instead (09-operations §4.3).

        领取一行用于投递；先提交的取消会使其作废（09-operations §4.3）。
        """
        with self.transaction():
            row = self.outbox_row(outbox_id)
            if row is None or row["state"] not in ACTIVE_OUTBOX:
                return None
            run = self.fence(row["run_id"], worker, epoch)
            if RunState(run["state"]) in CANCELLING_RUN or row["cancel_epoch"] != run["cancel_epoch"]:
                if row["state"] == "pending":
                    self._void(outbox_id, "cancelled")
                return None
            self._exec("UPDATE wf_outbox SET state='claimed', claimed_by=?, claim_epoch=?, attempts=attempts+1, "
                       "updated_at=? WHERE outbox_id=? AND state IN ('pending','claimed')",
                       (worker, epoch, self._now(), outbox_id))
            return self.outbox_row(outbox_id)

    def claim_guard(self, outbox_id: str, worker: str, epoch: int):
        """For the consumer's transaction: the row is still ours and no cancel committed since the claim.

        供消费者事务使用：该行仍由我方领取，且自领取后没有提交任何取消。
        """

        def guard(db: sqlite3.Connection) -> bool:
            row = db.execute("SELECT o.state, o.claimed_by, o.claim_epoch, o.cancel_epoch, r.state, r.cancel_epoch, "
                             "r.owner, r.owner_epoch FROM wf_outbox o JOIN wf_runs r ON r.run_id = o.run_id "
                             "WHERE o.outbox_id=?", (outbox_id,)).fetchone()
            return row is not None and row[0] == "claimed" and row[1] == worker and row[2] == epoch \
                and row[3] == row[5] and row[4] not in {s.value for s in CANCELLING_RUN | TERMINAL_RUN} \
                and row[6] == worker and row[7] == epoch
        return guard

    def delivered(self, outbox_id: str, *, worker: str, epoch: int, result: dict, node_version: int,
                  node_result: dict) -> int:
        """Record a delivery and complete its node in one transaction. / 在一个事务中记录投递并完成其节点。"""
        with self.transaction():
            row = self.outbox_row(outbox_id)
            if row is None or row["state"] != "claimed":
                raise StaleNode(f"{outbox_id} is not claimed")
            self.fence(row["run_id"], worker, epoch)
            self._exec("UPDATE wf_outbox SET state='delivered', result=?, updated_at=? WHERE outbox_id=? "
                       "AND state='claimed'", (_dump(result), self._now(), outbox_id))
            return self.transition(row["run_id"], row["node_id"], worker=worker, epoch=epoch, version=node_version,
                                   state=NodeState.COMPLETED, result=node_result)

    def settle_outbox(self, outbox_id: str, *, worker: str, epoch: int, state: str, result: dict) -> bool:
        """End a row without completing its node: `void` (never delivered) or `failed` (refused for good).

        不完成节点地结束一行：`void`（从未投递）或 `failed`（被永久拒绝）。
        """
        with self.transaction():
            row = self.outbox_row(outbox_id)
            if row is None or row["state"] not in ACTIVE_OUTBOX:
                return False
            self.fence(row["run_id"], worker, epoch)
            return self._exec("UPDATE wf_outbox SET state=?, result=?, updated_at=? WHERE outbox_id=? "
                              "AND state IN ('pending','claimed')",
                              (state, _dump(result), self._now(), outbox_id)).rowcount == 1

    def _void(self, outbox_id: str, reason: str) -> None:
        self._exec("UPDATE wf_outbox SET state='void', result=?, updated_at=? WHERE outbox_id=? AND state='pending'",
                   (_dump({"reason": reason}), self._now(), outbox_id))

    # ── cancellation / 取消 ──

    def child_missions(self, run_id: str) -> list[dict]:
        """Every mission this run created, found by the requester name every one of them carries.

        本运行创建的全部任务，按它们都携带的请求者名查找。
        """
        return self._rows("SELECT r.mission_id, r.idempotency_key, m.status FROM requests r JOIN missions m "
                          "ON m.mission_id = r.mission_id WHERE r.requested_by=? ORDER BY r.received_at",
                          (run_principal(run_id),))

    def request_cancel(self, run_id: str, *, actor: str, request_id: str, reason: str) -> dict:
        """Persist the cancel before any further dispatch: bump the cancel epoch, void unclaimed effects, and write
        the P1 cancel intent of every unsettled mission and a cancel of every child run, in one transaction.

        在任何后续派遣之前持久化取消：在一个事务中提升取消代次、作废未领取的效果，并为每个未了结的任务写入 P1 取消
        意图、取消每个子运行。
        """
        now = self._now()
        with self.transaction():
            run = self.run(run_id)
            state = RunState(run["state"])
            if state in CANCELLING_RUN or state is RunState.CANCELLED:
                return {"status": "already", "cancel": run["cancel"]}
            if state in TERMINAL_RUN:
                return {"status": "final", "state": state.value}
            cancel = {"requested_by": actor, "request_id": request_id, "reason": reason[:300], "requested_at": now,
                      "epoch": run["cancel_epoch"] + 1}
            self._exec("UPDATE wf_runs SET state=?, state_version=state_version+1, cancel_epoch=?, cancel=?, "
                       "updated_at=? WHERE run_id=? AND state_version=?",
                       (RunState.CANCEL_REQUESTED.value, cancel["epoch"], _dump(cancel), now, run_id,
                        run["state_version"]))
            voided = self._exec("UPDATE wf_outbox SET state='void', result=?, updated_at=? WHERE run_id=? "
                                "AND state='pending'", (_dump({"reason": "cancelled"}), now, run_id)).rowcount
            missions = []
            for row in self.child_missions(run_id):
                if row["status"] in MISSION_TERMINAL:
                    continue
                created = self._exec("INSERT OR IGNORE INTO op_cancellations VALUES (?,?,?,?,?,?)",
                                     (row["mission_id"], actor, f"{request_id}-{row['mission_id'][2:]}"[:80],
                                      f"workflow {run_id} cancelled", now, None)).rowcount == 1
                if created:
                    self.event(f"mission:{row['mission_id']}", "cancel.requested", actor,
                               {"request_id": request_id, "workflow_run": run_id})
                missions.append(row["mission_id"])
            children, cascaded = [], []
            for child in self.children(run_id):
                if RunState(child["state"]) not in TERMINAL_RUN | CANCELLING_RUN:
                    result = self.request_cancel(child["run_id"], actor=actor, request_id=request_id,
                                                 reason=f"parent {run_id} cancelled")
                    children.append(child["run_id"])
                    cascaded += result.get("missions", [])
            self.event(f"workflow:{run_id}", "run.cancel_requested", actor,
                       {**cancel, "voided_outbox": voided, "missions": missions, "child_runs": children})
        return {"status": "requested", "run_id": run_id, "cancel": cancel, "missions": missions + cascaded,
                "child_runs": children}

    # ── schedules / 排班 ──

    def schedule(self, project_id: str, workflow_id: str, trigger_id: str) -> dict | None:
        return self._one("SELECT * FROM wf_schedules WHERE project_id=? AND workflow_id=? AND trigger_id=?",
                         (project_id, workflow_id, trigger_id))

    def schedules(self, project_id: str | None = None) -> list[dict]:
        if project_id is None:
            return self._rows("SELECT * FROM wf_schedules ORDER BY project_id, workflow_id, trigger_id")
        return self._rows("SELECT * FROM wf_schedules WHERE project_id=? ORDER BY workflow_id, trigger_id",
                          (project_id,))

    def set_schedule(self, project_id: str, workflow_id: str, trigger_id: str, *, state: str, version: int,
                     catalog_sha256: str, cursor: datetime | None, actor: str, reason: str) -> dict:
        with self.transaction():
            self._exec("INSERT INTO wf_schedules VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id, workflow_id, "
                       "trigger_id) DO UPDATE SET version=excluded.version, catalog_sha256=excluded.catalog_sha256, "
                       "state=excluded.state, cursor_at=excluded.cursor_at, changed_by=excluded.changed_by, "
                       "changed_at=excluded.changed_at, reason=excluded.reason",
                       (project_id, workflow_id, trigger_id, version, catalog_sha256, state,
                        cursor.isoformat() if cursor else None, actor, self._now(), reason[:300]))
            self.event(f"schedule:{project_id}:{workflow_id}:{trigger_id}", f"schedule.{state}", actor,
                       {"version": version, "cursor_at": cursor.isoformat() if cursor else None,
                        "reason": reason[:300]})
        return self.schedule(project_id, workflow_id, trigger_id)

    def advance_cursor(self, project_id: str, workflow_id: str, trigger_id: str, before: str, after: datetime) -> bool:
        """Move an enabled schedule's cursor forward once; a second engine sees 0 rows. / 游标只前进一次。"""
        return self._exec("UPDATE wf_schedules SET cursor_at=? WHERE project_id=? AND workflow_id=? AND trigger_id=? "
                          "AND state='enabled' AND cursor_at=?",
                          (after.isoformat(), project_id, workflow_id, trigger_id, before)).rowcount == 1

    # ── analyses, reviews and work orders / 分析、复核与工单 ──

    def record_analysis(self, *, activity_key: str, project_id: str, run_id: str, mission_id: str,
                        mission_version: int, evidence_id: str, asset_id: str, analyzer: str, source: str,
                        verdict: str, body: dict) -> dict:
        """One analysis per activity key; a repeat returns the first. / 每个活动键一次分析；重复返回第一次。"""
        known = self.analysis(activity_key)
        if known is not None:
            return known
        self._exec("INSERT INTO wf_analyses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("an-" + uuid.uuid4().hex[:20], activity_key, project_id, run_id, mission_id, mission_version,
                    evidence_id, asset_id, analyzer, source, verdict, _dump(body), self._now()))
        return self.analysis(activity_key)

    def analysis(self, activity_key: str) -> dict | None:
        row = self._one("SELECT * FROM wf_analyses WHERE activity_key=?", (activity_key,))
        return None if row is None else {**row, "body": _load(row["body"])}

    def analyses(self, run_id: str) -> list[dict]:
        return [{**row, "body": _load(row["body"])}
                for row in self._rows("SELECT * FROM wf_analyses WHERE run_id=? ORDER BY created_at", (run_id,))]

    def review(self, activity_key: str) -> dict | None:
        return self._one("SELECT * FROM wf_reviews WHERE activity_key=?", (activity_key,))

    def reviews(self, run_id: str) -> list[dict]:
        return self._rows("SELECT * FROM wf_reviews WHERE run_id=? ORDER BY created_at", (run_id,))

    def record_review(self, *, activity_key: str, project_id: str, run_id: str, analysis_id: str, reviewer: str,
                      request_id: str, decision: str, note: str) -> tuple[dict, bool]:
        """The first decision of a review node wins. / 复核节点的首个决定有效。"""
        created = self._exec("INSERT OR IGNORE INTO wf_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                             ("rv-" + uuid.uuid4().hex[:20], activity_key, project_id, run_id, analysis_id, reviewer,
                              request_id, decision, note[:300], self._now())).rowcount == 1
        return self.review(activity_key), created

    @staticmethod
    def _decode_order(row: dict | None) -> dict | None:
        return None if row is None else {**row, "body": _load(row["body"]), "feedback": _load(row["feedback"])}

    def order(self, order_id: str) -> dict | None:
        return self._decode_order(self._one("SELECT * FROM wf_work_orders WHERE order_id=?", (order_id,)))

    def order_by_key(self, idempotency_key: str) -> dict | None:
        return self._decode_order(self._one("SELECT * FROM wf_work_orders WHERE idempotency_key=?",
                                            (idempotency_key,)))

    def orders(self, project_id: str, limit: int = 50) -> list[dict]:
        return [self._decode_order(r) for r in self._rows(
            "SELECT * FROM wf_work_orders WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))]

    def run_orders(self, run_id: str) -> list[dict]:
        return [self._decode_order(r) for r in self._rows("SELECT * FROM wf_work_orders WHERE run_id=? "
                                                           "ORDER BY created_at", (run_id,))]

    def create_order(self, *, idempotency_key: str, project_id: str, run_id: str, asset_id: str, review_id: str,
                     analysis_id: str, body: dict, actor: str, guard=None) -> tuple[dict | None, bool]:
        """The simulated work-order system: one order per idempotency key; `guard` refuses a new order after a cancel.

        模拟工单系统：每个幂等键一张工单；取消之后 `guard` 拒绝新建工单。
        """
        with self.transaction() as db:
            known = self.order_by_key(idempotency_key)
            if known is not None:
                return known, False
            if guard is not None and not guard(db):
                return None, False
            order_id = "wo-" + uuid.uuid4().hex[:16]
            now = self._now()
            self._exec("INSERT INTO wf_work_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (order_id, idempotency_key, project_id, run_id, asset_id, review_id, analysis_id, "open",
                        _dump(body), None, None, now, now))
            self.event(f"order:{order_id}", "order.created", actor, {"run_id": run_id, "asset_id": asset_id,
                                                                     "review_id": review_id})
        return self.order(order_id), True

    def record_feedback(self, order_id: str, feedback: dict, actor: str) -> tuple[dict, bool]:
        """Repair feedback moves an open order to `repair_reported`, once; it never closes it.

        维修反馈把未结工单转为 `repair_reported`，只一次；从不关单。
        """
        with self.transaction():
            applied = self._exec("UPDATE wf_work_orders SET state='repair_reported', feedback=?, updated_at=? "
                                 "WHERE order_id=? AND state='open'",
                                 (_dump(feedback), self._now(), order_id)).rowcount == 1
            if applied:
                self.event(f"order:{order_id}", "order.repair_reported", actor, feedback)
        return self.order(order_id), applied

    def link_reinspection(self, order_id: str, run_id: str, actor: str) -> bool:
        applied = self._exec("UPDATE wf_work_orders SET state='reinspection_requested', reinspection_run=?, "
                             "updated_at=? WHERE order_id=? AND state='repair_reported'",
                             (run_id, self._now(), order_id)).rowcount == 1
        if applied:
            self.event(f"order:{order_id}", "order.reinspection_requested", actor, {"run_id": run_id})
        return applied


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill", type=Path, required=True, help="a copy of the ledger to migrate (D058 drill)")
    args = parser.parse_args()
    result = drill(args.drill)
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
