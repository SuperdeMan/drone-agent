"""Localization health from PX4 estimator outputs (WP-M3-08); pure logic, no ROS imports.

GNSS is healthy only with a fresh 3D fix that the estimator is actually fusing; visual localization is healthy only
while the estimator fuses external-vision position and the local position is valid. Stale inputs are unhealthy,
never assumed healthy. The guardian decides what to do with the report; this module only reports.

基于 PX4 估计器输出的定位健康（WP-M3-08）；纯逻辑，不导入 ROS。

只有新鲜的三维定位且估计器确实在融合时 GNSS 才健康；只有估计器融合外部视觉位置且本地位置有效时视觉定位才健康。
过期输入视为不健康，绝不假定健康。guardian 决定如何处置报告；本模块只负责报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field

GPS_FIX_3D = 3


@dataclass
class Inputs:
    """Latest estimator facts with the local monotonic time each was received. / 最新估计器事实及本地接收时间。"""

    gps_fix_type: int = 0
    gps_satellites: int = 0
    gps_eph_m: float | None = None
    gps_time: float | None = None
    fusing_gps: bool = False
    fusing_ev_pos: bool = False
    flags_time: float | None = None
    xy_valid: bool = False
    z_valid: bool = False
    eph_m: float | None = None
    local_time: float | None = None
    status_time: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    gnss_ok: bool
    gnss_fix_type: int
    gnss_satellites: int
    gnss_eph_m: float | None
    visual_ok: bool
    ev_position_fused: bool
    gnss_position_fused: bool
    local_position_ok: bool
    position_std_m: float | None
    px4_status_age_s: float


def fresh(stamp: float | None, now: float, limit: float) -> bool:
    return stamp is not None and 0 <= now - stamp <= limit


def assess(inputs: Inputs, now: float, *, max_age_s: float = 0.5, max_gps_age_s: float = 1.0,
           max_eph_m: float = 3.0) -> Report:
    flags_fresh = fresh(inputs.flags_time, now, max_age_s)
    local_fresh = fresh(inputs.local_time, now, max_age_s)
    gps_fresh = fresh(inputs.gps_time, now, max_gps_age_s)
    gnss_fused = flags_fresh and inputs.fusing_gps
    ev_fused = flags_fresh and inputs.fusing_ev_pos
    local_ok = local_fresh and inputs.xy_valid and inputs.z_valid
    eph_ok = inputs.eph_m is not None and inputs.eph_m <= max_eph_m
    return Report(
        gnss_ok=bool(gps_fresh and inputs.gps_fix_type >= GPS_FIX_3D and gnss_fused),
        gnss_fix_type=inputs.gps_fix_type if gps_fresh else 0,
        gnss_satellites=inputs.gps_satellites if gps_fresh else 0,
        gnss_eph_m=inputs.gps_eph_m if gps_fresh else None,
        visual_ok=bool(ev_fused and local_ok and eph_ok),
        ev_position_fused=ev_fused,
        gnss_position_fused=gnss_fused,
        local_position_ok=local_ok,
        position_std_m=inputs.eph_m if local_fresh else None,
        px4_status_age_s=max(0.0, now - inputs.status_time) if inputs.status_time is not None else 1e6,
    )
