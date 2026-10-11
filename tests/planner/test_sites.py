"""The planner plans each bound request at its robot's site (D079): schema, SITE_DATA and the service path.

规划器按绑定请求的机器人站点规划（D079）：schema、SITE_DATA 与服务路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from drone_agent.admission.models import MissionRequest, RequestChannel
from drone_agent.contracts import utcnow
from drone_agent.fleet.api import dispatch
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import source_context
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
from drone_agent.runtime.permission import TrustLevel
from drone_agent.runtime.signing import SigningKey

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / M2_SCENE
SITE_S1 = ROOT / "configs/scenarios/p5_site_s1_v1.yaml"
OPERATOR = "harness:p5-operator"


def answer(asset: str) -> dict:
    return {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": {
        "decision": "plan", "decline_reason": "", "goal": f"Inspect {asset}", "goal_type": "inspect",
        "approved_volume_id": "campus_training",
        "tasks": [{"task_id": f"inspect_{asset}", "skill_id": "skill.inspect.asset", "asset_id": asset}],
        "notes": ""}}]}


class Capturing(KeyedScriptedProvider):
    """The scripted double, keeping what each call showed the model. / 脚本替身，保留每次调用给模型看的内容。"""

    def __init__(self, answers: dict[str, dict]):
        super().__init__(answers)
        self.seen: list[tuple[list[dict], list[dict]]] = []

    async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                             thinking=None, timeout_s=None):
        self.seen.append((messages, tools))
        return await super().complete_tools(messages, model, temperature, max_tokens, tools=tools,
                                            tool_choice=tool_choice, thinking=thinking, timeout_s=timeout_s)


def request(text: str) -> MissionRequest:
    return MissionRequest(request_id="req-sites", text=text, requested_by=OPERATOR, trust_level=TrustLevel.FIRST_PARTY,
                          channel=RequestChannel.HARNESS, approved_volume_id="campus_training",
                          idempotency_key="sites-1", received_at=utcnow())


def planner(provider, opened: list[Path] | None = None, **options) -> SitePlanner:
    def session(scene: Path):
        if opened is not None:
            opened.append(scene)
        return InProcessSession(ToolCatalog(ROOT, scene)).initialize()

    return SitePlanner(provider, ModelIdentity("scripted", "scripted-fixture"), Registry(ROOT, scene=BASE),
                       session(BASE), scene=BASE, session=session, **options)


def schema_assets(tools: list[dict]) -> list[str]:
    draft = tools[0]["function"]["parameters"]
    return draft["properties"]["tasks"]["items"]["properties"]["asset_id"]["enum"]


async def test_the_schema_and_site_data_follow_the_site():
    text = "Inspect the road segment north of the pad."
    provider = Capturing({text: answer("road_north")})
    opened: list[Path] = []
    plans = planner(provider, opened)
    # The base scene has no road segment: the draft is outside its schema. / 基础场景没有路段：草案超出其 schema。
    at_base = await plans.plan(request(text), mission_id="m-base")
    assert at_base.status == "failed" and "road_north" not in schema_assets(provider.seen[0][1])
    at_site = await plans.plan(request(text), mission_id="m-site", site=(Registry(ROOT, scene=SITE_S1), SITE_S1))
    messages, tools = provider.seen[-1]
    assert at_site.status == "planned" and at_site.spec.targets[0].asset_id == "road_north"
    assert "road_north" in schema_assets(tools) and "road_north" in messages[-1]["content"]
    # One tool session per site map, opened on first use and kept. / 每个站点地图一个工具会话，首次使用时打开并保留。
    await plans.plan(request(text), mission_id="m-again", site=(Registry(ROOT, scene=SITE_S1), SITE_S1))
    assert opened == [BASE, SITE_S1]
    same = await plans.plan(request(text), mission_id="m-base-site", site=(Registry(ROOT, scene=BASE), BASE))
    assert same.status == "failed" and opened == [BASE, SITE_S1], "the default site reuses its session"


def desk_service(tmp_path: Path, provider, **options) -> MissionService:
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    operations = build_operations(ROOT, ledger, ROOT / "configs/sites/p5_desk_v1.yaml",
                                  ROOT / "configs/sites/p5_members_desk_s0.yaml", backups=tmp_path / "backups")
    registry = Registry(ROOT, scene=BASE)
    # The resident desk runs its service with the base scene (sim/compose.desk.yaml). / 常驻任务台的服务以基础场景运行。
    return MissionService(root=ROOT, scene=BASE, ledger=ledger, hub=FleetHub(ledger, tmp_path / "media"),
                          signing_key=SigningKey.generate(),
                          approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                          planner=planner(provider, **options), operations=operations,
                          provenance_context=source_context(ROOT, BASE, registry.sha256, backend="px4_sitl"),
                          backends=("px4_sitl", "logical_sim", "vendor_protocol_sim"))


async def submit(service, project_id: str, robot_id: str, text: str, key: str) -> dict:
    reply = await dispatch(service, {"method": "missions.submit", "actor": OPERATOR, "trust": "first_party",
                                     "params": {"project_id": project_id, "robot_id": robot_id, "text": text,
                                                "volume_id": "campus_training", "asset_ids": [],
                                                "idempotency_key": key}})
    assert reply["ok"], reply
    return reply["result"]


async def test_desk_requests_can_name_the_assets_of_their_own_site(tmp_path):
    road, red_b = "Inspect the road segment north of the pad.", "Inspect the red marker of site B."
    provider = Capturing({road: answer("road_north"), red_b: answer("asset_red_b")})
    service = desk_service(tmp_path, provider)
    campus = await submit(service, "campus_s1", "uav_01", road, "k-road")
    fleet = await submit(service, "fleet_s0", "uav_fb", red_b, "k-red-b")
    for view, asset in ((campus, "road_north"), (fleet, "asset_red_b")):
        assert view["mission"]["status"] == "awaiting_approval", view["mission"]
        assert [t["asset_id"] for t in view["versions"][0]["spec"]["targets"]] == [asset]
    (campus_messages, _), (fleet_messages, _) = provider.seen
    assert '"road_north"' in campus_messages[-1]["content"]
    assert '"asset_red_b"' in fleet_messages[-1]["content"] and '"asset_red"' not in fleet_messages[-1]["content"]


async def test_another_sites_asset_is_outside_the_schema_and_never_planned(tmp_path):
    # uav_fb flies at site B: the base scene's asset_red is not registered there. / uav_fb 在站点 B：基础场景的 asset_red 不在那里登记。
    text = "Inspect the red marker east of the pad."
    provider = Capturing({text: answer("asset_red")})
    view = await submit(desk_service(tmp_path, provider), "fleet_s0", "uav_fb", text, "k-foreign")
    assert view["mission"]["status"] == "planning_failed"
    assert "asset_red" not in schema_assets(provider.seen[0][1])
    assert {"asset_red_b", "asset_blue_b", "road_north_b"} <= set(schema_assets(provider.seen[0][1]))


async def test_a_planning_request_does_not_depend_on_when_it_is_made():
    # The digest of a recording covers the whole request; any clock value in SITE_DATA would make every recording
    # unreplayable (D079). / 录制摘要覆盖整个请求；SITE_DATA 中的任何时钟值都会使所有录制无法回放（D079）。
    from datetime import datetime, timezone

    from drone_agent.planner.engine import PlannerEngine
    from drone_agent.providers.replay import digest, request_body

    seen = []

    class Capture:
        async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                                 thinking=None, timeout_s=None):
            seen.append(digest(request_body("complete_tools", messages, model, temperature, max_tokens, tools,
                                            tool_choice, thinking)))
            raise RuntimeError("captured")

    for moment in (datetime(2026, 10, 10, 16, 22, tzinfo=timezone.utc), datetime(2026, 10, 11, 1, 13, tzinfo=timezone.utc)):
        tools = InProcessSession(ToolCatalog(ROOT, SITE_S1, clock=lambda moment=moment: moment)).initialize()
        engine = PlannerEngine(Capture(), ModelIdentity("x", "y"), Registry(ROOT, scene=SITE_S1), tools,
                               clock=lambda moment=moment: moment)
        await engine.plan(request("Inspect the road segment north of the pad."), mission_id="m-time")
    assert len(seen) == 2 and seen[0] == seen[1]


def scope_of(content: str) -> dict:
    return json.loads(content.split("OPERATOR_SCOPE:\n", 1)[1].split("\n", 1)[0])


async def test_only_planner_v3_shows_the_binding_and_older_requests_stay_byte_identical():
    # D081: the binding enters OPERATOR_SCOPE from planner-v3 on; earlier prompts keep their exact requests, so their
    # recordings still replay. / D081：planner-v3 起绑定进入 OPERATOR_SCOPE；更早提示的请求保持不变，录制仍可回放。
    from drone_agent.planner.engine import PlannerEngine

    registry, tools = Registry(ROOT, scene=SITE_S1), InProcessSession(ToolCatalog(ROOT, SITE_S1)).initialize()
    binding = {"project_id": "campus_s1", "project_name": "Campus", "site_id": "site_s1", "robot_id": "uav_01"}
    identity, asked = ModelIdentity("scripted", "scripted-fixture"), request("Inspect the road segment north of the pad.")

    def engine(version, **options):
        return PlannerEngine(Capturing({}), identity, registry, tools, prompt_version=version, **options)

    context = engine("planner-v2").retrieve(asked)
    for version in ("planner-v1", "planner-v2"):
        assert engine(version).messages(asked, context) == engine(version, binding=binding).messages(asked, context)
    shown = scope_of(engine("planner-v3", binding=binding).messages(asked, context)[-1]["content"])
    assert {k: shown[k] for k in binding} == binding and shown["approved_volume_id"] == "campus_training"
    unbound = scope_of(engine("planner-v3").messages(asked, context)[-1]["content"])
    assert "project_id" not in unbound, "no binding outside catalog mode"


async def test_the_service_names_the_robots_binding_from_the_catalog(tmp_path):
    text = "Inspect the red marker of site B."
    provider = Capturing({text: answer("asset_red_b")})
    service = desk_service(tmp_path, provider, prompt_version="planner-v3")
    view = await submit(service, "fleet_s0", "uav_fb", text, "k-bound")
    assert view["mission"]["status"] == "awaiting_approval", view["mission"]
    shown = scope_of(provider.seen[0][0][-1]["content"])
    assert (shown["project_id"], shown["site_id"], shown["robot_id"]) == ("fleet_s0", "site_fb", "uav_fb")
    assert shown["project_name"] == service.ops.catalog.projects["fleet_s0"].name
