"""One-off estimate for WP-P6-06: how much airspace the current footprint holds against per-leg corridors and finer
cells, on the committed P3 site pair and on a long-route layout, and how many concurrent task pairs conflict.

Read-only and offline: the current footprint comes from `fleet/reservation.py` itself (`task_points` + `footprint`);
the corridor is a prototype: for each leg of the ordered flight path, the convex hull of the leg and every recovery
target (home and the landing sites reserved for the robot), grown by the buffer. Release timing assumes a constant
cruise speed and fixed takeoff / landing times; it is an indicator, not a measurement.

WP-P6-06 的一次性估算：当前航迹覆盖、航段走廊与更细网格各持有多少空域，以及并发任务对中有多少互相冲突。只读、离线；
走廊是原型，不被 src/ 依赖。

Usage: uv run python experiments/fine_reservations/estimate.py [--output result.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

from drone_agent.fleet.reservation import footprint, grid_cells, task_points

ROOT = Path(__file__).resolve().parents[2]
EPS = 1e-9


@dataclass(frozen=True)
class Grid:
    frame: str
    cell_m: float
    buffer_m: float


# ── geometry / 几何 ──

def hull(points):
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def point_segment(p, a, b):
    ax, ay, bx, by = *a, *b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length < EPS else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def segments_cross(a, b, c, d):
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < EPS else (1 if v > 0 else -1)

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 != o2 and o3 != o4


def inside_convex(p, poly):
    if len(poly) < 3:
        return False
    sign = 0
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        v = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        if abs(v) < EPS:
            continue
        s = 1 if v > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def edges(poly):
    if len(poly) == 1:
        return [(poly[0], poly[0])]
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly) if len(poly) > 2 else 1)]


def distance_poly_box(poly, box):
    x0, y0, x1, y1 = box
    square = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in poly) or any(inside_convex(c, poly) for c in square):
        return 0.0
    best = math.inf
    for a, b in edges(poly):
        for c, d in edges(square):
            if segments_cross(a, b, c, d):
                return 0.0
            best = min(best, point_segment(a, c, d), point_segment(b, c, d), point_segment(c, a, b),
                       point_segment(d, a, b))
    return best


def cells_near(poly, radius, size):
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    candidates = grid_cells((min(xs) - radius, min(ys) - radius, max(xs) + radius, max(ys) + radius), size)
    return {(i, j) for i, j in candidates
            if distance_poly_box(poly, (i * size, j * size, (i + 1) * size, (j + 1) * size)) <= radius + EPS}


# ── flights / 飞行 ──

def xy(p):
    return float(p[0]), float(p[1])


def ordered_path(data, asset_id, origin):
    """Home, the observation route, the asset, the return route and the landing site, in flight order (shared frame).

    按飞行顺序：home、观测航线、资产、返航航线、降落点（共享坐标）。
    """
    asset = data["assets"][asset_id]
    defaults = data.get("mission_defaults", {})
    routes = data.get("routes", {})
    steps = [("takeoff", [xy(data["home"]["position"])]),
             ("inspect", [*(xy(p) for p in routes.get(asset.get("observation_route")) or []), xy(asset["position"])]),
             ("return_home", [xy(p) for p in routes.get(asset.get("return_route")
                                                        or defaults.get("return_route_id")) or []]),
             ("land", [xy(data["landing_sites"][defaults["landing_site_id"]]["position"])])]
    out, owner = [], []
    for index, (_, places) in enumerate(steps):
        for p in places:
            q = (origin[0] + p[0], origin[1] + p[1])
            if not out or math.dist(out[-1], q) > EPS:
                out.append(q)
                owner.append(index)
    return out, owner


def recovery_targets(data, robot_id, origin):
    pts = [xy(data["home"]["position"])] + [xy(s["position"]) for s in data.get("landing_sites", {}).values()
                                             if s.get("reserved_for") == robot_id]
    return [(origin[0] + x, origin[1] + y) for x, y in pts]


def corridor_legs(path, targets, grid):
    legs = [path[i:i + 2] for i in range(len(path) - 1)] or [path[:1]]
    return [cells_near(hull([*leg, *targets]), grid.buffer_m, grid.cell_m) for leg in legs]


def leg_lengths(path):
    return [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)] or [0.0]


@dataclass
class Flight:
    name: str
    box_cells: set
    legs: list
    path: list
    lengths: list
    leg_step: list  # the step each leg belongs to (the step of the leg's end point) / 每段所属步骤（终点所属步骤）
    target_cells: set

    @property
    def corridor(self):
        return set().union(*self.legs)


def flight(data, robot_id, asset_id, origin, grid):
    box = footprint(grid, origin, task_points(data, asset_id, robot_id), None)
    cells = {tuple(int(v) for v in c.rsplit(".", 2)[1:]) for c in box.cells}
    path, owner = ordered_path(data, asset_id, origin)
    targets = recovery_targets(data, robot_id, origin)
    targets_cells = set().union(*(cells_near([t], grid.buffer_m, grid.cell_m) for t in targets))
    return Flight(f"{robot_id}:{asset_id}", cells, corridor_legs(path, targets, grid), path, leg_lengths(path),
                  owner[1:] or [0], targets_cells)


def step_release_time(first: Flight, second_cells: set, *, cruise, takeoff_s, landing_s, reconcile_s):
    """Like `release_time`, but cells are released only when a step completes (the evidence the service has).

    同 `release_time`，但只在步骤完成时释放（服务拿得到的证据）。
    """
    total = takeoff_s + sum(first.lengths) / cruise + landing_s + reconcile_s
    t = takeoff_s
    steps = sorted(set(first.leg_step))
    for step in steps:
        later = [k for k, s in enumerate(first.leg_step) if s > step]
        t += sum(first.lengths[k] for k, s in enumerate(first.leg_step) if s == step) / cruise
        remaining = set().union(first.target_cells, *(first.legs[k] for k in later))
        if not (remaining & second_cells):
            return t, total
    return total, total


def release_time(first: Flight, second_cells: set, *, cruise, takeoff_s, landing_s, reconcile_s):
    """(seconds until `first` no longer needs any cell of `second_cells` under leg-wise release, total seconds).

    （按航段释放时 `first` 不再需要 `second_cells` 中任何单元的时刻，整次飞行时长）。
    """
    total = takeoff_s + sum(first.lengths) / cruise + landing_s + reconcile_s
    t = takeoff_s
    for k in range(len(first.legs)):
        remaining = set().union(*first.legs[k:])
        if not (remaining & second_cells):
            return t, total
        t += first.lengths[k] / cruise
    return total, total


def compare(flights_a, flights_b, sizes, label, timing):
    rows = []
    for size, grid in sizes.items():
        pairs = conflicts_box = conflicts_corridor = 0
        held_box = held_corridor = 0.0
        saved, waited, saved_steps = [], [], []
        for fa, fb in itertools.product(flights_a[size], flights_b[size]):
            pairs += 1
            cb = bool(fa.box_cells & fb.box_cells)
            cc = bool(fa.corridor & fb.corridor)
            conflicts_box += cb
            conflicts_corridor += cc
            if cc:
                t, total = release_time(fa, fb.corridor, **timing)
                ts, _ = step_release_time(fa, fb.corridor, **timing)
                waited.append(total)
                saved.append(total - t)
                saved_steps.append(total - ts)
        for f in flights_a[size] + flights_b[size]:
            held_box += len(f.box_cells) * size * size
            held_corridor += len(f.corridor) * size * size
        n = len(flights_a[size]) + len(flights_b[size])
        rows.append({"layout": label, "cell_m": size, "task_pairs": pairs, "conflicts_box": conflicts_box,
                     "conflicts_corridor": conflicts_corridor, "mean_area_box_m2": round(held_box / n, 1),
                     "mean_area_corridor_m2": round(held_corridor / n, 1),
                     "release_saving_s_mean": round(sum(saved) / len(saved), 1) if saved else None,
                     "step_release_saving_s_mean": round(sum(saved_steps) / len(saved_steps), 1) if saved_steps else None,
                     "first_flight_s_mean": round(sum(waited) / len(waited), 1) if waited else None})
    return rows


def p3_pair(sizes):
    a = yaml.safe_load((ROOT / "configs/scenarios/p3_site_a_v1.yaml").read_text(encoding="utf-8"))
    b = yaml.safe_load((ROOT / "configs/scenarios/p3_site_b_v1.yaml").read_text(encoding="utf-8"))
    fa, fb = {}, {}
    for size, grid in sizes.items():
        fa[size] = [flight(a, "uav_a", asset, (0.0, 0.0), grid) for asset in sorted(a["assets"])]
        fb[size] = [flight(b, "uav_b", asset, (12.0, 0.0), grid) for asset in sorted(b["assets"])]
    return fa, fb


def long_layout(sizes):
    """Two docks 60 m apart, six assets 40-120 m out on both sides, M2-shaped routes (climb, then straight).

    两个相距 60 m 的机场，两侧 40–120 m 外六个资产，航线为 M2 形状（先爬升，再直飞）。
    """
    assets = {"n1": (20, 80), "n2": (-30, 110), "e1": (90, 40), "s1": (40, -70), "w1": (-60, -20), "c1": (30, 30)}
    maps = {}
    for robot, home in (("uav_l", (0.0, 0.0)), ("uav_r", (60.0, 0.0))):
        routes, registered = {"return": [[0, 4, 15], [0, 0, 15]]}, {}
        for name, (x, y) in assets.items():
            local = (x - home[0], y - home[1])
            routes[f"observe_{name}"] = [[0, 4, 15], [local[0], local[1], 15]]
            registered[name] = {"position": [local[0], local[1], 0], "observation_route": f"observe_{name}"}
        maps[robot] = ({"home": {"position": [0, 0, 15]}, "routes": routes, "assets": registered,
                        "landing_sites": {"home_pad": {"position": [0, 0, 0], "reserved_for": robot}},
                        "mission_defaults": {"return_route_id": "return", "landing_site_id": "home_pad"}}, home)
    out = {}
    for robot, (data, home) in maps.items():
        out[robot] = {size: [flight(data, robot, name, home, grid) for name in sorted(assets)]
                      for size, grid in sizes.items()}
    return out["uav_l"], out["uav_r"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sizes = {s: Grid("x", s, 2.0) for s in (4.0, 2.0, 1.0)}
    timing = {"cruise": 2.0, "takeoff_s": 10.0, "landing_s": 10.0, "reconcile_s": 5.0}
    rows = compare(*p3_pair(sizes), sizes, "p3_pair", timing)
    rows += compare(*long_layout(sizes), sizes, "long_routes", {**timing, "cruise": 5.0})
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    if args.output:
        args.output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
