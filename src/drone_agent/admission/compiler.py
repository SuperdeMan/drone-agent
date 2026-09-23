"""Compiler: MissionSpec -> MissionPackage as a pure function of the spec, the scene and the robot (WP-M2-04).

The compiler owns everything the model must not decide. It checks the recovery policy reference
against the scene, resolves skills from the manifests, validates any framework nodes the spec already
contains and inserts the missing ones (takeoff -> content -> return_home -> land) with scene
defaults, expands inspection targets into `skill.inspect.asset` nodes, fills registry-bound parameters
the spec left out (it never overwrites a value the spec gave; admission checks the binding), orders
the DAG deterministically and computes the package hash. Same input, same package hash.

编译器：把 MissionSpec 变成 MissionPackage，是规格、场景与机器人的纯函数（WP-M2-04）。

编译器负责一切不该由模型决定的内容：按场景核对恢复策略引用；从技能清单解析技能；校验规格中已有
的框架节点，并按场景默认值补齐缺失的（takeoff → 内容 → return_home → land）；把巡检目标展开为
`skill.inspect.asset` 节点；补齐规格未写的登记表绑定参数（从不覆盖规格已给的值，绑定是否正确由
准入核对）；确定性地排序 DAG 并计算任务包哈希。同一输入得到同一哈希。
"""

from __future__ import annotations

from dataclasses import dataclass

from drone_agent.admission.models import CompileResult
from drone_agent.contracts import GoalType, MissionPackage, MissionSpec, PackageNode, TaskNode
from drone_agent.mission.registry import Registry
from drone_agent.runtime.issues import Issue, issue

TAKEOFF = "skill.flight.takeoff"
RETURN_HOME = "skill.flight.return_home"
LAND = "skill.flight.land"
INSPECT = "skill.inspect.asset"
FLY_ROUTE = "skill.flight.fly_route"
CAPTURE = "skill.flight.capture_image"
FRAMEWORK = (TAKEOFF, RETURN_HOME, LAND)
DEFAULT_IDS = {TAKEOFF: "takeoff", RETURN_HOME: "return_home", LAND: "land"}


@dataclass(frozen=True)
class CompileContext:
    """The scene registry and the robot the coordinator selected. / 场景登记表与协调器选定的机器人。"""

    registry: Registry
    robot_id: str


def ancestors(tasks: dict[str, TaskNode], task_id: str) -> set[str]:
    """All transitive dependencies of `task_id`. / `task_id` 的全部传递依赖。"""
    found, pending = set(), list(tasks[task_id].depends_on)
    while pending:
        current = pending.pop()
        if current in found or current not in tasks:
            continue
        found.add(current)
        pending.extend(tasks[current].depends_on)
    return found


def complete_params(task: TaskNode, registry: Registry) -> tuple[dict, list[Issue]]:
    """Fill registry-bound parameters the spec omitted; never overwrite a value the spec provided.

    补齐规格遗漏的登记表绑定参数；从不覆盖规格已提供的值。
    """
    data, defaults = registry.data, registry.data.get("mission_defaults", {})
    params = dict(task.params)
    if task.skill_id == TAKEOFF:
        params.setdefault("altitude_m_agl", defaults.get("takeoff_altitude_m_agl"))
    elif task.skill_id == RETURN_HOME:
        params.setdefault("home_ref", defaults.get("home_ref"))
        params.setdefault("return_route_id", defaults.get("return_route_id"))
    elif task.skill_id == LAND:
        params.setdefault("landing_site_id", defaults.get("landing_site_id"))
    elif task.skill_id in (INSPECT, CAPTURE):
        asset_id = params.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            return params, [issue("compile.params_invalid", "an inspection needs an asset_id", affected=[task.task_id])]
        asset = data["assets"].get(asset_id)
        if asset is None:
            return params, [issue("compile.unknown_asset", f"asset {asset_id} is not registered",
                                  affected=[task.task_id, asset_id])]
        params.setdefault("camera_id", asset.get("camera_id"))
        params.setdefault("quality_profile_ref", f"{registry.registry_id}.image")
        if task.skill_id == INSPECT:
            params.setdefault("approach_route_id", asset.get("observation_route"))
            params.setdefault("speed_mps", defaults.get("approach_speed_mps"))
            params.setdefault("max_captures", defaults.get("max_captures"))
    elif task.skill_id == FLY_ROUTE:
        route_id = params.get("route_id")
        if route_id not in data["routes"]:
            return params, [issue("compile.unknown_route", f"route {route_id} is not registered",
                                  affected=[task.task_id, str(route_id)])]
        params.setdefault("frame_id", data["frame"]["frame_id"])
        params.setdefault("map_version", data["frame"]["map_version"])
        params.setdefault("speed_mps", defaults.get("approach_speed_mps"))
    missing = sorted(key for key, value in params.items() if value is None)
    if missing:
        return params, [issue("compile.params_invalid", f"scene defaults missing for {missing}",
                              affected=[task.task_id, *missing])]
    return params, []


