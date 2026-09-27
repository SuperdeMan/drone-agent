"""P4 business tables on the mission ledger: the business extension migration and every business read and write (D064).

The seven `bz_` tables live in the same SQLite file as the v1 ledger and the P1, P2 and P3 extensions, so a job
result, the finding it joins, a review, an order, a round of repair feedback and the reinspection run it starts
commit in one transaction with the workflow state they belong to. The migration is additive and keeps
`schema_version = 2` and the other extension keys: the business extension has its own `business_schema` key, so the
P3 build still starts on a migrated ledger. A ledger with data is backed up and checked first; the DDL and the
marker commit together or not at all. `bz_open_finding` keeps at most one open finding per cluster, and the unique
finding of an order and run of a round make duplicate orders and reinspections impossible whoever races.

任务账本上的 P4 业务表：业务扩展迁移与全部业务读写（D064）。

七张 `bz_` 表与 v1 账本及 P1、P2、P3 扩展位于同一个 SQLite 文件，使作业结果、它所挂的发现、复核、工单、维修反馈轮次及其
启动的复检运行与所属工作流状态在一个事务中提交。迁移是增量的，保持 `schema_version = 2` 与其他扩展键：业务扩展有自己的
`business_schema` 键，因此 P3 版本仍能在迁移后的账本上启动。已有数据的账本先备份并校验；DDL 与标记一起提交或完全不
提交。`bz_open_finding` 保证每个聚合键至多一个未结发现；工单的发现唯一、轮次的运行唯一，无论谁在竞争都不会出现重复
工单或重复复检。
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
from drone_agent.fleet.business_models import (
    BusinessCatalog,
    FindingState,
    JobInputs,
    JobResult,
    JobState,
    OrderState,
    RoundState,
)
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import SCHEMA_VERSION as OPERATIONS_SCHEMA
from drone_agent.fleet.operations_store import TABLES as OPERATIONS_TABLES
from drone_agent.fleet.operations_store import MigrationError, schema_version
from drone_agent.fleet.workflow_store import TABLES as WORKFLOW_TABLES
from drone_agent.fleet.workflow_store import WORKFLOW_SCHEMA, workflow_schema

BUSINESS_SCHEMA = "1"
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS bz_catalogs (catalog_sha256 TEXT PRIMARY KEY, catalog_id TEXT NOT NULL, "
    "body TEXT NOT NULL, profiles TEXT NOT NULL, loaded_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS bz_jobs (job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, requested_by TEXT NOT NULL, purpose TEXT NOT NULL, analyzer TEXT NOT NULL, "
    "analyzer_sha256 TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, asset_key TEXT NOT NULL, evidence_id TEXT NOT NULL, "
    "media_sha256 TEXT NOT NULL, inputs TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, "
    "attempts INTEGER NOT NULL, owner TEXT, owner_epoch INTEGER NOT NULL, lease_until TEXT, verdict TEXT, "
    "source TEXT, result TEXT, finding_id TEXT, order_id TEXT, round INTEGER, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS bz_jobs_state ON bz_jobs (state, created_at)",
    "CREATE INDEX IF NOT EXISTS bz_jobs_asset ON bz_jobs (asset_key, created_at)",
    "CREATE INDEX IF NOT EXISTS bz_jobs_media ON bz_jobs (project_id, media_sha256)",
    "CREATE TABLE IF NOT EXISTS bz_references (reference_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
    "asset_key TEXT NOT NULL, evidence_id TEXT NOT NULL, media_sha256 TEXT NOT NULL, body TEXT NOT NULL, "
    "state TEXT NOT NULL, registered_by TEXT NOT NULL, registered_at TEXT NOT NULL, revoked_by TEXT, revoked_at TEXT, "
    "UNIQUE (asset_key, evidence_id))",
    "CREATE TABLE IF NOT EXISTS bz_findings (finding_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
    "asset_key TEXT NOT NULL, family TEXT NOT NULL, cluster_key TEXT NOT NULL, state TEXT NOT NULL, "
    "state_version INTEGER NOT NULL, first_job TEXT NOT NULL, last_job TEXT NOT NULL, jobs INTEGER NOT NULL, "
    "review_id TEXT, order_id TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "closed_at TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS bz_open_finding ON bz_findings (cluster_key) "
    "WHERE state IN ('candidate', 'confirmed')",
    "CREATE INDEX IF NOT EXISTS bz_findings_project ON bz_findings (project_id, created_at)",
    "CREATE TABLE IF NOT EXISTS bz_reviews (review_id TEXT PRIMARY KEY, subject_kind TEXT NOT NULL, "
    "subject_id TEXT NOT NULL, project_id TEXT NOT NULL, reviewer TEXT NOT NULL, request_id TEXT NOT NULL, "
    "decision TEXT NOT NULL, note TEXT NOT NULL, basis TEXT NOT NULL, created_at TEXT NOT NULL, "
    "UNIQUE (subject_kind, subject_id))",
    "CREATE TABLE IF NOT EXISTS bz_orders (order_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL UNIQUE, "
    "idempotency_key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL, asset_key TEXT NOT NULL, review_id TEXT NOT NULL, "
    "catalog_sha256 TEXT NOT NULL, reinspection TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, "
    "round INTEGER NOT NULL, created_by TEXT NOT NULL, body TEXT NOT NULL, closure TEXT, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, closed_at TEXT)",
    "CREATE INDEX IF NOT EXISTS bz_orders_project ON bz_orders (project_id, created_at)",
    "CREATE TABLE IF NOT EXISTS bz_rounds (order_id TEXT NOT NULL, round INTEGER NOT NULL, feedback TEXT NOT NULL, "
    "state TEXT NOT NULL, reinspection_run TEXT UNIQUE, conclusion TEXT, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, PRIMARY KEY (order_id, round))",
)
TABLES = ("bz_catalogs", "bz_jobs", "bz_references", "bz_findings", "bz_reviews", "bz_orders", "bz_rounds")
INDEXES = ("bz_jobs_state", "bz_jobs_asset", "bz_jobs_media", "bz_open_finding", "bz_findings_project",
           "bz_orders_project")


class StaleRecord(RuntimeError):
    """A record changed since it was read; the caller re-reads and decides again. / 记录在读取后已变化；调用方重读后再决定。"""


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


def business_schema(ledger: BusinessLedger) -> str | None:
    row = ledger._one("SELECT value FROM meta WHERE key='business_schema'")
    return row["value"] if row else None


def migrate(ledger: BusinessLedger, *, backups: Path | None, statements: tuple[str, ...] = DDL,
            clock=utcnow) -> dict:
    """Add the business extension to a ledger with the workflow extension; back up and verify one with data first.

    为已有工作流扩展的账本加上业务扩展；已有数据的库先备份并校验（D064）。
    """
    with ledger._lock:
        if schema_version(ledger) != OPERATIONS_SCHEMA:
            raise MigrationError("the business extension needs the P1 operations schema v2 first")
        present = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in (*OPERATIONS_TABLES, *WORKFLOW_TABLES) if name not in present]
        if missing or workflow_schema(ledger) != WORKFLOW_SCHEMA:
            raise MigrationError(f"the business extension needs the P2 workflow extension first (missing {missing})")
        version = business_schema(ledger)
        if version == BUSINESS_SCHEMA:
            missing = [name for name in TABLES if name not in present]
            if missing:
                raise MigrationError(f"business schema {BUSINESS_SCHEMA} is missing tables {missing}")
            return {"status": "current", "business_schema": BUSINESS_SCHEMA}
        if version is not None:
            raise MigrationError(f"unexpected business schema version {version!r}")
        counts = _counts(ledger._db)
        backup = None
        has_data = any(counts.get(name, 0) for name in ("requests", "missions", "deliveries", "events"))
        if ledger.path is not None and has_data:
            if backups is None:
                raise MigrationError("a ledger with data needs a backup directory before migrating")
            stamp = clock().strftime("%Y%m%dT%H%M%S%fZ")
            target = backups / f"ledger-v2-bz-{stamp}.sqlite3"
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
                db.execute("INSERT INTO meta(key, value) VALUES ('business_schema', ?)", (BUSINESS_SCHEMA,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('business_migrated_at', ?)",
                           (clock().isoformat(),))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('business_backup', ?)",
                           (json.dumps(backup, sort_keys=True),))
        except sqlite3.Error as error:
            raise MigrationError(f"business schema migration rolled back: {error}") from error
        return {"status": "migrated", "business_schema": BUSINESS_SCHEMA, "backup": backup, "rows_before": counts}


_BUSINESS_LINE = re.compile(r'^(CREATE (TABLE|UNIQUE INDEX|INDEX) (IF NOT EXISTS )?"?bz_|INSERT INTO "bz_)')


def other_lines(path: Path) -> list[str]:
    """The SQL dump of everything but the business extension. / 除业务扩展以外的 SQL 转储。"""
    with sqlite3.connect(str(path)) as db:
        dump = list(db.iterdump())
    return [line for line in dump if not _BUSINESS_LINE.match(line)
            and not (line.startswith('INSERT INTO "meta"') and "'business_" in line)]


def drill(path: Path) -> dict:
    """D064 drill on a copy of a ledger: migrate it and prove the other data unchanged; counts only, never content.

    在账本副本上做 D064 演练：迁移并证明其他数据不变；只报告计数，从不报告内容。
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
                            if row[0].startswith("bz_"))
            indexes = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
                             if row[0].startswith("bz_"))
    finally:
        ledger.close()
    after = dump_digest(path)
    return {"status": "passed" if report["status"] in ("migrated", "current") and integrity == "ok"
            and before == after and tables == sorted(TABLES) and indexes == sorted(INDEXES) else "failed",
            "migration": report["status"], "business_schema": BUSINESS_SCHEMA, "integrity": integrity,
            "other_dump_sha256_before": before, "other_dump_sha256_after": after, "bz_tables": len(tables),
            "bz_indexes": len(indexes), "rows_before": report.get("rows_before"), "backup": report.get("backup")}


