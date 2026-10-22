"""Deterministic physical-effect predicates; missing evidence is unknown.

M2 adds the inspection skill's approach phase (a registered observation route) and image checks keyed to
the asset's registered visual signature; the M1 red-marker check is unchanged. M3 adds goal-based checks for the
external-mode skills: the fresh pose must stay within the registered goal tolerance for the hold duration. X1 (D088)
adds the ground skills, told apart by their `skill.ground.` prefix so the aerial suffix rules never apply to them:
2D waypoints and goals on the robot's own map, a stationary robot for every arrival, and the armed flag and speed for
arm, stop and disarm; a ground view is verified evidence of the place, never a verdict on the anomaly.

确定性物理效果谓词；缺少证据为 unknown。M2 增加巡检技能的接近相位（登记的观察航线）以及按资产登记的
视觉特征判定的影像检查；M1 的红色标记检查保持不变。M3 为外部模式技能增加按目标点的检查：新鲜位姿须在登记目标
容差内保持规定时长。X1（D088）增加地面技能，按 `skill.ground.` 前缀区分，空中的后缀规则从不作用于它们：在地面车自己
的地图上按二维航点与目标判定，每次到达都要求车已静止，arm、stop、disarm 按解锁状态与速度判定；地面画面只证明到过该处并
拍到了有效画面，从不是对异常物的结论。
"""

import hashlib
import math
from datetime import datetime
from pathlib import Path

from drone_agent.contracts import EffectVerdict, Evidence, FlightObservation, utcnow
from drone_agent.mission.registry import coordinates, distance

INSPECT = "skill.inspect.asset"
INSPECT_LOCAL = "skill.inspect.asset_local"
GROUND = "skill.ground."
GROUND_RECHECK = "skill.ground.recheck"
# Ground action -> the threshold it holds for. / 地面动作及其保持时长所用的阈值。
GROUND_HOLD = {"ground_arm": "arm", "ground_nav_to": "nav_to", "ground_approach": "nav_to",
               "ground_return_home": "return_home", "ground_stop": "stop", "ground_disarm": "disarm"}


class EffectVerifier:
    def __init__(self, registry, node, phase: str | None = None):
        self.registry, self.node = registry, node
        self.action = node.skill_id.rsplit(".", 1)[1]
        if node.skill_id == INSPECT:
            # Only the approach is judged from telemetry; the capture is judged from its evidence file.
            # 只有接近相位按遥测判定；拍摄相位按证据文件判定。
            self.action = "approach" if phase == "approach" else "capture_image"
        elif node.skill_id == INSPECT_LOCAL:
            # The approach is judged against the registered observation goal (M3). / 接近相位按登记观察点判定（M3）。
            self.action = "approach_local" if phase == "approach" else "capture_image"
        elif node.skill_id == GROUND_RECHECK:
            # The approach is the short face leg; the capture is judged from its evidence file (X1).
            # 接近相位是正对目标的短程；拍摄相位按证据文件判定（X1）。
            self.action = "ground_approach" if phase == "approach" else "capture_image"
        elif node.skill_id.startswith(GROUND):
            self.action = "ground_" + node.skill_id.removeprefix(GROUND)
        self.since = None
        self.last_sample = -1
        self.waypoint = 0

    def observe(self, obs: FlightObservation, monotonic: float) -> EffectVerdict:
        now, point = utcnow(), coordinates(obs)
        if not obs.timestamp <= now < obs.valid_until or point is None:
            self.since = None
            return EffectVerdict.UNKNOWN
        if obs.sample_id == self.last_sample:
            return EffectVerdict.UNVERIFIED
        self.last_sample = obs.sample_id
        data, params = self.registry.data, self.node.params
        if self.action == "capture_image":
            return EffectVerdict.UNVERIFIED
        if self.action.startswith("ground_"):
            return self._ground(obs, point, monotonic)
        threshold = data["thresholds"]["route" if self.action == "fly_route" else self.action]
        if self.action == "takeoff":
            matched = obs.in_air is True and abs(point[2] - params["altitude_m_agl"]) <= threshold["tolerance_m"]
        elif self.action in {"fly_route", "return_home", "approach"}:
            route_id = params.get("route_id", params.get("return_route_id", params.get("approach_route_id")))
            route = self.registry.route(route_id)
            if self.waypoint < len(route) and distance(point, route[self.waypoint]) <= threshold["tolerance_m"]:
                self.waypoint += 1
            matched = self.waypoint == len(route) and distance(point, route[-1]) <= threshold["tolerance_m"]
        elif self.action in {"goto_local", "approach_local"}:
            goal = self.registry.local_goal(params.get("goal_id", params.get("approach_goal_id")))
            matched = distance(point, goal) <= threshold["tolerance_m"]
        elif self.action == "land":
            site = data["landing_sites"][params["landing_site_id"]]
            matched = (
                obs.in_air is False
                and obs.armed is False
                and abs(point[2]) < threshold["tolerance_m"]
                and distance(point[:2], site["position"][:2]) <= site["radius_m"]
            )
        else:
            return EffectVerdict.UNKNOWN
        if not matched:
            self.since = None
            return EffectVerdict.UNVERIFIED
        if self.since is None:
            self.since = monotonic
        return (
            EffectVerdict.VERIFIED
            if monotonic - self.since >= threshold["hold_duration_s"]
            else EffectVerdict.UNVERIFIED
        )

    def _ground(self, obs: FlightObservation, point, monotonic: float) -> EffectVerdict:
        """X1 ground effects; every arrival also needs a stationary robot. / X1 地面效果；每次到达都要求车已静止。"""
        registry, params = self.registry, self.node.params
        if self.action not in GROUND_HOLD:
            return EffectVerdict.UNKNOWN
        thresholds = registry.data["thresholds"]
        hold = thresholds[GROUND_HOLD[self.action]]["hold_duration_s"]
        stationary = registry.stationary(obs)
        if self.action == "ground_arm":
            matched = obs.armed is True and stationary
        elif self.action == "ground_stop":
            matched = stationary
        elif self.action == "ground_disarm":
            matched = registry.at_safe_terminal(obs)
        elif self.action == "ground_approach":
            matched = stationary and distance(point[:2], params["face_point"]) <= thresholds["nav_to"]["tolerance_m"]
        else:
            tolerance = thresholds[GROUND_HOLD[self.action]]["tolerance_m"]
            path = params["waypoints"]
            if self.waypoint < len(path) and distance(point[:2], path[self.waypoint]) <= tolerance:
                self.waypoint += 1
            end = registry.parking(params["parking_id"]) if self.action == "ground_return_home" else params["goal"]
            matched = (self.waypoint == len(path) and stationary
                       and distance(point[:2], path[-1]) <= tolerance and distance(point[:2], end) <= tolerance)
        if not matched:
            self.since = None
            return EffectVerdict.UNVERIFIED
        if self.since is None:
            self.since = monotonic
        return EffectVerdict.VERIFIED if monotonic - self.since >= hold else EffectVerdict.UNVERIFIED


