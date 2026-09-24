"""Energy reachability for recovery policy v2 (D042): can the aircraft still reach home, or a landing site?

The drain rate is fitted from this flight's own battery samples over a sliding window. A step larger than
`jump_fraction` between consecutive samples is a measurement discontinuity (a swap, an estimator reset or an
injected input), not consumption, so it restarts the level but keeps the rate learned before it. Until enough
continuous samples exist the registered prior rate is used, and the conservative rate is the larger of the
fitted upper bound and the prior. Travel time assumes the registered worst-case headwind against the
flight controller's return speed, plus a climb allowance and a descent at the landing speed. Every missing
input makes the answer "not reachable": unknown never selects the optimistic edge.

Only recovery policy v2 uses this model; missions under v1 keep the M1 context unchanged.

恢复策略 v2 的能源可达性（D042）：飞行器还能否到达 home 或某个降落点？

放电率用本次飞行自己的电量样本在滑动窗口内拟合。相邻样本之间超过 `jump_fraction` 的跳变视为测量不连续
（换电、估计器重置或注入输入），不是消耗：它重置电量水平，但保留跳变前学到的速率。连续样本不足时使用登记的
先验速率；保守速率取拟合上界与先验中的较大者。航行时间按登记的最坏逆风对抗飞控返航速度计算，另加爬升裕量
与按降落速度的下降。任何输入缺失都给出「不可达」：未知绝不选乐观边。

只有恢复策略 v2 使用本模型；v1 下的任务仍用 M1 的上下文，保持不变。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class EnergySettings:
    """Registered energy-model parameters (scene `energy_model`). / 登记的能源模型参数（场景 `energy_model`）。"""

    prior_drain_rate_per_s: float
    return_speed_mps: float
    descent_speed_mps: float
    headwind_mps: float
    climb_allowance_m: float
    margin_fraction: float
    early_return_factor: float
    window_s: float = 20.0
    min_samples: int = 10
    min_span_s: float = 5.0
    jump_fraction: float = 0.05

    @classmethod
    def from_registry(cls, data: dict) -> EnergySettings:
        model = data["energy_model"]
        return cls(
            prior_drain_rate_per_s=float(model["prior_drain_rate_per_s"]),
            return_speed_mps=float(model["return_speed_mps"]),
            descent_speed_mps=float(model["descent_speed_mps"]),
            headwind_mps=float(model["headwind_mps"]),
            climb_allowance_m=float(model["climb_allowance_m"]),
            margin_fraction=float(model["margin_fraction"]),
            early_return_factor=float(model["early_return_factor"]),
        )


@dataclass
class Reach:
    reachable: bool
    needed_fraction: float | None
    time_s: float | None


class EnergyModel:
    def __init__(self, settings: EnergySettings):
        self.settings = settings
        self.samples: deque[tuple[float, float]] = deque()
        self.fitted_rate: float | None = None
        self.fitted_upper: float | None = None

    def observe(self, monotonic: float, fraction: float | None) -> None:
        """Add one battery sample; None or non-finite samples are ignored. / 加入一个电量样本；None 或非有限值忽略。"""
        if fraction is None or not math.isfinite(fraction):
            return
        if self.samples and abs(fraction - self.samples[-1][1]) > self.settings.jump_fraction:
            # A discontinuity restarts the window; the learned rate survives it. / 不连续跳变重启窗口，已学到的速率保留。
            self.samples.clear()
        self.samples.append((monotonic, fraction))
        while self.samples and monotonic - self.samples[0][0] > self.settings.window_s:
            self.samples.popleft()
        self._fit()

    def _fit(self) -> None:
        points = list(self.samples)
        if len(points) < self.settings.min_samples or points[-1][0] - points[0][0] < self.settings.min_span_s:
            return
        n = len(points)
        mean_t = sum(t for t, _ in points) / n
        mean_b = sum(b for _, b in points) / n
        var_t = sum((t - mean_t) ** 2 for t, _ in points)
        if var_t <= 0:
            return
        slope = sum((t - mean_t) * (b - mean_b) for t, b in points) / var_t
        residual = sum((b - (mean_b + slope * (t - mean_t))) ** 2 for t, b in points)
        slope_std = math.sqrt(residual / max(n - 2, 1) / var_t)
        rate = max(0.0, -slope)
        self.fitted_rate, self.fitted_upper = rate, rate + 2.0 * slope_std

    @property
    def conservative_rate(self) -> float:
        """Upper drain rate used for reachability: never below the registered prior. / 可达性使用的上界放电率：从不低于登记先验。"""
        prior = self.settings.prior_drain_rate_per_s
        return max(prior, self.fitted_upper) if self.fitted_upper is not None else prior

    def travel(self, position, target, *, landing: bool) -> float | None:
        if position is None or target is None:
            return None
        s = self.settings
        ground_speed = s.return_speed_mps - s.headwind_mps
        if ground_speed <= 0 or s.descent_speed_mps <= 0:
            return None
        horizontal = math.hypot(target[0] - position[0], target[1] - position[1])
        altitude = max(position[2] - (target[2] if not landing else 0.0), 0.0)
        climb = s.climb_allowance_m / max(s.descent_speed_mps, 0.1)
        descent = (altitude + s.climb_allowance_m) / s.descent_speed_mps if landing else 0.0
        return horizontal / ground_speed + climb + descent

    def reach(self, position, battery: float | None, reserve: float, target) -> Reach:
        if battery is None or not math.isfinite(battery):
            return Reach(False, None, None)
        time_s = self.travel(position, target, landing=True)
        if time_s is None:
            return Reach(False, None, None)
        needed = time_s * self.conservative_rate
        available = battery - reserve - self.settings.margin_fraction
        return Reach(available >= needed, needed, time_s)

    def context(self, position, battery: float | None, reserve: float, home, sites: dict) -> dict:
        """Recovery-policy context: home and nearest-site reachability plus the early-return signal.

        恢复策略上下文：home 与最近降落点的可达性，以及提前返航信号。
        """
        home_reach = self.reach(position, battery, reserve, home)
        best_name, best = None, None
        for name, site in sorted(sites.items()):
            option = self.reach(position, battery, reserve, site)
            if option.reachable and (best is None or option.needed_fraction < best.needed_fraction):
                best_name, best = name, option
        available = None if battery is None else battery - reserve - self.settings.margin_fraction
        early = bool(
            home_reach.needed_fraction is not None
            and available is not None
            and available < self.settings.early_return_factor * home_reach.needed_fraction
        )
        return {
            "rtl_reachable": home_reach.reachable,
            "nearest_site_reachable": best is not None,
            "nearest_site": best_name,
            "home_needed_fraction": home_reach.needed_fraction,
            "available_fraction": available,
            "return_due": early or not home_reach.reachable,
            "drain_rate_per_s": self.conservative_rate,
        }
