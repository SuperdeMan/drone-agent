"""P6 target-region quality (`quality-v2`, D078): heading-free geometry, the check order and the profile contract.

P6 目标区域质量（`quality-v2`，D078）：与航向无关的几何、检查顺序与画像契约。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from drone_agent.eval.p6_vision import render
from drone_agent.fleet.analysis import check_quality, check_target_quality, pixels, target_geometry
from drone_agent.fleet.business_models import (
    RECAPTURABLE,
    BusinessCatalog,
    QualityProfile,
    RecapturePolicy,
    TargetQualityProfile,
    load_catalog,
    load_quality,
)
from drone_agent.fleet.provenance import digest

ROOT = Path(__file__).resolve().parents[2]
PROFILE, _ = load_quality(ROOT, "configs/analysis/quality_target_s0_v1.yaml")
ASSET = {"position": [4, 4, 0], "camera_id": "cam_0", "size_m": 2}


def pose(dx: float = 0.0, dy: float = 0.0, z: float = 4.0) -> dict:
    return {"frame": {"frame_id": "map_enu", "map_version": "campus_v2"},
            "position": {"x": 4 + dx, "y": 4 + dy, "z": z, "covariance": [1, 0, 0, 0, 1, 0, 0, 0, 1]}}


def capture(**effects) -> np.ndarray:
    return pixels(render("red", nonce=7, **effects), 160, 120)


def check(image: np.ndarray, at: dict | None = None, asset: dict | None = None):
    return check_target_quality(image, PROFILE, pose=pose() if at is None else at, asset=asset or ASSET)


def test_a_centred_capture_matches_the_logical_camera():
    geometry = target_geometry(pose(), ASSET, PROFILE, 160, 120)
    assert geometry["side_px"] == pytest.approx(40.0) and geometry["visible_min"] == 1.0
    assert geometry["core_radius_px"] == pytest.approx(20.0) and geometry["offset_px"] == 0.0
    assert geometry["ring_outer_px"] == pytest.approx(20 * math.sqrt(2) + 2, abs=1e-3)


def test_clean_and_damaged_markers_pass_and_keep_their_measures():
    reason, measures = check(capture())
    assert reason is None and measures["target_dark_fraction"] == 0.0 and measures["target_sharpness"] > 30
    reason, measures = check(capture(damaged=True))
    # The damage band darkens about a quarter of the core: a defect for the analyzer, not an exposure problem.
    # 损伤带使约四分之一的核心变暗：这是分析器要判断的缺陷，不是曝光问题。
    assert reason is None and 0.15 < measures["target_dark_fraction"] < 0.35


def test_glare_or_shadow_over_the_target_is_refused_although_the_frame_passes():
    for overlay, key in (("glare", "target_bright_fraction"), ("shadow", "target_dark_fraction")):
        image = capture(overlay=overlay)
        assert check_quality(image, PROFILE)[0] is None
        reason, measures = check(image)
        assert reason == "quality.target_exposure" and measures[key] > 0.6


def test_a_blurred_frame_reports_the_whole_frame_reason_first():
    reason, measures = check(capture(blur=9))
    assert reason == "quality.blurry" and "target_side_px" in measures


def test_a_blurred_target_on_a_sharp_background_is_target_blurry():
    sharp = capture(blur=9).copy()
    # A sharp checker far from the target keeps the whole frame sharp. / 远离目标的清晰棋盘格使整幅画面保持清晰。
    tile = (np.indices((24, 24)).sum(axis=0) // 4 % 2 * 255).astype(np.uint8)
    sharp[:24, :24] = tile[..., None]
    assert check_quality(sharp, PROFILE)[0] is None
    assert check(sharp)[0] == "quality.target_blurry"


def test_drift_off_the_target_leaves_no_certain_pixels_even_with_the_marker_in_view():
    reason, measures = check(capture(offset_m=(1.2, 0.0)), at=pose(dx=1.2))
    assert reason == "quality.target_off_center"
    assert measures["target_visible_min"] == 1.0 and measures["target_core_px"] == 0.0


def test_out_of_frame_is_the_worst_heading_and_size_comes_from_depth():
    reason, measures = check(capture(offset_m=(2.4, 0.0)), at=pose(dx=2.4))
    assert reason == "quality.target_out_of_frame" and measures["target_visible_min"] < 0.95
    reason, measures = check(capture(), at=pose(z=12.0))
    assert reason == "quality.target_too_small" and measures["target_side_px"] < 24


def test_the_worst_visible_fraction_covers_every_heading():
    # 2 m off along x: at the heading that points the offset at the frame's short side a corner leaves the frame,
    # although the long side would still hold it. / 沿 x 偏移 2 m：在把偏移转向画面短边的航向下有一个角出画，
    # 尽管长边方向仍能容纳它。
    geometry = target_geometry(pose(dx=2.0), ASSET, PROFILE, 160, 120)
    assert 0.95 < geometry["visible_min"] < 1.0
    assert target_geometry(pose(dy=2.0), ASSET, PROFILE, 160, 120)["visible_min"] == geometry["visible_min"]
    assert target_geometry(pose(dx=1.5), ASSET, PROFILE, 160, 120)["visible_min"] == 1.0
    assert target_geometry(pose(dx=2.2), ASSET, PROFILE, 160, 120)["visible_min"] == pytest.approx(0.9)


def test_missing_or_mismatching_geometry_is_unknown_never_a_whole_frame_pass():
    image = capture()
    for at, asset in ((None, ASSET), ({"position": {"x": 4, "y": 4}}, ASSET), (pose(z=0.2), ASSET),
                      (pose(), {**ASSET, "camera_id": "cam_9"}), (pose(), {**ASSET, "size_m": None}),
                      (pose(), {"camera_id": "cam_0", "size_m": 2}), (pose(), {**ASSET, "size_m": True})):
        reason, _ = check_target_quality(image, PROFILE, pose=at, asset=asset)
        assert reason == "quality.target_unknown"
    assert check(np.asarray(image[:, :120]).copy())[0] == "quality.target_unknown"


def test_profiles_load_by_format_and_v2_is_validated():
    v1, _ = load_quality(ROOT, "configs/analysis/quality_sim_v1.yaml")
    assert type(v1) is QualityProfile and isinstance(PROFILE, TargetQualityProfile)
    data = yaml.safe_load((ROOT / "configs/analysis/quality_target_s0_v1.yaml").read_text(encoding="utf-8"))
    for broken in ({"hfov_rad": 0}, {"width": 4}, {"mount_below_m": -1}):
        with pytest.raises(ValidationError):
            TargetQualityProfile.model_validate({**data, "camera": {**data["camera"], **broken}})
    with pytest.raises(ValidationError):
        TargetQualityProfile.model_validate({**data, "format": "drone.quality-profile/v1"})


def test_only_reasons_a_new_flight_can_change_are_recapturable():
    assert RecapturePolicy(reasons=RECAPTURABLE).reasons == RECAPTURABLE
    for reason in ("model.timeout", "model.error", "target.mismatch", "quality.target_too_small",
                   "quality.target_unknown", "quality.resolution", "quality.media_mismatch", "analysis.no_reference"):
        with pytest.raises(ValidationError):
            RecapturePolicy(reasons=(reason,))
    with pytest.raises(ValidationError):
        RecapturePolicy(reasons=("quality.blurry", "quality.blurry"))


def test_catalogs_without_a_recapture_policy_keep_their_canonical_form():
    for path in sorted((ROOT / "configs/analysis").glob("p[45]_*.yaml")):
        catalog = load_catalog(path)
        dumped = catalog.model_dump(mode="json")
        assert catalog.recapture is None and "recapture" not in dumped
        assert catalog.sha256 == digest(dumped)
        assert BusinessCatalog.model_validate(dumped) == catalog
