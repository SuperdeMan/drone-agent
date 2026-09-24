"""Localization health from PX4 estimator outputs (WP-M3-08); pure logic, no ROS imports.

GNSS is healthy only with a fresh 3D fix that the estimator is actually fusing; visual localization is healthy only
while the estimator fuses external-vision position and the local position is valid. Stale inputs are unhealthy,
never assumed healthy. Freshness follows each topic's PX4 publication: EKF2 publishes `estimator_status_flags` on
every change and otherwise at 1 Hz, so a flag is fresh for 1.5 s; the local position streams at tens of hertz. The
report's PX4 input age is the age of the newest message on any input topic: it grows only when the whole DDS link is
gone, and it passes the guardian's limit before any single topic could be judged stale, so a lost link can never read
as a lost GNSS. Until every input topic has arrived once the age is unknown (1e6), and the flags carry their own age:
unknown fusion is not lost fusion (D048). The guardian decides what to do with the report; this module only reports.

基于 PX4 估计器输出的定位健康（WP-M3-08）；纯逻辑，不导入 ROS。

只有新鲜的三维定位且估计器确实在融合时 GNSS 才健康；只有估计器融合外部视觉位置且本地位置有效时视觉定位才健康。
过期输入视为不健康，绝不假定健康。新鲜度按各话题在 PX4 中的发布方式：EKF2 在每次变化时、否则以 1 Hz 发布
`estimator_status_flags`，因此标志的新鲜期为 1.5 s；本地位置以数十赫兹持续发布。报告中的 PX4 输入年龄是所有输入
话题中最新一条消息的年龄：只有整条 DDS 链路丢失时它才会增长，并且在任何单一话题可能被判过期之前就越过 guardian 的
限值，因此链路丢失永远不会被读成 GNSS 失效。全部输入话题到过一次之前该年龄未知（1e6），估计器标志另报自身年龄：
融合状态未知不等于融合丢失（D048）。guardian 决定如何处置报告；本模块只负责报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field

GPS_FIX_3D = 3
UNKNOWN_AGE_S = 1e6


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
    estimator_flags_age_s: float


def fresh(stamp: float | None, now: float, limit: float) -> bool:
    return stamp is not None and 0 <= now - stamp <= limit


def assess(inputs: Inputs, now: float, *, max_age_s: float = 0.5, max_flags_age_s: float = 1.5,
           max_gps_age_s: float = 1.0, max_eph_m: float = 3.0) -> Report:
    flags_fresh = fresh(inputs.flags_time, now, max_flags_age_s)
    local_fresh = fresh(inputs.local_time, now, max_age_s)
    gps_fresh = fresh(inputs.gps_time, now, max_gps_age_s)
    gnss_fused = flags_fresh and inputs.fusing_gps
    ev_fused = flags_fresh and inputs.fusing_ev_pos
    local_ok = local_fresh and inputs.xy_valid and inputs.z_valid
    eph_ok = inputs.eph_m is not None and inputs.eph_m <= max_eph_m
    stamps = (inputs.gps_time, inputs.flags_time, inputs.local_time, inputs.status_time)
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
        # Unknown until every input topic has arrived once: a link that is still coming up proves nothing (D048).
        # 全部输入话题到过一次之前未知：尚在建立的链路什么也不能证明（D048）。
        px4_status_age_s=max(0.0, now - max(stamps)) if all(t is not None for t in stamps) else UNKNOWN_AGE_S,
        estimator_flags_age_s=max(0.0, now - inputs.flags_time) if inputs.flags_time is not None else UNKNOWN_AGE_S,
    )
