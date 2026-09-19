"""ExecutionEvent / Evidence and the three-way verdict.

`execution_status` describes the command and skill process, `effect_verdict` whether the
physical effect was confirmed, `safety_verdict` whether the guardian allows the next phase.
"Call succeeded" is never "task succeeded"; UNKNOWN is never success.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from drone_agent.contracts.common import ContractModel, Pose, TimeWindow


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class EffectVerdict(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


class SafetyVerdict(StrEnum):
    PROCEED = "proceed"
    HOLD = "hold"
    RECOVER = "recover"
    ABORT = "abort"


class EventType(StrEnum):
    COMMAND_ACCEPTED = "command_accepted"
    COMMAND_REJECTED = "command_rejected"
    SKILL_STARTED = "skill_started"
    SKILL_PROGRESS = "skill_progress"
    SKILL_PAUSED = "skill_paused"
    SKILL_CANCEL_REQUESTED = "skill_cancel_requested"
    SKILL_COMPLETED = "skill_completed"
    SKILL_FAILED = "skill_failed"
    EFFECT_VERIFIED = "effect_verified"
    EFFECT_REFUTED = "effect_refuted"
    EFFECT_UNKNOWN = "effect_unknown"
    SAFETY_INTERVENTION = "safety_intervention"
    LEASE_ISSUED = "lease_issued"
    LEASE_EXPIRED = "lease_expired"
    LEASE_REVOKED = "lease_revoked"
    RECOVERED_TO = "recovered_to"
    HANDOFF_OFFERED = "handoff_offered"
    HANDOFF_ACCEPTED = "handoff_accepted"
    HANDOFF_REJECTED = "handoff_rejected"
    HANDOFF_COMPLETED = "handoff_completed"
    HANDOFF_TIMEOUT = "handoff_timeout"


class StepOutcome(ContractModel):
    mission_id: str
    mission_version: int
    step_id: str
    robot_id: str
    execution_status: ExecutionStatus
    effect_verdict: EffectVerdict = EffectVerdict.UNKNOWN
    safety_verdict: SafetyVerdict = SafetyVerdict.PROCEED

    @property
    def counts_as_completed(self) -> bool:
        """The only combination a report may list under 'completed'."""
        return self.execution_status is ExecutionStatus.SUCCEEDED and self.effect_verdict is EffectVerdict.VERIFIED


def may_run_successor(
    predecessor: StepOutcome,
    *,
    current_safety: SafetyVerdict,
    edge_allows_unverified: bool = False,
) -> bool:
    """DAG gating rule (docs/architecture/02-contracts.md §5).

    Successor runs iff predecessor succeeded AND its effect is verified (or merely unverified on an
    edge that admission explicitly allowed) AND the guardian currently says proceed. UNKNOWN and
    REFUTED never pass, whatever the edge says.
    """
    if current_safety is not SafetyVerdict.PROCEED:
        return False
    if predecessor.execution_status is not ExecutionStatus.SUCCEEDED:
        return False
    if predecessor.effect_verdict is EffectVerdict.VERIFIED:
        return True
    if predecessor.effect_verdict is EffectVerdict.UNVERIFIED and edge_allows_unverified:
        return True
    return False


class ExecutionEvent(ContractModel):
    event_id: str
    event_type: EventType
    timestamp: datetime
    mission_id: str
    mission_version: int
    robot_id: str
    step_id: str | None = None
    command_id: str | None = None
    lease_epoch: int | None = None
    execution_status: ExecutionStatus | None = None
    effect_verdict: EffectVerdict | None = None
    safety_verdict: SafetyVerdict | None = None
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class Evidence(ContractModel):
    evidence_id: str
    kind: str = Field(description="image | video | telemetry | pointcloud")
    media_ref: str
    sha256: str
    time_window: TimeWindow
    captured_pose: Pose | None = None
    subject_ids: list[str] = Field(default_factory=list)
    quality: dict[str, float] = Field(default_factory=dict)
    produced_by_skill_instance: str
