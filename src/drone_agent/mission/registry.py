"""Resolve approved references and fail closed on unsupported manifest predicates.

解析已批准引用；不支持的清单谓词失败关闭。
"""

from __future__ import annotations

import math
from pathlib import Path

import jsonschema
import yaml

from drone_agent.contracts import CapabilityDescriptor, FlightObservation, MissionPackage, SkillManifest, utcnow
from drone_agent.runtime.ledger import content_hash


def distance(a, b):
    return math.dist(a, b)


def coordinates(observation: FlightObservation):
    if observation.pose is None:
        return None
    p = observation.pose.position
    return [p.x, p.y, p.z]


class Registry:
    def __init__(self, root: Path, scene: Path | None = None):
        self.data = yaml.safe_load((scene or root / "configs/scenarios/m1_campus_v1.yaml").read_text(encoding="utf-8"))
        self.sha256 = content_hash(self.data)
        self.manifests = {}
        for path in (root / "configs/skills").glob("*.yaml"):
            manifest = SkillManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            self.manifests[manifest.skill_id] = manifest
        self.capability = CapabilityDescriptor.model_validate(
            yaml.safe_load((root / "configs/platforms/px4_sitl_multirotor.yaml").read_text(encoding="utf-8"))[
                "capability"
            ]
        )

    def inside(self, point):
        return len(point) == 3 and all(
            math.isfinite(v) and self.data["bounds"][axis][0] <= v <= self.data["bounds"][axis][1]
            for axis, v in zip("xyz", point, strict=True)
        )

    def route(self, route_id):
        points = self.data["routes"][route_id]
        if not points or not all(self.inside(point) for point in points):
            raise ValueError("route outside approved volume")
        return points

    def validate_package(self, package: MissionPackage, *, camera_available=True):
        if not package.is_authorized(robot_id=self.capability.robot_id, now=utcnow()):
            raise ValueError("package not authorized")
        if package.spatial_scope.approved_volume_id != self.data["approved_volume_id"]:
            raise ValueError("unregistered volume")
        if package.spatial_scope.frame.model_dump(exclude={"schema_version"}) != self.data["frame"]:
            raise ValueError("frame or map mismatch")
        if package.recovery_policy_ref != self.data["simulation"]["policy_ref"]:
            raise ValueError("policy mismatch")
        for node in package.nodes:
            manifest = self.manifests[node.skill_id]
            if node.robot_id != self.capability.robot_id or node.skill_version != manifest.version:
                raise ValueError("robot or skill version mismatch")
            if node.resources != manifest.resources or not 0 < node.timeout_s <= manifest.timeout_s:
                raise ValueError("resource or timeout mismatch")
            if self.capability.missing_for(skills=[node.skill_id], control_modes=manifest.required_control_modes):
                raise ValueError("unsupported capability")
            jsonschema.Draft202012Validator(manifest.params_schema).validate(node.params)
            action = node.skill_id.rsplit(".", 1)[1]
            p = node.params
            if action == "takeoff" and not self.inside([0, 0, p["altitude_m_agl"]]):
                raise ValueError("takeoff outside scope")
            if action == "fly_route":
                self.route(p["route_id"])
                if {k: p[k] for k in ("frame_id", "map_version")} != self.data["frame"]:
                    raise ValueError("route frame mismatch")
            if action == "return_home":
                route = self.route(p["return_route_id"])
                if p["home_ref"] != self.data["home"]["id"] or route[-1] != self.data["home"]["position"]:
                    raise ValueError("return route does not end at approved home")
            if action == "land":
                site = self.data["landing_sites"][p["landing_site_id"]]
                if site["reserved_for"] != node.robot_id:
                    raise ValueError("landing reservation mismatch")
            if action == "capture_image":
                if not camera_available or p["asset_id"] not in self.data["assets"]:
                    raise ValueError("camera or asset unavailable")

    def predicates(self, node, observation, package, *, lease_valid, heartbeat_ok, camera_available):
        now, point = utcnow(), coordinates(observation)
        fresh = observation.timestamp <= now < observation.valid_until and point is not None
        energy = observation.battery_fraction
        site = self.data["landing_sites"].get(node.params.get("landing_site_id"), {})
        state = {
            "package_authorized": package.is_authorized(robot_id=node.robot_id, now=now),
            "on_ground": observation.in_air is False and observation.armed is False,
            "airborne": observation.in_air is True,
            "localization_healthy": observation.localization_healthy,
            "home_verified": observation.home_healthy,
            "energy_budget_feasible": energy is not None
            and energy >= (package.energy_budget.max_consumption_fraction + package.energy_budget.reserve_fraction),
            "energy_above_reserve": energy is not None and energy > package.energy_budget.reserve_fraction,
            "motion_lease_valid": lease_valid,
            "camera_lease_valid": lease_valid,
            "lease_valid": lease_valid,
            "executive_heartbeat_ok": heartbeat_ok,
            "inside_approved_volume": point is not None and self.inside(point),
            "observation_fresh": fresh,
            "fc_failsafe_inactive": not observation.fc_failsafe,
            "route_resolved_and_in_scope": node.params.get("route_id") in self.data["routes"],
            "return_route_reachable": node.params.get("return_route_id") in self.data["routes"]
            and observation.home_healthy,
            "camera_available": camera_available,
            "asset_resolved": node.params.get("asset_id") in self.data["assets"],
            "observation_pose_valid": fresh and observation.pose.position.covariance is not None,
            "landing_site_verified_and_reserved": site.get("reserved_for") == node.robot_id,
            "above_landing_site": bool(
                point and site and distance(point[:2], site["position"][:2]) <= site["radius_m"]
            ),
        }
        return state

    def require_preconditions(self, node, observation, package, **context):
        predicates = self.predicates(node, observation, package, **context)
        missing = [name for name in self.manifests[node.skill_id].preconditions if not predicates.get(name, False)]
        if missing:
            raise ValueError("preconditions: " + ",".join(missing))
