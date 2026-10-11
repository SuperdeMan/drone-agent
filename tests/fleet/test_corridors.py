"""Per-leg corridors (D084): the v2 catalog switch, the corridor geometry and its contract against the box.

航段走廊（D084）：v2 目录开关、走廊几何，以及它相对外包矩形的契约。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from drone_agent.fleet.reservation import (
    UnresolvedFootprint,
    cell_resource,
    corridor_footprint,
    distance_to_box,
    execution_order,
    footprint,
    hull,
    package_points,
    package_stops,
    recovery_points,
    scope_corners,
    task_points,
    task_stops,
)
from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.scheduling_models import SchedulingCatalog, load_scheduling
from drone_agent.mission.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
M3_SCENE = ROOT / "configs/scenarios/m3_campus_v3.yaml"
CORRIDOR = load_scheduling(ROOT / "configs/scheduling/p3_campus_corridor_v1.yaml").airspace
# The digests every committed v1 catalog had before D084 / 每个已提交 v1 目录在 D084 之前的摘要
V1_DIGESTS = {"p3_campus_v1": "f2991071758b9d8015b95fd6cc3e0a7e1d873b931eb0131773f2ead7221e2000",
              "p3_desk_v1": "853f0b1a2046d9520056c6fe012da8944c9368466d3d03997cb0e62048b83a6d",
              "p3_s1_v1": "f7b658ffed4ff1c30860b8ae11cd0bca092b9e345b8d8b4fb1d442e99a15a739",
              "p5_desk_v1": "5e61a7098540c7ae45bfdcb2ff359e8ef906340d22676b57a755f893a3b07077"}
CATALOGS = ["p1_campus_v1", "p1_s1_v1", "p3_campus_v1", "p3_s1_v1", "p5_campus_v1", "p5_desk_v1", "p5_s1_v1"]


def registries(catalog):
    return {site_id: Registry(ROOT, scene=ROOT / site.scene) for site_id, site in catalog.sites.items()}


def route_package(asset_id: str, asset: dict, volume: str = "campus_training") -> dict:
    return {"spatial_scope": {"approved_volume_id": volume}, "nodes": [
        {"task_id": "takeoff", "skill_id": "skill.flight.takeoff", "params": {"altitude_m_agl": 4}},
        {"task_id": f"inspect_{asset_id}", "skill_id": "skill.inspect.asset", "depends_on": ["takeoff"],
         "params": {"asset_id": asset_id, "approach_route_id": asset["observation_route"]}},
        {"task_id": "return_home", "skill_id": "skill.flight.return_home", "depends_on": [f"inspect_{asset_id}"],
         "params": {"home_ref": "home_v1", "return_route_id": "return"}},
        {"task_id": "land", "skill_id": "skill.flight.land", "depends_on": ["return_home"],
         "params": {"landing_site_id": "home_pad"}}]}


def both(grid, origin, data, package, robot) -> tuple:
    coarse = footprint(grid, origin, package_points(data, package, robot),
                       data["volumes"].get(package["spatial_scope"]["approved_volume_id"]))
    corridor = corridor_footprint(grid, origin, package_stops(data, package), recovery_points(data, robot), coarse)
    return coarse, corridor


def cell_of(grid, point) -> str:
    return cell_resource(grid.frame, math.floor(point[0] / grid.cell_m), math.floor(point[1] / grid.cell_m))


def samples(a, b, step: float = 0.25):
    count = max(1, math.ceil(math.dist(a, b) / step))
    return [(a[0] + (b[0] - a[0]) * k / count, a[1] + (b[1] - a[1]) * k / count) for k in range(count + 1)]


# ── the catalog switch / 目录开关 ──


@pytest.mark.parametrize("name", sorted(V1_DIGESTS))
def test_v1_catalogs_keep_their_digests_and_the_box(name):
    catalog = load_scheduling(ROOT / f"configs/scheduling/{name}.yaml")
    assert catalog.sha256 == V1_DIGESTS[name]
    assert catalog.airspace.footprint == "box-v1"
    assert "footprint" not in catalog.body()["airspace"]


def test_only_a_v2_catalog_declares_the_footprint_and_it_must():
    body = yaml.safe_load((ROOT / "configs/scheduling/p3_campus_v1.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="v1 scheduling catalog keeps the box"):
        SchedulingCatalog.model_validate({**body, "airspace": {**body["airspace"], "footprint": "corridor-v1"}})
    with pytest.raises(ValueError, match="must declare airspace.footprint"):
        SchedulingCatalog.model_validate({**body, "format": "drone.scheduling-catalog/v2"})
    boxed = SchedulingCatalog.model_validate({**body, "format": "drone.scheduling-catalog/v2",
                                              "airspace": {**body["airspace"], "footprint": "box-v1"}})
    corridor = load_scheduling(ROOT / "configs/scheduling/p3_campus_corridor_v1.yaml")
    assert boxed.sha256 != corridor.sha256 and corridor.body()["airspace"]["footprint"] == "corridor-v1"


def test_the_corridor_catalogs_differ_from_their_v1_twins_only_in_format_id_and_footprint():
    for v1, v2 in (("p3_campus_v1", "p3_campus_corridor_v1"), ("p3_s1_v1", "p3_s1_corridor_v1")):
        old = load_scheduling(ROOT / f"configs/scheduling/{v1}.yaml").body()
        new = load_scheduling(ROOT / f"configs/scheduling/{v2}.yaml").body()
        assert new.pop("format") == "drone.scheduling-catalog/v2" and new.pop("catalog_id") == v2
        assert new["airspace"].pop("footprint") == "corridor-v1"
        old.pop("format"), old.pop("catalog_id")
        assert new == old


# ── geometry / 几何 ──


def test_hull_and_distance():
    assert hull([(0, 0), (1, 1), (2, 2)]) == [(0, 0), (2, 2)]
    assert hull([(0, 0), (0, 0)]) == [(0, 0)]
    assert sorted(hull([(0, 0), (4, 0), (0, 4), (1, 1)])) == [(0, 0), (0, 4), (4, 0)]
    triangle = hull([(0, 0), (0, 4), (5, 4)])
    assert distance_to_box(triangle, (1, 2, 2, 3)) == 0.0  # inside / 在内部
    assert distance_to_box([(0, 0), (10, 0)], (4, -1, 5, 1)) == 0.0  # a segment through the box / 线段穿过
    assert distance_to_box([(0, 0)], (3, 4, 5, 6)) == pytest.approx(5.0)
    # The P3 a-mid corner: (4, 0) lies 3.2 / |(1, 0.8)| = 2.499 m from the hypotenuse, so the corner cell is left out at
    # a 2 m buffer. / P3 a-mid 的角：(4, 0) 距斜边 3.2 / |(1, 0.8)| = 2.499 m，因此缓冲 2 m 时角上的单元被排除。
    assert distance_to_box(triangle, (4, -4, 8, 0)) == pytest.approx(3.2 / math.hypot(1, 0.8))


def test_execution_order_follows_the_executive():
    nodes = [{"task_id": "land", "depends_on": ["back"]}, {"task_id": "up"},
             {"task_id": "back", "depends_on": ["look"]}, {"task_id": "look", "depends_on": ["up"]}]
    assert [n["task_id"] for n in execution_order(nodes)] == ["up", "look", "back", "land"]
    with pytest.raises(UnresolvedFootprint):
        execution_order([{"task_id": "a", "depends_on": ["b"]}, {"task_id": "b", "depends_on": ["a"]}])


# ── the contract against the box / 相对外包矩形的契约 ──


@pytest.mark.parametrize("name", CATALOGS)
def test_a_corridor_holds_a_subset_of_the_box_and_every_leg_and_recovery_line(name):
    # On every committed site map: the corridor is cut from the box, and every point of every leg, every point within
    # the buffer of a leg, and every straight line from a leg to a recovery target lies in a held cell.
    # 在每张已提交的站点地图上：走廊取自外包矩形；每个航段上的点、距航段不超过缓冲的点，以及从航段直飞恢复目标的直线，
    # 都落在所持单元内。
    grid = CORRIDOR
    reach = grid.buffer_m - 0.05
    catalog = load_catalog(ROOT / f"configs/sites/{name}.yaml")
    smaller = 0
    for site, registry in registries(catalog).items():
        data = registry.data
        robot = next(r for r, entry in catalog.robots.items() if entry.site_id == site)
        for asset_id, asset in data["assets"].items():
            coarse, corridor = both(grid, (0.0, 0.0), data, route_package(asset_id, asset), robot)
            held = set(corridor.cells)
            assert held <= set(coarse.cells) and corridor.box == coarse.box, (site, asset_id)
            smaller += len(held) < len(coarse.cells)
            chain = [stop[0] for stop in package_stops(data, route_package(asset_id, asset))]
            targets = recovery_points(data, robot)
            for a, b in zip(chain, chain[1:], strict=False):
                for p in samples(a, b):
                    for dx, dy in ((0, 0), (reach, 0), (-reach, 0), (0, reach), (0, -reach)):
                        assert cell_of(grid, (p[0] + dx, p[1] + dy)) in held, (site, asset_id, p, dx, dy)
                for p in samples(a, b, step=1.0):
                    for target in targets:
                        for q in samples(p, target, step=0.5):
                            assert cell_of(grid, q) in held, (site, asset_id, p, target, q)
    assert smaller, "the corridor never left out a cell of the box / 走廊从未少于外包矩形"


def test_an_external_mode_node_holds_its_whole_scope():
    data = Registry(ROOT, scene=M3_SCENE).data
    package = {"spatial_scope": {"approved_volume_id": "campus_training"}, "nodes": [
        {"task_id": "up", "skill_id": "skill.flight.takeoff", "params": {"altitude_m_agl": 4}},
        {"task_id": "go", "skill_id": "skill.flight.goto_local", "depends_on": ["up"],
         "params": {"goal_id": "east_hold"}}]}
    coarse, corridor = both(CORRIDOR, (0.0, 0.0), data, package, "uav_01")
    (x0, y0), _, (x1, y1), _ = scope_corners(data)
    for x in range(math.floor(x0), math.ceil(x1) + 1):
        for y in range(math.floor(y0), math.ceil(y1) + 1):
            assert cell_of(CORRIDOR, (min(x, x1), min(y, y1))) in set(corridor.cells)
    assert set(corridor.cells) <= set(coarse.cells)


def compiled_package(registry: Registry, robot: str, asset_id: str, volume: str, modes=None) -> dict:
    """The package the service compiles for a scheduled single-asset task (`service.submit_assigned`).

    服务为调度的单资产任务单编译的任务包（`service.submit_assigned`）。
    """
    from drone_agent.admission.compiler import CompileContext, compile_spec
    from drone_agent.admission.models import MissionRequest, RequestChannel
    from drone_agent.contracts import Provenance, utcnow
    from drone_agent.planner.draft import draft_to_spec
    from drone_agent.runtime.permission import TrustLevel

    now = utcnow()
    draft = {"decision": "plan", "decline_reason": "", "goal": f"Inspect {asset_id}", "goal_type": "inspect",
             "approved_volume_id": volume,
             "tasks": [{"task_id": f"inspect_{asset_id}", "skill_id": "skill.inspect.asset", "asset_id": asset_id}],
             "notes": "corridor test"}
    request = MissionRequest(request_id="req-corridor-test", text=f"inspect {asset_id}", requested_by="harness:test",
                             trust_level=TrustLevel.FIRST_PARTY, channel=RequestChannel.SCHEDULER,
                             approved_volume_id=volume, asset_ids=[asset_id], idempotency_key="k", received_at=now)
    spec = draft_to_spec(draft, request, registry, mission_id="m-corridor-test", mission_version=1,
                         provenance=Provenance(model_id="test/template", prompt_version="test-v1", input_hash="0" * 64,
                                               generated_at=now), now=now)
    result = compile_spec(spec, CompileContext(registry=registry, robot_id=robot, control_modes=modes))
    assert result.package is not None, [i.code for i in result.issues]
    return result.package.model_dump(mode="json")


def planned_and_compiled(registry: Registry, robot: str, asset_id: str, modes=None) -> tuple:
    data = registry.data
    volume = data["assets"][asset_id]["volume"]
    coarse = footprint(CORRIDOR, (0.0, 0.0), task_points(data, asset_id, robot), data["volumes"][volume])
    planned = corridor_footprint(CORRIDOR, (0.0, 0.0), task_stops(data, asset_id), recovery_points(data, robot), coarse)
    _, compiled = both(CORRIDOR, (0.0, 0.0), data, compiled_package(registry, robot, asset_id, volume, modes), robot)
    return planned, compiled


@pytest.mark.parametrize("name", ["p3_campus_v1", "p3_s1_v1", "p5_desk_v1"])
def test_the_planned_corridor_equals_the_compiled_packages(name):
    # The scheduler judges a candidate on the planned corridor; dispatch holds the compiled package's. They must be
    # the same cells. / 调度器按计划走廊判定候选，派遣持有编译后任务包的走廊；二者必须是同一组单元。
    catalog = load_catalog(ROOT / f"configs/sites/{name}.yaml")
    for site, registry in registries(catalog).items():
        robot = next(r for r, entry in catalog.robots.items() if entry.site_id == site)
        for asset_id in registry.data["assets"]:
            planned, compiled = planned_and_compiled(registry, robot, asset_id)
            assert planned == compiled, (name, site, asset_id)


def test_the_planned_corridor_follows_an_assets_own_return_route_and_covers_the_lowered_inspection():
    # M3: asset_green registers a detour return route and a local observation goal; the compiler binds the detour,
    # and with external mode lowers the inspection to the whole scope. The planned corridor holds both forms.
    # M3：asset_green 登记了绕行返航航线与局部观测目标；编译器绑定绕行航线，具备外部模式时把巡检降为整个范围。
    # 计划走廊同时容纳两种形式。
    from drone_agent.contracts import ControlMode

    registry = Registry(ROOT, scene=M3_SCENE)
    for modes in (None, frozenset({ControlMode.MISSION_UPLOAD, ControlMode.EXTERNAL_MODE})):
        planned, compiled = planned_and_compiled(registry, "uav_01", "asset_green", modes)
        assert set(compiled.cells) <= set(planned.cells), modes
    # Without the goal, the planned corridor is exactly the compiled one, return detour included.
    # 去掉观测目标后，计划走廊与编译走廊完全相同，包括绕行返航。
    data = registry.data
    del data["assets"]["asset_green"]["observation_goal"]
    try:
        planned, compiled = planned_and_compiled(registry, "uav_01", "asset_green")
        assert planned == compiled
        detour = [tuple(map(float, p[:2])) for p in data["routes"][data["assets"]["asset_green"]["return_route"]]]
        assert all(cell_of(CORRIDOR, p) in set(planned.cells) for p in detour)
    finally:
        data["assets"]["asset_green"]["observation_goal"] = "green_observe"


def test_on_the_p3_pair_the_corridor_keeps_every_conflict_of_the_box():
    # The design estimate (D084): on the P3 site pair no conflicting pair goes away; shared markers still meet.
    # 设计估算（D084）：在 P3 站点对上没有一个冲突对消失；共享标记仍然相交。
    catalog = load_catalog(ROOT / "configs/sites/p3_campus_v1.yaml")
    scheduling = load_scheduling(ROOT / "configs/scheduling/p3_campus_corridor_v1.yaml")
    maps = registries(catalog)

    def cells(site: str, robot: str, asset: str, model: str) -> set[str]:
        data = maps[site].data
        coarse, corridor = both(scheduling.airspace, scheduling.origin(site), data,
                                route_package(asset, data["assets"][asset]), robot)
        return set(corridor.cells if model == "corridor" else coarse.cells)

    for model in ("box", "corridor"):
        a = {asset: cells("site_a", "uav_a", asset, model) for asset in ("asset_west", "asset_mid", "asset_north")}
        b = {asset: cells("site_b", "uav_b", asset, model) for asset in ("asset_mid", "asset_north", "asset_east")}
        conflicts = sorted((x, y) for x in a for y in b if a[x] & b[y])
        assert len(conflicts) == 5, (model, conflicts)
        assert ("asset_mid", "asset_mid") in conflicts and ("asset_mid", "asset_east") not in conflicts


# ── the long-route ladder (D084) / 长航线阶梯（D084） ──


def test_the_long_route_ladder_loads_checks_and_holds_far_less_under_corridors(tmp_path):
    from drone_agent.eval.p3_layout import LONG_ASSETS, ladder

    directory = ROOT / "outputs" / "test-long-ladder" / tmp_path.name
    try:
        boxed = ladder(ROOT, 4, directory / "box", layout="long")
        corridor = ladder(ROOT, 4, directory / "corridor", layout="long", footprint="corridor-v1")
        assert boxed["sha256"] != corridor["sha256"]
        for result, model in ((boxed, "box-v1"), (corridor, "corridor-v1")):
            catalog = load_catalog(Path(result["files"]["catalog"]))
            catalog.check_files(ROOT)
            scheduling = load_scheduling(Path(result["files"]["scheduling"]))
            scheduling.check(catalog, registries(catalog))  # every shared asset lies at one shared place
            assert scheduling.airspace.footprint == model and len(catalog.robots) == 4
        catalog = load_catalog(Path(corridor["files"]["catalog"]))
        scheduling = load_scheduling(Path(corridor["files"]["scheduling"]))
        maps = registries(catalog)
        ratios = []
        for robot, entry in catalog.robots.items():
            data, origin = maps[entry.site_id].data, scheduling.origin(entry.site_id)
            assert len(data["assets"]) == len(LONG_ASSETS)
            for asset_id, asset in data["assets"].items():
                coarse, fine = both(scheduling.airspace, origin, data, route_package(asset_id, asset), robot)
                ratios.append(len(fine.cells) / len(coarse.cells))
        assert max(ratios) < 0.6 and sum(ratios) / len(ratios) < 0.4
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
