"""P3 airspace reservations: the shared frame, grid cells, flight footprints and the lost-contact envelope (D059).

Every site map keeps its own local frame; the scheduling catalog places each map's origin in one shared frame. That
frame is cut into square cells, and a cell is an exclusive resource `air.<frame>.<i>.<j>` held through the same P1
hold table and unique index as a robot's motion or a dock's pad. A mission version's footprint is every cell that
meets the box around all the places its package may fly through, grown by a buffer. A robot that took its mission and
then went silent may be anywhere its speed allows since the last evidence of where it was, so its footprint grows with
the silence, bounded by the approved volume; new holds on those cells are refused until the P1 reconciliation releases
the old ones.

The places come from each node's skill (D075): the waypoints of the routes it flies, the landing site, home and asset
it names, every landing site reserved for the robot (a recovery may divert there), and for the external-mode skills
the whole external-mode task scope, because the local planner may move anywhere inside it to avoid obstacles. A skill
without a resolver here, or a name the map does not register, makes the footprint unresolvable: such a flight is
refused a reservation instead of being treated as using no airspace.

P3 空域预约：共享坐标、网格单元、航迹覆盖与失联包络（D059）。

每个站点地图保留各自的局部坐标；调度目录把每个地图的原点放进同一共享坐标系。该坐标系切成方形单元，单元是独占资源
`air.<frame>.<i>.<j>`，与机器人的运动资源、机场的机位经同一张 P1 持有表与唯一索引持有。任务版本的航迹覆盖是其任务包
可能经过的全部位置的外包矩形按缓冲外扩后相交的全部单元。领取任务后失联的机器人，可能位于自最后一次位置证据以来其速度
所及的任何地方，因此其航迹覆盖随失联时长增长，并以批准体积为界；在 P1 对账释放旧持有之前，这些单元上的新持有一律被拒绝。

位置来自每个节点的技能（D075）：所飞航线的航点、所引用的降落点、home 与资产，为该机器人预留的每个降落点（恢复可能改降到
那里），以及外部模式技能的整个外部模式任务范围——局部规划器为避障可以在其中任意移动。这里没有解析器的技能，或地图未登记的
名称，使航迹覆盖无法解析：这样的飞行被拒绝预约，而不是被当作不占用空域。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

Box = tuple[float, float, float, float]  # (x_min, y_min, x_max, y_max) in the shared frame / 共享坐标中的外包矩形
PREFIX = "air."


def bounds(points) -> Box:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def dilate(box: Box, distance: float) -> Box:
    return (box[0] - distance, box[1] - distance, box[2] + distance, box[3] + distance)


def clip(box: Box, limit: Box) -> Box | None:
    """The part of `box` inside `limit`, or None when they do not meet. / `box` 在 `limit` 内的部分，不相交时为 None。"""
    x0, y0, x1, y1 = max(box[0], limit[0]), max(box[1], limit[1]), min(box[2], limit[2]), min(box[3], limit[3])
    return (x0, y0, x1, y1) if x0 <= x1 and y0 <= y1 else None


def _span(low: float, high: float, size: float) -> range:
    first = math.floor(low / size)
    last = max(first, math.ceil(high / size) - 1)
    return range(first, last + 1)


def grid_cells(box: Box, size: float) -> list[tuple[int, int]]:
    """Cells `[i*size, (i+1)*size) x [j*size, (j+1)*size)` that meet the box. / 与矩形相交的单元。"""
    return [(i, j) for i in _span(box[0], box[2], size) for j in _span(box[1], box[3], size)]


def cell_resource(frame: str, i: int, j: int) -> str:
    return f"{PREFIX}{frame}.{i}.{j}"


def is_cell(resource: str) -> bool:
    return resource.startswith(PREFIX)


def cell_box(resource: str, size: float) -> Box:
    """The shared-frame square of one cell resource. / 单元资源对应的共享坐标方格。"""
    _, _, i, j = resource.rsplit(".", 3)
    x, y = int(i) * size, int(j) * size
    return (x, y, x + size, y + size)


def _xy(point) -> tuple[float, float]:
    return float(point[0]), float(point[1])


class UnresolvedFootprint(ValueError):
    """A node whose motion range the service cannot bound; its flight is refused a reservation (D075).

    服务无法界定其运动范围的节点；其飞行被拒绝预约（D075）。
    """


def local_scope_points(data: dict) -> list[tuple[float, float]]:
    """Corners of the external-mode task scope in the plane: the registered bounds that `Registry.task_scope` clips to
    the flight band. The local planner may move anywhere inside it (M3, D039), so all of it is reserved.

    外部模式任务范围在平面上的两个对角：即 `Registry.task_scope` 按飞行高度带裁剪的登记边界。局部规划器可以在其中任意
    移动（M3，D039），因此整个范围都被预约。
    """
    bounds = data["bounds"]
    return [(float(bounds["x"][0]), float(bounds["y"][0])), (float(bounds["x"][1]), float(bounds["y"][1]))]


def _named(table: dict, name, kind: str, key: str = "position") -> tuple[float, float]:
    entry = table.get(name) if isinstance(name, str) else None
    if entry is None:
        raise UnresolvedFootprint(f"{kind} {name!r} is not registered")
    return _xy(entry[key] if key else entry)


def _route(data: dict, route_id) -> list[tuple[float, float]]:
    route = data.get("routes", {}).get(route_id) if isinstance(route_id, str) else None
    if not route:
        raise UnresolvedFootprint(f"route {route_id!r} is not registered")
    return [_xy(p) for p in route]


def _asset(data: dict, params: dict) -> tuple[float, float]:
    return _named(data.get("assets", {}), params.get("asset_id"), "asset")


def _goal(data: dict, goal_id) -> tuple[float, float]:
    return _named(data.get("local_goals", {}), goal_id, "local goal", key="")


# The places each registered skill may fly through, from its own parameters (D075). A contract test pins this table to
# the skill manifests, so a new motion skill cannot be added without saying where it may go.
# 每个已登记技能按自身参数可能经过的位置（D075）。契约测试把本表钉在技能清单上，新增运动技能必须说明它可能去哪里。
SKILL_PLACES = {
    "skill.flight.takeoff": lambda data, p: [],  # vertical above the pad; home and the pad are always included
    "skill.flight.fly_route": lambda data, p: _route(data, p.get("route_id")),
    "skill.flight.return_home": lambda data, p: _route(data, p.get("return_route_id")),
    "skill.flight.land": lambda data, p: [_named(data.get("landing_sites", {}), p.get("landing_site_id"),
                                                 "landing site")],
    "skill.flight.capture_image": lambda data, p: [_asset(data, p)],
    "skill.inspect.asset": lambda data, p: [*_route(data, p.get("approach_route_id")), _asset(data, p)],
    "skill.flight.goto_local": lambda data, p: [_goal(data, p.get("goal_id")), *local_scope_points(data)],
    "skill.inspect.asset_local": lambda data, p: [_goal(data, p.get("approach_goal_id")), _asset(data, p),
                                                  *local_scope_points(data)],
}


def recovery_points(data: dict, robot_id: str | None) -> list[tuple[float, float]]:
    """Home and every landing site reserved for the robot: a recovery may return or divert to any of them.

    home 与为该机器人预留的每个降落点：恢复可能返航或改降到其中任何一处。
    """
    sites = data.get("landing_sites", {})
    return [_xy(data["home"]["position"])] + [_xy(site["position"]) for _, site in sorted(sites.items())
                                              if robot_id is not None and site.get("reserved_for") == robot_id]


def package_points(data: dict, package: dict, robot_id: str | None = None) -> list[tuple[float, float]]:
    """Every map place a mission version may fly through; raises UnresolvedFootprint when a node cannot be bounded.

    任务版本可能经过的全部地图位置；任一节点无法界定时抛出 UnresolvedFootprint。
    """
    points = recovery_points(data, robot_id)
    for node in package.get("nodes", []):
        places = SKILL_PLACES.get(node.get("skill_id"))
        if places is None:
            raise UnresolvedFootprint(f"skill {node.get('skill_id')!r} has no airspace footprint")
        points += places(data, node.get("params") or {})
    return points


def task_points(data: dict, asset_id: str, robot_id: str | None = None) -> list[tuple[float, float]] | None:
    """The same places for a planned single-asset inspection, before any package exists; None if unregistered. An
    asset with a local observation goal may be compiled to the external-mode inspection, so its scope is included.

    尚无任务包时，计划中的单资产巡检所经过的同样位置；资产未登记时为 None。带局部观测目标的资产可能被编译为外部模式
    巡检，因此包含其任务范围。
    """
    asset = data.get("assets", {}).get(asset_id)
    if asset is None:
        return None
    defaults = data.get("mission_defaults", {})
    routes = data.get("routes", {})
    points = [*recovery_points(data, robot_id), _xy(asset["position"])]
    for route in (asset.get("observation_route"), asset.get("return_route") or defaults.get("return_route_id")):
        points += [_xy(p) for p in routes.get(route) or []]
    site = data.get("landing_sites", {}).get(defaults.get("landing_site_id"))
    if site is not None:
        points.append(_xy(site["position"]))
    if asset.get("observation_goal") is not None:
        # A registered goal lies inside the scope (`Registry.local_goal`), so the scope bounds it.
        # 登记的目标位于范围之内（`Registry.local_goal`），因此范围已涵盖它。
        points += local_scope_points(data)
    return points


@dataclass(frozen=True)
class Footprint:
    """The cells one flight may use, with the boxes they came from. / 一次飞行可能使用的单元及其来源矩形。"""

    frame: str
    cells: tuple[str, ...]
    box: Box
    volume_box: Box | None

    def body(self) -> dict:
        return {"frame": self.frame, "cells": list(self.cells), "box": list(self.box),
                "volume_box": list(self.volume_box) if self.volume_box else None}


def footprint(grid, origin: tuple[float, float], points, volume: dict | None) -> Footprint:
    """Translate local points into the shared frame, bound, grow by the buffer and cut into cells.

    把局部点平移到共享坐标，求外包矩形、按缓冲外扩并切成单元。
    """
    ox, oy = origin
    shared = [(ox + x, oy + y) for x, y in points]
    box = dilate(bounds(shared), grid.buffer_m)
    volume_box = None
    if volume is not None:
        xs, ys = volume["bounds"]["x"], volume["bounds"]["y"]
        volume_box = (ox + float(xs[0]), oy + float(ys[0]), ox + float(xs[1]), oy + float(ys[1]))
    cells = tuple(sorted(cell_resource(grid.frame, i, j) for i, j in grid_cells(box, grid.cell_m)))
    return Footprint(grid.frame, cells, box, volume_box)


def envelope_cells(grid, fp: Footprint, radius_m: float) -> tuple[str, ...]:
    """The footprint grown by `radius_m`, bounded by the approved volume; always contains the footprint itself.

    按 `radius_m` 外扩并以批准体积为界的航迹覆盖；总是包含航迹覆盖本身。
    """
    grown = dilate(fp.box, radius_m)
    if fp.volume_box is not None:
        grown = clip(grown, fp.volume_box) or fp.box
    extra = {cell_resource(grid.frame, i, j) for i, j in grid_cells(grown, grid.cell_m)}
    return tuple(sorted(extra | set(fp.cells)))


def shared_distance(origin_a, point_a, origin_b, point_b) -> float:
    return math.dist((origin_a[0] + point_a[0], origin_a[1] + point_a[1]),
                     (origin_b[0] + point_b[0], origin_b[1] + point_b[1]))


class Airspace:
    """Service-side airspace state: footprints, current cell holders and lost-contact envelopes.

    `store` is the P1 operations store, `robots` the robot catalog (statuses), `scheduling` the P3 store holding the
    recorded footprints. Everything is read in the caller's transaction, so a check and the hold it guards commit
    together.

    服务侧空域状态：航迹覆盖、单元当前持有者与失联包络。`store` 是 P1 运营存储，`robots` 是机器人目录（状态），
    `scheduling` 是记录航迹覆盖的 P3 存储。全部在调用方的事务内读取，因此检查与其保护的持有一起提交。
    """

    def __init__(self, catalog, operations, robots, scheduling, *, clock):
        self.catalog, self.ops, self.robots, self.scheduling, self.clock = catalog, operations, robots, scheduling, clock
        self.grid = catalog.airspace

    # ── footprints / 航迹覆盖 ──

    def origin(self, robot_id: str) -> tuple[float, float]:
        return self.catalog.origin(self.ops.catalog.robots[robot_id].site_id)

    def _data(self, robot_id: str) -> dict:
        return self.ops.registry(robot_id).data

    def mission_footprint(self, robot_id: str, package: dict) -> Footprint:
        """The package's footprint; raises UnresolvedFootprint when any node cannot be bounded (D075).

        任务包的航迹覆盖；任一节点无法界定时抛出 UnresolvedFootprint（D075）。
        """
        data = self._data(robot_id)
        volume_id = (package.get("spatial_scope") or {}).get("approved_volume_id")
        return footprint(self.grid, self.origin(robot_id), package_points(data, package, robot_id),
                         data.get("volumes", {}).get(volume_id))

    def task_footprint(self, robot_id: str, asset_id: str, volume_id: str) -> Footprint | None:
        data = self._data(robot_id)
        points = task_points(data, asset_id, robot_id)
        if points is None:
            return None
        return footprint(self.grid, self.origin(robot_id), points, data.get("volumes", {}).get(volume_id))

    def footprint_of(self, activity: str) -> Footprint | None:
        row = self.scheduling.footprint(activity)
        if row is None:
            return None
        volume = row["volume_box"]
        return Footprint(row["frame"], tuple(row["cells"]), tuple(row["box"]), tuple(volume) if volume else None)

    # ── holders and envelopes / 持有者与包络 ──

    def held(self, cells, *, exclude: set[str] | None = None) -> dict[str, str]:
        """Cell -> activity actively holding it, other than `exclude`. / 单元 -> 当前有效持有它的活动（排除 `exclude`）。"""
        exclude = exclude or set()
        return {cell: holder for cell, holder in self.ops.store.holders(cells).items() if holder not in exclude}

    def evidence(self, reservation) -> tuple[datetime | None, bool]:
        """(time of the last evidence of where the robot is, whether it is known back on its pad).

        （最后一次能说明机器人位置的证据时间，是否已知回到机位）。
        """
        store = self.ops.store
        claim = next((c for c in store.claims(reservation.mission_id)
                      if c["mission_version"] == reservation.mission_version and c["state"] == "claimed"), None)
        if claim is None:
            return None, True
        claimed_at = datetime.fromisoformat(claim["decided_at"])
        departed = [datetime.fromisoformat(e["created_at"]) for e in store.events(reservation.activity_key)
                    if e["kind"] == "activity.departed"]
        status = self.robots.status(reservation.robot_id)
        dock = store.dock_status(reservation.dock_id)
        latest = claimed_at
        if status is not None and status.timestamp >= claimed_at:
            latest = max(latest, status.timestamp)
        on_pad = dock is not None and dock.report.aircraft.value == "present"
        if not departed and on_pad:
            latest = max(latest, dock.report.observed_at)
        landed = bool(departed) and status is not None and status.flight_phase == "grounded" \
            and status.timestamp > departed[0] and on_pad
        return latest, landed

    def envelopes(self, *, exclude: set[str] | None = None) -> dict[str, str]:
        """Cell -> activity whose silent robot may be there; recomputed at each call because it grows with time.

        单元 -> 其失联机器人可能在那里的活动；每次调用重算，因为它随时间增长。
        """
        exclude, now = exclude or set(), self.clock()
        found: dict[str, str] = {}
        for reservation in self.ops.store.reservations(states=("occupied", "uncertain")):
            if reservation.activity_key in exclude:
                continue
            fp = self.footprint_of(reservation.activity_key)
            if fp is None:
                continue
            latest, landed = self.evidence(reservation)
            if latest is None or landed:
                continue
            silence = (now - latest).total_seconds()
            if silence <= self.grid.contact_timeout_s:
                continue
            speed = self.ops.capabilities[reservation.robot_id].limits.max_speed_mps
            for cell in envelope_cells(self.grid, fp, speed * silence + self.grid.envelope_margin_m):
                found.setdefault(cell, reservation.activity_key)
        return found


def footprint_json(fp: Footprint) -> str:
    return json.dumps(fp.body(), sort_keys=True)
