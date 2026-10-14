"""X1 ground registry (D088): the factory, the 2D fence, the safe terminal without in_air, the ground phase and the
path checks, on the committed S0 ground site.

X1 地面登记表（D088）：登记表工厂、二维围栏、不读 in_air 的安全终态、地面阶段与路径检查，基于已提交的 S0 地面站点。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.contracts import Embodiment, FlightObservation, utcnow
from drone_agent.mission.ground import GroundRegistry
from drone_agent.mission.reachability import assess
from drone_agent.mission.registry import Registry, load_registry

ROOT = Path(__file__).resolve().parents[2]
SCENE = ROOT / "configs/scenarios/x1_ground_a_v1.yaml"
GROUND = load_registry(ROOT, SCENE)


def observation(x=0.0, y=0.0, *, armed=False, in_air=None, speed=0.0, age_s=0.0, **extra) -> FlightObservation:
    now = utcnow() - timedelta(seconds=age_s)
    return FlightObservation(timestamp=now, valid_until=now + timedelta(seconds=0.5), sample_id=1, robot_id="ugv_01",
                             pose={"frame": {"frame_id": "map_enu", "map_version": "x1_ground_a_v1"},
                                   "position": {"x": x, "y": y, "z": 0.0, "covariance": [0.1, 0, 0, 0, 0.1, 0, 0, 0,
                                                                                          0.1]},
                                   "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}},
                             velocity_enu_mps=[speed, 0.0, 0.0], armed=armed, in_air=in_air,
                             source="deterministic_test", **extra)


def test_the_factory_reads_the_kind_and_keeps_aerial_maps_aerial():
    assert isinstance(GROUND, GroundRegistry) and GROUND.safe_terminal == "stopped_disarmed" and GROUND.terminal_disarm
    aerial = load_registry(ROOT, ROOT / "configs/scenarios/p3_site_a_v1.yaml")
    assert type(aerial) is Registry and aerial.safe_terminal == "landed_disarmed" and not aerial.terminal_disarm
    assert GROUND.capability.embodiment is Embodiment.GROUND_WHEELED
    assert set(GROUND.manifests) == {s.skill_id for s in GROUND.capability.skills}
    with pytest.raises(ValueError, match="not a ground site"):
        GroundRegistry(ROOT, ROOT / "configs/scenarios/p3_site_a_v1.yaml")


@pytest.mark.parametrize(("point", "inside"), [
    ((0.0, 0.0), True),        # the parking spot / 停车位
    ((-2.0, 4.0), False),      # the aircraft pad / 飞行器机位
    ((8.0, 17.0), False),      # the fenced compound / 院子
    ((0.0, 11.0), False),      # the building / 楼
    ((50.0, 0.0), False),      # off the map / 地图之外
    ((float("nan"), 0.0), False),
])
def test_the_fence_is_two_dimensional_and_excludes_obstacles_and_no_go_zones(point, inside):
    assert GROUND.inside(point) is inside
    assert GROUND.inside((*point, 30.0)) is inside  # height never matters / 高度不影响


@pytest.mark.parametrize("in_air", [None, False, True])
def test_the_safe_terminal_never_reads_in_air(in_air):
    # The measured rover reports in_air true once armed; the terminal must not depend on it. / 实测地面车解锁后即报告
    # in_air 为 true；终态不得依赖它。
    assert GROUND.at_safe_terminal(observation(in_air=in_air)) is True
    assert GROUND.at_safe_terminal(observation(armed=True, in_air=in_air)) is False
    assert GROUND.at_safe_terminal(observation(speed=0.4, in_air=in_air)) is False
    assert GROUND.at_safe_terminal(observation(age_s=2, in_air=in_air)) is False


def test_the_ground_phase_table():
    assert GROUND.ground_phase(observation()) == "parked"
    assert GROUND.ground_phase(observation(5.0, 5.0)) == "stopped"
    assert GROUND.ground_phase(observation(armed=True)) == "moving"
    assert GROUND.ground_phase(observation(5.0, 5.0, speed=0.5)) == "moving"
    assert GROUND.ground_phase(observation(armed=None)) == "unknown"
    assert GROUND.ground_phase(observation(age_s=2)) == "unknown"


def test_paths_are_checked_on_the_robots_own_grid():
    planned = assess(GROUND.grid, position=(0.0, 0.0), target=(3.0, 8.0))
    assert planned.accepted and GROUND.path_problem([list(p) for p in planned.waypoints]) is None
    assert "blocked" in GROUND.path_problem([[0.0, 9.0], [0.0, 14.0]])  # through the building / 穿楼
    assert "outside" in GROUND.path_problem([[0.0, 0.0], [-2.0, 4.0]])  # into the pad / 进机位
    assert "at least two" in GROUND.path_problem([[0.0, 0.0]])
    long = [[x, 0.0] for x in (-10.0, 8.0, -10.0, 8.0, -10.0)]  # 72 m back and forth / 来回 72 m
    assert "longer" in GROUND.path_problem(long)


def test_a_stop_is_a_ground_behaviour_only():
    from drone_agent.contracts import RecoveryPolicy

    rover = RecoveryPolicy.from_yaml(ROOT / "configs/recovery_policies/rover_campus_v1.yaml")
    assert rover.validate_against(GROUND.capability) == []
    aerial = load_registry(ROOT, ROOT / "configs/scenarios/p3_site_a_v1.yaml").capability
    errors = rover.model_copy(update={"embodiment": aerial.embodiment}).validate_against(aerial)
    assert any("stop is a ground behaviour" in error for error in errors)
