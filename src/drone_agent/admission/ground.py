"""X1 ground compile and admission (D088): one recheck, its path from the robot's own map, and the ground skeleton.

The compiler owns everything the coordinator must not guess. A ground recheck spec carries exactly one
`skill.ground.recheck` task with the offered target (point or search region, already in the robot's frame). The
compiler asks the robot's own map, from its parking spot, for a viewing goal and a face leg pointing at the target
(the same `assess` the robot answered the offer with), and emits the ground skeleton: arm -> nav_to -> recheck ->
return_home (the outbound path reversed) -> stop -> disarm, every parameter written into the package so the approver
sees it and the hash covers it. When the robot's reply is given, a map version or path length that disagrees is a
compile issue rather than a silent replan. Admission reuses the generic capability, parameter, time, energy and
airspace checks and adds the ground spatial check (map, legs, ends) and the ground order (arm first, disarm last,
one chain, no aircraft skill).

X1 地面编译与准入（D088）：一次复核、来自地面车自身地图的路径，以及地面骨架。

编译器负责一切不该由协调器猜测的内容。地面复核规格只带一个 `skill.ground.recheck` 任务及所提供的目标（点或搜索区域，已在
地面车的坐标系中）。编译器从停车位出发，向地面车自己的地图求一个观察位和指向目标的正对段（与地面车回复提议时所用的
`assess` 相同），并生成地面骨架：arm -> nav_to -> recheck -> return_home（去程反向）-> stop -> disarm；每个参数都写进
任务包，审批人看得到，哈希也覆盖它。给出地面车的回复时，地图版本或路径长度不一致是编译问题，而不是静默重规划。准入复用
通用的能力、参数、时间、能源与空域检查，并增加地面空间检查（地图、各段、起止点）与地面顺序检查（arm 在首、disarm 在末、
单链、不含飞行器技能）。
"""

from __future__ import annotations

import math

from drone_agent.admission.admission import AdmissionContext, _capability, _Checks, _energy, _params, _time
from drone_agent.admission.airspace import airspace_issues
from drone_agent.admission.models import AdmissionResult, CompileResult
from drone_agent.contracts import MissionPackage, MissionSpec, PackageNode
from drone_agent.mission.ground import ARM, DISARM, NAV_TO, RECHECK, RETURN_HOME, STOP, GroundRegistry
from drone_agent.mission.reachability import assess
from drone_agent.mission.registry import distance
from drone_agent.runtime.issues import Issue, Severity, issue

ORDER = (ARM, NAV_TO, RECHECK, RETURN_HOME, STOP, DISARM)
TOLERANCE_PATH_M = 0.5  # how far the compiled path may differ from the reply's / 编译路径与回复路径允许的差


def viewing_band(registry: GroundRegistry, radius_m: float | None) -> tuple[float, float] | None:
    """The viewing distance band: the map's standoff for a point; for a search region far enough that the camera's
    field of view spans the region, with one metre of slack. / 观察距离区间：目标点用地图的观察距离；搜索区域则要远到
    相机视场能覆盖整个区域，另留一米余量。"""
    if radius_m is None:
        return None
    near = max(registry.map.ground.standoff_m[0],
               radius_m / math.tan(math.radians(registry.data["camera"]["hfov_deg"]) / 2))
    return near, near + 1.0


def plan(registry: GroundRegistry, target, radius_m: float | None):
    """The robot's own answer from its parking spot, as the compiler and the robot both compute it.

    从停车位出发、地面车自己的回答；编译器与地面车以同样方式求得。
    """
    return assess(registry.grid, position=registry.parking(), target=tuple(target),
                  band=viewing_band(registry, radius_m), face_m=registry.thresholds["face_m"])


