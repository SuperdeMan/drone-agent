"""X1 onboard reachability (D088): can this ground robot reach a viewing pose for the offered target on its own map?

A ground robot answers a handoff offer from its own map, never from the aerial view. The map is the drivable area
minus obstacles and no-go zones on a fixed grid; a cell is free only when the robot's whole footprint (its radius
around the cell centre) stays inside the drivable area and clear of every obstacle and no-go zone. From its current
cell the robot searches the shortest 8-connected path, never cutting a blocked corner, to any free cell at a viewing
distance from a target point, or, for a search region, to any free cell within the region grown by that distance.
A start or target off the map, no reachable viewing cell, or a path longer than the map allows each refuse with a
controlled code. Accepting only says the robot is willing: the check never replaces admission, signature and
registry checks, or the guardian's fences while it drives.

X1 机载可达性（D088）：这台地面车能否在自己的地图上到达所提供目标的观察位？

地面车依据自己的地图回答交接提议，从不依据空中视角。地图是固定网格上的可行驶区域减去障碍与禁入区；只有当车体的整个
覆盖范围（以单元中心为圆心、车体半径为半径）都留在可行驶区域内、且不碰任何障碍与禁入区时，单元才算可通行。地面车从当前
单元出发，搜索最短的八连通路径（不切过被占的拐角），终点是与目标点相距在观察距离内的任一可通行单元；目标是搜索区域时，
终点是区域按该距离外扩后范围内的任一可通行单元。起点或目标不在地图上、没有可达的观察单元、路径超过地图允许的长度，各以
受控原因码拒绝。接受只表示愿意接收：这一检查从不替代准入、验签与登记表复核，也不替代行驶期间 guardian 的围栏。
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ConfigDict, Field, field_validator

from drone_agent.contracts.common import ContractModel, Frame

Point = tuple[float, float]
Cell = tuple[int, int]
NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
OUTSIDE_MAP = "outside_map"
UNREACHABLE = "unreachable"


class GroundModel(ContractModel):
    """Ground map record: unknown fields rejected, instances frozen. / 地面地图记录：拒绝未知字段、实例冻结。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ParkingSpot(GroundModel):
    position: tuple[float, float]
    heading_deg: float = Field(ge=-180, le=360)


class GroundSection(GroundModel):
    """The robot's own drivable map. / 地面车自己的可行驶地图。"""

    cell_m: float = Field(gt=0.05, le=2.0, description="grid cell side / 网格单元边长")
    robot_radius_m: float = Field(gt=0, le=5, description="clearance kept around the robot centre / 车体中心周围保留的净空")
    drivable: list[list[tuple[float, float]]] = Field(min_length=1, description="drivable polygons (union) / 可行驶多边形（并集）")
    obstacles: list[list[tuple[float, float]]] = Field(default_factory=list, description="blocked polygons / 障碍多边形")
    no_go: list[list[tuple[float, float]]] = Field(default_factory=list, description="never entered even if drivable / 即使可行驶也不得进入")
    parking: dict[str, ParkingSpot] = Field(min_length=1)
    standoff_m: tuple[float, float] = Field(description="viewing distance band to a target / 到目标的观察距离区间")
    max_path_m: float = Field(gt=0, le=10_000)
    cruise_mps: float = Field(gt=0, le=10)

    @field_validator("drivable", "obstacles", "no_go")
    @classmethod
    def _polygons(cls, polygons: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
        if any(len(polygon) < 3 for polygon in polygons):
            raise ValueError("a polygon needs at least three corners")
        return polygons

    @field_validator("standoff_m")
    @classmethod
    def _band(cls, band: tuple[float, float]) -> tuple[float, float]:
        if not 0 <= band[0] < band[1]:
            raise ValueError("standoff_m must be [min, max] with 0 <= min < max")
        return band


class GroundMap(GroundModel):
    """A ground robot's map, versioned like the site maps. / 地面车的地图，与站点地图一样带版本。"""

    schema_version: Literal["0.1.0"] = "0.1.0"
    registry_id: str = Field(min_length=1)
    frame: Frame
    ground: GroundSection


def load_ground_map(path: Path) -> GroundMap:
    return GroundMap.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Reachability:
    """The robot's answer to an offer, with the evidence behind it. / 地面车对提议的回答及其依据。"""

    accepted: bool
    reason: str | None
    map_version: str
    path_m: float | None = None
    eta_s: float | None = None
    goal: Point | None = None
    waypoints: tuple[Point, ...] = ()
    detail: str = ""


def _inside(point: Point, polygon: Sequence[Point]) -> bool:
    # Even-odd ray casting; a point on an edge counts as whichever side the rounding gives. / 奇偶射线法。
    x, y, inside = point[0], point[1], False
    for (x1, y1), (x2, y2) in zip(polygon, [*polygon[1:], polygon[0]], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _edge_distance(point: Point, polygon: Sequence[Point]) -> float:
    best = math.inf
    for (x1, y1), (x2, y2) in zip(polygon, [*polygon[1:], polygon[0]], strict=True):
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((point[0] - x1) * dx + (point[1] - y1) * dy) / length2))
        best = min(best, math.hypot(point[0] - x1 - t * dx, point[1] - y1 - t * dy))
    return best


