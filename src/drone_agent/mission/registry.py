"""Resolve approved references and fail closed on unsupported manifest predicates.

The registry is the onboard map: one flight volume, prevalidated routes, home, landing sites, assets and
thresholds, bound to a content hash. M1 used `m1_campus_v1`; M2 adds `m2_campus_v2` (D034) with a second
asset and per-asset observation routes. Nothing here is guessed: a missing reference, predicate or
estimate is a failure, never a default.

解析已批准引用；不支持的清单谓词失败关闭。

登记表是机载地图：一个飞行体积、预验证航线、home、降落点、资产与阈值，并绑定内容哈希。M1 使用
`m1_campus_v1`；M2 增加 `m2_campus_v2`（D034），多一个资产并为每个资产登记观察航线。这里不做任何猜测：
缺少引用、谓词或估计都是失败，绝不取默认值。
"""

from __future__ import annotations

import math
from pathlib import Path

import jsonschema
import yaml

from drone_agent.contracts import CapabilityDescriptor, FlightObservation, MissionPackage, SkillManifest, utcnow
from drone_agent.runtime.ledger import content_hash

M1_SCENE = "configs/scenarios/m1_campus_v1.yaml"
M2_SCENE = "configs/scenarios/m2_campus_v2.yaml"


def distance(a, b):
    return math.dist(a, b)


def coordinates(observation: FlightObservation):
    if observation.pose is None:
        return None
    p = observation.pose.position
    return [p.x, p.y, p.z]


def descendants(package: MissionPackage, task_id: str) -> list[str]:
    """Task ids that transitively depend on `task_id`, in package order.

    按任务包顺序返回传递依赖于 `task_id` 的任务 ID。
    """
    found: set[str] = {task_id}
    changed = True
    while changed:
        changed = False
        for node in package.nodes:
            if node.task_id not in found and any(dep in found for dep in node.depends_on):
                found.add(node.task_id)
                changed = True
    return [n.task_id for n in package.nodes if n.task_id in found and n.task_id != task_id]


class Registry:
    def __init__(self, root: Path, scene: Path | None = None):
        self.data = yaml.safe_load((scene or root / M1_SCENE).read_text(encoding="utf-8"))
        self.sha256 = content_hash(self.data)
        self.registry_id = self.data.get("registry_id", "")
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

    def remaining_energy_upper(self, package: MissionPackage, task_id: str) -> float | None:
        """Conservative energy for `task_id` and everything after it; None if any estimate is missing.

        `task_id` 及其所有后继的保守能耗；任一估计缺失时返回 None。
        """
        total = 0.0
        for step in [task_id, *descendants(package, task_id)]:
            node = next(n for n in package.nodes if n.task_id == step)
            estimate = self.manifests[node.skill_id].estimated_energy_fraction
            if estimate is None:
                return None
            total += estimate
        return total

    def _validate_inspect(self, node, *, camera_available):
        p = node.params
        asset = self.data["assets"].get(p["asset_id"])
        if asset is None or not camera_available:
            raise ValueError("camera or asset unavailable")
        if p["approach_route_id"] != asset.get("observation_route") or p["camera_id"] != asset.get("camera_id"):
            raise ValueError("inspection is not bound to the asset's registered route and camera")
        if p["quality_profile_ref"] != f"{self.registry_id}.image":
            raise ValueError("quality profile belongs to another registry")
        final = self.route(p["approach_route_id"])[-1]
        if distance(final[:2], asset["position"][:2]) > self.data["thresholds"]["above_asset_m"]:
            raise ValueError("observation route does not end above the asset")

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
            if node.skill_id == "skill.inspect.asset":
                self._validate_inspect(node, camera_available=camera_available)

    def predicates(self, node, observation, package, *, lease_valid, heartbeat_ok, camera_available):
        now, point = utcnow(), coordinates(observation)
        fresh = observation.timestamp <= now < observation.valid_until and point is not None
        energy = observation.battery_fraction
        site = self.data["landing_sites"].get(node.params.get("landing_site_id"), {})
        asset = self.data["assets"].get(node.params.get("asset_id"), {})
        approach = node.params.get("approach_route_id")
        remaining = self.remaining_energy_upper(package, node.task_id)
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
            # M2 inspection predicates (D034). / M2 巡检谓词（D034）。
            "approach_route_resolved_and_in_scope": bool(
                approach in self.data["routes"]
                and asset.get("observation_route") == approach
                and all(self.inside(waypoint) for waypoint in self.data["routes"][approach])
            ),
            "above_asset": bool(
                fresh and asset and "above_asset_m" in self.data["thresholds"]
                and distance(point[:2], asset["position"][:2]) <= self.data["thresholds"]["above_asset_m"]
            ),
            # This step and everything after it, plus the reserve; a missing estimate is unknown, so false.
            # 本步骤及其所有后继加上余量；缺估计即未知，按假处理。
            "energy_remaining_plan_feasible": energy is not None and remaining is not None
            and energy >= remaining + package.energy_budget.reserve_fraction,
        }
        return state

    def required_predicates(self, node, phase: str | None = None) -> list[str]:
        """The preconditions for this intent: the declared phase's, or the skill's for single-phase skills.

        本次意图的前置条件：多相位技能取所声明相位的，单相位技能取技能的。
        """
        manifest = self.manifests[node.skill_id]
        if manifest.intent_phases:
            declared = manifest.phase(phase)
            if declared is None:
                raise ValueError("undeclared intent phase")
            return declared.preconditions
        if phase is not None:
            raise ValueError("this skill declares no intent phases")
        return manifest.preconditions

    def require_preconditions(self, node, observation, package, phase: str | None = None, **context):
        predicates = self.predicates(node, observation, package, **context)
        missing = [name for name in self.required_predicates(node, phase) if not predicates.get(name, False)]
        if missing:
            raise ValueError("preconditions: " + ",".join(missing))