def compile_ground(spec: MissionSpec, registry: GroundRegistry, *, robot_id: str,
                   reply: dict | None = None) -> CompileResult:
    """Compile a ground recheck deterministically; any issue means no package. / 确定性地编译地面复核；有任何问题就
    不产出任务包。"""
    policy = registry.data["simulation"]["policy_ref"]
    if spec.recovery_policy_ref != policy:
        return CompileResult(issues=[issue("compile.policy_mismatch",
                                           f"{spec.recovery_policy_ref} differs from the ground site's {policy}")])
    if spec.spatial_scope.approved_volume_id != registry.data["approved_volume_id"] \
            or spec.spatial_scope.frame.model_dump(exclude={"schema_version"}) != registry.data["frame"]:
        return CompileResult(issues=[issue("compile.ground_scope", "the scope is not this ground site's region")])
    if len(spec.tasks) != 1 or spec.tasks[0].skill_id != RECHECK:
        return CompileResult(issues=[issue("compile.ground_shape", "a ground recheck has exactly one recheck task",
                                           affected=[t.task_id for t in spec.tasks])])
    task = spec.tasks[0]
    p = task.params
    try:
        target = [float(p["target"][0]), float(p["target"][1])]
        kind = str(p["target_kind"])
        radius = float(p["radius_m"]) if kind == "search_region" else None
    except (KeyError, TypeError, ValueError, IndexError):
        return CompileResult(issues=[issue("compile.params_invalid", "the recheck target is malformed",
                                           affected=[task.task_id])])
    answer = plan(registry, target, radius)
    if not answer.accepted:
        return CompileResult(issues=[issue("compile.ground_unreachable", f"{answer.reason}: {answer.detail}",
                                           affected=[task.task_id])])
    if reply is not None and (reply.get("map_version") != registry.registry_id
                              or reply.get("path_length_m") is None
                              or abs(float(reply["path_length_m"]) - answer.path_m) > TOLERANCE_PATH_M):
        return CompileResult(issues=[issue("compile.reachability_mismatch",
                                           "the robot's reply and the compiled path disagree", affected=[task.task_id])])
    defaults, manifests = registry.data["mission_defaults"], registry.manifests
    parking_id, version = defaults["parking_id"], registry.registry_id
    outbound = [[round(x, 4), round(y, 4)] for x, y in answer.waypoints]
    goal, face = [round(v, 4) for v in answer.goal], [round(v, 4) for v in answer.face_point]
    params = {
        ARM: {"parking_id": parking_id},
        NAV_TO: {"map_version": version, "parking_id": parking_id, "waypoints": outbound, "goal": goal,
                 "speed_mps": defaults["cruise_speed_mps"]},
        RECHECK: {"asset_id": str(p["asset_id"]), "target": target, "target_kind": kind,
                  "radius_m": radius or 0.0, "signature": str(p["signature"]), "goal": goal, "face_point": face,
                  "camera_id": registry.data["camera"]["camera_id"], "quality_profile_ref": f"{version}.image",
                  "max_captures": defaults["max_captures"], "speed_mps": defaults["approach_speed_mps"]},
        RETURN_HOME: {"map_version": version, "parking_id": parking_id, "waypoints": [face, *outbound[::-1]],
                      "speed_mps": defaults["cruise_speed_mps"]},
        STOP: {}, DISARM: {},
    }
    ids = {ARM: "arm", NAV_TO: "nav_to", RECHECK: task.task_id, RETURN_HOME: "return_home", STOP: "stop",
           DISARM: "disarm"}
    nodes, previous = [], None
    for skill in ORDER:
        manifest = manifests[skill]
        nodes.append(PackageNode(task_id=ids[skill], skill_id=skill, skill_version=manifest.version, robot_id=robot_id,
                                 params=params[skill], depends_on=[previous] if previous else [],
                                 resources=list(manifest.resources), timeout_s=manifest.timeout_s,
                                 completion_evidence=[e.evidence_type for e in manifest.completion_evidence]))
        previous = ids[skill]
    package = MissionPackage(mission_id=spec.mission_id, mission_version=spec.mission_version, nodes=nodes,
                             recovery_policy_ref=policy, spatial_scope=spec.spatial_scope,
                             temporal_window=spec.temporal_window, energy_budget=spec.energy_budget)
    package.package_hash = package.compute_hash()
    return CompileResult(package=package, package_hash=package.package_hash,
                         inserted_framework=[ids[s] for s in ORDER if s != RECHECK], expanded_targets=[])


