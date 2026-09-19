"""MissionSpec (planner output, human-reviewable) and MissionPackage (compiled, machine-executable).

The LLM's output stops at MissionSpec. It references an approved volume, it never mints one;
it names skills and parameters, it never carries control commands (validators + contract tests
enforce this). The compiler expands it into a MissionPackage bound to an ApprovalRecord.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from drone_agent.contracts.common import ContractModel, Embodiment, Frame, Pose
from drone_agent.contracts.skill import ResourceClaim

# Keys a planner must never smuggle into task params: these belong to the control egress, which
# only accepts ControlCommandEnvelope objects carrying a lease epoch (see authority.py).
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(
    {"setpoint", "attitude", "thrust", "pwm", "actuator", "mavlink", "raw_command", "offboard", "lease_epoch"}
)


class GoalType(StrEnum):
    INSPECT = "inspect"
    SURVEY = "survey"
    DELIVER = "deliver"
    SEARCH = "search"
    RECHECK = "recheck"
    OTHER = "other"


class TargetRef(ContractModel):
    asset_id: str | None = None
    pose: Pose | None = None
    description: str = ""

    @model_validator(mode="after")
    def _identified(self) -> TargetRef:
        if self.asset_id is None and self.pose is None:
            raise ValueError("a target needs an asset_id or a pose")
        return self


class SpatialScope(ContractModel):
    """Always a reference to an approved volume; geometry is resolved by the compiler from the registry."""

    approved_volume_id: str = Field(min_length=1)
    frame: Frame


class TemporalWindow(ContractModel):
    not_before: datetime
    not_after: datetime
    tz: str = "UTC"

    @model_validator(mode="after")
    def _ordered(self) -> TemporalWindow:
        if self.not_after <= self.not_before:
            raise ValueError("not_after must be later than not_before")
        return self


class EnergyBudget(ContractModel):
    max_consumption_fraction: float = Field(gt=0.0, le=1.0)
    reserve_fraction: float = Field(ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def _feasible(self) -> EnergyBudget:
        if self.max_consumption_fraction + self.reserve_fraction > 1.0:
            raise ValueError("consumption + reserve cannot exceed 100% of energy")
        return self


class CollaborationRule(ContractModel):
    trigger: str = Field(description="e.g. anomaly_candidate_detected")
    required_skill: str
    required_embodiments: list[Embodiment] = Field(default_factory=list)
    handoff_timeout_s: float = Field(gt=0)


class Provenance(ContractModel):
    model_id: str
    prompt_version: str
    input_hash: str
    generated_at: datetime


def _check_params(params: dict[str, Any]) -> dict[str, Any]:
    bad = sorted(k for k in params if k.lower() in FORBIDDEN_PARAM_KEYS)
    if bad:
        raise ValueError(f"task params may not carry control-level keys: {bad}")
    return params


class TaskNode(ContractModel):
    task_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    completion_evidence: list[str] = Field(default_factory=list)
    allow_unverified_from: list[str] = Field(
        default_factory=list,
        description="predecessor ids whose edge may pass with effect_verdict=unverified; admission must approve",
    )

    @field_validator("params")
    @classmethod
    def _no_control_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_params(value)


def _validate_dag(nodes: list[TaskNode]) -> None:
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
    # cycle check (iterative DFS)
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
    mission_id: str = Field(min_length=1)
    mission_version: int = Field(ge=1)
    goal: str = Field(min_length=1)
    goal_type: GoalType = GoalType.OTHER
    targets: list[TargetRef] = Field(default_factory=list)
    spatial_scope: SpatialScope
    temporal_window: TemporalWindow
    tasks: list[TaskNode] = Field(min_length=1)
    energy_budget: EnergyBudget
    recovery_policy_ref: str = Field(min_length=1, description="scenario-configured; never generated by the LLM")
    collaboration_rules: list[CollaborationRule] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def _dag(self) -> MissionSpec:
        _validate_dag(self.tasks)
        return self


class ApprovalRecord(ContractModel):
    approver: str
    approved_at: datetime
    mission_id: str
    mission_version: int
    package_hash: str
    expires_at: datetime
    allowed_robots: list[str] = Field(default_factory=list)


class PackageNode(ContractModel):
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
    mission_id: str
    mission_version: int
    nodes: list[PackageNode] = Field(min_length=1)
    recovery_policy_ref: str
    approval: ApprovalRecord | None = None
    package_hash: str = ""

    def compute_hash(self) -> str:
        payload = {
            "mission_id": self.mission_id,
            "mission_version": self.mission_version,
            "recovery_policy_ref": self.recovery_policy_ref,
            "nodes": [n.model_dump(mode="json") for n in self.nodes],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_authorized(self, *, robot_id: str, now: datetime) -> bool:
        """Onboard acceptance rule: valid approval, matching hash and version, robot in scope."""
        a = self.approval
        if a is None:
            return False
        if a.mission_id != self.mission_id or a.mission_version != self.mission_version:
            return False
        if a.package_hash != self.compute_hash():
            return False
        if now >= a.expires_at:
            return False
        return not a.allowed_robots or robot_id in a.allowed_robots
