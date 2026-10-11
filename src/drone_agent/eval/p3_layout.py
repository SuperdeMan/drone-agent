"""P3 site layouts: the pair of adjacent sites every P3 world is built from, and the generated scale ladders (D059).

A pair is two sites 12 m apart on the shared frame, each with one robot on one logical dock. The left site's map is
the shared frame shifted by the pair origin; the right site's map is shifted 12 m further east. Four markers belong to
a pair: `west` (red, left only), `mid` (green, both), `north` (red, both) and `east` (blue, right only); an asset both
sites register lies at one shared place, which the scheduling catalog checks. Routes keep the M2 shape (4 m north,
then straight to the asset at 4 m height) and every map keeps the M2 thresholds, supervision, energy budget and
policy, so the flights stay prevalidated. The committed S0 and S1 maps are pinned to this generator by tests; the
10 / 30 / 100 node ladders are generated into `outputs/` with their digests recorded.

P3 站点布局：每个 P3 世界都由相邻站点对构成，以及生成的规模阶梯（D059）。

一对是共享坐标中相距 12 m 的两个站点，各有一台机器人与一个逻辑机场。左站地图是共享坐标按站点对原点平移；右站再向东
平移 12 m。每对有四个标记：`west`（红，只左站）、`mid`（绿，两站）、`north`（红，两站）与 `east`（蓝，只右站）；两站都
登记的资产在共享坐标中位于同一处，由调度目录核对。航线保持 M2 形状（先向北 4 m，再在 4 m 高度直飞资产），每个地图保留
M2 的阈值、监督、能源预算与策略，因此飞行仍是预验证的。仓库中的 S0 与 S1 地图由测试钉在本生成器上；10 / 30 / 100 节点
阶梯生成到 `outputs/` 并记录摘要。

The long-route layout (D084) keeps the pair idea at inspection distances: two docks 60 m apart, and six assets 40 to
120 m out that both sites register, each reached by the M2 route shape. Pairs sit 400 m apart, so only the two robots
of a pair contend for airspace. A generated ladder may declare per-leg corridors through a v2 scheduling catalog; the
pair layout with the box is generated exactly as before.

长航线布局（D084）在巡检距离上沿用站点对：两个机场相距 60 m，两站都登记的六个资产在 40–120 m 外，各自按 M2 航线形状
到达。站点对相距 400 m，因此只有同一对的两台机器人争用空域。生成的阶梯可以经 v2 调度目录声明航段走廊；外包矩形下的
站点对布局与以前逐字节相同。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml

from drone_agent.mission.registry import M2_SCENE

PAIR_SPACING_M = 12.0
# Shared-frame offsets of the markers from the pair origin: (x, y, colour, sides). / 标记相对站点对原点的偏移。
MARKERS = {"west": (-4.0, 4.0, "red", ("left",)), "mid": (5.0, 4.0, "green", ("left", "right")),
           "north": (6.0, 10.0, "red", ("left", "right")), "east": (16.0, 4.0, "blue", ("right",))}
BOUNDS = {"left": {"x": [-10, 12], "y": [-6, 16], "z": [-0.5, 30]},
          "right": {"x": [-12, 10], "y": [-6, 16], "z": [-0.5, 30]}}
DESCRIPTIONS = {"red": "Red equipment marker / 红色设备标记", "green": "Green equipment marker / 绿色设备标记",
                "blue": "Blue equipment marker / 蓝色设备标记"}
# The long-route layout (D084): docks 60 m apart, six shared assets as shared-frame offsets from the pair origin,
# pairs 400 m apart. / 长航线布局（D084）：机场相距 60 m，六个共享资产（相对站点对原点的共享坐标），站点对相距 400 m。
LONG_SPACING_M = 60.0
LONG_PAIR_PITCH_M = 400.0
LONG_MARGIN_M = 10.0
LONG_ASSETS = {"n1": (20.0, 80.0), "n2": (-30.0, 110.0), "e1": (90.0, 40.0), "s1": (40.0, -70.0),
               "w1": (-60.0, -20.0), "c1": (30.0, 30.0)}


def base_map(root: Path) -> dict:
    return yaml.safe_load((root / M2_SCENE).read_text(encoding="utf-8"))


def site_map(root: Path, *, registry_id: str, robot_id: str, side: str, assets: dict[str, str],
             markers: tuple[str, ...], camera_topic: str | None = None) -> dict:
    """One site map of a pair in its own local frame; `assets` maps marker names to asset ids.

    站点对中一个站点在其局部坐标中的地图；`assets` 把标记名映射到资产 ID。
    """
    data = copy.deepcopy(base_map(root))
    shift = 0.0 if side == "left" else PAIR_SPACING_M
    data["registry_id"] = registry_id
    data["frame"] = {"frame_id": "map_enu", "map_version": registry_id}
    data["bounds"] = copy.deepcopy(BOUNDS[side])
    data["volumes"]["campus_training"]["bounds"] = copy.deepcopy(BOUNDS[side])
    routes = {"inspection": [[0, 4, 4], [4, 4, 4]], "return": [[0, 4, 4], [0, 0, 4]]}
    registered = {}
    for marker in markers:
        x, y, colour, sides = MARKERS[marker]
        if side not in sides:
            raise ValueError(f"{marker} is not visible from the {side} site")
        asset_id = assets[marker]
        local = [x - shift, y, 0]
        routes[f"observe_{asset_id}"] = [[0, 4, 4], [local[0], local[1], 4]]
        registered[asset_id] = {"position": local, "camera_id": "cam_0", "visual_signature": colour, "size_m": 2,
                                "volume": "campus_training", "observation_route": f"observe_{asset_id}",
                                "kind": "equipment_marker", "description": DESCRIPTIONS[colour]}
    data["routes"] = routes
    data["assets"] = registered
    data["landing_sites"] = {"home_pad": {"position": [0, 0, 0], "radius_m": 2, "reserved_for": robot_id}}
    if camera_topic is not None:
        data["simulation"]["camera_topic"] = camera_topic
    return data


def long_site_map(root: Path, *, registry_id: str, robot_id: str, side: str, assets: dict[str, str]) -> dict:
    """One site map of a long-route pair (D084) in its own local frame; both sites register all six assets.

    长航线站点对（D084）中一个站点在其局部坐标中的地图；两站都登记全部六个资产。
    """
    data = copy.deepcopy(base_map(root))
    shift = 0.0 if side == "left" else LONG_SPACING_M
    places = {name: (x - shift, y) for name, (x, y) in LONG_ASSETS.items()}
    xs, ys = [0.0, *(x for x, _ in places.values())], [0.0, 4.0, *(y for _, y in places.values())]
    bounds = {"x": [min(xs) - LONG_MARGIN_M, max(xs) + LONG_MARGIN_M],
              "y": [min(ys) - LONG_MARGIN_M, max(ys) + LONG_MARGIN_M], "z": [-0.5, 30]}
    data["registry_id"] = registry_id
    data["frame"] = {"frame_id": "map_enu", "map_version": registry_id}
    data["bounds"] = copy.deepcopy(bounds)
    data["volumes"]["campus_training"]["bounds"] = copy.deepcopy(bounds)
    routes = {"inspection": [[0, 4, 4], [4, 4, 4]], "return": [[0, 4, 4], [0, 0, 4]]}
    registered = {}
    for name, (x, y) in places.items():
        asset_id = assets[name]
        routes[f"observe_{asset_id}"] = [[0, 4, 4], [x, y, 4]]
        registered[asset_id] = {"position": [x, y, 0], "camera_id": "cam_0", "visual_signature": "green", "size_m": 2,
                                "volume": "campus_training", "observation_route": f"observe_{asset_id}",
                                "kind": "equipment_marker", "description": DESCRIPTIONS["green"]}
    data["routes"] = routes
    data["assets"] = registered
    data["landing_sites"] = {"home_pad": {"position": [0, 0, 0], "radius_m": 2, "reserved_for": robot_id}}
    return data


def dump_map(data: dict, header: str) -> str:
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120, default_flow_style=None)


def pair_origin(index: int, columns: int = 10, spacing: float = 60.0) -> tuple[float, float]:
    return (index % columns) * spacing, (index // columns) * spacing


def ladder(root: Path, nodes: int, directory: Path, *, layout: str = "pair", footprint: str = "box-v1") -> dict:
    """Write a catalog of `nodes` logical nodes (nodes/2 pairs) with maps, members and a scheduling catalog.

    `layout` is `pair` (the P3 site pair) or `long` (the D084 long routes); a footprint other than the box writes a v2
    scheduling catalog declaring it. Returns the file paths (repository-relative for the maps, as the catalog needs)
    and their digests.

    写出含 `nodes` 个逻辑节点（nodes/2 对）的目录、地图、成员与调度目录。`layout` 为 `pair`（P3 站点对）或 `long`
    （D084 长航线）；外包矩形以外的航迹覆盖写出声明它的 v2 调度目录。返回文件路径（地图为目录需要的仓库相对路径）与摘要。
    """
    if nodes < 2 or nodes % 2:
        raise ValueError("a ladder has an even number of nodes")
    if layout not in ("pair", "long"):
        raise ValueError(f"unknown layout {layout!r}")
    directory.mkdir(parents=True, exist_ok=True)
    relative = directory.resolve().relative_to(root.resolve())
    prefix = "p3_ladder" if layout == "pair" else "p6_long"
    sites, docks, robots, origins = {}, {}, {}, {}
    for pair in range(nodes // 2):
        if layout == "pair":
            origin, spacing = pair_origin(pair), PAIR_SPACING_M
            sides = (("left", ("west", "mid", "north")), ("right", ("mid", "north", "east")))
        else:
            origin, spacing = pair_origin(pair, spacing=LONG_PAIR_PITCH_M), LONG_SPACING_M
            sides = (("left", tuple(LONG_ASSETS)), ("right", tuple(LONG_ASSETS)))
        for side, marks in sides:
            name = f"p{pair:02d}{side[0]}"
            robot, dock, site = f"uav_{name}", f"dock_{name}", f"site_{name}"
            if layout == "pair":
                assets = {m: f"asset_{m}_p{pair:02d}" for m in MARKERS}
                data = site_map(root, registry_id=f"p3_ladder_{name}", robot_id=robot, side=side, assets=assets,
                                markers=marks)
                header = "# Generated P3 ladder site map (eval/p3_layout.py). / 生成的 P3 阶梯站点地图。\n"
            else:
                assets = {m: f"asset_{m}_p{pair:02d}" for m in LONG_ASSETS}
                data = long_site_map(root, registry_id=f"p6_long_{name}", robot_id=robot, side=side, assets=assets)
                header = "# Generated D084 long-route site map (eval/p3_layout.py). / 生成的 D084 长航线站点地图。\n"
            path = directory / f"{site}.yaml"
            path.write_text(dump_map(data, header), encoding="utf-8", newline="\n")
            sites[site] = {"project_id": "fleet_ops", "scene": (relative / path.name).as_posix(), "max_wind_mps": 8.0}
            docks[dock] = {"site_id": site, "vendor": "drone-agent", "model": "logical-dock-v1", "serves": [robot],
                           "actions": ["open_lid", "close_lid", "start_charge"],
                           "backend": {"kind": "logical_sim", "principal": "dock:p3-ladder"}}
            robots[robot] = {"site_id": site, "dock_id": dock, "platform": "configs/platforms/px4_sitl_multirotor.yaml",
                             "execution_backend": "logical_sim"}
            origins[site] = {"origin": [origin[0] + (0.0 if side == "left" else spacing), origin[1]]}
    name = "P3 ladder" if layout == "pair" else "D084 long-route ladder"
    local = "P3 阶梯" if layout == "pair" else "D084 长航线阶梯"
    catalog = {"format": "drone.operations-catalog/v1", "schema_version": "0.1.0", "catalog_id": f"{prefix}_{nodes}",
               "legacy_project": {"project_id": "legacy_m2", "first_party_roles": ["viewer"]}, "default_binding": None,
               "projects": {"fleet_ops": {"name": f"{name}, {nodes} logical nodes / {local}（{nodes} 个逻辑节点）",
                                          "sites": sorted(sites)}},
               "sites": sites, "docks": docks, "robots": robots,
               "policy": {"version": "p1-dispatch-v1", "freshness_s": 3.0, "future_skew_s": 1.0, "reconcile_reports": 2,
                          "soft_reservation_s": 900, "claim_ack_timeout_s": 30, "result_grace_s": 5}}
    airspace = {"frame": "ladder", "cell_m": 4.0, "buffer_m": 2.0, "contact_timeout_s": 5.0, "envelope_margin_m": 2.0,
                "sites": origins}
    if footprint != "box-v1":
        airspace = {**{k: v for k, v in airspace.items() if k != "sites"}, "footprint": footprint, "sites": origins}
    scheduling = {"format": "drone.scheduling-catalog/v1" if footprint == "box-v1" else "drone.scheduling-catalog/v2",
                  "schema_version": "0.1.0",
                  "catalog_id": f"{prefix}_{nodes}" if footprint == "box-v1" else f"{prefix}_{nodes}_corridor",
                  "operations_catalog": f"{prefix}_{nodes}", "airspace": airspace,
                  "ranking": {"version": "p3-rank-v1", "cruise_mps": 2.0, "setup_s": 20.0, "usage_window_s": 3600.0},
                  "policy": {"version": "p3-sched-v1", "reassign_after_s": 5.0, "max_assignments": 4,
                             "task_window_s": 1800.0, "max_tasks_per_pass": 200}}
    members = {"format": "drone.project-members/v1", "schema_version": "0.1.0", "members": [
        {"principal": "harness:p3-operator", "project_id": "fleet_ops", "roles": ["operator", "approver"]},
        {"principal": "harness:p3-admin", "project_id": "fleet_ops", "roles": ["admin"]},
        {"principal": "harness:p3-judge", "project_id": "fleet_ops", "roles": ["viewer"]}]}
    files = {"catalog": directory / "catalog.yaml", "scheduling": directory / "scheduling.yaml",
             "members": directory / "members.yaml"}
    for key, value in (("catalog", catalog), ("scheduling", scheduling), ("members", members)):
        files[key].write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8",
                              newline="\n")
    digest = hashlib.sha256(json.dumps({"catalog": catalog, "scheduling": scheduling,
                                        "maps": sorted(p.read_text(encoding="utf-8") for p in
                                                       directory.glob("site_*.yaml"))},
                                       sort_keys=True).encode()).hexdigest()
    return {"nodes": nodes, "pairs": nodes // 2, "files": {k: str(v) for k, v in files.items()}, "sha256": digest}
