"""Admission: check a compiled package against capability, space, time, energy, airspace, resources,
parameters and approval rules before anyone may approve it (WP-M2-05).

Admission never trusts the compiler or the planner: it recomputes the package hash, re-reads every
reference from the scene registry and the robot's published capability, and treats anything unknown
(missing estimate, undeclared airspace mode, unregistered volume) as a rejection. The result lists every
problem with a controlled code, not just the first one, so the console and the adversarial corpus can
assert the exact reasons.

准入：在任何人能够审批之前，按能力、空间、时间、能源、空域、资源、参数与审批规则检查已编译的任务包
（WP-M2-05）。

准入从不信任编译器或规划器：它重算任务包哈希，从场景登记表与机器人发布的能力描述重新读取每个引用，
并把任何未知（缺估计、空域模式未声明、体积未登记）都当作拒绝。结果列出全部问题且带受控码，而不只是
第一个，便于控制台与对抗语料断言确切原因。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import jsonschema

from drone_agent.admission.airspace import AirspaceConstraintProvider, airspace_issues
from drone_agent.admission.compiler import CAPTURE, FLY_ROUTE, FRAMEWORK, INSPECT, LAND, RETURN_HOME, TAKEOFF
from drone_agent.admission.models import AdmissionResult, CheckRecord, MissionRequest
from drone_agent.contracts import CapabilityDescriptor, MissionPackage, MissionSpec, find_resource_conflicts
from drone_agent.contracts.mission import _check_params
from drone_agent.mission.registry import Registry, descendants, distance
from drone_agent.runtime.issues import Issue, Severity, issue
from drone_agent.runtime.permission import TrustLevel


@dataclass(frozen=True)
class AdmissionContext:
    """Everything admission reads besides the package. / 准入除任务包外读取的全部输入。"""

    registry: Registry
    capability: CapabilityDescriptor
    airspace: AirspaceConstraintProvider
    now: datetime
    request: MissionRequest | None = None


def _inside(bounds: dict, point) -> bool:
    return len(point) == 3 and all(
        isinstance(v, (int, float)) and math.isfinite(v) and bounds[axis][0] <= v <= bounds[axis][1]
        for axis, v in zip("xyz", point, strict=True)
    )


class _Checks:
    def __init__(self):
        self.records: list[CheckRecord] = []
        self.issues: list[Issue] = []

    def add(self, name: str, found: list[Issue], detail: str = "") -> None:
        self.records.append(CheckRecord(check=name, passed=not any(i.severity is Severity.ERROR for i in found),
                                        detail=detail))
        self.issues.extend(found)


def _capability(package: MissionPackage, ctx: AdmissionContext) -> list[Issue]:
    found, caps, manifests = [], ctx.capability, ctx.registry.manifests
    have = {s.skill_id: s.version for s in caps.skills}
    for node in package.nodes:
        if node.robot_id != caps.robot_id:
            found.append(issue("capability.robot_not_allowed", f"{node.task_id} targets {node.robot_id}",
                               affected=[node.task_id, node.robot_id]))
        manifest = manifests.get(node.skill_id)
        if manifest is None:
            found.append(issue("capability.missing_skill", f"{node.skill_id} has no manifest", affected=[node.task_id]))
            continue
        if caps.embodiment not in manifest.embodiments or node.skill_id not in have:
            found.append(issue("capability.missing_skill", f"{caps.robot_id} does not offer {node.skill_id}",
                               affected=[node.task_id, node.skill_id]))
        elif have[node.skill_id] != node.skill_version or manifest.version != node.skill_version:
            found.append(issue("capability.version_mismatch",
                               f"{node.skill_id} {node.skill_version} vs robot {have[node.skill_id]}",
                               affected=[node.task_id]))
        for mode in sorted(manifest.required_control_modes - caps.control_modes):
            found.append(issue("capability.missing_control_mode", f"{node.skill_id} needs {mode.value}",
                               affected=[node.task_id, mode.value]))
        if node.resources != manifest.resources or not 0 < node.timeout_s <= manifest.timeout_s:
            found.append(issue("params.binding_mismatch", f"{node.task_id} resources or timeout differ from the manifest",
                               affected=[node.task_id]))
    return found


def _params(package: MissionPackage, ctx: AdmissionContext) -> list[Issue]:
    found = []
    for node in package.nodes:
        manifest = ctx.registry.manifests.get(node.skill_id)
        if manifest is None:
            continue
        try:
            _check_params(node.params)
        except ValueError as error:
            found.append(issue("params.control_key", str(error), affected=[node.task_id]))
            continue
        errors = sorted(jsonschema.Draft202012Validator(manifest.params_schema).iter_errors(node.params),
                        key=lambda e: list(e.path))
        for error in errors:
            path = ".".join(str(part) for part in error.path) or "(params)"
            found.append(issue("params.schema_invalid", f"{node.task_id}.{path}: {error.message}",
                               affected=[node.task_id, path]))
    return found


def _route_inside(data: dict, bounds: dict, route_id) -> bool:
    points = data["routes"].get(route_id)
    return bool(points) and all(_inside(bounds, p) for p in points)


def _spatial(package: MissionPackage, ctx: AdmissionContext, spec: MissionSpec | None) -> list[Issue]:
    data, found = ctx.registry.data, []
    scope = package.spatial_scope
    volumes = data.get("volumes", {data["approved_volume_id"]: {"bounds": data["bounds"]}})
    volume_id = scope.approved_volume_id
    volume = volumes.get(volume_id)
    if volume is None:
        return [issue("scope.unregistered_volume", f"{volume_id} is not a registered volume", affected=[volume_id])]
    if ctx.request is not None and ctx.request.approved_volume_id != volume_id:
        found.append(issue("scope.volume_not_requested",
                           f"plan uses {volume_id}, request allowed {ctx.request.approved_volume_id}",
                           affected=[volume_id]))
    if scope.frame.model_dump(exclude={"schema_version"}) != data["frame"]:
        found.append(issue("scope.frame_mismatch", "frame or map version differs from the registry",
                           affected=[scope.frame.frame_id, scope.frame.map_version]))
    bounds = volume["bounds"]
    requested_assets = set(ctx.request.asset_ids) if ctx.request is not None and ctx.request.asset_ids else None
    for node in package.nodes:
        p = node.params
        if node.skill_id in (INSPECT, CAPTURE):
            asset = data["assets"].get(p.get("asset_id"))
            if asset is None:
                found.append(issue("compile.unknown_asset", f"{p.get('asset_id')} is not registered",
                                   affected=[node.task_id]))
                continue
            if asset.get("volume", volume_id) != volume_id or not _inside(bounds, asset["position"]):
                found.append(issue("scope.target_outside_volume", f"{p['asset_id']} lies outside {volume_id}",
                                   affected=[node.task_id, p["asset_id"]]))
            if requested_assets is not None and p["asset_id"] not in requested_assets:
                found.append(issue("scope.asset_not_requested", f"{p['asset_id']} was not in the requested scope",
                                   affected=[node.task_id, p["asset_id"]]))
            if node.skill_id == INSPECT:
                route = p.get("approach_route_id")
                if route != asset.get("observation_route") or p.get("camera_id") != asset.get("camera_id"):
                    found.append(issue("params.binding_mismatch",
                                       f"{node.task_id} must use {p['asset_id']}'s registered route and camera",
                                       affected=[node.task_id]))
                if p.get("quality_profile_ref") != f"{ctx.registry.registry_id}.image":
                    found.append(issue("params.binding_mismatch", "quality profile belongs to another registry",
                                       affected=[node.task_id]))
                if not _route_inside(data, bounds, route):
                    found.append(issue("scope.route_outside_volume", f"route {route} leaves {volume_id}",
                                       affected=[node.task_id, str(route)]))
                elif distance(data["routes"][route][-1][:2], asset["position"][:2]) > data["thresholds"].get(
                        "above_asset_m", 0):
                    found.append(issue("params.binding_mismatch", f"route {route} does not end above {p['asset_id']}",
                                       affected=[node.task_id]))
        elif node.skill_id == FLY_ROUTE:
            if not _route_inside(data, bounds, p.get("route_id")):
                found.append(issue("scope.route_outside_volume", f"route {p.get('route_id')} leaves {volume_id}",
                                   affected=[node.task_id]))
            if {k: p.get(k) for k in ("frame_id", "map_version")} != data["frame"]:
                found.append(issue("scope.frame_mismatch", f"{node.task_id} route frame differs", affected=[node.task_id]))
        elif node.skill_id == TAKEOFF:
            altitude = p.get("altitude_m_agl")
            if not isinstance(altitude, (int, float)) or not _inside(bounds, [0, 0, altitude]):
                found.append(issue("scope.target_outside_volume", f"takeoff altitude {altitude} leaves {volume_id}",
                                   affected=[node.task_id]))
        elif node.skill_id == RETURN_HOME:
            route = data["routes"].get(p.get("return_route_id"))
            if not _route_inside(data, bounds, p.get("return_route_id")):
                found.append(issue("scope.route_outside_volume", "return route leaves the volume", affected=[node.task_id]))
            elif p.get("home_ref") != data["home"]["id"] or route[-1] != data["home"]["position"]:
                found.append(issue("params.binding_mismatch", "return route does not end at the approved home",
                                   affected=[node.task_id]))
        elif node.skill_id == LAND:
            site = data["landing_sites"].get(p.get("landing_site_id"))
            if site is None or site.get("reserved_for") != node.robot_id:
                found.append(issue("params.binding_mismatch", "landing site is not reserved for this robot",
                                   affected=[node.task_id]))
            elif not _inside(bounds, site["position"]):
                found.append(issue("scope.target_outside_volume", "landing site lies outside the volume",
                                   affected=[node.task_id]))
    for target in spec.targets if spec is not None else []:
        if target.pose is not None:
            frame = target.pose.frame.model_dump(exclude={"schema_version"})
            point = [target.pose.position.x, target.pose.position.y, target.pose.position.z]
            if frame != data["frame"]:
                found.append(issue("scope.frame_mismatch", "target pose is in another frame or map version",
                                   affected=[target.asset_id or target.description or "target"]))
            elif not _inside(bounds, point):
                found.append(issue("scope.target_outside_volume", f"target at {point} lies outside {volume_id}",
                                   affected=[target.asset_id or target.description or "target"]))
        if target.asset_id is not None:
            asset = data["assets"].get(target.asset_id)
            if asset is None:
                found.append(issue("compile.unknown_asset", f"target {target.asset_id} is not registered",
                                   affected=[target.asset_id]))
            elif requested_assets is not None and target.asset_id not in requested_assets:
                found.append(issue("scope.asset_not_requested", f"target {target.asset_id} was not requested",
                                   affected=[target.asset_id]))
    return found


def _order(package: MissionPackage) -> list[Issue]:
    """Canonical framework order and no conflicting resources on unordered nodes.

    规范的框架顺序，且互不有序的节点之间没有资源冲突。
    """
    by_skill = {s: [n for n in package.nodes if n.skill_id == s] for s in FRAMEWORK}
    if any(len(v) != 1 for v in by_skill.values()):
        return [issue("order.invalid", "a mission needs exactly one takeoff, return_home and land")]
    takeoff, ret, land = (by_skill[s][0] for s in FRAMEWORK)
    found = []
    roots = [n.task_id for n in package.nodes if not n.depends_on]
    if roots != [takeoff.task_id]:
        found.append(issue("order.invalid", f"takeoff must be the only root, found {roots}", affected=roots))
    after_takeoff = set(descendants(package, takeoff.task_id))
    after_return = set(descendants(package, ret.task_id))
    content = [n.task_id for n in package.nodes if n.skill_id not in FRAMEWORK]
    if set(content) - after_takeoff:
        found.append(issue("order.invalid", "content runs before takeoff", affected=sorted(set(content) - after_takeoff)))
    if after_return != {land.task_id}:
        found.append(issue("order.invalid", "only land may follow return_home", affected=sorted(after_return)))
    if descendants(package, land.task_id):
        found.append(issue("order.invalid", "nothing may follow land", affected=[land.task_id]))
    missing = [c for c in content if ret.task_id not in descendants(package, c)]
    if missing:
        found.append(issue("order.invalid", "return_home must follow every content task", affected=missing))
    nodes = package.nodes
    for index, first in enumerate(nodes):
        after_first = set(descendants(package, first.task_id))
        for second in nodes[index + 1:]:
            if second.task_id in after_first or first.task_id in descendants(package, second.task_id):
                continue
            conflicts = find_resource_conflicts(first.resources, second.resources)
            if conflicts:
                found.append(issue("resource.conflict", f"{first.task_id} and {second.task_id} may run together on {conflicts}",
                                   affected=[first.task_id, second.task_id, *conflicts]))
    return found


def _time(package: MissionPackage, ctx: AdmissionContext) -> list[Issue]:
    window, defaults = package.temporal_window, ctx.registry.data.get("mission_defaults", {})
    max_window = timedelta(minutes=defaults.get("max_window_minutes", 120))
    max_delay = timedelta(minutes=defaults.get("max_start_delay_minutes", 10))
    duration = sum(ctx.registry.manifests[n.skill_id].estimated_duration_s or 0 for n in package.nodes
                   if n.skill_id in ctx.registry.manifests)
    found = []
    if window.not_after <= ctx.now:
        found.append(issue("time.window_invalid", "the mission window has already ended"))
    if window.not_before > ctx.now + max_delay:
        found.append(issue("time.window_invalid", "the mission window starts too far in the future"))
    if window.not_after - window.not_before > max_window:
        found.append(issue("time.window_invalid", f"the mission window exceeds {max_window}"))
    if window.not_after - max(ctx.now, window.not_before) < timedelta(seconds=duration):
        found.append(issue("time.window_invalid", f"the window is shorter than the estimated {duration:.0f} s"))
    return found


def _energy(package: MissionPackage, ctx: AdmissionContext) -> tuple[list[Issue], float | None]:
    budget, defaults = package.energy_budget, ctx.registry.data.get("mission_defaults", {})
    found, total = [], 0.0
    for node in package.nodes:
        manifest = ctx.registry.manifests.get(node.skill_id)
        estimate = manifest.estimated_energy_fraction if manifest else None
        if estimate is None:
            found.append(issue("energy.estimate_missing", f"{node.skill_id} has no energy estimate; unknown is not admitted",
                               affected=[node.task_id]))
        else:
            total += estimate
    if found:
        return found, None
    if budget.reserve_fraction < defaults.get("min_reserve_fraction", 0.0):
        found.append(issue("energy.budget_invalid", f"reserve {budget.reserve_fraction} is below the site minimum"))
    if total > budget.max_consumption_fraction:
        found.append(issue("energy.budget_exceeded",
                           f"conservative estimate {total:.3f} exceeds the budget {budget.max_consumption_fraction}",
                           affected=[n.task_id for n in package.nodes]))
    return found, round(total, 4)


def admit(package: MissionPackage, ctx: AdmissionContext, *, spec: MissionSpec | None = None) -> AdmissionResult:
    """Run every check and collect every issue; accepted only when no error remains.

    执行全部检查并收集全部问题；只有不存在错误时才接受。
    """
    checks = _Checks()
    computed = package.compute_hash()
    if package.package_hash and package.package_hash != computed:
        checks.add("hash", [issue("package.hash_mismatch", "the package hash does not match its content")])
    else:
        checks.add("hash", [])
    missing = [name for name in ("spatial_scope", "temporal_window", "energy_budget") if getattr(package, name) is None]
    if missing:
        checks.add("boundaries", [issue("package.boundary_missing", f"missing {missing}", affected=missing)])
        return AdmissionResult(accepted=False, package_hash=computed, issues=checks.issues, checks=checks.records,
                               checked_at=ctx.now)
    checks.add("boundaries", [])
    policy = ctx.registry.data["simulation"]["policy_ref"]
    checks.add("recovery_policy", [] if package.recovery_policy_ref == policy else [
        issue("policy.recovery_mismatch", f"{package.recovery_policy_ref} is not the scene policy {policy}")])
    checks.add("capability", _capability(package, ctx))
    checks.add("params", _params(package, ctx))
    checks.add("spatial", _spatial(package, ctx, spec))
    checks.add("order_and_resources", _order(package))
    checks.add("time", _time(package, ctx))
    energy_issues, energy_total = _energy(package, ctx)
    checks.add("energy", energy_issues, "" if energy_total is None else f"upper={energy_total}")
    volume_id = package.spatial_scope.approved_volume_id
    status = ctx.airspace.query(volume_id, now=ctx.now)
    volume = ctx.registry.data.get("volumes", {}).get(volume_id)
    checks.add("airspace", airspace_issues(volume_id, volume, status), status.filing_status)
    ack = [n.task_id for n in package.nodes if n.allow_unverified_from]
    acknowledged = bool(ctx.request and ctx.request.unverified_ack_by
                        and ctx.request.trust_level is TrustLevel.FIRST_PARTY)
    # Unverified edges pass only with a recorded first-party acknowledgement, and still need human approval.
    # 未证实边只有在记录了第一方操作者确认时才放行，且仍需人工审批。
    checks.add("unverified_edges", [issue(
        "approval.unverified_edge_requires_human",
        "allow_unverified_from edges need an explicit human acknowledgement",
        affected=ack, severity=Severity.WARNING if acknowledged else Severity.ERROR,
    )] if ack else [])
    accepted = not any(i.severity is Severity.ERROR for i in checks.issues)
    return AdmissionResult(accepted=accepted, package_hash=computed, issues=checks.issues, checks=checks.records,
                           requires_human_ack=ack, energy_upper_fraction=energy_total,
                           airspace=status.model_dump(mode="json"), checked_at=ctx.now)
