"""Deterministic physical-effect predicates; missing evidence is unknown.

确定性物理效果谓词；缺少证据为 unknown。
"""

import hashlib
import math
from pathlib import Path

from drone_agent.contracts import EffectVerdict, FlightObservation, utcnow
from drone_agent.mission.registry import coordinates, distance


class EffectVerifier:
    def __init__(self, registry, node):
        self.registry, self.node = registry, node
        self.action = node.skill_id.rsplit(".", 1)[1]
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
        threshold = data["thresholds"]["route" if self.action == "fly_route" else self.action]
        if self.action == "takeoff":
            matched = obs.in_air is True and abs(point[2] - params["altitude_m_agl"]) <= threshold["tolerance_m"]
        elif self.action in {"fly_route", "return_home"}:
            route = self.registry.route(params.get("route_id", params.get("return_route_id")))
            if self.waypoint < len(route) and distance(point, route[self.waypoint]) <= threshold["tolerance_m"]:
                self.waypoint += 1
            matched = self.waypoint == len(route) and distance(point, route[-1]) <= threshold["tolerance_m"]
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


def image_quality(raw: bytes, width: int, height: int) -> dict:
    if len(raw) != width * height * 3 or width <= 0 or height <= 0:
        return {}
    red = sum(r > 70 and r > g * 1.5 and r > b * 1.5 for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3], strict=True))
    return {"width": width, "height": height, "red_fraction": red / (width * height)}


def verify_image(evidence: dict, root: Path, node, registry) -> EffectVerdict:
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
        stamp = evidence["capture_timestamp"]
        from datetime import datetime

        captured = datetime.fromisoformat(stamp)
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
            or quality.get("red_fraction", 0) < threshold["min_red_fraction"]
        ):
            return EffectVerdict.UNVERIFIED
        asset = registry.data["assets"][node.params["asset_id"]]
        if distance(coordinates(obs)[:2], asset["position"][:2]) > 2:
            return EffectVerdict.REFUTED
        return EffectVerdict.VERIFIED
    except (KeyError, ValueError, OSError, TypeError):
        return EffectVerdict.UNKNOWN
