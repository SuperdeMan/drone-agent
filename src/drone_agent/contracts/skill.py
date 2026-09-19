"""SkillManifest v2: the contract for long-running physical skills.

A skill is a physical process lasting seconds to minutes, not a function call. The manifest
declares parameters, preconditions, invariants (must hold for the whole run), resource claims,
cancel/pause semantics, completion evidence and failure modes. Learned implementations must
declare their shadow/limited/full stage.
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
    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class ResourceClaim(ContractModel):
    resource_id: str = Field(min_length=1, description="<robot_id>.<resource>, e.g. uav_01.motion")
    mode: ResourceMode
    shared_mode: str | None = Field(default=None, description="e.g. camera 'still' vs 'video'")

    def conflicts_with(self, other: ResourceClaim) -> bool:
        if self.resource_id != other.resource_id:
            return False
        if self.mode is ResourceMode.EXCLUSIVE or other.mode is ResourceMode.EXCLUSIVE:
            return True
        return self.shared_mode != other.shared_mode


class ImplementationKind(StrEnum):
    DETERMINISTIC = "deterministic"
    LEARNED = "learned"


class LearnedStage(StrEnum):
    SHADOW = "shadow"  # logs its output, never executes
    LIMITED = "limited"  # executes only in whitelisted scenarios with tighter envelopes
    FULL = "full"


class Implementation(ContractModel):
    kind: ImplementationKind = ImplementationKind.DETERMINISTIC
    learned_stage: LearnedStage | None = None
    model_version: str | None = None

    @model_validator(mode="after")
    def _learned_needs_stage(self) -> Implementation:
        if self.kind is ImplementationKind.LEARNED and self.learned_stage is None:
            raise ValueError("learned implementations must declare learned_stage (shadow|limited|full)")
        return self

    @property
    def may_execute(self) -> bool:
        return self.kind is ImplementationKind.DETERMINISTIC or self.learned_stage is not LearnedStage.SHADOW


class CancelSpec(ContractModel):
    cancel_safe_states: list[str] = Field(default_factory=list)
    cancel_procedure: str = Field(min_length=1, description="what the skill does to wind down when cancelled")


class PauseSpec(ContractModel):
    pausable: bool = False
    safe_wait_condition: str = ""
    max_wait_s: float | None = None


class EvidenceRequirement(ContractModel):
    evidence_type: str
    criteria: dict[str, Any] = Field(default_factory=dict)


class FailureMode(ContractModel):
    code: str
    description: str = ""
    recovery_hint: str = ""


class SkillManifest(ContractModel):
    skill_id: str
    version: str
    description: str = ""
    embodiments: list[Embodiment]
    params_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema with units")
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
    return target in ALLOWED_TRANSITIONS[current]


def find_resource_conflicts(active: Iterable[ResourceClaim], candidate: Iterable[ResourceClaim]) -> list[str]:
    """Resource ids that would be double-claimed if `candidate` started while `active` runs."""
    active = list(active)
    return sorted({c.resource_id for c in candidate for a in active if c.conflicts_with(a)})