# A pixel belongs to a signature when that channel exceeds 70 and 1.5x both other channels (M1 rule for red).
# 某通道大于 70 且大于另两通道 1.5 倍时，像素属于该特征（红色沿用 M1 规则）。
_SIGNATURES = ("red", "green", "blue")


def image_quality(raw: bytes, width: int, height: int) -> dict:
    if len(raw) != width * height * 3 or width <= 0 or height <= 0:
        return {}
    red_c, green_c, blue_c = raw[0::3], raw[1::3], raw[2::3]
    red = sum(r > 70 and r > g * 1.5 and r > b * 1.5 for r, g, b in zip(red_c, green_c, blue_c, strict=True))
    green = sum(g > 70 and g > r * 1.5 and g > b * 1.5 for r, g, b in zip(red_c, green_c, blue_c, strict=True))
    blue = sum(b > 70 and b > r * 1.5 and b > g * 1.5 for r, g, b in zip(red_c, green_c, blue_c, strict=True))
    edge = sum(abs(raw[index] - raw[index - 3]) for index in range(3, len(raw)) if (index // 3) % width)
    pixels = width * height
    return {"width": width, "height": height, "red_fraction": red / pixels, "green_fraction": green / pixels,
            "blue_fraction": blue / pixels, "edge_contrast": edge / len(raw)}


def _verify_capture(evidence: dict, root: Path, node, registry, *, fraction_key, low_key, high_key, max_distance):
    try:
        media = (root / evidence["media_ref"]).resolve()
        if not media.is_relative_to(root.resolve()) or not media.is_file():
            return EffectVerdict.UNKNOWN
        raw = media.read_bytes()
        if hashlib.sha256(raw).hexdigest() != evidence["sha256"]:
            return EffectVerdict.REFUTED
        obs = FlightObservation.model_validate(evidence["observation"])
        if node.params["asset_id"] != evidence["asset_id"] or obs.pose is None:
            return EffectVerdict.REFUTED
        captured = datetime.fromisoformat(evidence["capture_timestamp"])
        contract = Evidence.model_validate(evidence["contract"])
        if (
            contract.sha256 != evidence["sha256"]
            or contract.media_ref != evidence["media_ref"]
            or contract.subject_ids != [node.params["asset_id"]]
            or contract.captured_pose != obs.pose
            or contract.time_window.timestamp != captured
            or contract.produced_by_skill_instance != node.task_id
        ):
            return EffectVerdict.REFUTED
        threshold = registry.data["thresholds"]["image"]
        if not obs.timestamp <= captured < obs.valid_until:
            return EffectVerdict.UNKNOWN
        covariance = obs.pose.position.covariance
        if covariance is None or any(not math.isfinite(v) for v in covariance):
            return EffectVerdict.UNKNOWN
        if max(covariance[0], covariance[4], covariance[8]) > threshold["max_position_variance"]:
            return EffectVerdict.UNVERIFIED
        quality = image_quality(raw, evidence["width"], evidence["height"])
        if (
            quality.get("width", 0) < threshold["min_width"]
            or quality.get("height", 0) < threshold["min_height"]
            or quality.get(fraction_key, 0) < threshold[low_key]
            or quality.get(fraction_key, 0) > threshold[high_key]
            or quality.get("edge_contrast", 0) < threshold["min_edge_contrast"]
        ):
            return EffectVerdict.UNVERIFIED
        asset = registry.data["assets"][node.params["asset_id"]]
        if distance(coordinates(obs)[:2], asset["position"][:2]) > max_distance:
            return EffectVerdict.REFUTED
        return EffectVerdict.VERIFIED
    except (KeyError, ValueError, OSError, TypeError):
        return EffectVerdict.UNKNOWN


def verify_image(evidence: dict, root: Path, node, registry) -> EffectVerdict:
    """M1 capture_image check: the red marker profile. / M1 capture_image 检查：红色标记档案。"""
    return _verify_capture(evidence, root, node, registry, fraction_key="red_fraction", low_key="min_red_fraction",
                           high_key="max_red_fraction", max_distance=2)


def verify_ground_view(evidence: dict, root: Path, node, registry) -> EffectVerdict:
    """X1 ground recheck view (D088): a fresh, sharp frame captured at the face point. It proves the place was
    observed, never whether the anomaly is there: relocation is the service's deterministic finding.

    X1 地面复核画面（D088）：在正对点拍到的新鲜、清晰的帧。它证明到过并观察了该处，从不证明异常物在不在：是否重新定位由
    服务的确定性检测判定。
    """
    try:
        media = (root / evidence["media_ref"]).resolve()
        if not media.is_relative_to(root.resolve()) or not media.is_file():
            return EffectVerdict.UNKNOWN
        raw = media.read_bytes()
        if hashlib.sha256(raw).hexdigest() != evidence["sha256"]:
            return EffectVerdict.REFUTED
        obs = FlightObservation.model_validate(evidence["observation"])
        if node.params["asset_id"] != evidence["asset_id"] or obs.pose is None:
            return EffectVerdict.REFUTED
        captured = datetime.fromisoformat(evidence["capture_timestamp"])
        contract = Evidence.model_validate(evidence["contract"])
        if (
            contract.sha256 != evidence["sha256"]
            or contract.media_ref != evidence["media_ref"]
            or contract.subject_ids != [node.params["asset_id"]]
            or contract.captured_pose != obs.pose
            or contract.time_window.timestamp != captured
            or contract.produced_by_skill_instance != node.task_id
        ):
            return EffectVerdict.REFUTED
        threshold = registry.data["thresholds"]["image"]
        if not obs.timestamp <= captured < obs.valid_until:
            return EffectVerdict.UNKNOWN
        covariance = obs.pose.position.covariance
        if covariance is None or any(not math.isfinite(v) for v in covariance):
            return EffectVerdict.UNKNOWN
        if max(covariance[0], covariance[4], covariance[8]) > threshold["max_position_variance"]:
            return EffectVerdict.UNVERIFIED
        quality = image_quality(raw, evidence["width"], evidence["height"])
        if (
            quality.get("width", 0) < threshold["min_width"]
            or quality.get("height", 0) < threshold["min_height"]
            or quality.get("edge_contrast", 0) < threshold["min_edge_contrast"]
        ):
            return EffectVerdict.UNVERIFIED
        limit = registry.data["thresholds"]["nav_to"]["tolerance_m"]
        if distance(coordinates(obs)[:2], node.params["face_point"]) > limit:
            return EffectVerdict.REFUTED
        return EffectVerdict.VERIFIED
    except (KeyError, ValueError, OSError, TypeError):
        return EffectVerdict.UNKNOWN


def verify_asset_image(evidence: dict, root: Path, node, registry) -> EffectVerdict:
    """M2 inspection check keyed to the asset's registered visual signature (D034).

    按资产登记视觉特征的 M2 巡检检查（D034）。
    """
    try:
        asset = registry.data["assets"][node.params["asset_id"]]
        signature = asset["visual_signature"]
        max_distance = registry.data["thresholds"]["above_asset_m"]
    except (KeyError, TypeError):
        return EffectVerdict.UNKNOWN
    if signature not in _SIGNATURES:
        return EffectVerdict.UNKNOWN
    return _verify_capture(evidence, root, node, registry, fraction_key=f"{signature}_fraction",
                           low_key="min_signature_fraction", high_key="max_signature_fraction",
                           max_distance=max_distance)
