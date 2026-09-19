"""ExecutionEvent / Evidence and the three-way verdict.

`execution_status` describes the command and skill process, `effect_verdict` whether the
physical effect was confirmed, `safety_verdict` whether the guardian allows the next phase.
"Call succeeded" is never "task succeeded"; UNKNOWN is never success.

ExecutionEvent / Evidence 与三元判定。

`execution_status` 描述命令与技能过程，`effect_verdict` 描述物理效果是否被证实，
`safety_verdict` 描述 guardian 是否允许进入下一阶段。「调用成功」永远不等于「任务成功」；
UNKNOWN 永远不是成功。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from drone_agent.contracts.common import ContractModel, Pose, TimeWindow


class ExecutionStatus(StrEnum):
    """State of the command / skill process itself. / 命令与技能过程本身的状态。"""

    PENDING = "pending"
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class EffectVerdict(StrEnum):
    """Whether the intended physical effect was confirmed. / 预期物理效果是否被证实。"""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


class SafetyVerdict(StrEnum):
    """What the guardian currently allows. / guardian 当前允许的动作。"""

    PROCEED = "proceed"
    HOLD = "hold"
    RECOVER = "recover"
    ABORT = "abort"


class EventType(StrEnum):
    """Minimal event vocabulary shared by executive, guardian, coordinator and ledger.

    执行器、guardian、协调器与账本共用的最小事件词表。
    """

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
    """The three verdicts for one step. / 单个步骤的三元判定。"""

    mission_id: str
    mission_version: int
    step_id: str
    robot_id: str
    execution_status: ExecutionStatus
    effect_verdict: EffectVerdict = EffectVerdict.UNKNOWN
    safety_verdict: SafetyVerdict = SafetyVerdict.PROCEED

    @property
    def counts_as_completed(self) -> bool:
        """The only combination a report may list under 'completed'.

        报告中「已完成」一栏唯一可接受的组合。
        """
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

    DAG 放行规则（docs/architecture/02-contracts.md §5）。

    后继运行当且仅当：前驱已成功，且其效果已被证实（或在准入明确允许的边上仅为 unverified），
    且 guardian 当前判定为 proceed。UNKNOWN 与 REFUTED 无论边如何标注都不放行。
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
    """One append-only event in the mission event stream. / 任务事件流中的一条只追加事件。"""

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
    """A hashed, pose- and time-bound artifact tied to the skill instance that produced it.

    带哈希、绑定位姿与时间、并关联到生产它的技能实例的证据。
    """

    evidence_id: str
    kind: str = Field(description="image | video | telemetry | pointcloud / 影像 | 视频 | 遥测 | 点云")
    media_ref: str
    sha256: str
    time_window: TimeWindow
    captured_pose: Pose | None = None
    subject_ids: list[str] = Field(default_factory=list)
    quality: dict[str, float] = Field(default_factory=dict)
    produced_by_skill_instance: str
