"""X1 recheck activity (D088): the workflow catalog names a ground robot and a P4 analysis, the declared headings and
the forward range agree with the logical cameras, and the S0 business loop concludes `relocated`, or `not_attempted`
when the capture has no anomaly of the declared colour or the robot refuses the offer.

X1 复核活动（D088）：工作流目录点名地面车与 P4 分析；声明航向与前视测距与逻辑相机一致；S0 业务闭环得出 `relocated`，
在采集中没有声明颜色的异常物或地面车拒绝提议时得出 `not_attempted`。
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from pathlib import Path

import numpy as np
import pytest
import yaml

from drone_agent.contracts import FactSource, Frame, Position, WorldFact, utcnow
from drone_agent.eval.logical_ground import Block, view
from drone_agent.eval.x1_world import X1World
from drone_agent.fleet.locator import COLOURS, locate
from drone_agent.fleet.rechecks import forward_ground, route_heading, within
from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.workflow_models import WorkflowCatalog
from drone_agent.mission.registry import load_registry

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "configs/workflows/x1_campus_v1.yaml"
OPERATIONS = ROOT / "configs/sites/x1_campus_v1.yaml"


def checked(data: dict) -> WorkflowCatalog:
    catalog = WorkflowCatalog.model_validate(data)
    ops = load_catalog(OPERATIONS)
    catalog.check(ops, {s: load_registry(ROOT, ROOT / e.scene) for s, e in ops.sites.items()}, ROOT)
    return catalog


def recheck_workflow(data: dict) -> dict:
    return next(w for w in data["workflows"] if w["workflow_id"] == "anomaly_recheck")


def test_the_x1_workflow_catalog_hands_findings_to_its_ground_robot():
    catalog = checked(yaml.safe_load(WORKFLOWS.read_text(encoding="utf-8")))
    spec = catalog.latest("campus_ops", "anomaly_recheck")
    assert [n.activity for n in spec.nodes].count("ground_recheck") == 1


@pytest.mark.parametrize("mutate", [
    lambda w: next(n for n in w["nodes"] if n["node_id"] == "recheck")["params"].update(robot_id="uav_a"),
    lambda w: next(n for n in w["nodes"] if n["node_id"] == "analyze")["params"].pop("findings"),
    lambda w: w["nodes"].remove(next(n for n in w["nodes"] if n["node_id"] == "await_recheck"))
    or next(n for n in w["nodes"] if n["node_id"] == "review").update(after=[]),
    lambda w: next(n for n in w["nodes"] if n["node_id"] == "recheck")["params"].update(signature="green"),
], ids=["aircraft_named", "p2_analysis", "never_awaited", "undeclared_colour"])
def test_a_recheck_needs_a_ground_robot_a_p4_analysis_and_one_wait(mutate):
    data = yaml.safe_load(WORKFLOWS.read_text(encoding="utf-8"))
    mutate(recheck_workflow(data))
    with pytest.raises(ValueError):
        checked(copy.deepcopy(data))


def test_the_declared_heading_is_the_last_leg_of_the_observation_route():
    data = load_registry(ROOT, ROOT / "configs/scenarios/p3_site_a_v1.yaml").data
    assert route_heading(data, {"approach_route_id": "observe_asset_mid"}) == pytest.approx(0.0)
    assert route_heading(data, {"approach_route_id": "observe_asset_west"}) == pytest.approx(math.pi)
    assert route_heading(data, {"approach_route_id": "observe_asset_north"}) == pytest.approx(math.pi / 4)


@pytest.mark.parametrize(("block", "heading"), [((3.3, 8.2), 0.0), ((2.0, 6.5), math.pi / 2), ((1.2, 7.4), 2.1)])
def test_the_forward_range_reads_back_what_the_forward_camera_drew(block, heading):
    camera = load_registry(ROOT, ROOT / "configs/scenarios/x1_ground_a_v1.yaml").data["camera"]
    position = (block[0] - 1.6 * math.cos(heading), block[1] - 1.6 * math.sin(heading))
    raw = view(camera, position, heading, [Block(*block, 0.8, COLOURS["orange"])])
    found = locate(np.frombuffer(raw, dtype=np.uint8).reshape(120, 160, 3), "orange")
    point = forward_ground(camera, position, heading, found.u, found.bottom, half_depth_m=0.4)
    # The ground row ranges the near face; half the declared size gives the centre back. / 地面行测的是近侧面距离，
    # 加声明尺寸的一半回到中心。
    assert math.dist(point, block) <= 0.15
    near = forward_ground(camera, position, heading, found.u, found.bottom)
    assert 0.25 <= math.dist(near, block) <= 0.55


def test_inside_means_the_ellipse_or_the_region_plus_the_tolerance():
    def fact(kind, **value):
        return WorldFact(fact_id="t", subject="asset_mid", predicate="x1.target_point", value={"kind": kind, **value},
                         frame=Frame(frame_id="map_enu", map_version="x1_ground_a_v1"),
                         position=Position(x=0, y=0, z=0, covariance=(0.1, 0, 0, 0, 0.1, 0, 0, 0, 0)),
                         timestamp=utcnow(), source=FactSource.SENSOR)

    point = fact("point", semi_major_95_m=0.8)
    assert within(point, (1.2, 0.0), 0.5) and not within(point, (1.4, 0.0), 0.5)
    region = fact("search_region", radius_m=2.5)
    assert within(region, (2.9, 0.0), 0.5) and not within(region, (3.1, 0.0), 0.5)


async def run_until(w: X1World, run_id: str, *, timeout_s: float = 120) -> dict:
    """Drive one anomaly_recheck run, approving its missions and the ground mission and confirming the review.

    推进一次 anomaly_recheck 运行：审批其任务与地面任务，并确认复核。
    """
    approved: set[str] = set()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await w.step()
        approved |= set(await w.approve_waiting(run_id))
        for recheck in w.handoffs.store.rechecks():
            task = w.scheduler.store.task(recheck["ground_task_id"]) if recheck["ground_task_id"] else None
            mission = task["mission_id"] if task else None
            if mission and mission not in approved and w.status(mission) == "awaiting_approval":
                await w.approve(mission)
                approved.add(mission)
        nodes = w.store.nodes(run_id)
        if nodes["review"]["state"] == "waiting":
            await w.review(run_id, "review", "confirmed")
        if w.run(run_id)["state"] in ("completed", "failed", "outcome_unknown", "cancelled"):
            return w.store.nodes(run_id)
    raise TimeoutError("the run did not end")


@pytest.fixture
def world(tmp_path):
    w = X1World(tmp_path / "case", ROOT, seed=7)
    yield w
    w.ledger.close()


def test_an_anomaly_on_the_marker_is_relocated_by_the_ground_robot(world):
    async def scenario():
        await world.settle()
        world.anomaly("site_a", 5.3, 4.2)
        nodes = await run_until(world, await world.start("anomaly_recheck", {"asset": "asset_mid"}))
        result = nodes["await_recheck"]["result"]
        assert result["outcome"] == "relocated" and result["aerial_evidence"] and result["ground_evidence"]
        # Site a's (5.3, 4.2) is (3.3, 8.2) on the ground map. / 站点 a 的 (5.3, 4.2) 在地面地图中为 (3.3, 8.2)。
        assert math.dist(result["relocated"], (3.3, 8.2)) <= 0.5
        [recheck] = world.handoffs.store.rechecks()
        assert recheck["detail"]["kind"] == "point" and recheck["closed_at"] is not None
        assert nodes["review"]["state"] == nodes["report"]["state"] == "completed"
    asyncio.run(scenario())


def test_a_capture_without_the_declared_colour_hands_nothing_over(world):
    async def scenario():
        await world.settle()
        # A magenta block darkens the marker, so the analysis is suspected, but no orange is there.
        # 品红方块遮住标记，分析判为疑似，但没有橙色。
        world.anomaly("site_a", 5.3, 4.2, colour="magenta")
        nodes = await run_until(world, await world.start("anomaly_recheck", {"asset": "asset_mid"}))
        result = nodes["await_recheck"]["result"]
        assert (result["outcome"], result["reason"]) == ("not_attempted", "not_located")
        assert world.handoffs.store.open_offers() == [] and not world.rovers["ugv_01"].flights
    asyncio.run(scenario())


def test_a_robot_without_the_energy_refuses_and_the_recheck_is_not_attempted(world):
    async def scenario():
        await world.settle()
        world.rovers["ugv_01"].battery.fraction = 0.5
        world.anomaly("site_a", 5.3, 4.2)
        nodes = await run_until(world, await world.start("anomaly_recheck", {"asset": "asset_mid"}))
        result = nodes["await_recheck"]["result"]
        assert (result["outcome"], result["reason"]) == ("not_attempted", "handoff.energy")
        assert not world.rovers["ugv_01"].flights
    asyncio.run(scenario())
