"""D058 drills and store invariants: additive migration with a verified backup, P1 still reads the ledger, versions
are immutable, and a stale worker's writes are refused.

D058 演练与存储不变量：带校验备份的增量迁移、P1 仍能读取账本、版本不可变、过期 worker 的写入被拒。
"""

from __future__ import annotations

import copy
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.contracts import utcnow
from drone_agent.fleet import operations_store, workflow_store
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import MigrationError
from drone_agent.fleet.workflow_models import NodeState, WorkflowCatalog, load_workflows
from drone_agent.fleet.workflow_store import Fenced, StaleNode, WorkflowStore, drill, migrate, other_lines
from tests.fleet.harness import request

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "configs/sites/p1_campus_v1.yaml"
MEMBERS = ROOT / "configs/sites/p2_members_s0.yaml"
WORKFLOWS = ROOT / "configs/workflows/p2_campus_v1.yaml"


def v2_ledger(path: Path) -> Path:
    """A schema v2 ledger with P1 rows in it, like the resident desk after P1. / 含 P1 数据的 schema v2 账本。"""
    ledger = BusinessLedger(path)
    operations = build_operations(ROOT, ledger, CATALOG, MEMBERS, backups=path.parent / "p1-backups")
    ledger.record_request(request(key="idem-p2-drill"), "m-000000000001",
                          also=operations.store.binding_writer("m-000000000001", project_id="campus_ops",
                                                               robot_id="uav_a"))
    operations.store.record_cancel("m-000000000001", requested_by="harness:p2-operator", request_id="c-0001",
                                   reason="drill")
    ledger.close()
    return path


def catalog_and_fixtures():
    catalog = load_workflows(WORKFLOWS)
    from drone_agent.fleet.resources import load_catalog
    from drone_agent.mission.registry import Registry

    ops = load_catalog(CATALOG)
    fixtures = catalog.check(ops, {s: Registry(ROOT, scene=ROOT / site.scene) for s, site in ops.sites.items()}, ROOT)
    return catalog, fixtures


def test_the_drill_migrates_a_copy_and_leaves_v1_and_p1_data_unchanged(tmp_path):
    path = v2_ledger(tmp_path / "ledger.sqlite3")
    before = other_lines(path)
    result = drill(path)
    assert result["status"] == "passed" and result["migration"] == "migrated" and result["wf_tables"] == 9
    assert result["other_dump_sha256_before"] == result["other_dump_sha256_after"]
    assert result["backup"]["rows"]["op_cancellations"] == 1 and result["backup"]["rows"]["missions"] == 1
    backup = tmp_path / "backups" / result["backup"]["path"]
    assert sqlite3.connect(str(backup)).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert other_lines(path) == before
    again = drill(path)
    assert again["status"] == "passed" and again["migration"] == "current"
    assert len(list((tmp_path / "backups").glob("ledger-v2-*.sqlite3"))) == 1


def test_a_failed_ddl_rolls_back_and_the_service_refuses_to_start(tmp_path):
    path = v2_ledger(tmp_path / "ledger.sqlite3")
    before = other_lines(path)
    ledger = BusinessLedger(path)
    broken = (*workflow_store.DDL[:3], "CREATE TABLE wf_runs (this is not sql)")
    with pytest.raises(MigrationError, match="rolled back"):
        migrate(ledger, backups=tmp_path / "backups", statements=broken)
    assert workflow_store.workflow_schema(ledger) is None
    assert not [r for r in ledger._rows("SELECT name FROM sqlite_master WHERE name LIKE 'wf%'")]
    catalog, fixtures = catalog_and_fixtures()
    with pytest.raises(MigrationError):
        WorkflowStore(ledger, catalog, fixtures)
    ledger.close()
    assert other_lines(path) == before


def test_the_p1_build_still_starts_on_a_migrated_ledger_and_the_backup_restores_v2(tmp_path):
    path = v2_ledger(tmp_path / "ledger.sqlite3")
    result = drill(path)
    ledger = BusinessLedger(path)
    # What the P1 candidate does on start: schema v2 and all op_ tables present, so it continues (D058 §5).
    # P1 候选启动时的动作：schema v2 且 op_ 表齐全，因此继续运行（D058 §5）。
    assert operations_store.migrate(ledger, backups=tmp_path / "unused")["status"] == "current"
    assert ledger.mission("m-000000000001")["status"] == "planning"
    ledger.close()
    restored = tmp_path / "restored.sqlite3"
    shutil.copyfile(tmp_path / "backups" / result["backup"]["path"], restored)
    names = {r[0] for r in sqlite3.connect(str(restored)).execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("wf_") for name in names) and "op_reservations" in names


def test_a_v1_ledger_is_refused_until_the_p1_schema_is_in_place(tmp_path):
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    with pytest.raises(MigrationError, match="operations schema v2"):
        migrate(ledger, backups=tmp_path / "backups")


