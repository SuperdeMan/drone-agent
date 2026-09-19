"""WorldFact / Observation contracts and per-embodiment state payloads.

Nothing without a frame, a timestamp, a source and (for positions) a covariance is a fact.
Truth-world facts are for the judge process only; predicted-world facts never satisfy
preconditions.
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
    """Common header shared by every embodiment state; payloads are typed per embodiment."""

    robot_id: str
    timestamp: datetime
    valid_until: datetime | None = None
    frame: Frame
    source: FactSource = FactSource.SENSOR
    source_version: str | None = None

    def is_fresh(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.valid_until is None or now <= self.valid_until


class AerialState(ContractModel):
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
    header: ObservationHeader
    pose: Pose
    velocity_mps: tuple[float, float, float] = (0.0, 0.0, 0.0)
    traversability_ok: bool | None = Field(default=None, description="local judgement; never inferred from the air")
    localization: LocalizationHealth = Field(default_factory=LocalizationHealth)
    energy: EnergyState


class WorldFact(ContractModel):
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
        now = now or utcnow()
        return self.valid_until is None or now <= self.valid_until

    def usable_as_precondition(self, *, now: datetime | None = None, judge_process: bool = False) -> bool:
        """Runtime rule: only fresh belief facts; truth only inside the judge; predicted never."""
        if self.world_kind is WorldKind.PREDICTED:
            return False
        if self.world_kind is WorldKind.TRUTH and not judge_process:
            return False
        return self.is_valid_at(now)
