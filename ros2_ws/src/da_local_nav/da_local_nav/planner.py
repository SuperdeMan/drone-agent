"""Deterministic short-horizon planner (WP-M3-09); pure numpy + heapq, no ROS imports.

The executive states what and where; this planner decides how, here and now. Each cycle it rasterizes the sensed
voxels near the flight level, the registered obstacles and the task scope into a local 2-D grid, inflates them, runs
A* from the current position to the goal and turns the path into a short candidate segment toward the furthest
visible path point. It reports `reached` inside the goal tolerance and `no_path` when the goal cannot be reached — it
never retries silently. Unknown space is treated as free and re-planned as the depth camera reveals it; the guardian's
CBF filter, not this planner, is the safety boundary.

确定性短时域规划器（WP-M3-09）；纯 numpy + heapq，不导入 ROS。

executive 给出做什么与去哪；本规划器决定此时此地怎么飞。每个周期把飞行高度附近的感知体素、登记障碍与任务范围
栅格化为局部二维网格并膨胀，从当前位置到目标跑 A*，再把路径变成朝最远可见路径点的短候选片段。进入目标容差时
报告 `reached`，无法到达时报告 `no_path`——从不静默重试。未知空间视为空闲，随深度相机看到更多而重新规划；安全
边界是 guardian 的 CBF 过滤，不是本规划器。
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field

import numpy as np

SQRT2 = math.sqrt(2.0)


@dataclass(frozen=True)
class PlannerSettings:
    resolution_m: float = 0.5
    half_extent_m: float = 34.0
    inflation_m: float = 1.6
    vertical_band_m: float = 2.0
    lookahead_m: float = 3.0
    climb_rate_mps: float = 0.5
    horizons_s: tuple[float, ...] = (0.5, 1.0, 1.5)
    max_expansions: int = 40000


@dataclass
class Plan:
    status: str
    points: list[tuple[float, tuple[float, float, float]]]
    cycle_ms: float
    path_cells: int = 0
    expansions: int = 0
    notes: dict = field(default_factory=dict)


def _disk(radius_cells: int) -> list[tuple[int, int]]:
    return [(di, dj) for di in range(-radius_cells, radius_cells + 1) for dj in range(-radius_cells, radius_cells + 1)
            if di * di + dj * dj <= radius_cells * radius_cells]


class LocalPlanner:
    def __init__(self, priors: dict | None = None, settings: PlannerSettings | None = None):
        self.priors = priors or {}
        self.settings = settings or PlannerSettings()
        self.kernel = _disk(int(math.ceil(self.settings.inflation_m / self.settings.resolution_m)))

    def _grid(self, center, scope_low, scope_high, points: np.ndarray, altitude: float):
        s = self.settings
        size = int(round(2 * s.half_extent_m / s.resolution_m))
        origin = (center[0] - s.half_extent_m, center[1] - s.half_extent_m)
        occupied = np.zeros((size, size), dtype=bool)

        def mark(xs, ys):
            i = np.floor((np.asarray(xs) - origin[0]) / s.resolution_m).astype(int)
            j = np.floor((np.asarray(ys) - origin[1]) / s.resolution_m).astype(int)
            keep = (i >= 0) & (i < size) & (j >= 0) & (j < size)
            occupied[i[keep], j[keep]] = True

        if len(points):
            band = np.abs(points[:, 2] - altitude) <= s.vertical_band_m
            mark(points[band, 0], points[band, 1])
        centres = origin[0] + (np.arange(size) + 0.5) * s.resolution_m, origin[1] + (np.arange(size) + 0.5) * s.resolution_m
        gx, gy = np.meshgrid(centres[0], centres[1], indexing="ij")
        # A cell is occupied when the obstacle overlaps any part of it, so obstacles thinner than a cell are never
        # missed between cell centres. / 障碍与格子任一部分重叠即占据，比格子薄的障碍不会漏在格心之间。
        half = s.resolution_m / 2
        for spec in self.priors.values():
            if spec["kind"] == "box":
                low, high = spec["min"], spec["max"]
                if high[2] < altitude - s.vertical_band_m or low[2] > altitude + s.vertical_band_m:
                    continue
                occupied |= ((gx + half >= low[0]) & (gx - half <= high[0])
                             & (gy + half >= low[1]) & (gy - half <= high[1]))
            elif spec["kind"] == "cylinder":
                if spec["z_max"] < altitude - s.vertical_band_m or spec["z_min"] > altitude + s.vertical_band_m:
                    continue
                cx, cy = spec["center"][:2]
                reach = spec["radius_m"] + half * math.sqrt(2)
                occupied |= (gx - cx) ** 2 + (gy - cy) ** 2 <= reach ** 2
        blocked = occupied.copy()
        for di, dj in self.kernel:
            shifted = np.zeros_like(occupied)
            src_i = slice(max(0, -di), size - max(0, di))
            dst_i = slice(max(0, di), size - max(0, -di))
            src_j = slice(max(0, -dj), size - max(0, dj))
            dst_j = slice(max(0, dj), size - max(0, -dj))
            shifted[dst_i, dst_j] = occupied[src_i, src_j]
            blocked |= shifted
        margin = self.settings.inflation_m
        blocked |= (gx < scope_low[0] + margin) | (gx > scope_high[0] - margin)
        blocked |= (gy < scope_low[1] + margin) | (gy > scope_high[1] - margin)
        return blocked, origin, size

    def _cell(self, xy, origin, size):
        i = int(math.floor((xy[0] - origin[0]) / self.settings.resolution_m))
        j = int(math.floor((xy[1] - origin[1]) / self.settings.resolution_m))
        return (i, j) if 0 <= i < size and 0 <= j < size else None

    def _centre(self, cell, origin):
        r = self.settings.resolution_m
        return (origin[0] + (cell[0] + 0.5) * r, origin[1] + (cell[1] + 0.5) * r)

    def _astar(self, blocked, start, goal, size):
        def h(cell):
            dx, dy = abs(cell[0] - goal[0]), abs(cell[1] - goal[1])
            return max(dx, dy) + (SQRT2 - 1) * min(dx, dy)

        frontier = [(h(start), 0.0, start)]
        came = {start: None}
        cost = {start: 0.0}
        expansions = 0
        while frontier and expansions < self.settings.max_expansions:
            _, g, cell = heapq.heappop(frontier)
            if cell == goal:
                path = []
                while cell is not None:
                    path.append(cell)
                    cell = came[cell]
                return path[::-1], expansions
            if g > cost[cell]:
                continue
            expansions += 1
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    if not di and not dj:
                        continue
                    nxt = (cell[0] + di, cell[1] + dj)
                    if not (0 <= nxt[0] < size and 0 <= nxt[1] < size) or blocked[nxt]:
                        continue
                    if di and dj and (blocked[cell[0] + di, cell[1]] or blocked[cell[0], cell[1] + dj]):
                        continue  # No corner cutting. / 不切角。
                    step = g + (SQRT2 if di and dj else 1.0)
                    if step < cost.get(nxt, math.inf):
                        cost[nxt], came[nxt] = step, cell
                        heapq.heappush(frontier, (step + h(nxt), step, nxt))
        return None, expansions

    def _visible(self, blocked, a, b):
        steps = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))) * 2 + 1
        for k in range(steps + 1):
            t = k / steps
            cell = (int(round(a[0] + (b[0] - a[0]) * t)), int(round(a[1] + (b[1] - a[1]) * t)))
            if blocked[cell]:
                return False
        return True

    def plan(self, position, goal, *, speed: float, tolerance: float, scope_low, scope_high,
             points: np.ndarray) -> Plan:
        started = time.perf_counter()
        s = self.settings

        def done(status, pts, **extra):
            return Plan(status, pts, (time.perf_counter() - started) * 1000, **extra)

        position, goal = tuple(float(v) for v in position), tuple(float(v) for v in goal)
        if math.dist(position, goal) <= tolerance:
            return done("reached", [(t, goal) for t in s.horizons_s])
        blocked, origin, size = self._grid(position, scope_low, scope_high, points, goal[2])
        start, target = self._cell(position, origin, size), self._cell(goal, origin, size)
        hold = [(t, position) for t in s.horizons_s]
        if start is None or target is None:
            return done("no_path", hold, notes={"reason": "goal_outside_local_window"})
        if blocked[target]:
            return done("no_path", hold, notes={"reason": "goal_blocked"})
        was_blocked = bool(blocked[start])
        blocked[start] = False  # Always allow leaving the current cell. / 总是允许离开当前格。
        path, expansions = self._astar(blocked, start, target, size)
        if path is None:
            return done("no_path", hold, expansions=expansions, notes={"reason": "no_route"})
        carrot = path[-1]
        for cell in path[1:]:
            if math.dist(self._centre(cell, origin), position[:2]) > s.lookahead_m:
                break
            if self._visible(blocked, start, cell):
                carrot = cell
        target_xy = goal[:2] if carrot == path[-1] else self._centre(carrot, origin)
        dx, dy = target_xy[0] - position[0], target_xy[1] - position[1]
        planar = math.hypot(dx, dy)
        remaining = math.dist(position[:2], goal[:2])
        velocity = min(speed, max(remaining, 0.3))
        points_out = []
        for t in s.horizons_s:
            travel = min(velocity * t, planar)
            dz = max(-s.climb_rate_mps * t, min(s.climb_rate_mps * t, goal[2] - position[2]))
            ux, uy = (dx / planar, dy / planar) if planar > 1e-6 else (0.0, 0.0)
            points_out.append((t, (position[0] + ux * travel, position[1] + uy * travel, position[2] + dz)))
        return done("ok", points_out, path_cells=len(path), expansions=expansions,
                    notes={"start_blocked": was_blocked})
