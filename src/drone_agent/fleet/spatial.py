"""X1 spatial alignment (D088): from an aerial capture to the ground target a receiving robot is offered.

An aerial annotation is the image centroid of an anomaly in one frame of the downward camera. Evidence carries a
position but no attitude, so the centroid is projected to flat ground under a nadir camera and turned by the heading
the flight declares (the bearing of its route leg); every unknown only widens the covariance: the position
covariance, the heading and tilt bounds of the frozen profile, the centroid error and the height. The estimate then
moves into the receiver's map frame through the shared frame, translation only as in P3, and a move never shrinks the
covariance. A target whose 95 % ellipse is small enough is offered as a point with its covariance; otherwise it is a
search region (centre, radius, description) the receiver re-localizes in; a target too uncertain even for that is
refused, and so is a frame whose map version is not the one the receiver checks against. The receiver never takes
the aerial coordinates as a goal without this step.

X1 空间对齐（D088）：从空中采集得到提供给接收方的地面目标。

空中标注是下视相机一帧影像中异常物的图像质心。证据带位置、不带姿态，因此在平地与下视假设下把质心投影到地面，再按飞行
声明的航向（航段方位）旋转；每一项未知都只放大协方差：位置协方差、冻结画像中的航向与倾斜上限、质心误差与高度。估计随后
经共享坐标转换到接收方的地图坐标，与 P3 一样只有平移，且转换从不缩小协方差。95% 置信椭圆足够小的目标以「点 + 协方差」
提供；否则提供「搜索区域」（中心、半径、描述），由接收方在其中重新定位；连搜索都嫌不确定的目标被拒绝，地图版本与接收方
核对版本不一致的坐标系同样被拒绝。接收方不经这一步，绝不把空中坐标当作目标点。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ConfigDict, Field

from drone_agent.contracts.common import ContractModel, FactSource, Frame, Position, WorldKind
from drone_agent.contracts.world import WorldFact

ALIGNMENT_CONFIG = "configs/analysis/alignment_v1.yaml"
CHI2_95_2D = 5.991  # 95 % quantile of chi-square with two degrees of freedom / 二自由度卡方分布的 95% 分位数
MIN_HEIGHT_M = 0.5  # a capture this close to the ground is no aerial view / 离地这么近的采集不算空中视角
TARGET_POINT = "x1.target_point"
SEARCH_REGION = "x1.search_region"
Covariance = tuple[float, float, float]  # (xx, xy, yy) of a 2x2 covariance / 2x2 协方差的 (xx, xy, yy)


class AlignmentError(ValueError):
    """A target that cannot be offered; `code` is the controlled reason. / 无法提供的目标；`code` 是受控原因码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class AlignmentConfig(ContractModel):
    """`alignment-v1` bounds and thresholds (design §6). / `alignment-v1` 的上限与阈值（设计 §6）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["drone.alignment/v1"]
    version: str = Field(pattern=r"^alignment-v[0-9]+$")
    heading_sigma_deg: float = Field(gt=0, le=90)
    tilt_sigma_deg: float = Field(gt=0, le=45)
    pixel_sigma_px: float = Field(gt=0, le=20)
    frame_sigma_m: float = Field(ge=0, le=10)
    point_max_semi_major_m: float = Field(gt=0, le=20)
    region_min_radius_m: float = Field(gt=0, le=50)
    region_max_radius_m: float = Field(gt=0, le=200)


def load_alignment(root: Path) -> tuple[AlignmentConfig, str]:
    """The profile and the digest of its exact bytes. / 画像及其原始字节的摘要。"""
    raw = (root / ALIGNMENT_CONFIG).read_bytes()
    return AlignmentConfig.model_validate(yaml.safe_load(raw)), hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Camera:
    """Pinhole camera with square pixels; image up is the body's forward axis and image right its right side.

    方形像素的针孔相机；画面上方是机体前向，画面右方是机体右侧。
    """

    width_px: int
    height_px: int
    hfov_rad: float

    @property
    def focal_px(self) -> float:
        return self.width_px / 2 / math.tan(self.hfov_rad / 2)


@dataclass(frozen=True)
class MapPlacement:
    """A map in force: its frame id and where its origin lies in the shared frame (translation only).

    Every site map names its frame `map_enu`, so a map is identified by its version (registry id), not its frame id.

    在用的地图：其坐标系 id 与原点在共享坐标中的位置（仅平移）。每个站点地图的坐标系都叫 `map_enu`，因此按地图版本
    （登记表 id）而不是坐标系 id 识别地图。
    """

    frame_id: str
    origin: tuple[float, float]


@dataclass(frozen=True)
class Estimate:
    """A ground position in one map frame with its 2x2 covariance. / 某地图坐标系中的地面位置及其 2x2 协方差。"""

    frame: Frame
    x: float
    y: float
    cov: Covariance

    def semi_major_95(self) -> float:
        """Semi-major axis of the 95 % ellipse. / 95% 置信椭圆的长半轴。"""
        xx, xy, yy = self.cov
        largest = (xx + yy) / 2 + math.sqrt(((xx - yy) / 2) ** 2 + xy**2)
        return math.sqrt(CHI2_95_2D * max(largest, 0.0))


@dataclass(frozen=True)
class Target:
    """What a receiver is offered: a point with its covariance, or a search region. / 提供给接收方的目标：点或搜索区域。"""

    kind: Literal["point", "search_region"]
    estimate: Estimate
    radius_m: float | None
    description: str


def _grow(cov: Covariance, xx: float, xy: float, yy: float) -> Covariance:
    return (cov[0] + xx, cov[1] + xy, cov[2] + yy)


def project_nadir(*, frame: Frame, capture: Sequence[float], position_cov: Sequence[float] | None,
                  position_sigma_m: float, pixel: Sequence[float], camera: Camera, heading_rad: float,
                  ground_z: float, config: AlignmentConfig) -> Estimate:
    """Project an image centroid of a nadir capture to flat ground, widening the covariance for every unknown.

    `position_cov` is the row-major 3x3 covariance of the capture position; without one the platform's nominal
    accuracy `position_sigma_m` stands for every axis. `heading_rad` is the declared ENU yaw (from +x towards +y).

    把下视采集中的图像质心投影到平地，每一项未知都放大协方差。`position_cov` 是采集位置的行优先 3x3 协方差；缺失时每一轴都
    用平台标称精度 `position_sigma_m`。`heading_rad` 是声明的 ENU 偏航角（自 +x 向 +y 旋转）。
    """
    height = capture[2] - ground_z
    if not height >= MIN_HEIGHT_M:
        raise AlignmentError("capture_not_above_ground", f"height {height:.2f} m")
    u, v = pixel
    if not (0 <= u <= camera.width_px and 0 <= v <= camera.height_px):
        raise AlignmentError("pixel_outside_image", f"({u}, {v})")
    focal = camera.focal_px
    du, dv = u - camera.width_px / 2, v - camera.height_px / 2
    forward, left = -dv * height / focal, -du * height / focal
    c, s = math.cos(heading_rad), math.sin(heading_rad)
    ox, oy = forward * c - left * s, forward * s + left * c
    if position_cov is not None:
        cov: Covariance = (position_cov[0], position_cov[1], position_cov[4])
        var_z = position_cov[8]
    else:
        var_z = position_sigma_m**2
        cov = (var_z, 0.0, var_z)
    # A height error scales the whole ground offset. / 高度误差按比例缩放整个地面偏移。
    cov = _grow(cov, var_z * (ox / height) ** 2, var_z * ox * oy / height**2, var_z * (oy / height) ** 2)
    # A heading error turns the offset about the capture point. / 航向误差使偏移绕采集点旋转。
    heading_var = math.radians(config.heading_sigma_deg) ** 2
    cov = _grow(cov, heading_var * oy**2, -heading_var * ox * oy, heading_var * ox**2)
    # Tilt and centroid errors grow off nadir (the ray meets the ground at a flatter angle); applied to both axes.
    # 倾斜与质心误差偏离下视越远越大（射线与地面夹角更小）；两轴都加。
    off_nadir = math.atan2(math.hypot(ox, oy), height)
    tilt = height / math.cos(off_nadir) ** 2 * math.radians(config.tilt_sigma_deg)
    centroid = height / focal / math.cos(off_nadir) ** 2 * config.pixel_sigma_px
    cov = _grow(cov, tilt**2 + centroid**2, 0.0, tilt**2 + centroid**2)
    return Estimate(frame=frame, x=capture[0] + ox, y=capture[1] + oy, cov=cov)


def to_frame(estimate: Estimate, target: Frame, *, maps: Mapping[str, MapPlacement],
             config: AlignmentConfig) -> Estimate:
    """Move an estimate into the receiver's frame through the shared frame; the covariance never shrinks.

    `maps` holds the maps in force by version. A frame on a map no longer in force refuses the move instead of
    guessing, and so does a frame id the map does not declare.

    经共享坐标把估计转换到接收方的坐标系；协方差从不缩小。`maps` 按版本列出在用的地图。坐标系所在的地图已不在用时拒绝转换，
    而不是猜测；坐标系 id 与地图声明的不一致时同样拒绝。
    """
    for frame in (estimate.frame, target):
        placement = maps.get(frame.map_version)
        if placement is None:
            raise AlignmentError("map_version_mismatch", f"{frame.map_version} is not a map in force")
        if placement.frame_id != frame.frame_id:
            raise AlignmentError("frame_unknown", f"{frame.map_version} declares {placement.frame_id}, "
                                                  f"not {frame.frame_id}")
    source, goal = maps[estimate.frame.map_version].origin, maps[target.map_version].origin
    cov = estimate.cov
    if estimate.frame != target:
        cov = _grow(cov, config.frame_sigma_m**2, 0.0, config.frame_sigma_m**2)
    return Estimate(frame=target, x=estimate.x + source[0] - goal[0], y=estimate.y + source[1] - goal[1], cov=cov)


def decide(estimate: Estimate, description: str, config: AlignmentConfig) -> Target:
    """Offer a point when the 95 % ellipse is small enough, else a search region; refuse beyond the maximum.

    95% 置信椭圆足够小时提供点，否则提供搜索区域；超过上限即拒绝。
    """
    semi_major = estimate.semi_major_95()
    if semi_major <= config.point_max_semi_major_m:
        return Target(kind="point", estimate=estimate, radius_m=None, description=description)
    radius = max(config.region_min_radius_m, semi_major)
    if radius > config.region_max_radius_m:
        raise AlignmentError("alignment_insufficient", f"95 % semi-major {semi_major:.1f} m")
    return Target(kind="search_region", estimate=estimate, radius_m=radius, description=description)


def to_fact(target: Target, *, fact_id: str, subject: str, timestamp: datetime, ground_z: float,
            evidence_refs: Sequence[str], config: AlignmentConfig, config_digest: str) -> WorldFact:
    """The handoff target as a belief fact in the receiver's frame (no wire change; contracts §13).

    交接目标表示为接收方坐标系中的 belief 事实（不改 wire；契约 §13）。
    """
    xx, xy, yy = target.estimate.cov
    value = {"kind": target.kind, "description": target.description,
             "semi_major_95_m": round(target.estimate.semi_major_95(), 3),
             "profile": config.version, "profile_sha256": config_digest}
    if target.radius_m is not None:
        value["radius_m"] = round(target.radius_m, 3)
    return WorldFact(fact_id=fact_id, subject=subject,
                     predicate=TARGET_POINT if target.kind == "point" else SEARCH_REGION, value=value,
                     frame=target.estimate.frame,
                     position=Position(x=target.estimate.x, y=target.estimate.y, z=ground_z,
                                       covariance=(xx, xy, 0.0, xy, yy, 0.0, 0.0, 0.0, 0.0)),
                     timestamp=timestamp, source=FactSource.SENSOR, world_kind=WorldKind.BELIEF,
                     evidence_refs=list(evidence_refs))
