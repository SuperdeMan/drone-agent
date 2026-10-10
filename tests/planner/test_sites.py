"""The planner plans each bound request at its robot's site (D079): schema, SITE_DATA and the service path.

规划器按绑定请求的机器人站点规划（D079）：schema、SITE_DATA 与服务路径。
"""

from __future__ import annotations

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


def planner(provider, opened: list[Path] | None = None) -> SitePlanner:
    def session(scene: Path):
        if opened is not None:
            opened.append(scene)
        return InProcessSession(ToolCatalog(ROOT, scene)).initialize()

    return SitePlanner(provider, ModelIdentity("scripted", "scripted-fixture"), Registry(ROOT, scene=BASE),
                       session(BASE), scene=BASE, session=session)


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


def desk_service(tmp_path: Path, provider) -> MissionService:
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    operations = build_operations(ROOT, ledger, ROOT / "configs/sites/p5_desk_v1.yaml",
                                  ROOT / "configs/sites/p5_members_desk_s0.yaml", backups=tmp_path / "backups")
    registry = Registry(ROOT, scene=BASE)
    # The resident desk runs its service with the base scene (sim/compose.desk.yaml). / 常驻任务台的服务以基础场景运行。
    return MissionService(root=ROOT, scene=BASE, ledger=ledger, hub=FleetHub(ledger, tmp_path / "media"),
                          signing_key=SigningKey.generate(),
                          approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                          planner=planner(provider), operations=operations,
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
