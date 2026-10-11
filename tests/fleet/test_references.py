"""D082: names of other projects, sites, robots or docks in a request are rejected before planning.

D082：请求中点名其他项目、站点、机器人或机场的，在规划前被拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from drone_agent.fleet.api import dispatch
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import source_context
from drone_agent.fleet.references import ReferenceIndex, compile_name, load_aliases
from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.transport import FleetHub
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.planner.draft import TOOL_NAME
from drone_agent.planner.engine import ModelIdentity
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.planner.sites import SitePlanner
from drone_agent.planner.tools.catalog import ToolCatalog
from drone_agent.planner.tools.client import InProcessSession
from drone_agent.providers import KeyedScriptedProvider
from drone_agent.runtime.signing import SigningKey

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / M2_SCENE
DESK = ROOT / "configs/sites/p5_desk_v1.yaml"
OPERATOR = "harness:p5-operator"


def test_names_keep_identifier_boundaries_and_the_case_of_a_lone_letter():
    site = compile_name("site A")
    assert site.search("Inspect SITE A now") and site.search("siteA") and site.search("site  A's marker")
    assert not site.search("the site a few metres away") and not site.search("website A") and not site.search("site AB")
    station = compile_name("A 站")
    assert station.search("园区 A 站的标记") and station.search("A站") and not station.search("a站")
    assert not station.search("DATA 站")
    ident = compile_name("site_fa")
    assert ident.search("Check site_fa's marker") and ident.search("SITE_FA") and not ident.search("site_fab")
    assert compile_name("港区").search("港区项目") and compile_name("campus").search("the Campus crew")
    assert not compile_name("campus").search("campus_training"), "a volume id is not the campus project"


def test_alias_files_must_name_known_entities_once(tmp_path):
    catalog = load_catalog(DESK)
    aliases, digest = load_aliases(DESK, catalog)
    assert aliases["sites"]["site_fa"] == ["site A", "A 站", "站点 A"] and len(digest) == 64
    assert load_aliases(tmp_path / "none.yaml", catalog) == ({"projects": {}, "sites": {}}, None)
    folder = tmp_path / "aliases"
    folder.mkdir()
    bad = {"format": "drone.catalog-aliases/v1", "catalog_id": catalog.catalog_id, "sites": {"site_zz": ["site Z"]}}
    (folder / f"{catalog.catalog_id}.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="site_zz"):
        load_aliases(tmp_path / "catalog.yaml", catalog)
    registries = {s: Registry(ROOT, scene=ROOT / site.scene) for s, site in catalog.sites.items()}
    twice = {"format": "drone.catalog-aliases/v1", "catalog_id": catalog.catalog_id,
             "sites": {"site_fa": ["north yard"], "site_fb": ["North Yard"]}}
    (folder / f"{catalog.catalog_id}.yaml").write_text(yaml.safe_dump(twice), encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)north yard"):
        ReferenceIndex.build(catalog, registries, tmp_path / "catalog.yaml")


def answer(asset: str) -> dict:
    return {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": {
        "decision": "plan", "decline_reason": "", "goal": f"Inspect {asset}", "goal_type": "inspect",
        "approved_volume_id": "campus_training",
        "tasks": [{"task_id": f"inspect_{asset}", "skill_id": "skill.inspect.asset", "asset_id": asset}],
        "notes": ""}}]}


class Counting(KeyedScriptedProvider):
    calls = 0

    async def complete_tools(self, *args, **kwargs):
        Counting.calls += 1
        return await super().complete_tools(*args, **kwargs)


def service(tmp_path: Path, provider, **options) -> MissionService:
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    operations = build_operations(ROOT, ledger, DESK, ROOT / "configs/sites/p5_members_desk_s0.yaml",
                                  backups=tmp_path / "backups")

    def session(scene: Path):
        return InProcessSession(ToolCatalog(ROOT, scene)).initialize()

    registry = Registry(ROOT, scene=BASE)
    planner = SitePlanner(provider, ModelIdentity("scripted", "scripted-fixture"), registry, session(BASE),
                          scene=BASE, session=session)
    return MissionService(root=ROOT, scene=BASE, ledger=ledger, hub=FleetHub(ledger, tmp_path / "media"),
                          signing_key=SigningKey.generate(),
                          approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                          planner=planner, operations=operations,
                          provenance_context=source_context(ROOT, BASE, registry.sha256, backend="px4_sitl"),
                          backends=("px4_sitl", "logical_sim", "vendor_protocol_sim"), **options)


async def submit(svc, robot_id: str, text: str, key: str) -> dict:
    reply = await dispatch(svc, {"method": "missions.submit", "actor": OPERATOR, "trust": "first_party",
                                 "params": {"project_id": "fleet_s0", "robot_id": robot_id, "text": text,
                                            "volume_id": "campus_training", "asset_ids": [], "idempotency_key": key}})
    assert reply["ok"], reply
    return reply["result"]


async def test_a_request_naming_another_site_is_rejected_before_the_model_is_asked(tmp_path):
    foreign, own = "Smoke: the red marker over at site A.", "Smoke: the red marker of site B."
    Counting.calls = 0
    provider = Counting({foreign: answer("asset_red_b"), own: answer("asset_red_b")})
    svc = service(tmp_path, provider)
    rejected = await submit(svc, "uav_fb", foreign, "k-foreign")
    assert rejected["mission"]["status"] == "rejected" and Counting.calls == 0
    found = [i for i in rejected["issues"] if i["code"] == "scope.foreign_reference"]
    assert found and "site_fa" in found[0]["message"] and "site site_fb" in found[0]["message"]
    planned = await submit(svc, "uav_fb", own, "k-own")
    assert planned["mission"]["status"] == "awaiting_approval" and Counting.calls == 1


async def test_prompt_evaluations_switch_the_check_off(tmp_path):
    text = "Smoke: the red marker over at site A."
    Counting.calls = 0
    svc = service(tmp_path, Counting({text: answer("asset_red_b")}), reference_check=False)
    view = await submit(svc, "uav_fb", text, "k-off")
    assert view["mission"]["status"] == "awaiting_approval" and Counting.calls == 1
