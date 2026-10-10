"""The P5 catalog switch drill on a ledger written under the P4 desk catalogs (D069).

在 P4 任务台目录下写入的账本上运行 P5 目录切换演练（D069）。
"""

from __future__ import annotations

from pathlib import Path

from drone_agent.fleet.api import dispatch
from drone_agent.fleet.business import BusinessEngine, build_business
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import source_context
from drone_agent.fleet.scheduler import Scheduler, build_scheduling
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.switch_drill import drill
from drone_agent.fleet.transport import FleetHub
from drone_agent.fleet.workflow import WorkflowEngine, build_workflows
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.runtime.signing import SigningKey

ROOT = Path(__file__).resolve().parents[2]
P4_MEMBERS = """format: drone.project-members/v1
schema_version: "0.1.0"
members:
  - {principal: "harness:p4-operator", project_id: campus_s1, roles: [operator, approver]}
  - {principal: "harness:desk-supervisor", project_id: campus_s1, roles: [viewer]}
"""


async def p4_desk_ledger(tmp: Path) -> Path:
    """A ledger written by the P4 desk configuration with one started appearance run. / P4 任务台配置写入的账本。"""
    members = tmp / "members-p4.yaml"
    members.write_text(P4_MEMBERS, encoding="utf-8")
    ledger = BusinessLedger(tmp / "ledger.sqlite3")
    ops = build_operations(ROOT, ledger, ROOT / "configs/sites/p1_s1_v1.yaml", members, backups=tmp / "b")
    flows = build_workflows(ROOT, ledger, ROOT / "configs/workflows/p4_s1_v1.yaml", ops, backups=tmp / "b")
    plan = build_scheduling(ledger, ROOT / "configs/scheduling/p3_desk_v1.yaml", ops, backups=tmp / "b")
    trade = build_business(ROOT, ledger, ROOT / "configs/analysis/p4_s1_v1.yaml", ops, flows, backups=tmp / "b")
    scene = ROOT / M2_SCENE
    service = MissionService(root=ROOT, scene=scene, ledger=ledger, hub=FleetHub(ledger, tmp / "media"),
                             signing_key=SigningKey.generate(),
                             approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                             provenance_context=source_context(ROOT, scene, Registry(ROOT, scene=scene).sha256,
                                                               backend="px4_sitl"),
                             operations=ops, backends=("px4_sitl",))
    service.workflows = WorkflowEngine(service, flows, root=ROOT)
    service.scheduler = Scheduler(service, plan)
    service.business = BusinessEngine(service, trade, root=ROOT, vision=None, recordings=tmp / "rec")
    response = await dispatch(service, {"method": "workflows.start", "actor": "harness:p4-operator",
                                        "trust": "first_party",
                                        "params": {"project_id": "campus_s1", "workflow_id": "appearance_watch",
                                                   "request_id": "p4-run-1", "inputs": {"asset": "asset_red"}}})
    assert response["ok"], response
    for _ in range(5):
        service.tick_workflows()
    service.business.jobs.stop()
    ledger.close()
    return tmp / "ledger.sqlite3"


async def test_the_p5_catalogs_open_a_p4_desk_ledger_and_render_its_records_with_only_catalog_rows_added(tmp_path):
    path = await p4_desk_ledger(tmp_path)
    result = await drill(ROOT, path, scene=ROOT / M2_SCENE, catalog=ROOT / "configs/sites/p5_desk_v1.yaml",
                         members=ROOT / "configs/sites/p5_members_desk_s0.yaml",
                         workflows=ROOT / "configs/workflows/p5_desk_v1.yaml",
                         scheduling=ROOT / "configs/scheduling/p5_desk_v1.yaml",
                         business=ROOT / "configs/analysis/p5_desk_v1.yaml",
                         backends=("px4_sitl", "logical_sim", "vendor_protocol_sim"))
    assert result["status"] == "passed", result
    assert result["catalogs"]["operations"][0] == "p5_desk_v1" and result["catalogs"]["workflows"][0] == "p5_desk_v1"
    assert set(result["catalogs"]["migrations"].values()) <= {"current", "migrated"}
    assert result["read"]["runs"] >= 1 and result["read"]["missions"] >= 1
    assert set(result["read"]["audit"]) == {"campus_s1", "fleet_s0", "vendor_s3"}
    assert result["rows_after"] - result["rows_before"] == 4  # one new row per catalog table / 每个目录表新增一行


async def test_the_p6_desk_catalogs_open_an_earlier_desk_ledger_with_only_catalog_rows_added(tmp_path):
    # P6 (D078): the recapture catalogs replace the workflow and business ones; no table changes.
    # P6（D078）：补拍目录替换工作流与业务目录；表不变。
    path = await p4_desk_ledger(tmp_path)
    result = await drill(ROOT, path, scene=ROOT / M2_SCENE, catalog=ROOT / "configs/sites/p5_desk_v1.yaml",
                         members=ROOT / "configs/sites/p5_members_desk_s0.yaml",
                         workflows=ROOT / "configs/workflows/p6_desk_v1.yaml",
                         scheduling=ROOT / "configs/scheduling/p5_desk_v1.yaml",
                         business=ROOT / "configs/analysis/p6_desk_v1.yaml",
                         backends=("px4_sitl", "logical_sim", "vendor_protocol_sim"))
    assert result["status"] == "passed", result
    assert result["catalogs"]["workflows"][0] == "p6_desk_v1" and result["catalogs"]["business"][0] == "p6_desk_v1"
    assert result["read"]["runs"] >= 1 and result["rows_after"] - result["rows_before"] == 4


async def test_a_member_list_naming_an_unknown_project_fails_the_drill(tmp_path):
    path = await p4_desk_ledger(tmp_path)
    members = tmp_path / "bad.yaml"
    members.write_text(P4_MEMBERS + '  - {principal: "harness:x", project_id: harbor_ops, roles: [viewer]}\n',
                       encoding="utf-8")
    result = await drill(ROOT, path, scene=ROOT / M2_SCENE, catalog=ROOT / "configs/sites/p5_desk_v1.yaml",
                         members=members, workflows=ROOT / "configs/workflows/p5_desk_v1.yaml",
                         scheduling=ROOT / "configs/scheduling/p5_desk_v1.yaml",
                         business=ROOT / "configs/analysis/p5_desk_v1.yaml",
                         backends=("px4_sitl", "logical_sim", "vendor_protocol_sim"))
    assert result["status"] == "failed" and any("unknown project" in f for f in result["failures"])
