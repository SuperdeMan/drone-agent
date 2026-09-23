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


class OperatorRequest(ContractModel):
    """A pause/resume/cancel request delivered by the mission service to one robot (M2).

    It is bound to the exact mission version, control epoch and step the operator saw, and it
    expires. The uplink only relays it into the onboard operator mailbox; the executive and the
    guardian re-check the binding and validity, so a stale or retargeted request is rejected
    rather than applied to whatever is running now.

    由任务服务下发给某台机器人的暂停 / 恢复 / 取消请求（M2）。

    它绑定到操作者看到的确切任务版本、控制权代次与步骤，并且会过期。uplink 只把它转写进
    机载操作者信箱；executive 与 guardian 会再次核对绑定与时效，因此过期或目标已变的请求
    被拒绝，而不是作用到当前正在执行的内容上。
    """

    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    robot_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    mission_version: int = Field(ge=1)
    lease_epoch: int = Field(ge=0)
    step_id: str = Field(min_length=1)
    action: MissionAction
    valid_until: AwareDatetime
    requested_by: str = Field(min_length=1, description="authenticated operator identity / 已认证的操作者身份")
