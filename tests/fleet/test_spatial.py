"""X1 spatial alignment (D088): nadir projection, frame moves that never shrink the covariance, point or region.

X1 空间对齐（D088）：下视投影、从不缩小协方差的坐标转换、点或搜索区域。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drone_agent.contracts.common import Frame
from drone_agent.fleet.spatial import (
    SEARCH_REGION,
    TARGET_POINT,
    AlignmentError,
    Camera,
    Estimate,
    MapPlacement,
    decide,
    load_alignment,
    project_nadir,
    to_fact,
    to_frame,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG, DIGEST = load_alignment(ROOT)
CAMERA = Camera(width_px=640, height_px=480, hfov_rad=math.radians(90))
# Every site map names its frame map_enu; maps are told apart by version. / 每个站点地图的坐标系都叫 map_enu，按版本区分。
AIR = Frame(frame_id="map_enu", map_version="site_a_v1")
GROUND = Frame(frame_id="map_enu", map_version="ground_a_v1")
MAPS = {"site_a_v1": MapPlacement(frame_id="map_enu", origin=(0.0, 0.0)),
        "ground_a_v1": MapPlacement(frame_id="map_enu", origin=(5.0, -2.0))}


def project(pixel, *, height=10.0, heading=0.0, cov=None):
    return project_nadir(frame=AIR, capture=(3.0, 4.0, height), position_cov=cov, position_sigma_m=0.5,
                         pixel=pixel, camera=CAMERA, heading_rad=heading, ground_z=0.0, config=CONFIG)


def eigen_min(cov):
    xx, xy, yy = cov
    return (xx + yy) / 2 - math.sqrt(((xx - yy) / 2) ** 2 + xy**2)


def test_the_committed_profile_loads_with_a_digest():
    assert CONFIG.version == "alignment-v1"
    assert len(DIGEST) == 64


def test_the_image_centre_lands_below_the_capture():
    estimate = project((320, 240))
    assert (estimate.x, estimate.y) == pytest.approx((3.0, 4.0))
    assert decide(estimate, "red block", CONFIG).kind == "point"


def test_image_up_is_forward_and_the_heading_turns_it():
    # 120 px above the centre at 10 m with a 320 px focal length is 3.75 m ahead. / 10 m 高、焦距 320 px 时，中心上方
    # 120 px 即前方 3.75 m。
    east = project((320, 120), heading=0.0)
    north = project((320, 120), heading=math.pi / 2)
    assert (east.x - 3.0, east.y - 4.0) == pytest.approx((3.75, 0.0))
    assert (north.x - 3.0, north.y - 4.0) == pytest.approx((0.0, 3.75))
    # Image right is the body's right: with heading east, right is south (-y). / 画面右方是机体右侧：航向朝东时右侧为南。
    right = project((440, 240), heading=0.0)
    assert (right.x - 3.0, right.y - 4.0) == pytest.approx((0.0, -3.75))


def test_off_centre_targets_are_more_uncertain_and_become_regions():
    near, far = project((330, 230)), project((630, 10))
    assert far.semi_major_95() > near.semi_major_95()
    assert decide(near, "red block", CONFIG).kind == "point"
    region = decide(far, "red block", CONFIG)
    assert region.kind == "search_region"
    assert region.radius_m == pytest.approx(far.semi_major_95())


def test_a_missing_covariance_uses_the_nominal_accuracy_and_a_given_one_is_kept():
    given = project((320, 240), cov=(4.0, 0.0, 0.0, 0.0, 4.0, 0.0, 0.0, 0.0, 1.0))
    nominal = project((320, 240))
    assert given.cov[0] > nominal.cov[0] >= 0.25


def test_moving_between_frames_translates_and_never_shrinks():
    estimate = project((400, 200), heading=0.7)
    moved = to_frame(estimate, GROUND, maps=MAPS, config=CONFIG)
    assert (moved.x, moved.y) == pytest.approx((estimate.x - 5.0, estimate.y + 2.0))
    assert moved.frame == GROUND
    assert eigen_min(moved.cov) >= eigen_min(estimate.cov)
    assert moved.semi_major_95() >= estimate.semi_major_95()
    same = to_frame(estimate, AIR, maps=MAPS, config=CONFIG)
    assert same.cov == estimate.cov


@pytest.mark.parametrize(("frame", "maps", "code"), [
    (Frame(frame_id="map_enu", map_version="ground_a_v0"), MAPS, "map_version_mismatch"),
    (Frame(frame_id="ground_enu", map_version="ground_a_v1"), MAPS, "frame_unknown"),
    # The capture's own map was replaced since. / 采集所在的地图此后已被替换。
    (GROUND, {"site_a_v2": MapPlacement(frame_id="map_enu", origin=(0.0, 0.0)), "ground_a_v1": MAPS["ground_a_v1"]},
     "map_version_mismatch"),
])
def test_a_stale_or_unknown_map_refuses_the_move(frame, maps, code):
    with pytest.raises(AlignmentError) as raised:
        to_frame(project((320, 240)), frame, maps=maps, config=CONFIG)
    assert raised.value.code == code


def test_a_target_too_uncertain_to_search_is_refused():
    wide = Estimate(frame=GROUND, x=0.0, y=0.0, cov=(100.0, 0.0, 100.0))
    with pytest.raises(AlignmentError) as raised:
        decide(wide, "red block", CONFIG)
    assert raised.value.code == "alignment_insufficient"


@pytest.mark.parametrize(("height", "pixel", "code"), [
    (0.2, (320, 240), "capture_not_above_ground"),
    (10.0, (700, 240), "pixel_outside_image"),
])
def test_impossible_captures_are_refused(height, pixel, code):
    with pytest.raises(AlignmentError) as raised:
        project(pixel, height=height)
    assert raised.value.code == code


def test_the_fact_carries_frame_covariance_kind_and_profile():
    at = datetime(2026, 10, 14, tzinfo=UTC)
    point = decide(to_frame(project((330, 230)), GROUND, maps=MAPS, config=CONFIG),
                   "red block", CONFIG)
    fact = to_fact(point, fact_id="f1", subject="finding-1", timestamp=at, ground_z=0.0, evidence_refs=["ev-1"],
                   config=CONFIG, config_digest=DIGEST)
    assert fact.predicate == TARGET_POINT and fact.frame == GROUND
    assert fact.position is not None and fact.position.covariance is not None
    assert fact.value["profile_sha256"] == DIGEST and "radius_m" not in fact.value
    region = decide(project((630, 10)), "red block", CONFIG)
    fact = to_fact(region, fact_id="f2", subject="finding-1", timestamp=at, ground_z=0.0, evidence_refs=["ev-1"],
                   config=CONFIG, config_digest=DIGEST)
    assert fact.predicate == SEARCH_REGION and fact.value["radius_m"] >= CONFIG.region_min_radius_m