def _unique(existing: set[str], preferred: str) -> str:
    candidate, index = preferred, 1
    while candidate in existing:
        index += 1
        candidate = f"{preferred}_{index}"
    return candidate


def _framework_violations(tasks: dict[str, TaskNode], content: list[TaskNode], framework: dict) -> list[str]:
    """Reasons the provided framework nodes are out of canonical order. / 已给框架节点偏离规范顺序的原因。"""
    reasons = []
    takeoff, ret, land = (framework[s][0] if framework[s] else None for s in FRAMEWORK)
    content_ids = {t.task_id for t in content}
    if takeoff is not None:
        if takeoff.depends_on:
            reasons.append("takeoff must be the first node")
        for task in content:
            if takeoff.task_id not in ancestors(tasks, task.task_id):
                reasons.append(f"{task.task_id} does not follow takeoff")
    for end in (ret, land):
        if end is None:
            continue
        before = ancestors(tasks, end.task_id)
        if not content_ids <= before:
            reasons.append(f"{end.task_id} runs before content tasks {sorted(content_ids - before)}")
        for task in content:
            if end.task_id in ancestors(tasks, task.task_id):
                reasons.append(f"{task.task_id} depends on {end.task_id}")
    if land is not None and ret is None:
        reasons.append("land requires return_home before it")
    if ret is not None and land is not None:
        if ret.task_id not in ancestors(tasks, land.task_id):
            reasons.append("land must follow return_home")
        if land.task_id in ancestors(tasks, ret.task_id):
            reasons.append("return_home cannot follow land")
    if land is not None and any(land.task_id in t.depends_on for t in tasks.values()):
        reasons.append("nothing may follow land")
    return reasons


def _topological(tasks: list[TaskNode], already: set[str]) -> list[TaskNode]:
    """Stable topological order after `already`: a task follows its dependencies, otherwise spec order.

    在 `already` 之后的稳定拓扑顺序：任务排在其依赖之后，其余按规格顺序。
    """
    placed, ordered, remaining = set(already), [], list(tasks)
    while remaining:
        for task in remaining:
            if all(dep in placed for dep in task.depends_on):
                ordered.append(task)
                placed.add(task.task_id)
                remaining.remove(task)
                break
        else:  # pragma: no cover - MissionSpec already rejects cycles / MissionSpec 已拒绝环
            raise ValueError("dependency cycle")
    return ordered


