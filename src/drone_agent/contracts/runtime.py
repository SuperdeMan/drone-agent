"""Typed observations and local mission operations for M1.

M1 的类型化观测与本地任务操作。
"""

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from drone_agent.contracts.common import ContractModel, Pose


class FlightObservation(ContractModel):
    timestamp: AwareDatetime
    valid_until: AwareDatetime
    sample_id: int = Field(ge=0)
    robot_id: str
    pose: Pose | None = None
    velocity_enu_mps: list[float] = Field(default_factory=list)
    armed: bool | None = None
    in_air: bool | None = None
    flight_mode: str = "UNKNOWN"
    localization_healthy: bool = False
    home_healthy: bool = False
    battery_fraction: float | None = Field(default=None, ge=0, le=1)
    mission_current: int = 0
    mission_total: int = 0
    fc_failsafe: bool = False
    source: str = "px4_telemetry"

    @model_validator(mode="after")
    def _finite_belief(self):
        import math

        if self.source not in {"px4_telemetry", "deterministic_test"}:
            raise ValueError("only estimated observations may enter the runtime")
        values = list(self.velocity_enu_mps)
        if self.pose:
            values += [self.pose.position.x, self.pose.position.y, self.pose.position.z]
            values += list(self.pose.position.covariance or ())
        if any(not math.isfinite(value) for value in values):
            raise ValueError("non-finite observation")
        if self.velocity_enu_mps and len(self.velocity_enu_mps) != 3:
            raise ValueError("velocity needs three ENU components")
        return self


class MissionAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


class MissionOperation(ContractModel):
    robot_id: str
    mission_id: str
    mission_version: int
    lease_epoch: int
    executive_instance: str
    action: MissionAction
    request_id: str


class ObservationRequest(ContractModel):
    robot_id: str
