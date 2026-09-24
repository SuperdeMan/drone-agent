"""The CBF filter keeps targets inside the fence and away from obstacles, or refuses them (D042).

CBF 过滤让目标留在围栏内并远离障碍，否则拒绝（D042）。
"""

import math

from drone_agent.guardian.constraint_filter import FilterSettings, filter_target

BOUNDS = {"x": (-40, 40), "y": (-40, 40), "z": (2, 10)}
WALL = {"wall_a": {"kind": "box", "min": [-4, 14.8, 0], "max": [4, 15.2, 8]}}
PILLAR = {"pillar": {"kind": "cylinder", "center": [0, 5], "radius_m": 0.5, "z_min": 0, "z_max": 8}}


def run(position, target, *, obstacles=None, belief=None, v_max=2.0, **settings):
    return filter_target(position, target, v_max=v_max, bounds=BOUNDS, obstacles=obstacles or {}, belief=belief,
                         settings=FilterSettings(**settings) if settings else None)


def test_free_space_target_passes_unchanged():
    result = run((0, 0, 4), (1, 0, 4))
    assert result.accepted and not result.modified
    assert math.dist(result.target, (1, 0, 4)) < 1e-6


def test_speed_is_capped_and_hold_is_trivially_safe():
    result = run((0, 0, 4), (10, 0, 4), v_max=1.5)
    assert result.accepted and math.dist(result.target, (0, 0, 4)) <= 1.5 + 1e-6
    hold = run((0, 0, 4), (0, 0, 4))
    assert hold.accepted and hold.target == (0, 0, 4)


def test_motion_toward_the_wall_is_slowed_before_the_barrier():
    # 1 m margin: the wall face at y=14.8 is 2.8 m away, so h = 1.8 and the approach speed is limited to alpha*h.
    # 1 米余量：墙面 y=14.8 相距 2.8 米，h = 1.8，接近速度受限于 alpha*h。
    result = run((0, 12, 4), (0, 14, 4), obstacles=WALL, alpha=0.5)
    assert result.accepted and result.modified
    assert result.target[1] - 12 <= 0.5 * 1.8 + 1e-3
    assert any(source.startswith("registered:wall_a") for source in result.binding)


def test_sliding_along_the_wall_is_allowed():
    result = run((0, 13, 4), (1.5, 13, 4), obstacles=WALL)
    assert result.accepted and abs(result.target[1] - 13) < 1e-3 and result.target[0] > 1.0


def test_inside_the_margin_is_refused_not_passed_through():
    result = run((0, 14.3, 4), (0, 13, 4), obstacles=WALL)
    assert not result.accepted and result.reason.startswith("inside_unsafe_set:registered:wall_a")


def test_the_fence_and_the_altitude_band_bound_every_target():
    result = run((38.5, 0, 4), (45, 0, 4))
    assert result.accepted and result.target[0] <= 39.0 + 1e-6
    low = run((0, 0, 3.2), (0, 0, 0))
    assert low.accepted and low.target[2] >= 3.0 - 1e-6


def test_belief_points_are_inflated_by_their_uncertainty():
    belief = {"points": [[0, 3, 4]], "point_radius_m": 0.1, "position_std_m": 0.2}
    # inflate = 0.1 + 1.0 + 2*0.2 = 1.5; at 1.5 m away the vehicle is on the barrier. / 膨胀 1.5 米。
    at_barrier = run((0, 1.5, 4), (0, 3, 4), belief=belief)
    assert at_barrier.accepted and at_barrier.target[1] <= 1.5 + 1e-3
    inside = run((0, 2.0, 4), (0, 1, 4), belief=belief)
    assert not inside.accepted


def test_a_segment_that_would_cut_a_pillar_is_shortened():
    # Desired straight line passes through the pillar; the filter must not leave a target beyond it.
    # 期望直线穿过立柱；过滤后不得留下立柱另一侧的目标。
    result = run((0, 2.5, 4), (0, 7.5, 4), obstacles=PILLAR, v_max=5.0, horizon_s=1.0)
    assert result.accepted
    assert result.target[1] <= 5 - 0.5 - 1.0 + 1e-3


def test_non_finite_input_is_refused():
    assert not run((0, 0, float("nan")), (0, 0, 4)).accepted
    assert not run((0, 0, 4), (0, 0, 4), v_max=0).accepted
