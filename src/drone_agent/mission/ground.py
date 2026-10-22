"""X1 ground registry (D088): the onboard map of a ground robot and the ground versions of the registry's checks.

A ground site scene (`kind: ground_site`) holds the robot's own drivable map, its parking spot, camera, thresholds and
supervision, bound to a content hash like the aerial site maps. The registry answers the same questions the aerial one
does, in ground terms:
- the fence is 2D: a point is inside when a drivable polygon holds it and no obstacle or no-go zone does;
- the safe terminal is `stopped_disarmed`: a fresh observation, disarmed and stationary. `in_air` is never read: the
  measured PX4 rover reports it true once armed (docs/verification/x1-rover-probe-2026-10-14);
- a package is re-checked against the ground skeleton (arm first, then stop and disarm last), every path leg is
  clear on the robot's own grid, and a path ends where its skill says;
- its predicate vocabulary is the ground one; an aerial predicate a ground manifest might name is missing, so false.
Nothing here is guessed: a missing reference or an unknown skill fails, never defaults.

X1 地面登记表（D088）：地面车的机载地图，以及登记表各项检查的地面版本。

地面站点场景（`kind: ground_site`）保存地面车自己的可行驶地图、停车位、相机、阈值与监督参数，与空中站点地图一样绑定内容
哈希。本登记表回答与空中登记表相同的问题，只是用地面的方式：
- 围栏是二维的：某个可行驶多边形包含该点、且没有障碍或禁入区包含它时才算在内；
- 安全终态是 `stopped_disarmed`：观测新鲜、已上锁、静止。从不读 `in_air`：实测 PX4 地面车解锁后就报告为 true
  （docs/verification/x1-rover-probe-2026-10-14）；
- 任务包按地面骨架复核（arm 在首，stop 与 disarm 在末），每段路径都在地面车自己的网格上畅通，路径终点与技能声明一致；
- 谓词词表是地面的；地面技能清单若写了空中谓词，该谓词缺失即为假。
这里不做任何猜测：缺少引用或未知技能一律失败，绝不取默认值。
"""

from __future__ import annotations

import math
from pathlib import Path

import jsonschema
import yaml

from drone_agent.contracts import CapabilityDescriptor, Embodiment, MissionPackage, SkillManifest, utcnow
from drone_agent.mission.reachability import Grid, Reachability, assess, ground_map_of, inside_polygon
from drone_agent.mission.registry import Registry, coordinates, distance
from drone_agent.runtime.ledger import content_hash

GROUND_SITE = "ground_site"
GROUND_PLATFORM = "configs/platforms/px4_sitl_rover.yaml"
GROUND_SKILLS = "configs/skills/ground"
ARM, NAV_TO, RECHECK = "skill.ground.arm", "skill.ground.nav_to", "skill.ground.recheck"
RETURN_HOME, STOP, DISARM = "skill.ground.return_home", "skill.ground.stop", "skill.ground.disarm"
SKELETON = (ARM, STOP, DISARM)


def speed(observation) -> float | None:
    velocity = observation.velocity_enu_mps
    return math.hypot(velocity[0], velocity[1]) if velocity else None


