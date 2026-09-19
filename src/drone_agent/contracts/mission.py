"""MissionSpec (planner output, human-reviewable) and MissionPackage (compiled, machine-executable).

The LLM's output stops at MissionSpec. It references an approved volume, it never mints one;
it names skills and parameters, it never carries control commands (validators + contract tests
enforce this). The compiler expands it into a MissionPackage bound to an ApprovalRecord.

MissionSpec（规划器输出，人可审阅）与 MissionPackage（编译后，机器可执行）。

LLM 的输出止于 MissionSpec：它引用已批准的空间体积而不能新建；它指定技能与参数，
但永远不携带控制命令（校验器与契约测试共同保证）。编译器把它展开为绑定
ApprovalRecord 的 MissionPackage。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

from drone_agent.contracts.common import ContractModel, Embodiment, Frame, Pose
from drone_agent.contracts.skill import ResourceClaim

# Keys a planner must never smuggle into task params: these belong to the control egress, which
# only accepts ControlCommandEnvelope objects carrying a lease epoch (see authority.py).
# 规划器不得夹带进任务参数的键：它们属于控制出口，而控制出口只接受带租约代次的
# ControlCommandEnvelope（见 authority.py）。
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(
    {"setpoint", "attitude", "thrust", "pwm", "actuator", "mavlink", "raw_command", "offboard", "lease_epoch", "command_seq"}
)


class GoalType(StrEnum):
    """Coarse mission goal categories. / 任务目标的粗分类。"""

    INSPECT = "inspect"
    SURVEY = "survey"
    DELIVER = "deliver"
    SEARCH = "search"
    RECHECK = "recheck"
    OTHER = "other"


class TargetRef(ContractModel):
    """A mission target: an asset id, a pose, or both. / 任务对象：资产 id、位姿，或两者兼有。"""

    asset_id: str | None = None
    pose: Pose | None = None
    description: str = ""

    @model_validator(mode="after")
    def _identified(self) -> TargetRef:
        if self.asset_id is None and self.pose is None:
            raise ValueError("a target needs an asset_id or a pose")
        return self


class SpatialScope(ContractModel):
    """Always a reference to an approved volume; geometry is resolved by the compiler from the registry.

    永远是对已批准体积的引用；几何由编译器从登记表解析，规划器不能自造。
    """

    approved_volume_id: str = Field(min_length=1)
    frame: Frame


class TemporalWindow(ContractModel):
    """Earliest start and latest end. / 最早开始与最晚结束时间。"""

    not_before: AwareDatetime
    not_after: AwareDatetime
    tz: str = "UTC"

    @model_validator(mode="after")
    def _ordered(self) -> TemporalWindow:
        if self.not_after <= self.not_before:
            raise ValueError("not_after must be later than not_before")
        return self


class EnergyBudget(ContractModel):
    """Allowed consumption plus the reserve that must remain. / 允许消耗的能源与必须保留的余量。"""

    max_consumption_fraction: float = Field(gt=0.0, le=1.0)
    reserve_fraction: float = Field(ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def _feasible(self) -> EnergyBudget:
        if self.max_consumption_fraction + self.reserve_fraction > 1.0:
            raise ValueError("consumption + reserve cannot exceed 100% of energy")
        return self


class CollaborationRule(ContractModel):
    """When and to whom a follow-up task is handed off. / 何时、向谁交接后续任务。"""

    trigger: str = Field(description="e.g. anomaly_candidate_detected / 如 anomaly_candidate_detected")
    required_skill: str
    required_embodiments: list[Embodiment] = Field(default_factory=list)
    handoff_timeout_s: float = Field(gt=0)


class Provenance(ContractModel):
    """Which model and prompt produced the spec. / 由哪个模型与提示版本生成。"""

    model_id: str
    prompt_version: str
    input_hash: str
    generated_at: datetime


def _check_params(params: dict[str, Any]) -> dict[str, Any]:
    # Inspect containers recursively; nesting must not bypass the boundary.
    # 递归检查容器；嵌套包装不得绕过边界。
    pending: list[Any] = [params]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, (dict, list, tuple)):
            if id(value) in seen:
                continue
            seen.add(id(value))
        if isinstance(value, dict):
            bad = sorted(k for k in value if isinstance(k, str) and k.lower() in FORBIDDEN_PARAM_KEYS)
            if bad:
                raise ValueError(f"task params may not carry control-level keys: {bad}")
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return params


class TaskNode(ContractModel):
    """One node of the mission DAG as the planner writes it. / 规划器写出的任务 DAG 节点。"""

    task_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    completion_evidence: list[str] = Field(default_factory=list)
    allow_unverified_from: list[str] = Field(
        default_factory=list,
        description=(
            "predecessor ids whose edge may pass with effect_verdict=unverified; admission must approve / "
            "允许以 effect_verdict=unverified 放行的前驱 id；须经准入审批"
        ),
    )

    @field_validator("params")
    @classmethod
    def _no_control_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_params(value)


def _validate_dag(nodes: list[TaskNode] | list[PackageNode]) -> None:
    """Unique ids, known dependencies, allow_unverified only on direct edges, and no cycles.

    id 唯一、依赖存在、allow_unverified 只能指向直接前驱、且无环。
    """
    ids = [n.task_id for n in nodes]
    if len(ids) != len(set(ids)):
        raise ValueError("task ids must be unique")
    known = set(ids)
    for n in nodes:
        for dep in n.depends_on:
            if dep not in known:
                raise ValueError(f"task {n.task_id} depends on unknown task {dep}")
        for dep in n.allow_unverified_from:
            if dep not in n.depends_on:
                raise ValueError(f"task {n.task_id}: allow_unverified_from must name a direct dependency")
    # Cycle check with an iterative DFS (0 = unvisited, 1 = on stack, 2 = done).
    # 迭代式 DFS 检环（0 = 未访问，1 = 在栈中，2 = 已完成）。
    adj = {n.task_id: list(n.depends_on) for n in nodes}
    state: dict[str, int] = {}
    for start in adj:
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, idx = stack[-1]
            if state.get(node, 0) == 2:
                stack.pop()
                continue
            state[node] = 1
            deps = adj[node]
            if idx < len(deps):
                stack[-1] = (node, idx + 1)
                nxt = deps[idx]
                if state.get(nxt, 0) == 1:
                    raise ValueError(f"dependency cycle through {nxt}")
                if state.get(nxt, 0) == 0:
                    stack.append((nxt, 0))
            else:
                state[node] = 2
                stack.pop()


class MissionSpec(ContractModel):
    """The planner's whole output: goal, targets, approved scope, task DAG, budgets, provenance.

    规划器的全部输出：目标、对象、已批准范围、任务 DAG、预算、来源。
    """

    mission_id: str = Field(min_length=1)
    mission_version: int = Field(ge=1)
    goal: str = Field(min_length=1)
    goal_type: GoalType = GoalType.OTHER
    targets: list[TargetRef] = Field(default_factory=list)
    spatial_scope: SpatialScope
    temporal_window: TemporalWindow
    tasks: list[TaskNode] = Field(min_length=1)
    energy_budget: EnergyBudget
    recovery_policy_ref: str = Field(
        min_length=1, description="scenario-configured; never generated by the LLM / 场景配置指定，不由 LLM 生成"
    )
    collaboration_rules: list[CollaborationRule] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def _dag(self) -> MissionSpec:
        _validate_dag(self.tasks)
        return self


class ApprovalRecord(ContractModel):
    """A human approval bound to one mission version and package hash.

    绑定到特定任务版本与任务包哈希的人工审批记录。
    """

    approver: str
    approved_at: AwareDatetime
    mission_id: str
    mission_version: int
    package_hash: str
    expires_at: AwareDatetime
    allowed_robots: list[str] = Field(default_factory=list)


class PackageNode(ContractModel):
    """A compiled task node: skill version, robot, resources and timeout are now bound.

    编译后的任务节点：技能版本、机器人、资源与超时已绑定。
    """

    task_id: str
    skill_id: str
    skill_version: str
    robot_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    resources: list[ResourceClaim] = Field(default_factory=list)
    timeout_s: float = Field(gt=0)
    completion_evidence: list[str] = Field(default_factory=list)
    allow_unverified_from: list[str] = Field(default_factory=list)

    @field_validator("params")
    @classmethod
    def _no_control_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_params(value)


class MissionPackage(ContractModel):
    """What the executive receives over the uplink and re-checks before accepting.

    执行器经上行链路收到、并在接受前复核的任务包。
    """

    mission_id: str
    mission_version: int
    nodes: list[PackageNode] = Field(min_length=1)
    recovery_policy_ref: str
    approval: ApprovalRecord | None = None
    package_hash: str = ""
    # Legacy drafts remain parseable, but cannot be authorized without all boundaries.
    # 旧草案仍可解析，但缺少任一边界时不能获得授权。
    spatial_scope: SpatialScope | None = None
    temporal_window: TemporalWindow | None = None
    energy_budget: EnergyBudget | None = None

    @model_validator(mode="after")
    def _dag(self) -> MissionPackage:
        _validate_dag(self.nodes)
        return self

    def compute_hash(self) -> str:
        """SHA-256 over the canonical JSON of the executable content (approval excluded).

        对可执行内容（不含审批记录）的规范化 JSON 计算 SHA-256。
        """
        window = self.temporal_window
        # Timestamp wire values normalize to UTC; hashes must survive that roundtrip.
        # Timestamp 在线路上传输时规范化为 UTC；哈希必须在该往返中保持一致。
        if window is not None:
            window = window.model_copy(update={
                "not_before": window.not_before.astimezone(timezone.utc),
                "not_after": window.not_after.astimezone(timezone.utc),
            })
        payload = {
            "mission_id": self.mission_id,
            "mission_version": self.mission_version,
            "recovery_policy_ref": self.recovery_policy_ref,
            "nodes": [n.model_dump(mode="json") for n in self.nodes],
            "spatial_scope": self.spatial_scope.model_dump(mode="json") if self.spatial_scope else None,
            "temporal_window": window.model_dump(mode="json") if window else None,
            "energy_budget": self.energy_budget.model_dump(mode="json") if self.energy_budget else None,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_authorized(self, *, robot_id: str, now: datetime) -> bool:
        """Onboard acceptance rule: valid approval, matching hash and version, robot in scope.

        机载接受规则：审批有效、哈希与版本匹配、机器人在授权范围内。
        """
        a = self.approval
        if a is None or self.spatial_scope is None or self.temporal_window is None or self.energy_budget is None:
            return False
        if not self.temporal_window.not_before <= now < self.temporal_window.not_after:
            return False
        if a.mission_id != self.mission_id or a.mission_version != self.mission_version:
            return False
        if a.package_hash != self.package_hash or self.package_hash != self.compute_hash():
            return False
        if not a.approved_at <= now < a.expires_at:
            return False
        return robot_id in a.allowed_robots and all(n.robot_id in a.allowed_robots for n in self.nodes)
