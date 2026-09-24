"""Control-barrier-function filter for every external-mode target before it is authorized (D042).

The guardian treats the planner's next point as a desired velocity over a short horizon and projects it
onto the safe set of a single-integrator model: for every constraint `h(x) >= 0` with outward normal `n`,
the command must satisfy `n·u >= -alpha·h` and `|u| <= v_max`. Constraints come from three places: the six
faces of the registered geofence (trusted), registered obstacles inflated by their radius (trusted) and the
nearby points of a fresh local map (belief, inflated by its stated uncertainty). The projection is solved by
Hildreth's dual coordinate ascent; the result is then checked, and the straight segment to the resulting
target is sampled so that an interrupted stream leaves the aircraft at a safe point. Anything infeasible —
already inside the unsafe set, conflicting constraints, a segment that cannot be made safe — is rejected,
never passed through. Pure Python: its cost is part of the supervision period.

外部模式下每个目标在授权前都经过控制屏障函数过滤（D042）。

guardian 把规划节点给出的下一个点视为短时域内的期望速度，按单积分器模型投影到安全集合：对每个约束
`h(x) >= 0` 及其外法向 `n`，指令必须满足 `n·u >= -alpha·h` 且 `|u| <= v_max`。约束来自三处：登记围栏的六个面
（可信）、按半径膨胀的登记障碍（可信）、新鲜局部地图的邻近点（信念，按其声明的不确定性膨胀）。投影用
Hildreth 对偶坐标上升求解；求解后再逐项核对，并对通往结果目标的直线段采样，保证设定值流中断时飞行器停在
安全点。任何不可行——已在不安全集合内、约束冲突、无法使线段安全——都拒绝，绝不放行。纯 Python 实现：其
耗时计入监督周期。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

Vector = tuple[float, float, float]


def _sub(a: Vector, b: Vector) -> Vector:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vector, b: Vector) -> Vector:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vector, k: float) -> Vector:
    return (a[0] * k, a[1] * k, a[2] * k)


def _dot(a: Vector, b: Vector) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Vector) -> float:
    return math.sqrt(_dot(a, a))


@dataclass(frozen=True)
class Constraint:
    """`normal·u >= -alpha·h` with `h` the signed clearance. / `normal·u >= -alpha·h`，`h` 为有符号余量。"""

    source: str
    normal: Vector
    h: float


@dataclass(frozen=True)
class FilterSettings:
    """Tunable margins; values come from the scene registry, never from a planner.

    可调余量；取值来自场景登记表，从不来自规划节点。
    """

    alpha: float = 1.0
    horizon_s: float = 1.0
    fence_margin_m: float = 1.0
    obstacle_margin_m: float = 1.0
    sigma_factor: float = 2.0
    neighbour_radius_m: float = 6.0
    max_neighbours: int = 48
    segment_samples: int = 8
    tolerance: float = 1e-6
    iterations: int = 200


@dataclass
class FilterResult:
    accepted: bool
    reason: str = ""
    target: Vector | None = None
    speed_mps: float = 0.0
    desired_speed_mps: float = 0.0
    modified: bool = False
    min_h: float = math.inf
    binding: list[str] = field(default_factory=list)
    constraints: int = 0


def fence_constraints(point: Vector, bounds: dict, margin: float) -> list[Constraint]:
    """Six half-space constraints of an axis-aligned geofence. / 轴对齐围栏的六个半空间约束。"""
    result = []
    for axis, name in enumerate("xyz"):
        low, high = bounds[name]
        unit = tuple(1.0 if i == axis else 0.0 for i in range(3))
        result.append(Constraint(f"fence:{name}_min", unit, point[axis] - low - margin))
        result.append(Constraint(f"fence:{name}_max", _scale(unit, -1.0), high - margin - point[axis]))
    return result


def _nearest_on_obstacle(point: Vector, obstacle: dict) -> Vector:
    """Closest point of a registered box or vertical cylinder. / 登记方盒或竖直圆柱上的最近点。"""
    kind = obstacle["kind"]
    if kind == "box":
        low, high = obstacle["min"], obstacle["max"]
        return tuple(min(max(point[i], low[i]), high[i]) for i in range(3))
    if kind == "cylinder":
        cx, cy = obstacle["center"][:2]
        radius, z_low, z_high = obstacle["radius_m"], obstacle["z_min"], obstacle["z_max"]
        dx, dy = point[0] - cx, point[1] - cy
        planar = math.hypot(dx, dy)
        if planar <= radius:
            nx, ny = point[0], point[1]
        else:
            nx, ny = cx + dx / planar * radius, cy + dy / planar * radius
        return (nx, ny, min(max(point[2], z_low), z_high))
    raise ValueError(f"unsupported obstacle kind {kind!r}")


def _point_constraint(source: str, point: Vector, nearest: Vector, inflate: float) -> Constraint:
    offset = _sub(point, nearest)
    distance = _norm(offset)
    if distance < 1e-9:
        # Inside the geometry: no outward direction exists, so the constraint is violated. / 位于几何体内部：没有外法向，约束不满足。
        return Constraint(source, (0.0, 0.0, 1.0), -inflate)
    return Constraint(source, _scale(offset, 1.0 / distance), distance - inflate)


def obstacle_constraints(point: Vector, obstacles: dict, margin: float) -> list[Constraint]:
    return [
        _point_constraint("registered:" + name, point, _nearest_on_obstacle(point, spec), margin)
        for name, spec in sorted(obstacles.items())
    ]


def belief_constraints(point: Vector, belief: dict | None, settings: FilterSettings) -> list[Constraint]:
    """Nearest points of a fresh local map, inflated by radius, margin and k·sigma. / 新鲜局部地图的邻近点，按半径、余量与 k·σ 膨胀。"""
    if not belief:
        return []
    inflate = belief["point_radius_m"] + settings.obstacle_margin_m + settings.sigma_factor * belief["position_std_m"]
    candidates = []
    for obstacle in belief["points"]:
        distance = _norm(_sub(point, obstacle))
        if distance <= settings.neighbour_radius_m:
            candidates.append((distance, obstacle))
    candidates.sort(key=lambda item: item[0])
    return [
        _point_constraint(f"belief:{index}", point, obstacle, inflate)
        for index, (_, obstacle) in enumerate(candidates[: settings.max_neighbours])
    ]


def project(desired: Vector, constraints: list[Constraint], v_max: float, settings: FilterSettings) -> Vector:
    """Hildreth dual ascent for min |u - desired|^2 s.t. n·u >= -alpha·h, then the speed bound.

    Hildreth 对偶上升求解 min |u - desired|^2，约束 n·u >= -alpha·h，然后施加速度上界。
    """
    rows = [(c.normal, -settings.alpha * c.h) for c in constraints]
    duals = [0.0] * len(rows)
    u = desired
    for _ in range(settings.iterations):
        changed = 0.0
        for index, (normal, bound) in enumerate(rows):
            squared = _dot(normal, normal)
            if squared <= 0:
                continue
            step = (bound - _dot(normal, u)) / squared
            new_dual = max(0.0, duals[index] + step)
            delta = new_dual - duals[index]
            if delta:
                u = _add(u, _scale(normal, delta))
                duals[index] = new_dual
                changed = max(changed, abs(delta))
        if changed < settings.tolerance:
            break
    speed = _norm(u)
    if speed > v_max > 0:
        u = _scale(u, v_max / speed)
    return u


def filter_target(
    position: Vector,
    target: Vector,
    *,
    v_max: float,
    bounds: dict,
    obstacles: dict,
    belief: dict | None,
    settings: FilterSettings | None = None,
) -> FilterResult:
    """Filter one desired target; returns the safe target or a rejection reason.

    过滤一个期望目标；返回安全目标或拒绝原因。
    """
    settings = settings or FilterSettings()
    if not all(math.isfinite(v) for v in (*position, *target)) or not math.isfinite(v_max) or v_max <= 0:
        return FilterResult(False, "non_finite_input")

    def constraints_at(point: Vector) -> list[Constraint]:
        return (
            fence_constraints(point, bounds, settings.fence_margin_m)
            + obstacle_constraints(point, obstacles, settings.obstacle_margin_m)
            + belief_constraints(point, belief, settings)
        )

    here = constraints_at(position)
    min_h = min((c.h for c in here), default=math.inf)
    if min_h < -settings.tolerance:
        worst = min(here, key=lambda c: c.h)
        return FilterResult(False, "inside_unsafe_set:" + worst.source, min_h=min_h, constraints=len(here))
    desired = _scale(_sub(target, position), 1.0 / settings.horizon_s)
    desired_speed = _norm(desired)
    if desired_speed > v_max:
        desired = _scale(desired, v_max / desired_speed)
    u = project(desired, here, v_max, settings)
    violated = [c.source for c in here if _dot(c.normal, u) < -settings.alpha * c.h - 1e-4]
    if violated:
        return FilterResult(False, "infeasible:" + violated[0], min_h=min_h, constraints=len(here))
    # Shrink the step until the straight segment to the target is clear at every sample.
    # 缩短步长，直到通往目标的直线段在每个采样点都满足约束。
    step = _scale(u, settings.horizon_s)
    for _ in range(6):
        candidate = _add(position, step)
        samples = [_add(position, _scale(step, k / settings.segment_samples)) for k in range(1, settings.segment_samples + 1)]
        if all(c.h >= -settings.tolerance for sample in samples for c in constraints_at(sample)):
            binding = [c.source for c in here if abs(_dot(c.normal, u) + settings.alpha * c.h) < 1e-3]
            modified = _norm(_sub(u, desired)) > 1e-3
            return FilterResult(
                True,
                target=candidate,
                speed_mps=max(_norm(u), 0.1),
                desired_speed_mps=desired_speed,
                modified=modified,
                min_h=min_h,
                binding=binding,
                constraints=len(here),
            )
        step = _scale(step, 0.5)
    return FilterResult(False, "segment_unsafe", min_h=min_h, constraints=len(here))
