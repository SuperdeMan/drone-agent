"""WorldFact / Observation contracts and per-embodiment state payloads.

Nothing without a frame, a timestamp, a source and (for positions) a covariance is a fact.
Truth-world facts are for the judge process only; predicted-world facts never satisfy
preconditions.

WorldFact / Observation 契约与按本体区分的状态载荷。

没有坐标系、时间、来源，以及（对位置而言）协方差的信息都不是事实。
真值世界的事实只给裁判进程；预测世界的事实永远不能满足前置条件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from drone_agent.contracts.capability import EnergyState, LocalizationHealth
from drone_agent.contracts.common import (
    ContractModel,
    ControlMode,
    FactSource,
    Frame,
    Pose,
    Position,
    WorldKind,
    utcnow,
)


class ObservationHeader(ContractModel):
    """Common header shared by every embodiment state; payloads are typed per embodiment.

    所有本体状态共用的公共头；载荷按本体分别定型。
    """

    robot_id: str
    timestamp: datetime
    valid_until: datetime | None = None
    frame: Frame
    source: FactSource = FactSource.SENSOR
    source_version: str | None = None

    def is_fresh(self, now: datetime | None = None) -> bool:
        """True while the observation has not expired. / 观测尚未过期时为真。"""
        now = now or utcnow()
        return self.valid_until is None or now <= self.valid_until


class AerialState(ContractModel):
    """Typed state of an aerial robot. / 空中机器人的定型状态。"""

    header: ObservationHeader
    pose: Pose
    velocity_mps: tuple[float, float, float] = (0.0, 0.0, 0.0)
    flight_phase: str = "unknown"
    control_mode: ControlMode = ControlMode.NONE
    armed: bool = False
    fc_failsafe_active: bool = False
    localization: LocalizationHealth = Field(default_factory=LocalizationHealth)
    energy: EnergyState


class GroundMobileState(ContractModel):
    """Typed state of a ground mobile robot. / 地面移动机器人的定型状态。"""

    header: ObservationHeader
    pose: Pose
    velocity_mps: tuple[float, float, float] = (0.0, 0.0, 0.0)
    traversability_ok: bool | None = Field(
        default=None, description="local judgement; never inferred from the air / 本地判断，不能由空中推断"
    )
    localization: LocalizationHealth = Field(default_factory=LocalizationHealth)
    energy: EnergyState


class WorldFact(ContractModel):
    """A subject-predicate-value triple with space, time, source, confidence and world kind.

    带空间、时间、来源、置信度与世界类型的「主语-谓词-值」三元组。
    """

    fact_id: str
    subject: str
    predicate: str
    value: Any = None
    frame: Frame | None = None
    position: Position | None = None
    timestamp: datetime
    valid_until: datetime | None = None
    source: FactSource
    source_version: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    world_kind: WorldKind = WorldKind.BELIEF
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _spatial_facts_need_uncertainty(self) -> WorldFact:
        # Positioned facts need a frame and a covariance; model facts need a version;
        # sim-truth facts cannot pose as belief. / 带位置的事实需要坐标系与协方差；
        # 模型来源需要版本；仿真真值不能冒充 belief。
        if self.position is not None:
            if self.frame is None:
                raise ValueError("a positioned fact needs a frame")
            if self.position.covariance is None:
                raise ValueError("a positioned fact needs a covariance; uncertainty-free positions are not facts")
        if self.source is FactSource.MODEL and not self.source_version:
            raise ValueError("model-sourced facts must carry source_version")
        if self.source is FactSource.SIM_TRUTH and self.world_kind is not WorldKind.TRUTH:
            raise ValueError("sim_truth facts must be world_kind=truth")
        return self

    def is_valid_at(self, now: datetime | None = None) -> bool:
        """True while the fact has not expired. / 事实尚未过期时为真。"""
        now = now or utcnow()
        return self.valid_until is None or now <= self.valid_until

    def usable_as_precondition(self, *, now: datetime | None = None, judge_process: bool = False) -> bool:
        """Runtime rule: only fresh belief facts; truth only inside the judge; predicted never.

        运行时规则：只接受新鲜的 belief 事实；truth 只在裁判进程内可用；predicted 永远不可用。
        """
        if self.world_kind is WorldKind.PREDICTED:
            return False
        if self.world_kind is WorldKind.TRUTH and not judge_process:
            return False
        return self.is_valid_at(now)
