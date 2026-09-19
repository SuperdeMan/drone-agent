"""SkillManifest v2: the contract for long-running physical skills.

A skill is a physical process lasting seconds to minutes, not a function call. The manifest
declares parameters, preconditions, invariants (must hold for the whole run), resource claims,
cancel/pause semantics, completion evidence and failure modes. Learned implementations must
declare their shadow/limited/full stage.

SkillManifest v2：长时间物理技能的契约。

技能是持续数秒到数分钟的物理过程，不是一次函数调用。清单声明参数、前置条件、
持续约束（整个执行期间必须成立）、资源占用、取消 / 暂停语义、完成证据与失败模式。
学习型实现必须声明其 shadow / limited / full 阶段。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from drone_agent.contracts.common import ContractModel, ControlMode, Embodiment

SKILL_ID_RE = re.compile(r"^skill\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class ResourceMode(StrEnum):
    """Exclusive or shared occupation of a resource. / 资源的独占或共享占用方式。"""

    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class ResourceClaim(ContractModel):
    """A resource a skill occupies while running. / 技能运行期间占用的资源。"""

    resource_id: str = Field(min_length=1, description="<robot_id>.<resource>, e.g. uav_01.motion / 如 uav_01.motion")
    mode: ResourceMode
    shared_mode: str | None = Field(default=None, description="e.g. camera 'still' vs 'video' / 如相机的 still 与 video")

    def conflicts_with(self, other: ResourceClaim) -> bool:
        """Two claims conflict on the same resource unless both are shared in the same mode.

        同一资源上的两个占用冲突，除非两者都是同一模式的共享占用。
        """
        if self.resource_id != other.resource_id:
            return False
        if self.mode is ResourceMode.EXCLUSIVE or other.mode is ResourceMode.EXCLUSIVE:
            return True
        return self.shared_mode != other.shared_mode


class ImplementationKind(StrEnum):
    """Deterministic code or a learned policy. / 确定性实现或学习型策略。"""

    DETERMINISTIC = "deterministic"
    LEARNED = "learned"


class LearnedStage(StrEnum):
    """Rollout stage of a learned implementation. / 学习型实现的上线阶段。"""

    SHADOW = "shadow"  # logs its output, never executes / 只记录输出，不执行
    LIMITED = "limited"  # executes only in whitelisted scenarios with tighter envelopes / 只在白名单场景、更严包络下执行
    FULL = "full"


class Implementation(ContractModel):
    """How the skill is implemented and, if learned, how far it may act.

    技能的实现方式；若为学习型，则声明允许它走到哪一步。
    """

    kind: ImplementationKind = ImplementationKind.DETERMINISTIC
    learned_stage: LearnedStage | None = None
    model_version: str | None = None

    @model_validator(mode="after")
    def _learned_needs_stage(self) -> Implementation:
        # A learned skill without a declared stage would silently default to executing.
        # 未声明阶段的学习型技能会悄悄默认为可执行，因此拒绝。
        if self.kind is ImplementationKind.LEARNED and self.learned_stage is None:
            raise ValueError("learned implementations must declare learned_stage (shadow|limited|full)")
        return self

    @property
    def may_execute(self) -> bool:
        """Shadow-stage policies only observe. / 影子阶段的策略只观察不执行。"""
        return self.kind is ImplementationKind.DETERMINISTIC or self.learned_stage is not LearnedStage.SHADOW


class CancelSpec(ContractModel):
    """What cancelling this skill means physically. / 取消该技能在物理上意味着什么。"""

    cancel_safe_states: list[str] = Field(default_factory=list)
    cancel_procedure: str = Field(min_length=1, description="what the skill does to wind down when cancelled / 取消时的收尾动作")


class PauseSpec(ContractModel):
    """Whether and how the skill can wait safely. / 技能能否以及如何安全等待。"""

    pausable: bool = False
    safe_wait_condition: str = ""
    max_wait_s: float | None = None


class EvidenceRequirement(ContractModel):
    """Evidence the skill must produce to count as complete. / 技能被判定完成所需的证据。"""

    evidence_type: str
    criteria: dict[str, Any] = Field(default_factory=dict)


class FailureMode(ContractModel):
    """A known way the skill fails and the recovery hint. / 已知失败模式及其恢复提示。"""

    code: str
    description: str = ""
    recovery_hint: str = ""


class SkillManifest(ContractModel):
    """Declarative description of a skill; the executive never special-cases skill names.

    技能的声明式描述；执行器不对技能名做特殊分支。
    """

    skill_id: str
    version: str
    description: str = ""
    embodiments: list[Embodiment]
    params_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema with units / 带单位的 JSON Schema")
    preconditions: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    resources: list[ResourceClaim] = Field(default_factory=list)
    progress_schema: dict[str, Any] = Field(default_factory=dict)
    cancel: CancelSpec
    pause: PauseSpec = Field(default_factory=PauseSpec)
    completion_evidence: list[EvidenceRequirement] = Field(default_factory=list)
    failure_modes: list[FailureMode] = Field(default_factory=list)
    timeout_s: float = Field(gt=0)
    estimated_duration_s: float | None = None
    estimated_energy_fraction: float | None = None
    implementation: Implementation = Field(default_factory=Implementation)
    required_control_modes: set[ControlMode] = Field(default_factory=set)

    @field_validator("skill_id")
    @classmethod
    def _skill_id_format(cls, value: str) -> str:
        if not SKILL_ID_RE.match(value):
            raise ValueError("skill_id must match skill.<domain>.<action>")
        return value


class SkillInstanceState(StrEnum):
    """Lifecycle of one running skill instance. / 单个技能实例的生命周期状态。"""

    ACCEPTED = "accepted"
    PREPARING = "preparing"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    RECOVERING = "recovering"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


S = SkillInstanceState
TERMINAL_STATES: frozenset[SkillInstanceState] = frozenset({S.COMPLETED, S.CANCELLED, S.FAILED, S.OUTCOME_UNKNOWN})

# Allowed transitions. "cancel requested" and "recovered to a safe state" are distinct steps,
# so RUNNING never jumps straight to CANCELLED.
# 允许的状态迁移。「请求取消」与「已恢复到安全状态」是两个不同步骤，
# 因此 RUNNING 不能直接跳到 CANCELLED。
ALLOWED_TRANSITIONS: dict[SkillInstanceState, frozenset[SkillInstanceState]] = {
    S.ACCEPTED: frozenset({S.PREPARING, S.BLOCKED, S.CANCEL_REQUESTED, S.FAILED}),
    S.PREPARING: frozenset({S.RUNNING, S.BLOCKED, S.CANCEL_REQUESTED, S.FAILED}),
    S.RUNNING: frozenset({S.VERIFYING, S.PAUSE_REQUESTED, S.CANCEL_REQUESTED, S.BLOCKED, S.FAILED, S.OUTCOME_UNKNOWN}),
    S.VERIFYING: frozenset({S.COMPLETED, S.RUNNING, S.FAILED, S.OUTCOME_UNKNOWN, S.CANCEL_REQUESTED}),
    S.PAUSE_REQUESTED: frozenset({S.PAUSED, S.RUNNING, S.CANCEL_REQUESTED, S.RECOVERING}),
    S.PAUSED: frozenset({S.RUNNING, S.CANCEL_REQUESTED, S.RECOVERING}),
    S.CANCEL_REQUESTED: frozenset({S.RECOVERING, S.CANCELLED, S.OUTCOME_UNKNOWN}),
    S.RECOVERING: frozenset({S.CANCELLED, S.FAILED, S.OUTCOME_UNKNOWN, S.PAUSED}),
    S.BLOCKED: frozenset({S.PREPARING, S.RUNNING, S.CANCEL_REQUESTED, S.FAILED}),
    S.COMPLETED: frozenset(),
    S.CANCELLED: frozenset(),
    S.FAILED: frozenset(),
    S.OUTCOME_UNKNOWN: frozenset(),
}


def can_transition(current: SkillInstanceState, target: SkillInstanceState) -> bool:
    """Whether the lifecycle allows `current -> target`. / 生命周期是否允许 `current -> target`。"""
    return target in ALLOWED_TRANSITIONS[current]


def find_resource_conflicts(active: Iterable[ResourceClaim], candidate: Iterable[ResourceClaim]) -> list[str]:
    """Resource ids that would be double-claimed if `candidate` started while `active` runs.

    若 `candidate` 在 `active` 运行期间启动，会被重复占用的资源 id。
    """
    active = list(active)
    return sorted({c.resource_id for c in candidate for a in active if c.conflicts_with(a)})