def compile_spec(spec: MissionSpec, ctx: CompileContext) -> CompileResult:
    """Compile deterministically; any issue means no package. / 确定性编译；有任何问题就不产出任务包。"""
    registry = ctx.registry
    manifests = registry.manifests
    policy = registry.data["simulation"]["policy_ref"]
    if spec.recovery_policy_ref != policy:
        return CompileResult(issues=[issue(
            "compile.policy_mismatch",
            f"recovery policy {spec.recovery_policy_ref} differs from the scene's {policy}; the planner may not choose it",
            affected=[spec.recovery_policy_ref],
        )])
    unknown = [t for t in spec.tasks if t.skill_id not in manifests]
    if unknown:
        return CompileResult(issues=[issue("compile.unknown_skill", f"unknown skill {t.skill_id}",
                                           affected=[t.task_id, t.skill_id]) for t in unknown])
    framework = {s: [t for t in spec.tasks if t.skill_id == s] for s in FRAMEWORK}
    duplicated = [s for s, found in framework.items() if len(found) > 1]
    if duplicated:
        return CompileResult(issues=[issue("compile.framework_skill_misplaced", f"{s} appears more than once",
                                           affected=[t.task_id for t in framework[s]]) for s in duplicated])
    content = [t for t in spec.tasks if t.skill_id not in FRAMEWORK]
    tasks = {t.task_id: t for t in spec.tasks}
    violations = _framework_violations(tasks, content, framework)
    if violations:
        return CompileResult(issues=[issue("compile.invalid_order", "; ".join(violations),
                                           affected=sorted(tasks))])

    # Expand inspection targets that have no inspection task. / 为没有巡检任务的巡检目标展开节点。
    expanded, used = [], set(tasks)
    if spec.goal_type in (GoalType.INSPECT, GoalType.RECHECK):
        inspected = {t.params.get("asset_id") for t in content if t.skill_id in (INSPECT, CAPTURE)}
        for target in spec.targets:
            if target.asset_id is None or target.asset_id in inspected:
                continue
            task_id = _unique(used, f"inspect_{target.asset_id}")
            previous = content[-1].task_id if content else None
            content.append(TaskNode(task_id=task_id, skill_id=INSPECT, params={"asset_id": target.asset_id},
                                    depends_on=[previous] if previous else []))
            used.add(task_id)
            inspected.add(target.asset_id)
            expanded.append(task_id)
    if not content:
        return CompileResult(issues=[issue("compile.goal_uncovered", "the mission has no content task to perform")])

    # Insert missing framework nodes around the content. / 在内容周围补齐缺失的框架节点。
    inserted = []
    takeoff = framework[TAKEOFF][0] if framework[TAKEOFF] else None
    if takeoff is None:
        takeoff = TaskNode(task_id=_unique(used, DEFAULT_IDS[TAKEOFF]), skill_id=TAKEOFF)
        used.add(takeoff.task_id)
        inserted.append(takeoff.task_id)
        content = [t.model_copy(update={"depends_on": [takeoff.task_id]}) if not t.depends_on else t for t in content]
    content_ids = {t.task_id for t in content}
    sinks = [t.task_id for t in content if not any(t.task_id in other.depends_on for other in content)]
    ret = framework[RETURN_HOME][0] if framework[RETURN_HOME] else None
    if ret is None:
        ret = TaskNode(task_id=_unique(used, DEFAULT_IDS[RETURN_HOME]), skill_id=RETURN_HOME, depends_on=sinks)
        used.add(ret.task_id)
        inserted.append(ret.task_id)
    land = framework[LAND][0] if framework[LAND] else None
    if land is None:
        land = TaskNode(task_id=_unique(used, DEFAULT_IDS[LAND]), skill_id=LAND, depends_on=[ret.task_id])
        used.add(land.task_id)
        inserted.append(land.task_id)
    ordered = [takeoff, *_topological([t for t in content if t.task_id in content_ids], {takeoff.task_id}), ret, land]

    issues, nodes = [], []
    for task in ordered:
        params, found = complete_params(task, registry)
        issues.extend(found)
        manifest = manifests[task.skill_id]
        nodes.append(PackageNode(
            task_id=task.task_id,
            skill_id=task.skill_id,
            skill_version=manifest.version,
            robot_id=ctx.robot_id,
            params=params,
            depends_on=list(task.depends_on),
            resources=list(manifest.resources),
            timeout_s=manifest.timeout_s,
            completion_evidence=[e.evidence_type for e in manifest.completion_evidence],
            allow_unverified_from=list(task.allow_unverified_from),
        ))
    if issues:
        return CompileResult(issues=issues, inserted_framework=inserted, expanded_targets=expanded)
    package = MissionPackage(
        mission_id=spec.mission_id,
        mission_version=spec.mission_version,
        nodes=nodes,
        recovery_policy_ref=policy,
        spatial_scope=spec.spatial_scope,
        temporal_window=spec.temporal_window,
        energy_budget=spec.energy_budget,
    )
    package.package_hash = package.compute_hash()
    return CompileResult(package=package, package_hash=package.package_hash, inserted_framework=inserted,
                         expanded_targets=expanded)