class GroundRegistry(Registry):
    """The onboard map of a ground robot; the generic manifest and energy methods come from `Registry`.

    地面车的机载地图；通用的清单与能耗方法沿用 `Registry`。
    """

    kind = GROUND_SITE
    safe_terminal = "stopped_disarmed"
    # PX4 never disarms a rover resting in Hold, so a terminal recovery ends with the guardian's stationary disarm.
    # PX4 从不为停在 Hold 中的地面车上锁，因此结束任务的恢复以 guardian 的静止上锁收尾。
    terminal_disarm = True

    def __init__(self, root: Path, scene: Path, platform: Path | None = None):  # noqa: D107 - documented on the class
        self.data = yaml.safe_load(scene.read_text(encoding="utf-8"))
        if self.data.get("kind") != GROUND_SITE:
            raise ValueError(f"{scene.name} is not a ground site")
        self.sha256 = content_hash(self.data)
        self.registry_id = self.data["registry_id"]
        self.map = ground_map_of(self.data)
        self.grid = Grid(self.map)
        self.manifests = {}
        for path in sorted((root / GROUND_SKILLS).glob("*.yaml")):
            manifest = SkillManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            self.manifests[manifest.skill_id] = manifest
        description = yaml.safe_load((platform or root / GROUND_PLATFORM).read_text(encoding="utf-8"))
        self.capability = CapabilityDescriptor.model_validate(description["capability"])
        if self.capability.embodiment is not Embodiment.GROUND_WHEELED:
            raise ValueError("a ground site needs a ground_wheeled platform")
        self.optional_control_modes = set()

    # ── geometry and state / 几何与状态 ──

    @property
    def thresholds(self) -> dict:
        return self.data["thresholds"]

    def parking(self, parking_id: str | None = None) -> tuple[float, float]:
        spots = self.map.ground.parking
        spot = spots.get(parking_id or self.data["mission_defaults"]["parking_id"])
        if spot is None:
            raise ValueError("unregistered parking spot")
        return spot.position

    def inside(self, point) -> bool:
        """The 2D fence: inside a drivable polygon and outside every obstacle and no-go zone.

        二维围栏：在某个可行驶多边形内，且在所有障碍与禁入区之外。
        """
        if point is None or len(point) < 2 or not all(math.isfinite(v) for v in point[:2]):
            return False
        flat = (float(point[0]), float(point[1]))
        section = self.map.ground
        return any(inside_polygon(flat, p) for p in section.drivable) and not any(
            inside_polygon(flat, p) for p in (*section.obstacles, *section.no_go))

    def fresh(self, observation) -> bool:
        now = utcnow()
        return observation.timestamp <= now < observation.valid_until and coordinates(observation) is not None

    def stationary(self, observation) -> bool:
        value = speed(observation)
        return self.fresh(observation) and value is not None and value <= self.thresholds["stationary_mps"]

    def at_safe_terminal(self, observation) -> bool:
        """`stopped_disarmed`, judged by speed and the armed flag only. / 只按速度与解锁状态判定 `stopped_disarmed`。"""
        return observation.armed is False and self.stationary(observation)

    def ground_phase(self, observation) -> str:
        """parked | moving | stopped | unknown, for `RobotStatus.ground_phase`. / 供 `RobotStatus.ground_phase` 使用。"""
        value = speed(observation)
        if not self.fresh(observation) or observation.armed is None or value is None:
            return "unknown"
        if observation.armed or value > self.thresholds["stationary_mps"]:
            return "moving"
        return "parked" if self.resting(observation) else "stopped"

    def resting(self, observation) -> bool:
        """Disarmed, still and at a registered parking spot, whatever the observation's age: the robot's last known
        state, never a fresh fact. / 已上锁、静止且位于登记停车位，不论观测新旧：这是地面车最后已知的状态，不是新鲜事实。
        """
        value, point = speed(observation), coordinates(observation)
        return (observation.armed is False and value is not None and value <= self.thresholds["stationary_mps"]
                and point is not None and any(distance(point[:2], spot.position) <= self.thresholds["parking_radius_m"]
                                              for spot in self.map.ground.parking.values()))

    # ── handoff answers / 交接作答 ──

    def viewing_band(self, radius_m: float | None) -> tuple[float, float] | None:
        """The viewing distance band: the map's standoff for a point (None); for a search region far enough that the
        camera's field of view spans it, with one metre of slack. / 观察距离区间：目标点用地图的观察距离（None）；搜索
        区域则要远到相机视场能覆盖整个区域，另留一米余量。"""
        if radius_m is None:
            return None
        near = max(self.map.ground.standoff_m[0],
                   radius_m / math.tan(math.radians(self.data["camera"]["hfov_deg"]) / 2))
        return near, near + 1.0

    def answer(self, target, radius_m: float | None = None) -> Reachability:
        """The robot's own answer from its parking spot, with a face leg; the compiler computes the same one.

        地面车从停车位出发、带正对段的自身回答；编译器求得的是同一个。
        """
        return assess(self.grid, position=self.parking(), target=(float(target[0]), float(target[1])),
                      band=self.viewing_band(radius_m), face_m=self.thresholds["face_m"])

    def path_problem(self, waypoints) -> str | None:
        """Why a path is not drivable on this map, or None. / 路径在本地图上不可行驶的原因；可行驶时为 None。"""
        if not isinstance(waypoints, list) or len(waypoints) < 2:
            return "a path needs at least two points"
        points = [(float(p[0]), float(p[1])) for p in waypoints]
        if not all(self.inside(p) for p in points):
            return "a path point lies outside the drivable area"
        if not all(self.grid.clear(a, b) for a, b in zip(points, points[1:], strict=False)):
            return "a path leg crosses a blocked cell"
        if sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False)) > self.map.ground.max_path_m:
            return "the path is longer than this map allows"
        return None

    # ── package re-check / 任务包复核 ──

    def validate_package(self, package: MissionPackage, *, camera_available=True, control_modes=None):
        """Re-check an approved ground package against this registry; any doubt raises. / 按本登记表复核已批准的地面
        任务包；任何疑点都抛出。"""
        modes = set(control_modes) if control_modes is not None else set(self.capability.control_modes)
        offered = self.capability.model_copy(update={"control_modes": modes})
        if not package.is_authorized(robot_id=self.capability.robot_id, now=utcnow()):
            raise ValueError("package not authorized")
        if package.spatial_scope.approved_volume_id != self.data["approved_volume_id"]:
            raise ValueError("unregistered ground region")
        if package.spatial_scope.frame.model_dump(exclude={"schema_version"}) != self.data["frame"]:
            raise ValueError("frame or map mismatch")
        if package.recovery_policy_ref != self.data["simulation"]["policy_ref"]:
            raise ValueError("policy mismatch")
        skills = [node.skill_id for node in package.nodes]
        if len(skills) < 3 or skills[0] != ARM or skills[-2:] != [STOP, DISARM] \
                or any(skill in SKELETON for skill in skills[1:-2]):
            raise ValueError("not a ground skeleton: arm first, stop and disarm last")
        for previous, node in zip(package.nodes, package.nodes[1:], strict=False):
            if node.depends_on != [previous.task_id]:
                raise ValueError("a ground package runs as one chain")
        for node in package.nodes:
            manifest = self.manifests.get(node.skill_id)
            if manifest is None:
                raise ValueError(f"{node.skill_id} is not a ground skill")
            if node.robot_id != self.capability.robot_id or node.skill_version != manifest.version:
                raise ValueError("robot or skill version mismatch")
            if node.resources != manifest.resources or not 0 < node.timeout_s <= manifest.timeout_s:
                raise ValueError("resource or timeout mismatch")
            if offered.missing_for(skills=[node.skill_id], control_modes=manifest.required_control_modes):
                raise ValueError("unsupported capability")
            jsonschema.Draft202012Validator(manifest.params_schema).validate(node.params)
            self._validate_node(node, camera_available=camera_available)

    def _validate_node(self, node, *, camera_available) -> None:
        p, radius = node.params, self.thresholds["parking_radius_m"]
        if node.skill_id == ARM:
            self.parking(p["parking_id"])
        elif node.skill_id in (NAV_TO, RETURN_HOME):
            if p["map_version"] != self.registry_id:
                raise ValueError("path map mismatch")
            problem = self.path_problem(p["waypoints"])
            if problem:
                raise ValueError(problem)
            if node.skill_id == NAV_TO and list(p["goal"]) != list(p["waypoints"][-1]):
                raise ValueError("the goal is not where the path ends")
            if node.skill_id == RETURN_HOME and distance(p["waypoints"][-1], self.parking(p["parking_id"])) > radius:
                raise ValueError("the return path does not end at the parking spot")
        elif node.skill_id == RECHECK:
            camera = self.data["camera"]
            if not camera_available or p["camera_id"] != camera["camera_id"]:
                raise ValueError("camera unavailable")
            if p["quality_profile_ref"] != f"{self.registry_id}.image":
                raise ValueError("quality profile belongs to another registry")
            # The goal is a cell centre, so the leg may be up to one cell longer. / goal 取单元中心，正对段最多可长一个单元。
            if distance(p["goal"], p["face_point"]) > self.thresholds["face_m"] + self.grid.cell \
                    or not self.grid.clear(tuple(p["goal"]), tuple(p["face_point"])):
                raise ValueError("the face leg is not clear")

    # ── predicates / 谓词 ──

    def predicates(self, node, observation, package, *, lease_valid, heartbeat_ok, camera_available, overrides=None):
        now, point = utcnow(), coordinates(observation)
        fresh = self.fresh(observation)
        energy = observation.battery_fraction
        p = node.params
        remaining = self.remaining_energy_upper(package, node.task_id)
        tolerance = self.thresholds["nav_to"]["tolerance_m"]

        def near(target, limit) -> bool:
            return bool(fresh and target is not None and distance(point[:2], target) <= limit)

        try:
            parking = self.parking(p.get("parking_id"))
        except ValueError:
            parking = None
        waypoints = p.get("waypoints")
        state = {
            "package_authorized": package.is_authorized(robot_id=node.robot_id, now=now),
            "armed": observation.armed is True,
            "disarmed": observation.armed is False,
            "stationary": self.stationary(observation),
            "at_parking": near(parking, self.thresholds["parking_radius_m"]),
            "at_path_start": bool(waypoints) and near(waypoints[0], tolerance),
            "ground_path_valid": p.get("map_version") == self.registry_id and self.path_problem(waypoints) is None,
            "path_ends_at_parking": bool(waypoints) and parking is not None
            and distance(waypoints[-1], parking) <= self.thresholds["parking_radius_m"],
            "at_goal": near(p.get("goal"), tolerance),
            "face_leg_clear": p.get("goal") is not None and p.get("face_point") is not None
            and self.grid.clear(tuple(p["goal"]), tuple(p["face_point"])),
            "localization_healthy": observation.localization_healthy,
            "home_verified": observation.home_healthy,
            "energy_budget_feasible": energy is not None
            and energy >= (package.energy_budget.max_consumption_fraction + package.energy_budget.reserve_fraction),
            "energy_above_reserve": energy is not None and energy > package.energy_budget.reserve_fraction,
            "energy_remaining_plan_feasible": energy is not None and remaining is not None
            and energy >= remaining + package.energy_budget.reserve_fraction,
            "motion_lease_valid": lease_valid,
            "camera_lease_valid": lease_valid,
            "lease_valid": lease_valid,
            "executive_heartbeat_ok": heartbeat_ok,
            "inside_ground_area": point is not None and self.inside(point),
            "observation_fresh": fresh,
            "fc_failsafe_inactive": not observation.fc_failsafe,
            "camera_available": camera_available,
            "observation_pose_valid": fresh and observation.pose.position.covariance is not None,
        }
        # The guardian may only supply facts it owns; unknown keys are ignored. / guardian 只能提供它掌握的事实；未知键忽略。
        for key, value in (overrides or {}).items():
            if key in state:
                state[key] = bool(value)
        return state
