"""P3 airspace reservations: the shared frame, grid cells, flight footprints and the lost-contact envelope (D059).

Every site map keeps its own local frame; the scheduling catalog places each map's origin in one shared frame. That
frame is cut into square cells, and a cell is an exclusive resource `air.<frame>.<i>.<j>` held through the same P1
hold table and unique index as a robot's motion or a dock's pad. A mission version's footprint is every cell that
meets the box around all the places its package may fly through (home, landing site, asset and every waypoint of the
routes its nodes name), grown by a buffer. A robot that took its mission and then went silent may be anywhere its
speed allows since the last evidence of where it was, so its footprint grows with the silence, bounded by the approved
volume; new holds on those cells are refused until the P1 reconciliation releases the old ones.

P3 空域预约：共享坐标、网格单元、航迹覆盖与失联包络（D059）。

每个站点地图保留各自的局部坐标；调度目录把每个地图的原点放进同一共享坐标系。该坐标系切成方形单元，单元是独占资源
`air.<frame>.<i>.<j>`，与机器人的运动资源、机场的机位经同一张 P1 持有表与唯一索引持有。任务版本的航迹覆盖是其任务包
可能经过的全部位置（home、降落点、资产及各节点引用航线的全部航点）的外包矩形按缓冲外扩后相交的全部单元。领取任务后失联
的机器人，可能位于自最后一次位置证据以来其速度所及的任何地方，因此其航迹覆盖随失联时长增长，并以批准体积为界；在 P1 对账
释放旧持有之前，这些单元上的新持有一律被拒绝。
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


def package_points(data: dict, package: dict) -> list[tuple[float, float]]:
    """Every map place a mission version may fly through: home, landing sites, assets and route waypoints its nodes name.

    任务版本可能经过的全部地图位置：home、降落点、资产及其节点引用航线的航点。
    """
    points = [_xy(data["home"]["position"])]
    routes, sites, assets = data.get("routes", {}), data.get("landing_sites", {}), data.get("assets", {})
    for node in package.get("nodes", []):
        params = node.get("params") or {}
        for key in ("route_id", "approach_route_id", "return_route_id"):
            points += [_xy(p) for p in routes.get(params.get(key)) or []]
        if params.get("landing_site_id") in sites:
            points.append(_xy(sites[params["landing_site_id"]]["position"]))
        if params.get("asset_id") in assets:
            points.append(_xy(assets[params["asset_id"]]["position"]))
    return points


def task_points(data: dict, asset_id: str) -> list[tuple[float, float]] | None:
    """The same places for a planned single-asset inspection, before any package exists; None if unregistered.

    尚无任务包时，计划中的单资产巡检所经过的同样位置；资产未登记时为 None。
    """
    asset = data.get("assets", {}).get(asset_id)
    if asset is None:
        return None
    defaults = data.get("mission_defaults", {})
    routes = data.get("routes", {})
    points = [_xy(data["home"]["position"]), _xy(asset["position"])]
    for route in (asset.get("observation_route"), asset.get("return_route") or defaults.get("return_route_id")):
        points += [_xy(p) for p in routes.get(route) or []]
    site = data.get("landing_sites", {}).get(defaults.get("landing_site_id"))
    if site is not None:
        points.append(_xy(site["position"]))
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
        data = self._data(robot_id)
        volume_id = (package.get("spatial_scope") or {}).get("approved_volume_id")
        return footprint(self.grid, self.origin(robot_id), package_points(data, package),
                         data.get("volumes", {}).get(volume_id))

    def task_footprint(self, robot_id: str, asset_id: str, volume_id: str) -> Footprint | None:
        data = self._data(robot_id)
        points = task_points(data, asset_id)
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