class Grid:
    """The free cells of one ground map, computed once. / 一张地面地图的可通行单元，只计算一次。"""

    def __init__(self, ground_map: GroundMap) -> None:
        self.map = ground_map
        section = ground_map.ground
        xs = [x for polygon in section.drivable for x, _ in polygon]
        ys = [y for polygon in section.drivable for _, y in polygon]
        self.origin = (min(xs), min(ys))
        self.cell = section.cell_m
        self.size = (math.ceil((max(xs) - self.origin[0]) / self.cell), math.ceil((max(ys) - self.origin[1]) / self.cell))

    def centre(self, cell: Cell) -> Point:
        return (self.origin[0] + (cell[0] + 0.5) * self.cell, self.origin[1] + (cell[1] + 0.5) * self.cell)

    def cell_of(self, point: Point) -> Cell | None:
        i = math.floor((point[0] - self.origin[0]) / self.cell)
        j = math.floor((point[1] - self.origin[1]) / self.cell)
        return (i, j) if 0 <= i < self.size[0] and 0 <= j < self.size[1] else None

    @cached_property
    def free(self) -> frozenset[Cell]:
        section, radius = self.map.ground, self.map.ground.robot_radius_m
        blocked = [*section.obstacles, *section.no_go]
        cells = set()
        for i in range(self.size[0]):
            for j in range(self.size[1]):
                point = self.centre((i, j))
                # The whole footprint inside one drivable polygon, clear of every blocked one. / 整个覆盖范围在某个
                # 可行驶多边形内，且远离每个被占多边形。
                if not any(_inside(point, p) and _edge_distance(point, p) >= radius for p in section.drivable):
                    continue
                if any(_inside(point, p) or _edge_distance(point, p) < radius for p in blocked):
                    continue
                cells.add((i, j))
        return frozenset(cells)

    def clear(self, a: Point, b: Point) -> bool:
        """True when the straight segment from a to b stays on free cells. / 线段 a→b 全程在可通行单元上时为真。"""
        steps = max(1, math.ceil(math.dist(a, b) / (self.cell / 2)))
        return all(self.cell_of((a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)) in self.free
                   for k in range(steps + 1))