def _spatial_ground(package: MissionPackage, registry: GroundRegistry) -> list[Issue]:
    found = []
    if package.spatial_scope.approved_volume_id != registry.data["approved_volume_id"]:
        found.append(issue("scope.unregistered_volume", "the package is not scoped to this ground site's region"))
    if package.spatial_scope.frame.model_dump(exclude={"schema_version"}) != registry.data["frame"]:
        found.append(issue("scope.frame_mismatch", "the package frame is not this ground site's"))
    radius = registry.thresholds["parking_radius_m"]
    for node in package.nodes:
        p = node.params
        try:
            if node.skill_id in (NAV_TO, RETURN_HOME):
                problem = registry.path_problem(p["waypoints"])
                if p["map_version"] != registry.registry_id:
                    problem = "the path belongs to another map"
                if problem:
                    found.append(issue("scope.ground_path", problem, affected=[node.task_id]))
                parking = registry.parking(p["parking_id"])
                if node.skill_id == NAV_TO and (distance(p["waypoints"][0], parking) > radius
                                                or list(p["goal"]) != list(p["waypoints"][-1])):
                    found.append(issue("scope.ground_path", "nav_to must start at the parking spot and end at its goal",
                                       affected=[node.task_id]))
                if node.skill_id == RETURN_HOME and distance(p["waypoints"][-1], parking) > radius:
                    found.append(issue("scope.ground_path", "return_home must end at the parking spot",
                                       affected=[node.task_id]))
            elif node.skill_id == RECHECK:
                if distance(p["goal"], p["face_point"]) > registry.thresholds["face_m"] + registry.grid.cell \
                        or not registry.grid.clear(tuple(p["goal"]), tuple(p["face_point"])):
                    found.append(issue("scope.ground_path", "the face leg is not clear", affected=[node.task_id]))
        except (KeyError, TypeError, ValueError, IndexError) as error:
            found.append(issue("scope.ground_path", f"{node.task_id}: {error}"[:300], affected=[node.task_id]))
    return found


def _order_ground(package: MissionPackage) -> list[Issue]:
    skills = [node.skill_id for node in package.nodes]
    if tuple(skills) != ORDER:
        return [issue("compile.invalid_order", "a ground package runs arm, nav_to, recheck, return_home, stop, "
                                               "disarm in that order", affected=[n.task_id for n in package.nodes])]
    for previous, node in zip(package.nodes, package.nodes[1:], strict=False):
        if node.depends_on != [previous.task_id]:
            return [issue("compile.invalid_order", "a ground package runs as one chain", affected=[node.task_id])]
    return []


def admit_ground(package: MissionPackage, ctx: AdmissionContext) -> AdmissionResult:
    """Run every check on a ground package; accepted only when no error remains.

    对地面任务包执行全部检查；只有不存在错误时才接受。
    """
    checks = _Checks()
    computed = package.compute_hash()
    checks.add("hash", [issue("package.hash_mismatch", "the package hash does not match its content")]
               if package.package_hash and package.package_hash != computed else [])
    missing = [name for name in ("spatial_scope", "temporal_window", "energy_budget") if getattr(package, name) is None]
    if missing:
        checks.add("boundaries", [issue("package.boundary_missing", f"missing {missing}", affected=missing)])
        return AdmissionResult(accepted=False, package_hash=computed, issues=checks.issues, checks=checks.records,
                               checked_at=ctx.now)
    checks.add("boundaries", [])
    registry = ctx.registry
    policy = registry.data["simulation"]["policy_ref"]
    checks.add("recovery_policy", [] if package.recovery_policy_ref == policy else [
        issue("policy.recovery_mismatch", f"{package.recovery_policy_ref} is not the ground site policy {policy}")])
    checks.add("capability", _capability(package, ctx))
    checks.add("params", _params(package, ctx))
    checks.add("spatial", _spatial_ground(package, registry))
    checks.add("order_and_resources", _order_ground(package))
    checks.add("time", _time(package, ctx))
    energy_issues, energy_total = _energy(package, ctx)
    checks.add("energy", energy_issues, "" if energy_total is None else f"upper={energy_total}")
    volume_id = package.spatial_scope.approved_volume_id
    status = ctx.airspace.query(volume_id, now=ctx.now)
    window = (package.temporal_window.not_before, package.temporal_window.not_after)
    checks.add("airspace", airspace_issues(volume_id, registry.data.get("volumes", {}).get(volume_id), status,
                                           window=window, registration_id=ctx.registration_id), status.filing_status)
    accepted = not any(i.severity is Severity.ERROR for i in checks.issues)
    return AdmissionResult(accepted=accepted, package_hash=computed, issues=checks.issues, checks=checks.records,
                           requires_human_ack=[], energy_upper_fraction=energy_total,
                           airspace=status.model_dump(mode="json"), checked_at=ctx.now)
