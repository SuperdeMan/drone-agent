"""Pure logic of the ROS 2 nodes: localization health, depth geometry, local planning and detection (D041).

ROS 2 节点的纯逻辑：定位健康、深度几何、局部规划与检测（D041）。
"""

import math
import time
from pathlib import Path

import numpy as np
import pytest
import yaml
from da_common.px4 import enu_to_ned, ned_to_enu, topic, yaw_from_quaternion
from da_local_nav.geometry import VoxelMap, depth_to_enu
from da_local_nav.planner import LocalPlanner
from da_localization.health import Inputs, assess
from da_perception.detector import detect, match_asset

ROOT = Path(__file__).resolve().parents[2]
SCENE = yaml.safe_load((ROOT / "configs/scenarios/m3_campus_v3.yaml").read_text(encoding="utf-8"))
LEVEL = (1.0, 0.0, 0.0, 0.0)  # Identity attitude: body FRD aligned with NED. / 单位姿态：机体 FRD 与 NED 对齐。


def yawed(angle):
    return (math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))


def test_px4_topic_versions_and_frames():
    class V1:
        MESSAGE_VERSION = 1

    class V0:
        pass

    assert topic("/fmu/out/vehicle_status", V1) == "/fmu/out/vehicle_status_v1"
    assert topic("/fmu/in/vehicle_command", V0) == "/fmu/in/vehicle_command"
    assert ned_to_enu(*enu_to_ned(1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)
    assert yaw_from_quaternion(yawed(0.5)) == pytest.approx(0.5)


def inputs(**overrides):
    now = time.monotonic()
    values = dict(gps_fix_type=3, gps_satellites=12, gps_eph_m=0.8, gps_time=now, fusing_gps=True,
                  fusing_ev_pos=True, flags_time=now, xy_valid=True, z_valid=True, eph_m=0.3, local_time=now,
                  status_time=now)
    values.update(overrides)
    return Inputs(**values), now


def test_localization_health_never_assumes_health_from_stale_inputs():
    healthy, now = inputs()
    report = assess(healthy, now)
    assert report.gnss_ok and report.visual_ok and report.local_position_ok
    lost, now = inputs(gps_fix_type=0, fusing_gps=False)
    report = assess(lost, now)
    assert not report.gnss_ok and report.visual_ok
    stale, now = inputs(flags_time=time.monotonic() - 5)
    report = assess(stale, now)
    assert not report.gnss_ok and not report.visual_ok and not report.ev_position_fused
    no_vision, now = inputs(fusing_ev_pos=False)
    assert assess(no_vision, now).visual_ok is False
    drifting, now = inputs(eph_m=6.0)
    assert assess(drifting, now).visual_ok is False


def test_depth_projects_forward_and_follows_yaw():
    depth = np.full((48, 64), 5.0, dtype=np.float32)
    level = depth_to_enu(depth, 1.5, (0.0, 0.0, -4.0), LEVEL)
    # Facing north (NED x): points lie 5 m north at the flight height. / 朝北：点位于北侧 5 米、飞行高度。
    assert np.median(level[:, 1]) == pytest.approx(5.12, abs=0.05)
    assert np.median(level[:, 2]) == pytest.approx(4.0, abs=0.1)
    east = depth_to_enu(depth, 1.5, (0.0, 0.0, -4.0), yawed(math.pi / 2))
    assert np.median(east[:, 0]) == pytest.approx(5.12, abs=0.05)
    sparse = depth.copy()
    sparse[:, :] = np.inf
    assert depth_to_enu(sparse, 1.5, (0, 0, -4), LEVEL).shape == (0, 3)


def test_voxel_map_expires_and_ignores_the_ground():
    grid = VoxelMap(resolution=0.5, ttl_s=2.0)
    grid.integrate(np.array([[1.0, 1.0, 3.0], [1.0, 1.0, 0.1]]), now=0.0)
    assert len(grid.voxels) == 1
    assert grid.nearest((0, 0, 3), 5.0, 10).shape == (1, 3)
    grid.expire(10.0)
    assert not grid.voxels


def planner_scope():
    bounds = SCENE["bounds"]
    return (bounds["x"][0], bounds["y"][0], 2), (bounds["x"][1], bounds["y"][1], 10)


def test_planner_routes_around_the_registered_wall():
    planner = LocalPlanner(priors=SCENE["obstacles"])
    low, high = planner_scope()
    plan = planner.plan((0, 4, 4), (0, 28, 4), speed=2.0, tolerance=0.8, scope_low=low, scope_high=high,
                        points=np.zeros((0, 3)))
    assert plan.status == "ok" and plan.path_cells > 0
    first = plan.points[0][1]
    # From just south of the wall it must not head straight north into it. / 从墙南侧出发，不能直冲墙体。
    at_wall = planner.plan((0, 12.5, 4), (0, 28, 4), speed=2.0, tolerance=0.8, scope_low=low, scope_high=high,
                           points=np.zeros((0, 3)))
    step = at_wall.points[-1][1]
    assert abs(step[0]) > 0.3 and step[1] - 12.5 < 1.5
    assert first[2] == pytest.approx(4.0)
    assert plan.cycle_ms < 1000


def test_planner_reports_reached_and_no_path_without_retrying():
    planner = LocalPlanner()
    low, high = planner_scope()
    reached = planner.plan((0, 27.6, 4), (0, 28, 4), speed=2.0, tolerance=0.8, scope_low=low, scope_high=high,
                           points=np.zeros((0, 3)))
    assert reached.status == "reached" and reached.points[0][1] == (0.0, 28.0, 4.0)
    ring = np.array([[math.cos(a) * 3, 20 + math.sin(a) * 3, 4.0] for a in np.linspace(0, 2 * math.pi, 120)])
    boxed = planner.plan((0, 0, 4), (0, 20, 4), speed=2.0, tolerance=0.8, scope_low=low, scope_high=high,
                         points=ring)
    assert boxed.status == "no_path" and all(point == (0.0, 0.0, 4.0) for _, point in boxed.points)


def test_planner_avoids_a_sensed_obstacle_it_was_never_told_about():
    planner = LocalPlanner()
    low, high = planner_scope()
    crate = np.array([[x, y, z] for x in np.arange(-1, 1.01, 0.25) for y in np.arange(5, 6.01, 0.25)
                      for z in (3.5, 4.0, 4.5)])
    plan = planner.plan((0, 2, 4), (0, 10, 4), speed=2.0, tolerance=0.8, scope_low=low, scope_high=high, points=crate)
    assert plan.status == "ok"
    assert abs(plan.points[-1][1][0]) > 0.2


def test_detector_grounds_a_green_marker_below_a_level_aircraft():
    rgb = np.zeros((120, 160, 3), dtype=np.uint8)
    rgb[50:70, 70:90] = (0, 200, 0)
    found = detect(rgb, hfov=1.4, position_ned=(28.0, 0.0, -4.0), q_wxyz=LEVEL, eph_m=0.3)
    assert [d.signature for d in found] == ["green"]
    detection = found[0]
    assert detection.position_enu[0] == pytest.approx(0.0, abs=0.3)
    assert detection.position_enu[1] == pytest.approx(28.0, abs=0.3)
    assert detection.sigma_m > 0.3
    assert match_asset(detection, SCENE["assets"]) == "asset_green"
    assert detect(rgb, hfov=1.4, position_ned=(28.0, 0.0, -0.2), q_wxyz=LEVEL, eph_m=0.3) == []
    grey = np.full((120, 160, 3), 128, dtype=np.uint8)
    assert detect(grey, hfov=1.4, position_ned=(0, 0, -4), q_wxyz=LEVEL, eph_m=0.3) == []
