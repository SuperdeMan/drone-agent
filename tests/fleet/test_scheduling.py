"""P3 scheduling contracts: catalogs, airspace geometry, the pure decision and the D060 store (D059, D060).

P3 调度契约：目录、空域几何、纯函数判定与 D060 存储（D059、D060）。
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from drone_agent.contracts import utcnow
from drone_agent.eval.p3_layout import dump_map, ladder, site_map
from drone_agent.fleet.coordinator import decide, same_decision
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import MigrationError
from drone_agent.fleet.operations_store import migrate as migrate_operations
from drone_agent.fleet.reservation import (
    cell_resource,
    clip,
    envelope_cells,
    footprint,
    grid_cells,
    package_points,
    task_points,
)
from drone_agent.fleet.resources import REASONS, load_catalog
from drone_agent.fleet.scheduling_models import PERMANENT, SchedulingCatalog, load_scheduling
from drone_agent.fleet.scheduling_store import (
    DDL,
    INDEXES,
    TABLES,
    SchedulingStore,
    StaleTask,
    drill,
    migrate,
    other_lines,
    scheduling_schema,
)
from drone_agent.mission.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "configs/sites/p3_campus_v1.yaml"
SCHEDULING = ROOT / "configs/scheduling/p3_campus_v1.yaml"
S1_CATALOG = ROOT / "configs/sites/p3_s1_v1.yaml"
S1_SCHEDULING = ROOT / "configs/scheduling/p3_s1_v1.yaml"
DESK_CATALOG = ROOT / "configs/sites/p1_s1_v1.yaml"
DESK_SCHEDULING = ROOT / "configs/scheduling/p3_desk_v1.yaml"


def registries(catalog):
    return {site_id: Registry(ROOT, scene=ROOT / site.scene) for site_id, site in catalog.sites.items()}


# ── catalogs / 目录 ──


def test_the_committed_catalogs_check_against_their_operations_catalogs():
    for operations, scheduling in ((CATALOG, SCHEDULING), (S1_CATALOG, S1_SCHEDULING),
                                   (DESK_CATALOG, DESK_SCHEDULING)):
        catalog = load_catalog(operations)
        catalog.check_files(ROOT)
        load_scheduling(scheduling).check(catalog, registries(catalog))


def test_a_site_without_an_origin_or_a_wrong_operations_catalog_is_refused():
    catalog = load_catalog(CATALOG)
    data = yaml.safe_load(SCHEDULING.read_text(encoding="utf-8"))
    missing = copy.deepcopy(data)
    del missing["airspace"]["sites"]["site_d"]
    with pytest.raises(ValueError, match="missing"):
        SchedulingCatalog.model_validate(missing).check(catalog, registries(catalog))
    other = {**copy.deepcopy(data), "operations_catalog": "p1_campus_v1"}
    with pytest.raises(ValueError, match="written for"):
        SchedulingCatalog.model_validate(other).check(catalog, registries(catalog))
    with pytest.raises(ValueError):
        SchedulingCatalog.model_validate({**copy.deepcopy(data), "surprise": 1})


def test_a_shared_asset_at_two_shared_places_is_refused():
    catalog = load_catalog(CATALOG)
    data = yaml.safe_load(SCHEDULING.read_text(encoding="utf-8"))
    data["airspace"]["sites"]["site_b"]["origin"] = [13, 0]
    with pytest.raises(ValueError, match="asset_mid"):
        SchedulingCatalog.model_validate(data).check(catalog, registries(catalog))


def test_the_p3_reason_codes_are_controlled_and_permanent_codes_are_blocking():
    for code in ("asset.unregistered", "volume.unapproved", "task.robot_excluded", "airspace.cell_held",
                 "airspace.envelope", "airspace.hold_missing"):
        assert REASONS[code].value == "blocked"
    assert PERMANENT <= set(REASONS)
    assert all(REASONS[code].value == "blocked" for code in PERMANENT)


def test_the_committed_site_maps_are_what_the_layout_generator_writes():
    expected = {
        "p3_site_a_v1": ("left", "uav_a", {"west": "asset_west", "mid": "asset_mid", "north": "asset_north"},
                         ("west", "mid", "north"), None),
        "p3_site_b_v1": ("right", "uav_b", {"mid": "asset_mid", "north": "asset_north", "east": "asset_east"},
                         ("mid", "north", "east"), None),
        "p3_s1_a_v1": ("left", "uav_01", {"west": "asset_west", "mid": "asset_mid"}, ("west", "mid"),
                       "/world/default/model/x500_0/link/inspection_camera/sensor/cam_0/image"),
        "p3_s1_b_v1": ("right", "uav_02", {"mid": "asset_mid", "east": "asset_east"}, ("mid", "east"),
                       "/world/default/model/x500_1/link/inspection_camera/sensor/cam_0/image"),
    }
    for name, (side, robot, assets, markers, topic) in expected.items():
        generated = site_map(ROOT, registry_id=name, robot_id=robot, side=side, assets=assets, markers=markers,
                             camera_topic=topic)
        committed = yaml.safe_load((ROOT / f"configs/scenarios/{name}.yaml").read_text(encoding="utf-8"))
        assert committed == yaml.safe_load(dump_map(generated, "")), name


def test_a_generated_ladder_loads_and_checks(tmp_path):
    directory = ROOT / "outputs" / "test-ladder" / tmp_path.name
    try:
        result = ladder(ROOT, 10, directory)
        catalog = load_catalog(Path(result["files"]["catalog"]))
        catalog.check_files(ROOT)
        assert len(catalog.robots) == 10 and len(catalog.docks) == 10
        load_scheduling(Path(result["files"]["scheduling"])).check(catalog, registries(catalog))
        assert ladder(ROOT, 10, directory)["sha256"] == result["sha256"]
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)


# ── airspace geometry / 空域几何 ──


def test_grid_cells_are_half_open_and_a_degenerate_box_keeps_one_cell():
    assert grid_cells((0.0, 0.0, 4.0, 4.0), 4.0) == [(0, 0)]
    assert grid_cells((-2.0, -2.0, 7.0, 6.0), 4.0) == [(i, j) for i in (-1, 0, 1) for j in (-1, 0, 1)]
    assert grid_cells((4.0, 4.0, 4.0, 4.0), 4.0) == [(1, 1)]
    assert clip((0, 0, 10, 10), (20, 20, 30, 30)) is None


def test_task_footprints_match_the_compiled_package_and_pair_neighbours_meet_only_on_shared_markers():
    catalog, scheduling = load_catalog(CATALOG), load_scheduling(SCHEDULING)
    maps = registries(catalog)
    grid = scheduling.airspace

    def task_cells(site: str, asset: str) -> set[str]:
        data = maps[site].data
        return set(footprint(grid, scheduling.origin(site), task_points(data, asset),
                             data["volumes"]["campus_training"]).cells)

    a_mid, a_west, a_north = (task_cells("site_a", a) for a in ("asset_mid", "asset_west", "asset_north"))
    b_mid, b_east = task_cells("site_b", "asset_mid"), task_cells("site_b", "asset_east")
    assert a_mid & b_mid and a_north & b_mid
    assert not a_mid & b_east and not a_west & b_east and not a_north & b_east
    assert not a_mid & task_cells("site_c", "asset_mid2")
    # The planned footprint equals the one of a compiled package. / 计划的航迹覆盖等于编译后任务包的航迹覆盖。
    data = maps["site_a"].data
    package = {"nodes": [{"skill_id": "skill.flight.takeoff", "params": {"altitude_m_agl": 4}},
                         {"skill_id": "skill.inspect.asset",
                          "params": {"asset_id": "asset_mid", "approach_route_id": "observe_asset_mid"}},
                         {"skill_id": "skill.flight.return_home", "params": {"home_ref": "home_v1",
                                                                              "return_route_id": "return"}},
                         {"skill_id": "skill.flight.land", "params": {"landing_site_id": "home_pad"}}]}
    assert set(package_points(data, package, "uav_a")) == set(task_points(data, "asset_mid", "uav_a"))


# ── the spatial contract of every skill (D075) / 每个技能的空间契约（D075） ──

M3_SCENE = ROOT / "configs/scenarios/m3_campus_v3.yaml"


def m3_package(*nodes) -> dict:
    return {"spatial_scope": {"approved_volume_id": "campus_training"},
            "nodes": [{"skill_id": skill, "robot_id": "uav_01", "params": params} for skill, params in nodes]}


def test_every_registered_skill_has_an_airspace_footprint():
    # A new motion skill cannot be added without saying where it may fly (D075).
    # 新增运动技能必须说明它可能飞到哪里（D075）。
    from drone_agent.fleet.reservation import SKILL_PLACES

    manifests = {yaml.safe_load(path.read_text(encoding="utf-8"))["skill_id"]
                 for path in (ROOT / "configs/skills").glob("*.yaml")}
    assert manifests == set(SKILL_PLACES)


def test_a_local_goal_flight_reserves_the_whole_external_mode_scope():
    # The external review's counterexample: home (0, 0) and goto_local to a goal far from every route. The goal, and
    # every place the local planner may go around obstacles, must lie inside the reserved box (D075).
    # 外部评审的反例：home (0, 0) 与 goto_local 去一个远离所有航线的目标。目标以及局部规划器为避障可能去的每个位置，都必须
    # 位于预约矩形之内（D075）。
    registry = Registry(ROOT, scene=M3_SCENE)
    data, grid = registry.data, load_scheduling(SCHEDULING).airspace
    low, high = registry.task_scope()
    for package in (m3_package(("skill.flight.takeoff", {"altitude_m_agl": 4}),
                               ("skill.flight.goto_local", {"goal_id": "east_hold"})),
                    m3_package(("skill.inspect.asset_local", {"asset_id": "asset_green",
                                                               "approach_goal_id": "green_observe"}))):
        fp = footprint(grid, (0.0, 0.0), package_points(data, package, "uav_01"),
                       data["volumes"]["campus_training"])
        x0, y0, x1, y1 = fp.box
        assert x0 <= low[0] and y0 <= low[1] and x1 >= high[0] and y1 >= high[1]
        for goal in data["local_goals"].values():
            assert x0 <= goal[0] <= x1 and y0 <= goal[1] <= y1
    # The same asset planned before compilation is covered just as widely. / 编译前计划的同一资产覆盖同样宽。
    planned = footprint(grid, (0.0, 0.0), task_points(data, "asset_green", "uav_01"), data["volumes"]["campus_training"])
    assert planned.box[0] <= low[0] and planned.box[3] >= high[1]


def test_a_route_flight_covers_every_landing_site_a_recovery_may_divert_to():
    # Recovery v2 may land at a reserved backup site (D042); it lies off every route of the M3 inspection.
    # 恢复策略 v2 可能改降到预留的备用降落点（D042）；它不在 M3 巡检的任何航线上。
    data = Registry(ROOT, scene=M3_SCENE).data
    package = m3_package(("skill.flight.takeoff", {"altitude_m_agl": 4}),
                         ("skill.inspect.asset", {"asset_id": "asset_red", "approach_route_id": "observe_red"}),
                         ("skill.flight.return_home", {"home_ref": "home_v1", "return_route_id": "return"}),
                         ("skill.flight.land", {"landing_site_id": "home_pad"}))
    points = package_points(data, package, "uav_01")
    assert (-3.0, 31.0) in points
    assert (-3.0, 31.0) not in package_points(data, package, "uav_99")


@pytest.mark.parametrize("node", [("skill.flight.orbit", {"center": [100, 0, 10]}),
                                  ("skill.flight.fly_route", {"route_id": "unregistered"}),
                                  ("skill.flight.goto_local", {"goal_id": "nowhere"}),
                                  ("skill.flight.land", {}),
                                  ("skill.inspect.asset", {"asset_id": "asset_red"})])
def test_a_node_that_cannot_be_bounded_is_unresolvable_never_empty(node):
    from drone_agent.fleet.reservation import UnresolvedFootprint

    data = Registry(ROOT, scene=M3_SCENE).data
    with pytest.raises(UnresolvedFootprint):
        package_points(data, m3_package(("skill.flight.takeoff", {"altitude_m_agl": 4}), node), "uav_01")


@pytest.mark.parametrize("name", ["p1_campus_v1", "p1_s1_v1", "p3_campus_v1", "p3_s1_v1", "p5_campus_v1",
                                  "p5_desk_v1", "p5_s1_v1"])
def test_the_committed_route_flights_keep_their_footprints(name):
    # On every committed site map the resolver gives a route-based package exactly the places of the P3 contract it
    # replaced (D059): home, the observation route, the asset, the return route and the pad.
    # 在每张已提交的站点地图上，解析器给航线任务包的位置与其替代的 P3 契约完全相同（D059）：home、观测航线、资产、返航
    # 航线与机位。
    catalog = load_catalog(ROOT / f"configs/sites/{name}.yaml")
    for site, registry in registries(catalog).items():
        data = registry.data
        robot = next(r for r, entry in catalog.robots.items() if entry.site_id == site)
        for asset_id, asset in data["assets"].items():
            package = {"nodes": [
                {"skill_id": "skill.flight.takeoff", "params": {"altitude_m_agl": 4}},
                {"skill_id": "skill.inspect.asset", "params": {"asset_id": asset_id,
                                                               "approach_route_id": asset["observation_route"]}},
                {"skill_id": "skill.flight.return_home", "params": {"home_ref": "home_v1", "return_route_id": "return"}},
                {"skill_id": "skill.flight.land", "params": {"landing_site_id": "home_pad"}}]}
            legacy = [tuple(map(float, data["home"]["position"][:2])),
                      *[tuple(map(float, p[:2])) for p in data["routes"][asset["observation_route"]]],
                      tuple(map(float, asset["position"][:2])),
                      *[tuple(map(float, p[:2])) for p in data["routes"]["return"]],
                      tuple(map(float, data["landing_sites"]["home_pad"]["position"][:2]))]
            assert set(package_points(data, package, robot)) == set(legacy), (site, asset_id)


def test_the_envelope_grows_with_the_radius_and_stays_inside_the_volume():
    scheduling = load_scheduling(SCHEDULING)
    data = registries(load_catalog(CATALOG))["site_a"].data
    fp = footprint(scheduling.airspace, (0.0, 0.0), task_points(data, "asset_mid"), data["volumes"]["campus_training"])
    small, large = envelope_cells(scheduling.airspace, fp, 1.0), envelope_cells(scheduling.airspace, fp, 500.0)
    assert set(fp.cells) <= set(small) <= set(large)
    bounds = data["volumes"]["campus_training"]["bounds"]
    assert all(bounds["x"][0] - 4 <= int(c.split(".")[2]) * 4 <= bounds["x"][1] for c in large)
    assert cell_resource("campus", 2, -1) in large
    assert cell_resource("campus", 5, 0) not in large


# ── the pure decision / 纯函数判定 ──


def snapshot(**changes) -> dict:
    catalog = load_catalog(CATALOG)
    now = utcnow()
    capability = yaml.safe_load((ROOT / "configs/platforms/px4_sitl_multirotor.yaml").read_text())["capability"]

    def candidate(robot: str, pad: list[float], asset: list[float], usage: int = 0) -> dict:
        dock = catalog.robots[robot].dock_id
        report = {"dock_id": dock, "boot_id": "boot-test-0001", "seq": 3, "observed_at": now.isoformat(),
                  "link": "online", "lid": "closed", "aircraft": "present",
                  "energy": {"state": "ready", "charge_fraction": 1.0},
                  "environment": {"state": "permitted", "wind_mps": 2.0}, "upkeep": "normal", "actions": []}
        return {"robot_id": robot, "site_id": catalog.robots[robot].site_id, "dock_id": dock,
                "volume_registered": True, "asset_registered": True, "pad": pad, "asset": asset,
                "capability": {**capability, "robot_id": robot}, "robot_status": None,
                "dock": {"dock_id": dock, "source": "logical_sim", "session": "active", "complete_reports": 3,
                         "report": report, "received_at": now.isoformat(), "lock": None},
                "holders": {}, "needs": {"skills": ["skill.flight.land", "skill.flight.return_home",
                                                    "skill.flight.takeoff", "skill.inspect.asset"],
                                         "energy_fraction": 0.98,
                                         "not_after": (now + timedelta(minutes=30)).isoformat()},
                "cells": ["air.campus.0.0"], "held": {}, "envelope": {}, "usage": usage}

    value = {"now": now.isoformat(),
             "task": {"task_id": "tk-test", "project_id": "campus_ops", "asset_id": "asset_mid",
                      "volume_id": "campus_training", "priority": 0,
                      "not_after": (now + timedelta(minutes=30)).isoformat(), "candidates": ["uav_a", "uav_b"],
                      "excluded": []},
             "ranking": {"version": "p3-rank-v1", "cruise_mps": 2.0, "setup_s": 20.0, "usage_window_s": 3600.0},
             "policy_version": "p3-sched-v1", "catalog_sha256": catalog.sha256, "scheduling_sha256": "0" * 64,
             "candidates": [candidate("uav_a", [0.0, 0.0], [5.0, 4.0]), candidate("uav_b", [12.0, 0.0], [5.0, 4.0])]}
    for key, change in changes.items():
        change(value)
    return value


def test_the_same_snapshot_always_gives_the_same_decision_and_the_nearest_eligible_wins():
    catalog = load_catalog(CATALOG)
    first, again = decide(catalog, snapshot()), decide(catalog, snapshot())
    assert first["verdict"] == "assign" and first["robot_id"] == "uav_a" and first["order"] == ["uav_a", "uav_b"]
    assert same_decision(first, again)
    frozen = snapshot()
    assert decide(catalog, frozen)["snapshot_sha256"] == decide(catalog, copy.deepcopy(frozen))["snapshot_sha256"]


def test_an_excluded_nearest_candidate_is_explained_and_the_next_one_assigned():
    catalog = load_catalog(CATALOG)

    def busy(value):
        value["candidates"][0]["holders"] = {"uav_a.motion": "mission:m-other:v1"}
        value["candidates"][0]["held"] = {"air.campus.0.0": "mission:m-other:v1"}

    decision = decide(catalog, snapshot(busy=busy))
    assert decision["robot_id"] == "uav_b"
    excluded = decision["candidates"][0]
    assert excluded["verdict"] == "blocked" and {"reservation.conflict", "airspace.cell_held"} <= set(excluded["reasons"])


def test_equal_arrival_is_broken_by_usage_then_robot_id():
    catalog = load_catalog(CATALOG)

    def equal(value):
        value["candidates"][1]["pad"] = [10.0, 0.0]

    def used(value):
        equal(value)
        value["candidates"][0]["usage"] = 3

    assert decide(catalog, snapshot(equal=equal))["robot_id"] == "uav_a"
    assert decide(catalog, snapshot(used=used))["robot_id"] == "uav_b"


def test_transient_blocks_wait_and_permanent_ones_reject_never_optimistically():
    catalog = load_catalog(CATALOG)

    def envelope(value):
        for cand in value["candidates"]:
            cand["envelope"] = {"air.campus.0.0": "mission:m-silent:v1"}

    def unregistered(value):
        for cand in value["candidates"]:
            cand["asset_registered"] = False

    def unknown(value):
        for cand in value["candidates"]:
            cand["dock"] = None

    assert decide(catalog, snapshot(envelope=envelope))["verdict"] == "wait"
    assert decide(catalog, snapshot(unknown=unknown))["verdict"] == "wait"
    rejected = decide(catalog, snapshot(unregistered=unregistered))
    assert rejected["verdict"] == "reject" and all(c["permanent"] == ["asset.unregistered"]
                                                   for c in rejected["candidates"])
    with pytest.raises(ValueError, match="another operations catalog"):
        decide(load_catalog(S1_CATALOG), snapshot())


# ── the D060 store / D060 存储 ──


def v2_ledger(path: Path) -> BusinessLedger:
    ledger = BusinessLedger(path)
    migrate_operations(ledger, backups=path.parent / "backups")
    return ledger


def test_migration_adds_the_tables_and_indexes_once_and_keeps_every_other_row(tmp_path):
    ledger = v2_ledger(tmp_path / "ledger.sqlite3")
    ledger._exec("INSERT INTO missions(mission_id, request_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                 ("m-000000000001", "req-1", "completed", utcnow().isoformat(), utcnow().isoformat()))
    ledger.close()
    before = other_lines(tmp_path / "ledger.sqlite3")
    report = drill(tmp_path / "ledger.sqlite3")
    assert report["status"] == "passed" and report["migration"] == "migrated"
    assert report["sc_tables"] == len(TABLES) and report["sc_indexes"] == len(INDEXES)
    assert report["backup"]["rows"]["missions"] == 1 and (tmp_path / "backups" / report["backup"]["path"]).is_file()
    assert other_lines(tmp_path / "ledger.sqlite3") == before
    again = drill(tmp_path / "ledger.sqlite3")
    assert again["migration"] == "current" and again["backup"] is None
    assert len(list((tmp_path / "backups").glob("ledger-v2-sc-*"))) == 1


def test_a_failed_migration_rolls_back_and_leaves_no_table(tmp_path):
    ledger = v2_ledger(tmp_path / "ledger.sqlite3")
    with pytest.raises(MigrationError, match="rolled back"):
        migrate(ledger, backups=tmp_path / "backups", statements=(*DDL[:3], "CREATE TABLE broken ("))
    assert scheduling_schema(ledger) is None
    names = {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("sc_") for name in names)
    with pytest.raises(MigrationError, match="operations schema v2"):
        migrate(BusinessLedger(":memory:"), backups=None)


def test_the_p2_and_p1_code_still_open_a_migrated_ledger(tmp_path):
    from drone_agent.fleet.operations_store import OperationsStore
    from drone_agent.fleet.workflow_models import load_workflows
    from drone_agent.fleet.workflow_store import WorkflowStore
    from drone_agent.fleet.workflow_store import migrate as migrate_workflows

    ledger = v2_ledger(tmp_path / "ledger.sqlite3")
    migrate_workflows(ledger, backups=tmp_path / "backups")
    migrate(ledger, backups=tmp_path / "backups")
    catalog = load_catalog(ROOT / "configs/sites/p1_campus_v1.yaml")
    assert migrate_operations(ledger, backups=None)["status"] == "current"
    OperationsStore(ledger, catalog)
    WorkflowStore(ledger, load_workflows(ROOT / "configs/workflows/p2_campus_v1.yaml"), {})


def store(tmp_path: Path) -> SchedulingStore:
    ledger = v2_ledger(tmp_path / "ledger.sqlite3")
    migrate(ledger, backups=tmp_path / "backups")
    return SchedulingStore(ledger, load_scheduling(SCHEDULING))


def test_a_task_is_created_once_per_key_and_changes_by_compare_and_set(tmp_path):
    tasks = store(tmp_path)
    fields = dict(requested_by="harness:p3-operator", key="k-1", project_id="campus_ops", asset_id="asset_mid",
                  volume_id="campus_training", candidates=[], priority=0,
                  not_after=(utcnow() + timedelta(minutes=5)).isoformat(), source="api")
    first, created = tasks.create_task(**fields)
    again, repeated = tasks.create_task(**fields)
    assert created and not repeated and again["task_id"] == first["task_id"]
    moved = tasks.update_task(first["task_id"], 1, actor="test", state="cancel_requested", cancel={"reason": "t"})
    with pytest.raises(StaleTask):
        tasks.update_task(first["task_id"], 1, actor="test", state="assigned")
    with pytest.raises(StaleTask, match="only the cancel"):
        tasks.update_task(first["task_id"], moved["state_version"], actor="test", state="assigned")
    done = tasks.update_task(first["task_id"], moved["state_version"], actor="test", state="cancelled")
    with pytest.raises(StaleTask, match="already"):
        tasks.update_task(first["task_id"], done["state_version"], actor="test", state="queued")


def test_the_unique_index_refuses_a_second_active_assignment(tmp_path):
    tasks = store(tmp_path)
    task, _ = tasks.create_task(requested_by="harness:x", key="k", project_id="campus_ops", asset_id="asset_mid",
                                volume_id="campus_training", candidates=[], priority=0,
                                not_after=utcnow().isoformat(), source="api")
    with tasks.transaction():
        tasks.insert_assignment(task_id=task["task_id"], epoch=1, robot_id="uav_a", mission_id="m-a",
                                decision_id="sd-1")
    with pytest.raises(sqlite3.IntegrityError):
        with tasks.transaction():
            tasks.insert_assignment(task_id=task["task_id"], epoch=2, robot_id="uav_b", mission_id="m-b",
                                    decision_id="sd-2")
    assert [a["epoch"] for a in tasks.assignments(task["task_id"])] == [1]
    assert tasks.end_assignment(task["task_id"], 1, state="withdrawn", reason="test")
    with tasks.transaction():
        tasks.insert_assignment(task_id=task["task_id"], epoch=2, robot_id="uav_b", mission_id="m-b",
                                decision_id="sd-2")
    assert tasks.live(task["task_id"])["epoch"] == 2