def test_a_changed_template_under_an_existing_version_is_refused(tmp_path):
    path = v2_ledger(tmp_path / "ledger.sqlite3")
    ledger = BusinessLedger(path)
    migrate(ledger, backups=tmp_path / "backups")
    catalog, fixtures = catalog_and_fixtures()
    WorkflowStore(ledger, catalog, fixtures)
    data = copy.deepcopy(catalog.model_dump(mode="json"))
    data["workflows"][0]["title"] = "silently edited / 被悄悄修改"
    with pytest.raises(MigrationError, match="publish a new version"):
        WorkflowStore(ledger, WorkflowCatalog.model_validate(data), fixtures)
    data["workflows"][0]["version"] = 2
    WorkflowStore(ledger, WorkflowCatalog.model_validate(data), fixtures)
    ledger.close()


def test_leases_fence_a_stale_worker_and_nodes_are_compare_and_set(tmp_path):
    path = v2_ledger(tmp_path / "ledger.sqlite3")
    ledger = BusinessLedger(path)
    migrate(ledger, backups=tmp_path / "backups")
    offset = [timedelta(0)]
    clock = lambda: utcnow() + offset[0]  # noqa: E731
    catalog, fixtures = catalog_and_fixtures()
    store = WorkflowStore(ledger, catalog, fixtures, clock=clock)
    spec = catalog.latest("campus_ops", "quick_check")
    run_id, created = store.start_run(spec, catalog_sha256=catalog.sha256, source="manual:harness:p2-operator",
                                      event_id="r-1", actor="harness:p2-operator", started_by="harness:p2-operator",
                                      inputs={"asset": "asset_red"}, body={})
    assert created and store.start_run(spec, catalog_sha256=catalog.sha256, source="manual:harness:p2-operator",
                                       event_id="r-1", actor="x", started_by="x", inputs={}, body={}) == (run_id, False)
    first = store.acquire(run_id, "worker-a", 10)
    assert first == 1 and store.acquire(run_id, "worker-b", 10) is None
    node = store.nodes(run_id)["inspect"]
    store.transition(run_id, "inspect", worker="worker-a", epoch=first, version=node["state_version"],
                     state=NodeState.RUNNING, outbox={"key": "wf:k", "kind": "submit_mission", "payload": {}})
    with pytest.raises(StaleNode):
        store.transition(run_id, "inspect", worker="worker-a", epoch=first, version=node["state_version"],
                         state=NodeState.FAILED)
    offset[0] = timedelta(seconds=11)
    second = store.acquire(run_id, "worker-b", 10)
    assert second == 2
    current = store.nodes(run_id)["inspect"]
    with pytest.raises(Fenced):
        store.transition(run_id, "inspect", worker="worker-a", epoch=first, version=current["state_version"],
                         state=NodeState.FAILED)
    row = store.outbox(run_id)[0]
    with pytest.raises(Fenced):
        store.claim(row["outbox_id"], worker="worker-a", epoch=first)
    assert store.claim(row["outbox_id"], worker="worker-b", epoch=second)["claimed_by"] == "worker-b"
    assert store.nodes(run_id)["inspect"]["state"] == "running"
    ledger.close()


def test_a_cancel_voids_unclaimed_effects_and_refuses_new_ones(tmp_path):
    path = v2_ledger(tmp_path / "ledger.sqlite3")
    ledger = BusinessLedger(path)
    migrate(ledger, backups=tmp_path / "backups")
    catalog, fixtures = catalog_and_fixtures()
    store = WorkflowStore(ledger, catalog, fixtures)
    spec = catalog.latest("campus_ops", "campus_round")
    run_id, _ = store.start_run(spec, catalog_sha256=catalog.sha256, source="manual:harness:p2-operator",
                                event_id="r-2", actor="harness:p2-operator", started_by="harness:p2-operator",
                                inputs={}, body={})
    epoch = store.acquire(run_id, "worker-a", 10)
    node = store.nodes(run_id)["inspect_red"]
    store.transition(run_id, "inspect_red", worker="worker-a", epoch=epoch, version=node["state_version"],
                     state=NodeState.RUNNING, outbox={"key": "wf:red", "kind": "submit_mission", "payload": {}})
    result = store.request_cancel(run_id, actor="harness:p2-operator", request_id="c-1", reason="test")
    assert result["status"] == "requested" and store.run(run_id)["cancel_epoch"] == 1
    assert [r["state"] for r in store.outbox(run_id)] == ["void"]
    blue = store.nodes(run_id)["inspect_blue"]
    with pytest.raises(StaleNode, match="no new effect"):
        store.transition(run_id, "inspect_blue", worker="worker-a", epoch=epoch, version=blue["state_version"],
                         state=NodeState.RUNNING, outbox={"key": "wf:blue", "kind": "submit_mission", "payload": {}})
    assert store.request_cancel(run_id, actor="other", request_id="c-2", reason="again")["status"] == "already"
    ledger.close()
