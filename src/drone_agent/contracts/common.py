"""Shared building blocks for every contract object.

Rules (docs/architecture/02-contracts.md §0):
- every contract object carries ``schema_version``;
- anything spatial carries a ``Frame`` (frame_id + map_version);
- anything temporal carries ``timestamp`` and, where applicable, ``valid_until``;
- receivers ignore unknown fields so additive schema evolution stays backward compatible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "0.1.0"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class ContractModel(BaseModel):
    """Base for all contract objects: versioned, tolerant to unknown fields, validated on assignment."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    schema_version: str = CONTRACT_VERSION


class Embodiment(StrEnum):
    AERIAL_MULTIROTOR = "aerial_multirotor"
    AERIAL_VTOL = "aerial_vtol"
    AERIAL_FIXED_WING = "aerial_fixed_wing"
    GROUND_WHEELED = "ground_wheeled"
    GROUND_LEGGED = "ground_legged"
    MANIPULATOR = "manipulator"


class ControlMode(StrEnum):
    """Control modes a platform adapter may declare. Absence means unsupported; never fake one."""

    MISSION_UPLOAD = "mission_upload"
    OFFBOARD_POSITION = "offboard_position"
    OFFBOARD_VELOCITY = "offboard_velocity"
    OFFBOARD_TRAJECTORY = "offboard_trajectory"
    EXTERNAL_MODE = "external_mode"  # px4_ros2 external modes
    NAV_TO_POSE = "nav_to_pose"  # Nav2 ground robots
    NONE = "none"


class WorldKind(StrEnum):
    TRUTH = "truth"  # simulator ground truth: judge process only
    BELIEF = "belief"  # perceived/estimated: runtime may use, with time + uncertainty
    PREDICTED = "predicted"  # model prediction: candidate evaluation only, never a precondition


class FactSource(StrEnum):
    SENSOR = "sensor"
    MODEL = "model"
    HUMAN = "human"
    SIM_TRUTH = "sim_truth"


class Frame(ContractModel):
    frame_id: str = Field(min_length=1)
    map_version: str = Field(min_length=1)


class Position(ContractModel):
    x: float
    y: float
    z: float
    covariance: tuple[float, ...] | None = Field(default=None, description="row-major 3x3, 9 values")

    @field_validator("covariance")
    @classmethod
    def _covariance_shape(cls, value: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if value is not None and len(value) != 9:
            raise ValueError("position covariance must be a row-major 3x3 (9 values)")
        return value


class Quaternion(ContractModel):
    x: float
    y: float
    z: float
    w: float


class Pose(ContractModel):
    frame: Frame
    position: Position
    orientation: Quaternion | None = None


class TimeWindow(ContractModel):
    timestamp: datetime
    valid_until: datetime | None = None

    def is_fresh(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.valid_until is None or now <= self.valid_until