class BusinessStore:
    """Reads and writes of the business tables; callers group writes with `transaction()`.

    业务表的读写；调用方用 `transaction()` 组合写入。
    """

    def __init__(self, ledger: BusinessLedger, catalog: BusinessCatalog, profiles: dict[str, str], *, clock=utcnow):
        if schema_version(ledger) != OPERATIONS_SCHEMA or business_schema(ledger) != BUSINESS_SCHEMA:
            raise MigrationError("run migrate() before opening the business store")
        self.ledger, self.catalog, self.profiles, self.clock = ledger, catalog, dict(profiles), clock
        self._catalogs: dict[str, BusinessCatalog] = {catalog.sha256: catalog}
        self._exec("INSERT OR IGNORE INTO bz_catalogs VALUES (?,?,?,?,?)",
                   (catalog.sha256, catalog.catalog_id, _dump(catalog), _dump(dict(sorted(profiles.items()))),
                    self._now()))

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
        """Business audit rows share the P1 audit stream. / 业务审计行与 P1 审计流共用。"""
        self._exec("INSERT INTO op_events(subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)",
                   (subject, kind, actor, _dump(body or {}), self._now()))

    def events(self, subject: str, limit: int = 200) -> list[dict]:
        rows = self._rows("SELECT * FROM op_events WHERE subject=? ORDER BY id DESC LIMIT ?", (subject, limit))
        return [{**row, "body": _load(row["body"])} for row in reversed(rows)]

    def pinned(self, catalog_sha256: str) -> BusinessCatalog:
        """A catalog an object was created under; kept in the ledger, so a later deploy cannot change it.

        对象创建时所用的目录；保存在账本中，之后的部署无法改变它。
        """
        if catalog_sha256 not in self._catalogs:
            row = self._one("SELECT body FROM bz_catalogs WHERE catalog_sha256=?", (catalog_sha256,))
            if row is None:
                raise KeyError(catalog_sha256)
            self._catalogs[catalog_sha256] = BusinessCatalog.model_validate(_load(row["body"]))
        return self._catalogs[catalog_sha256]

    # ── jobs / 作业 ──

    @staticmethod
    def _job(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "inputs": _load(row["inputs"]), "result": _load(row["result"])}

    def job(self, job_id: str) -> dict | None:
        return self._job(self._one("SELECT * FROM bz_jobs WHERE job_id=?", (job_id,)))

    def job_by_key(self, key: str) -> dict | None:
        return self._job(self._one("SELECT * FROM bz_jobs WHERE idempotency_key=?", (key,)))

    def jobs(self, *, project_id: str | None = None, asset: str | None = None, finding_id: str | None = None,
             media_sha256: str | None = None, requested_by: str | None = None, limit: int = 200) -> list[dict]:
        clauses, args = [], []
        for column, value in (("project_id", project_id), ("asset_key", asset), ("finding_id", finding_id),
                              ("media_sha256", media_sha256), ("requested_by", requested_by)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._rows(f"SELECT * FROM bz_jobs{where} ORDER BY created_at DESC, job_id LIMIT ?", (*args, limit))
        return [self._job(row) for row in rows]

    def create_job(self, *, key: str, project_id: str, requested_by: str, inputs: JobInputs, asset: str) \
            -> tuple[dict, bool]:
        """One job per idempotency key; a repeat returns the first. / 每个幂等键一个作业；重复返回第一个。"""
        known = self.job_by_key(key)
        if known is not None:
            return known, False
        job_id = "job-" + uuid.uuid4().hex[:20]
        now = self._now()
        evidence = inputs.evidence
        self._exec("INSERT INTO bz_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, key, project_id, requested_by, inputs.purpose.value, inputs.analyzer,
                    inputs.analyzer_sha256, self.catalog.sha256, asset, evidence.evidence_id, evidence.media_sha256,
                    _dump(inputs), JobState.QUEUED.value, 1, 0, None, 0, None, None, None, None, None,
                    inputs.order_id, inputs.round, now, now))
        self.event(f"job:{job_id}", "job.queued", requested_by,
                   {"analyzer": inputs.analyzer, "purpose": inputs.purpose.value, "evidence_id": evidence.evidence_id})
        return self.job(job_id), True

    def runnable(self, limit: int) -> list[dict]:
        """Queued jobs and running ones whose lease expired, oldest first. / 排队中与租约过期的运行中作业，先到先得。"""
        now = self._now()
        rows = self._rows("SELECT * FROM bz_jobs WHERE state='queued' OR (state='running' AND lease_until < ?) "
                          "ORDER BY created_at, job_id LIMIT ?", (now, limit))
        return [self._job(row) for row in rows]

    def claim(self, job_id: str, *, owner: str, lease_s: float, max_attempts: int) -> int | None:
        """Take a job: a new fencing epoch, one more attempt and a lease; None if someone else holds it.

        领取作业：新的 fencing 代次、尝试次数加一与租约；他人持有时为 None。
        """
        with self.transaction():
            row = self.job(job_id)
            now = self.clock()
            if row is None:
                return None
            expired = row["state"] == JobState.RUNNING.value and row["lease_until"] \
                and datetime.fromisoformat(row["lease_until"]) < now
            if row["state"] != JobState.QUEUED.value and not expired:
                return None
            if row["attempts"] >= max_attempts:
                return None
            epoch = row["owner_epoch"] + 1
            self._exec("UPDATE bz_jobs SET state='running', owner=?, owner_epoch=?, attempts=attempts+1, "
                       "lease_until=?, state_version=state_version+1, updated_at=? WHERE job_id=? AND state_version=?",
                       (owner, epoch, (now + timedelta(seconds=lease_s)).isoformat(), now.isoformat(), job_id,
                        row["state_version"]))
        return epoch

    def abandon(self, job_id: str, *, max_attempts: int, result: JobResult) -> bool:
        """Refuse a job whose every attempt ran out its lease; a live holder is never overtaken.

        拒判每次尝试都耗尽租约的作业；从不抢占仍在租约内的持有者。
        """
        return self._exec("UPDATE bz_jobs SET state='refused', verdict=?, source=?, result=?, lease_until=NULL, "
                          "state_version=state_version+1, updated_at=? WHERE job_id=? AND state='running' "
                          "AND attempts >= ? AND lease_until < ?",
                          (result.verdict, result.source, _dump(result), self._now(), job_id, max_attempts,
                           self._now())).rowcount == 1

    def renew(self, job_id: str, *, owner: str, epoch: int, lease_s: float) -> bool:
        until = (self.clock() + timedelta(seconds=lease_s)).isoformat()
        return self._exec("UPDATE bz_jobs SET lease_until=? WHERE job_id=? AND owner=? AND owner_epoch=? "
                          "AND state='running'", (until, job_id, owner, epoch)).rowcount == 1

    def finish(self, job_id: str, *, owner: str | None, epoch: int | None, result: JobResult,
               finding_id: str | None = None, state: JobState | None = None) -> bool:
        """Write a job's result once; a stale owner writes nothing. / 只写一次作业结果；过期持有者什么也写不进去。"""
        final = state or (JobState.REFUSED if result.verdict == "refused" else JobState.COMPLETED)
        if owner is None:
            sql = ("UPDATE bz_jobs SET state=?, verdict=?, source=?, result=?, finding_id=?, owner=NULL, "
                   "lease_until=NULL, state_version=state_version+1, updated_at=? WHERE job_id=? AND state='queued'")
            args = (final.value, result.verdict, result.source, _dump(result), finding_id, self._now(), job_id)
        else:
            sql = ("UPDATE bz_jobs SET state=?, verdict=?, source=?, result=?, finding_id=?, lease_until=NULL, "
                   "state_version=state_version+1, updated_at=? WHERE job_id=? AND owner=? AND owner_epoch=? "
                   "AND state='running'")
            args = (final.value, result.verdict, result.source, _dump(result), finding_id, self._now(), job_id,
                    owner, epoch)
        return self._exec(sql, args).rowcount == 1

    def link_finding(self, job_id: str, finding_id: str) -> None:
        self._exec("UPDATE bz_jobs SET finding_id=? WHERE job_id=? AND finding_id IS NULL", (finding_id, job_id))

    def tokens_since(self, project_id: str, since: datetime) -> int:
        total = 0
        for row in self._rows("SELECT result FROM bz_jobs WHERE project_id=? AND updated_at >= ? AND result IS NOT NULL",
                              (project_id, since.isoformat())):
            usage = (_load(row["result"]) or {}).get("usage") or {}
            total += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        return total

    # ── references / 参考外观 ──

    @staticmethod
    def _reference(row: dict | None) -> dict | None:
        return None if row is None else {**row, "body": _load(row["body"])}

    def reference(self, reference_id: str) -> dict | None:
        return self._reference(self._one("SELECT * FROM bz_references WHERE reference_id=?", (reference_id,)))

    def references(self, *, project_id: str | None = None, asset: str | None = None,
                   active: bool = False) -> list[dict]:
        clauses, args = [], []
        if project_id is not None:
            clauses.append("project_id=?")
            args.append(project_id)
        if asset is not None:
            clauses.append("asset_key=?")
            args.append(asset)
        if active:
            clauses.append("state='active'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [self._reference(r) for r in self._rows(
            f"SELECT * FROM bz_references{where} ORDER BY registered_at DESC, reference_id", args)]

    def register_reference(self, *, project_id: str, asset: str, evidence_id: str, media_sha256: str, body: dict,
                           actor: str) -> tuple[dict, bool]:
        with self.transaction():
            known = self._one("SELECT * FROM bz_references WHERE asset_key=? AND evidence_id=?", (asset, evidence_id))
            if known is not None:
                return self._reference(known), False
            reference_id = "ref-" + uuid.uuid4().hex[:16]
            self._exec("INSERT INTO bz_references VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (reference_id, project_id, asset, evidence_id, media_sha256, _dump(body), "active", actor,
                        self._now(), None, None))
            self.event(f"reference:{reference_id}", "reference.registered", actor,
                       {"asset": asset, "evidence_id": evidence_id})
        return self.reference(reference_id), True

    def revoke_reference(self, reference_id: str, actor: str) -> bool:
        with self.transaction():
            applied = self._exec("UPDATE bz_references SET state='revoked', revoked_by=?, revoked_at=? "
                                 "WHERE reference_id=? AND state='active'",
                                 (actor, self._now(), reference_id)).rowcount == 1
            if applied:
                self.event(f"reference:{reference_id}", "reference.revoked", actor, {})
        return applied

    # ── findings / 发现 ──

    @staticmethod
    def _finding(row: dict | None) -> dict | None:
        return None if row is None else {**row, "body": _load(row["body"])}

    def finding(self, finding_id: str) -> dict | None:
        return self._finding(self._one("SELECT * FROM bz_findings WHERE finding_id=?", (finding_id,)))

    def open_finding(self, cluster: str) -> dict | None:
        return self._finding(self._one("SELECT * FROM bz_findings WHERE cluster_key=? "
                                       "AND state IN ('candidate', 'confirmed')", (cluster,)))

    def findings(self, *, project_id: str | None = None, asset: str | None = None, limit: int = 100) -> list[dict]:
        clauses, args = [], []
        if project_id is not None:
            clauses.append("project_id=?")
            args.append(project_id)
        if asset is not None:
            clauses.append("asset_key=?")
            args.append(asset)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [self._finding(r) for r in self._rows(
            f"SELECT * FROM bz_findings{where} ORDER BY created_at DESC, finding_id LIMIT ?", (*args, limit))]

    def create_finding(self, *, project_id: str, asset: str, family: str, cluster: str, job_id: str,
                       body: dict) -> dict:
        finding_id = "fd-" + uuid.uuid4().hex[:16]
        now = self._now()
        self._exec("INSERT INTO bz_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (finding_id, project_id, asset, family, cluster, FindingState.CANDIDATE.value, 1, job_id, job_id,
                    1, None, None, _dump(body), now, now, None))
        return self.finding(finding_id)

    def attach(self, finding_id: str, job_id: str, body: dict) -> None:
        self._exec("UPDATE bz_findings SET last_job=?, jobs=jobs+1, body=?, state_version=state_version+1, "
                   "updated_at=? WHERE finding_id=?", (job_id, _dump(body), self._now(), finding_id))

    def set_finding(self, finding_id: str, *, version: int, state: FindingState, **fields) -> None:
        """Compare-and-set a finding's state. / 对发现状态做比较并交换。"""
        assignments = ["state=?", "state_version=state_version+1", "updated_at=?"]
        args: list = [state.value, self._now()]
        for column, value in fields.items():
            assignments.append(f"{column}=?")
            args.append(_dump(value) if column == "body" else value)
        if state is FindingState.RESOLVED or state is FindingState.DISMISSED:
            assignments.append("closed_at=?")
            args.append(self._now())
        cursor = self._exec(f"UPDATE bz_findings SET {', '.join(assignments)} WHERE finding_id=? AND state_version=?",
                            (*args, finding_id, version))
        if cursor.rowcount != 1:
            raise StaleRecord(finding_id)

    # ── reviews / 复核 ──

    def review(self, subject_kind: str, subject_id: str) -> dict | None:
        row = self._one("SELECT * FROM bz_reviews WHERE subject_kind=? AND subject_id=?", (subject_kind, subject_id))
        return None if row is None else {**row, "basis": _load(row["basis"])}

    def review_by_id(self, review_id: str) -> dict | None:
        row = self._one("SELECT * FROM bz_reviews WHERE review_id=?", (review_id,))
        return None if row is None else {**row, "basis": _load(row["basis"])}

    def reviews(self, project_id: str, limit: int = 200) -> list[dict]:
        return [{**row, "basis": _load(row["basis"])} for row in self._rows(
            "SELECT * FROM bz_reviews WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))]

    def record_review(self, *, subject_kind: str, subject_id: str, project_id: str, reviewer: str, request_id: str,
                      decision: str, note: str, basis: dict) -> tuple[dict, bool]:
        """The first decision on a subject wins. / 对象的首个决定有效。"""
        created = self._exec("INSERT OR IGNORE INTO bz_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                             ("rv-" + uuid.uuid4().hex[:16], subject_kind, subject_id, project_id, reviewer,
                              request_id, decision, note[:300], _dump(basis), self._now())).rowcount == 1
        return self.review(subject_kind, subject_id), created

    # ── orders and rounds / 工单与轮次 ──

    @staticmethod
    def _order(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "reinspection": _load(row["reinspection"]), "body": _load(row["body"]),
                "closure": _load(row["closure"])}

    def order(self, order_id: str) -> dict | None:
        return self._order(self._one("SELECT * FROM bz_orders WHERE order_id=?", (order_id,)))

    def order_by_key(self, key: str) -> dict | None:
        return self._order(self._one("SELECT * FROM bz_orders WHERE idempotency_key=?", (key,)))

    def order_by_finding(self, finding_id: str) -> dict | None:
        return self._order(self._one("SELECT * FROM bz_orders WHERE finding_id=?", (finding_id,)))

    def orders(self, project_id: str, limit: int = 100) -> list[dict]:
        return [self._order(r) for r in self._rows(
            "SELECT * FROM bz_orders WHERE project_id=? ORDER BY created_at DESC, order_id LIMIT ?", (project_id, limit))]

    def create_order(self, *, key: str, finding: dict, review_id: str, reinspection: dict, body: dict,
                     actor: str) -> tuple[dict, bool]:
        """One order per finding and per key; a repeat returns the existing order. / 每个发现、每个键一张工单；重复返回原工单。"""
        known = self.order_by_finding(finding["finding_id"]) or self.order_by_key(key)
        if known is not None:
            return known, False
        order_id = "ord-" + uuid.uuid4().hex[:16]
        now = self._now()
        self._exec("INSERT INTO bz_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (order_id, finding["finding_id"], key, finding["project_id"], finding["asset_key"], review_id,
                    self.catalog.sha256, _dump(reinspection), OrderState.OPEN.value, 1, 0, actor, _dump(body), None,
                    now, now, None))
        self._exec("UPDATE bz_findings SET order_id=?, updated_at=? WHERE finding_id=?",
                   (order_id, now, finding["finding_id"]))
        self.event(f"order:{order_id}", "order.created", actor, {"finding_id": finding["finding_id"],
                                                                 "review_id": review_id})
        return self.order(order_id), True

    def set_order(self, order_id: str, *, version: int, state: OrderState, **fields) -> None:
        assignments = ["state=?", "state_version=state_version+1", "updated_at=?"]
        args: list = [state.value, self._now()]
        for column, value in fields.items():
            assignments.append(f"{column}=?")
            args.append(_dump(value) if column in ("closure", "body") else value)
        if state is OrderState.CLOSED:
            assignments.append("closed_at=?")
            args.append(self._now())
        cursor = self._exec(f"UPDATE bz_orders SET {', '.join(assignments)} WHERE order_id=? AND state_version=?",
                            (*args, order_id, version))
        if cursor.rowcount != 1:
            raise StaleRecord(order_id)

    @staticmethod
    def _round(row: dict | None) -> dict | None:
        return None if row is None else {**row, "feedback": _load(row["feedback"]),
                                         "conclusion": _load(row["conclusion"])}

    def round(self, order_id: str, number: int) -> dict | None:
        return self._round(self._one("SELECT * FROM bz_rounds WHERE order_id=? AND round=?", (order_id, number)))

    def rounds(self, order_id: str) -> list[dict]:
        return [self._round(r) for r in self._rows("SELECT * FROM bz_rounds WHERE order_id=? ORDER BY round",
                                                   (order_id,))]

    def round_by_run(self, run_id: str) -> dict | None:
        return self._round(self._one("SELECT * FROM bz_rounds WHERE reinspection_run=?", (run_id,)))

    def create_round(self, order_id: str, number: int, feedback: dict) -> None:
        now = self._now()
        self._exec("INSERT INTO bz_rounds VALUES (?,?,?,?,?,?,?,?)",
                   (order_id, number, _dump(feedback), RoundState.REINSPECTING.value, None, None, now, now))

    def link_round(self, order_id: str, number: int, run_id: str) -> None:
        cursor = self._exec("UPDATE bz_rounds SET reinspection_run=?, updated_at=? WHERE order_id=? AND round=? "
                            "AND reinspection_run IS NULL", (run_id, self._now(), order_id, number))
        if cursor.rowcount != 1:
            raise StaleRecord(f"{order_id}:r{number}")

    def conclude_round(self, order_id: str, number: int, state: RoundState, conclusion: dict) -> bool:
        """Write a round's conclusion once. / 只写一次轮次结论。"""
        return self._exec("UPDATE bz_rounds SET state=?, conclusion=?, updated_at=? WHERE order_id=? AND round=? "
                          "AND state='reinspecting'",
                          (state.value, _dump(conclusion), self._now(), order_id, number)).rowcount == 1

    def open_findings(self) -> list[dict]:
        return [self._finding(r) for r in self._rows("SELECT * FROM bz_findings WHERE state IN ('candidate', "
                                                     "'confirmed') ORDER BY created_at")]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill", type=Path, required=True, help="a copy of the ledger to migrate (D064 drill)")
    args = parser.parse_args()
    result = drill(args.drill)
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()

