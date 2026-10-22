"""X1 onboard reachability (D088): paths on the robot's own map, viewing bands, refusals with controlled codes.

X1 机载可达性（D088）：在地面车自己的地图上找路径、观察距离区间，以受控原因码拒绝。
"""

from __future__ import annotations

import math

import pytest

from drone_agent.mission.reachability import OUTSIDE_MAP, UNREACHABLE, Grid, GroundMap, assess

# A 20 x 10 m yard: a wall from the south edge up to y = 7 (gap above it) and a fenced no-go block in the east.
# 20 x 10 m 场地：一道从南边延伸到 y = 7 的墙（上方留缺口），东侧一块围起来的禁入区。
MAP = GroundMap.model_validate({
    "registry_id": "ground_test_v1",
    "frame": {"frame_id": "map_enu", "map_version": "ground_test_v1"},
    "ground": {
        "cell_m": 0.25, "robot_radius_m": 0.4,
        "drivable": [[[0, 0], [20, 0], [20, 10], [0, 10]]],
        "obstacles": [[[8, 0], [9, 0], [9, 7], [8, 7]]],
        "no_go": [[[14, 2], [19, 2], [19, 8], [14, 8]]],
        "parking": {"park": {"position": [1, 1], "heading_deg": 0}},
        "standoff_m": [1.0, 2.0], "max_path_m": 60, "cruise_mps": 1.0,
    },
})
GRID = Grid(MAP)


def test_a_target_behind_the_wall_is_reached_around_it():
    answer = assess(GRID, position=(2.0, 2.0), target=(12.0, 2.0))
    assert answer.accepted and answer.reason is None and answer.map_version == "ground_test_v1"
    assert answer.path_m > 10.0  # the straight line crosses the wall / 直线会穿墙
    assert 1.0 <= math.dist(answer.goal, (12.0, 2.0)) <= 2.0
    assert answer.waypoints[0] == (2.0, 2.0) and answer.waypoints[-1] == answer.goal
    # Every leg stays on free cells and the route passes above the wall. / 每段都在可通行单元上，且从墙的上方绕过。
    assert all(GRID.clear(a, b) for a, b in zip(answer.waypoints, answer.waypoints[1:], strict=False))
    assert max(y for _, y in answer.waypoints) > 7.0
    assert answer.eta_s == pytest.approx(answer.path_m / 1.0, abs=0.1)


def test_a_target_deep_in_a_no_go_zone_is_unreachable():
    answer = assess(GRID, position=(2.0, 2.0), target=(16.5, 5.0))
    assert not answer.accepted and answer.reason == UNREACHABLE


def test_a_target_just_inside_the_fence_is_seen_over_it():
    answer = assess(GRID, position=(2.0, 9.0), target=(14.5, 5.0))
    assert answer.accepted and answer.goal[0] < 14.0 - 0.4 + 0.25


def test_a_search_region_needs_only_its_edge_within_the_viewing_distance():
    assert not assess(GRID, position=(2.0, 9.0), target=(16.5, 5.0)).accepted
    region = assess(GRID, position=(2.0, 9.0), target=(16.5, 5.0), radius_m=1.5)
    assert region.accepted and math.dist(region.goal, (16.5, 5.0)) <= 1.5 + 2.0


def test_a_robot_off_its_map_or_a_target_far_off_it_is_refused_as_outside_the_map():
    assert assess(GRID, position=(-5.0, 2.0), target=(12.0, 2.0)).reason == OUTSIDE_MAP
    assert assess(GRID, position=(2.0, 2.0), target=(60.0, 60.0)).reason == OUTSIDE_MAP


def test_a_robot_standing_in_the_clearance_band_starts_from_the_nearest_free_cell():
    answer = assess(GRID, position=(0.2, 2.0), target=(4.0, 2.0))
    assert answer.accepted


def test_a_path_longer_than_the_map_allows_is_unreachable():
    short = GroundMap.model_validate({**MAP.model_dump(), "ground": {**MAP.ground.model_dump(), "max_path_m": 8}})
    answer = assess(Grid(short), position=(2.0, 2.0), target=(12.0, 2.0))
    assert not answer.accepted and answer.reason == UNREACHABLE and answer.path_m > 8


@pytest.mark.parametrize("field", [{"standoff_m": [2.0, 1.0]}, {"drivable": [[[0, 0], [1, 1]]]}])
def test_a_malformed_map_is_rejected(field):
    with pytest.raises(ValueError):
        GroundMap.model_validate({**MAP.model_dump(), "ground": {**MAP.ground.model_dump(), **field}})