def _start(grid: Grid, position: Point) -> Cell | None:
    # The robot may stand a little inside the clearance band; take the nearest free cell within one radius.
    # 地面车可能略微站在净空带内；取一个车体半径内最近的可通行单元。
    cell = grid.cell_of(position)
    if cell in grid.free:
        return cell
    reach = math.ceil((grid.map.ground.robot_radius_m + grid.cell) / grid.cell)
    near = [(cell_i, cell_j) for cell_i in range(cell[0] - reach, cell[0] + reach + 1)
            for cell_j in range(cell[1] - reach, cell[1] + reach + 1)] if cell else []
    near = [c for c in near if c in grid.free and math.dist(grid.centre(c), position)
            <= grid.map.ground.robot_radius_m + grid.cell]
    return min(near, key=lambda c: (math.dist(grid.centre(c), position), c)) if near else None


def _shortest(grid: Grid, start: Cell, goals: frozenset[Cell]) -> tuple[float, list[Cell]] | None:
    # Dijkstra on the 8-connected grid; a diagonal step needs both side cells free. / 八连通网格上的 Dijkstra；
    # 斜向一步要求两侧单元都可通行。
    best, previous, queue = {start: 0.0}, {}, [(0.0, start)]
    while queue:
        cost, cell = heapq.heappop(queue)
        if cost > best[cell]:
            continue
        if cell in goals:
            path = [cell]
            while path[-1] in previous:
                path.append(previous[path[-1]])
            return cost * grid.cell, path[::-1]
        for di, dj in NEIGHBOURS:
            nxt = (cell[0] + di, cell[1] + dj)
            if nxt not in grid.free:
                continue
            if di and dj and ((cell[0] + di, cell[1]) not in grid.free or (cell[0], cell[1] + dj) not in grid.free):
                continue
            step = cost + (math.sqrt(2) if di and dj else 1.0)
            if step < best.get(nxt, math.inf):
                best[nxt], previous[nxt] = step, cell
                heapq.heappush(queue, (step, nxt))
    return None


def _simplify(grid: Grid, points: list[Point]) -> tuple[Point, ...]:
    # Keep only the corners a straight segment cannot skip. / 只保留直线段无法跳过的拐点。
    kept, anchor = [points[0]], 0
    while anchor < len(points) - 1:
        reach = anchor + 1
        for k in range(len(points) - 1, anchor, -1):
            if grid.clear(points[anchor], points[k]):
                reach = k
                break
        kept.append(points[reach])
        anchor = reach
    return tuple(kept)


def assess(grid: Grid, *, position: Point, target: Point, radius_m: float | None = None) -> Reachability:
    """Answer an offer for a target point (`radius_m` None) or a search region from the robot's own map.

    依据地面车自己的地图，回答针对目标点（`radius_m` 为 None）或搜索区域的提议。
    """
    section, version = grid.map.ground, grid.map.registry_id
    start = _start(grid, position)
    if start is None:
        return Reachability(False, OUTSIDE_MAP, version, detail="the robot is not on a free cell of its map")
    near, far = section.standoff_m
    if radius_m is not None:
        near, far = 0.0, radius_m + far
    if grid.cell_of(target) is None and not any(grid.cell_of((target[0] + dx * far, target[1] + dy * far))
                                                for dx, dy in NEIGHBOURS):
        return Reachability(False, OUTSIDE_MAP, version, detail="the target is off the map")
    goals = frozenset(c for c in grid.free if near <= math.dist(grid.centre(c), target) <= far)
    if not goals:
        return Reachability(False, UNREACHABLE, version, detail="no free cell at a viewing distance")
    found = _shortest(grid, start, goals)
    if found is None:
        return Reachability(False, UNREACHABLE, version, detail="no path to a viewing cell")
    length, cells = found
    if length > section.max_path_m:
        return Reachability(False, UNREACHABLE, version, path_m=round(length, 2),
                            detail=f"path {length:.1f} m exceeds {section.max_path_m} m")
    points = [position, *(grid.centre(c) for c in cells[1:])]
    goal = grid.centre(cells[-1])
    return Reachability(True, None, version, path_m=round(length, 2), eta_s=round(length / section.cruise_mps, 1),
                        goal=goal, waypoints=_simplify(grid, points))
