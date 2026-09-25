"""The P1 service through its API: project scope, backend identities, default bindings and two full S0 cases.

经 API 验证 P1 服务：项目范围、后端身份、默认绑定，以及两个完整的 S0 用例。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drone_agent.eval.p1_world import JUDGE, OPERATOR, P1World, run_case, suite
from drone_agent.fleet.api import dispatch
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import source_context
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.transport import FleetHub
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.runtime.signing import SigningKey
from tests.fleet.harness import scripted_planner

ROOT = Path(__file__).resolve().parents[2]


async def call(service, actor, method, trust="first_party", **params):
    return await dispatch(service, {"method": method, "actor": actor, "trust": trust, "params": params})


async def test_projects_and_resources_are_scoped_to_membership(tmp_path):
    world = P1World(tmp_path / "case", ROOT, seed=7)
    service = world.service
    projects = (await call(service, OPERATOR, "projects"))["result"]
    assert [(p["project_id"], p["roles"]) for p in projects] == [("campus_ops", ["approver", "operator"]),
                                                                 ("legacy_m2", ["viewer"])]
    listed = (await call(service, OPERATOR, "resources.list", project_id="campus_ops"))["result"]
    assert [s["site_id"] for s in listed["sites"]] == ["site_a", "site_b", "site_c"]
    robot = listed["sites"][0]["robots"][0]
    assert robot["capability_source"] == "static" and robot["execution_backend"] == "logical_sim"
    assert robot["eligibility"]["verdict"] == "unknown" and "dock.status_missing" in robot["eligibility"]["reasons"]
    for method, params in (("resources.list", {"project_id": "harbor_ops"}),
                           ("resources.get", {"project_id": "campus_ops", "resource_id": "dock_h"}),
                           ("resources.eligibility", {"project_id": "campus_ops", "robot_id": "uav_h"})):
        refused = await call(service, OPERATOR, method, **params)
        assert not refused["ok"] and refused["issue"]["code"] == "service.not_found"
    stranger = (await call(service, "tailnet:stranger@x", "projects"))["result"]
    assert [(p["project_id"], p["roles"]) for p in stranger] == [("legacy_m2", ["viewer"])]
    assert (await call(service, "", "projects"))["result"] == []
    assert (await call(service, JUDGE, "list"))["result"] == []


async def test_backend_identities_only_report_and_people_cannot_report(tmp_path):
    world = P1World(tmp_path / "case", ROOT, seed=7)
    service, dock = world.service, world.docks["dock_a"]
    for method, params in (("list", {}), ("view", {"mission_id": "m-000000000000"}),
                           ("resources.list", {"project_id": "campus_ops"})):
        refused = await call(service, "dock:p1-s0-sim", method, trust="first_party", **params)
        assert refused["issue"]["code"] in ("auth.method_not_allowed", "auth.scope_missing")
    person = await call(service, OPERATOR, "docks.report", report=dock.report(world.clock()))
    assert person["issue"]["code"] == "auth.scope_missing"
    accepted = await call(service, "dock:p1-s0-sim", "docks.report", trust="backend", report=dock.report(world.clock()))
    assert accepted["result"]["accepted"] is True
    assert (await call(service, "dock:p1-s0-sim", "docks.actions", trust="backend", dock_id="dock_h"))["issue"][
        "code"] == "auth.backend_mismatch"


def s1_service(tmp_path, backend="px4_sitl") -> MissionService:
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    operations = build_operations(ROOT, ledger, ROOT / "configs/sites/p1_s1_v1.yaml",
                                  ROOT / "configs/sites/p1_members_s1.yaml", backups=tmp_path / "backups")
    registry = Registry(ROOT, scene=ROOT / M2_SCENE)
    return MissionService(root=ROOT, scene=ROOT / M2_SCENE, ledger=ledger, hub=FleetHub(ledger, tmp_path / "media"),
                          signing_key=SigningKey.generate(),
                          approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                          planner=scripted_planner(), operations=operations,
                          provenance_context=source_context(ROOT, ROOT / M2_SCENE, registry.sha256, backend=backend))


async def test_the_unscoped_submit_uses_the_default_binding_and_still_needs_membership(tmp_path):
    service = s1_service(tmp_path)
    text = "Inspect the red equipment marker east of the pad and bring back a photo."
    params = {"text": text, "volume_id": "campus_training", "asset_ids": [], "idempotency_key": "k-1"}
    view = (await call(service, OPERATOR, "submit", **params))["result"]
    assert view["binding"]["project_id"] == "campus_s1" and view["binding"]["robot_id"] == "uav_01"
    assert view["mission"]["status"] == "awaiting_approval"
    assert view["dispatch"]["reservations"][0]["state"] == "reserved"
    assert view["versions"][0]["provenance"]["execution_backend"] == "px4_sitl"
    refused = await call(service, "tailnet:stranger@x", "submit", **{**params, "idempotency_key": "k-2"})
    assert refused["issue"]["code"] == "service.not_found"
    agent = await call(service, "a2a:client", "submit", trust="third_party", **{**params, "idempotency_key": "k-3"})
    assert agent["issue"]["code"] == "service.not_found", "A2A needs membership in the default project"
    conflict = await call(service, OPERATOR, "missions.submit", project_id="campus_s1", robot_id="uav_01",
                          **{**params, "idempotency_key": "k-1"})
    assert conflict["ok"] and conflict["result"]["mission"]["mission_id"] == view["mission"]["mission_id"]


async def test_a_robot_on_another_backend_cannot_be_bound(tmp_path):
    service = s1_service(tmp_path, backend="logical_sim")
    refused = await call(service, OPERATOR, "missions.submit", project_id="campus_s1", robot_id="uav_01",
                         text="Inspect the red equipment marker east of the pad and bring back a photo.",
                         volume_id="campus_training", asset_ids=[], idempotency_key="k-1")
    assert refused["issue"]["code"] == "dispatch.backend_mismatch"


@pytest.mark.parametrize("scenario_id", ["p1_f01_nominal", "p1_f13_projects"])
async def test_full_s0_cases_pass_their_independent_judge(tmp_path, scenario_id):
    scenario = next(s for s in suite(ROOT)["s0"] if s["id"] == scenario_id)
    result = await run_case(tmp_path / scenario_id, ROOT, scenario, 7, "test")
    assert result["passed"], json.dumps(result, indent=1, default=str)
    assert not any(result["counts"].values())
