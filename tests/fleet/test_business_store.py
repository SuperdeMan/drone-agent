"""P4 business store: the D064 migration and drill, and the uniqueness the tables enforce (D064).

P4 业务存储：D064 迁移与演练，以及表所保证的唯一性（D064）。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drone_agent.fleet.business_models import (
    EvidenceRef,
    FindingState,
    JobInputs,
    JobPurpose,
    JobResult,
    RoundState,
    load_catalog,
)
from drone_agent.fleet.business_store import (
    BUSINESS_SCHEMA,
    DDL,
    TABLES,
    BusinessStore,
    StaleRecord,
    business_schema,
    drill,
    migrate,
    other_lines,
)
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import MigrationError
from drone_agent.fleet.operations_store import migrate as migrate_operations
from drone_agent.fleet.workflow_store import migrate as migrate_workflows

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "configs/analysis/p4_campus_v1.yaml")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def ready_ledger(path: Path | str) -> BusinessLedger:
    ledger = BusinessLedger(path)
    migrate_operations(ledger, backups=None)
    migrate_workflows(ledger, backups=None)
    return ledger


def inputs(evidence: str = "capture:1", sha: str = "a" * 64) -> JobInputs:
    return JobInputs(purpose=JobPurpose.INSPECTION, analyzer="signature_s0_v1", analyzer_kind="deterministic",
                     analyzer_sha256="c" * 64, workflow_catalog_sha256="d" * 64, quality_sha256="e" * 64,
                     evidence=EvidenceRef(evidence_id=evidence, mission_id="m-1", mission_version=1,
                                          asset_id="asset_red", media_sha256=sha, captured_at=None,
                                          image_source="test_fixture", width=160, height=120))


def test_migration_needs_the_workflow_extension_and_is_idempotent(tmp_path):
    bare = BusinessLedger(":memory:")
    migrate_operations(bare, backups=None)
    with pytest.raises(MigrationError):
        migrate(bare, backups=None)
    ledger = ready_ledger(":memory:")
    assert migrate(ledger, backups=None)["status"] == "migrated"
    assert business_schema(ledger) == BUSINESS_SCHEMA
    assert migrate(ledger, backups=None)["status"] == "current"
    tables = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= tables
    assert ledger._one("SELECT value FROM meta WHERE key='schema_version'")["value"] == "2"


def test_failed_ddl_rolls_back_and_the_drill_proves_other_data_unchanged(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = ready_ledger(path)
    broken = (*DDL[:3], "CREATE TABLE bz_jobs (oops)")
    with pytest.raises(MigrationError):
        migrate(ledger, backups=tmp_path / "backups", statements=broken)
    assert business_schema(ledger) is None
    assert not {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master")} & set(TABLES)
    ledger.close()
    copy = tmp_path / "copy.sqlite3"
    shutil.copyfile(path, copy)
    before = other_lines(copy)
    report = drill(copy)
    assert report["status"] == "passed" and report["bz_tables"] == len(TABLES)
    assert other_lines(copy) == before
    with sqlite3.connect(str(copy)) as db:
        assert db.execute("SELECT value FROM meta WHERE key='business_schema'").fetchone()[0] == BUSINESS_SCHEMA


def test_jobs_claims_and_stale_results():
    ledger, clock = ready_ledger(":memory:"), Clock()
    migrate(ledger, backups=None)
    store = BusinessStore(ledger, CATALOG, {}, clock=clock)
    job, created = store.create_job(key="wf:r:analyze:1", project_id="campus_ops", requested_by="workflow:r",
                                    inputs=inputs(), asset="campus_ops/site_b/asset_red")
    again, created_again = store.create_job(key="wf:r:analyze:1", project_id="campus_ops", requested_by="workflow:r",
                                            inputs=inputs(), asset="campus_ops/site_b/asset_red")
    assert created and not created_again and again["job_id"] == job["job_id"]
    first = store.claim(job["job_id"], owner="a", lease_s=10, max_attempts=2)
    assert first == 1 and store.claim(job["job_id"], owner="b", lease_s=10, max_attempts=2) is None
    clock.now += timedelta(seconds=11)
    second = store.claim(job["job_id"], owner="b", lease_s=10, max_attempts=2)
    assert second == 2
    result = JobResult(verdict="normal", source="deterministic")
    assert not store.finish(job["job_id"], owner="a", epoch=first, result=result)
    assert store.finish(job["job_id"], owner="b", epoch=second, result=result)
    assert store.job(job["job_id"])["state"] == "completed"
    clock.now += timedelta(seconds=30)
    assert store.claim(job["job_id"], owner="c", lease_s=10, max_attempts=5) is None


def test_one_open_finding_per_cluster_and_one_order_per_finding():
    ledger, clock = ready_ledger(":memory:"), Clock()
    migrate(ledger, backups=None)
    store = BusinessStore(ledger, CATALOG, {}, clock=clock)
    finding = store.create_finding(project_id="campus_ops", asset="campus_ops/site_b/asset_red", family="appearance",
                                   cluster="campus_ops/site_b/asset_red#appearance", job_id="job-1", body={})
    with pytest.raises(sqlite3.IntegrityError):
        store.create_finding(project_id="campus_ops", asset="campus_ops/site_b/asset_red", family="appearance",
                             cluster="campus_ops/site_b/asset_red#appearance", job_id="job-2", body={})
    review, created = store.record_review(subject_kind="finding", subject_id=finding["finding_id"],
                                          project_id="campus_ops", reviewer="harness:r1", request_id="a",
                                          decision="confirmed", note="", basis={})
    other, created_other = store.record_review(subject_kind="finding", subject_id=finding["finding_id"],
                                               project_id="campus_ops", reviewer="harness:r2", request_id="b",
                                               decision="dismissed", note="", basis={})
    assert created and not created_other and other["review_id"] == review["review_id"]
    with pytest.raises(StaleRecord):
        store.set_finding(finding["finding_id"], version=finding["state_version"] + 7,
                          state=FindingState.CONFIRMED)
    order, made = store.create_order(key="wf:r:order:1", finding=finding, review_id=review["review_id"],
                                     reinspection={}, body={}, actor="workflow:r")
    same, made_again = store.create_order(key="wf:other:order:1", finding=finding, review_id=review["review_id"],
                                          reinspection={}, body={}, actor="workflow:other")
    assert made and not made_again and same["order_id"] == order["order_id"]
    store.create_round(order["order_id"], 1, {"feedback_id": "fb-1"})
    store.link_round(order["order_id"], 1, "wr-1")
    with pytest.raises(StaleRecord):
        store.link_round(order["order_id"], 1, "wr-2")
    assert store.conclude_round(order["order_id"], 1, RoundState.UNKNOWN, {"status": "unknown"})
    assert not store.conclude_round(order["order_id"], 1, RoundState.PASSED, {})
