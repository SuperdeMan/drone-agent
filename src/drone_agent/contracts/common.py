"""Shared building blocks for every contract object.

Rules (docs/architecture/02-contracts.md §0):
- every contract object carries ``schema_version``;
- anything spatial carries a ``Frame`` (frame_id + map_version);
- anything temporal carries ``timestamp`` and, where applicable, ``valid_until``;
- receivers ignore unknown fields so additive schema evolution stays backward compatible.

所有契约对象共用的基础构件。

规则（docs/architecture/02-contracts.md §0）：
- 每个契约对象都带 ``schema_version``；
- 任何空间信息都带 ``Frame``（frame_id + map_version）；
- 任何时间信息都带 ``timestamp``，适用时再带 ``valid_until``；
- 接收方忽略未知字段，使增量式 schema 演进保持向后兼容。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "0.1.0"


def utcnow() -> datetime:
    """Timezone-aware UTC now; contracts never use naive datetimes.

    带时区的 UTC 当前时间；契约中不使用无时区的 datetime。
    """
    return datetime.now(tz=timezone.utc)


class ContractModel(BaseModel):
    """Base for all contract objects: versioned, tolerant to unknown fields, validated on assignment.

    所有契约对象的基类：带版本号、容忍未知字段、赋值时校验。
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    schema_version: str = CONTRACT_VERSION


class Embodiment(StrEnum):
    """Robot body types the contracts distinguish. / 契约区分的机器人本体类型。"""

    AERIAL_MULTIROTOR = "aerial_multirotor"
    AERIAL_VTOL = "aerial_vtol"
    AERIAL_FIXED_WING = "aerial_fixed_wing"
    GROUND_WHEELED = "ground_wheeled"
    GROUND_LEGGED = "ground_legged"
    MANIPULATOR = "manipulator"


class ControlMode(StrEnum):
    """Control modes a platform adapter may declare. Absence means unsupported; never fake one.

    平台适配器可声明的控制模式。缺席即不支持，绝不伪造。
    """

    MISSION_UPLOAD = "mission_upload"
    OFFBOARD_POSITION = "offboard_position"
    OFFBOARD_VELOCITY = "offboard_velocity"
    OFFBOARD_TRAJECTORY = "offboard_trajectory"
    EXTERNAL_MODE = "external_mode"  # px4_ros2 external modes / px4_ros2 外部模式
    NAV_TO_POSE = "nav_to_pose"  # Nav2 ground robots / Nav2 地面机器人
    NONE = "none"


class WorldKind(StrEnum):
    """Which of the three worlds a fact belongs to. / 事实属于三种世界中的哪一种。"""

    TRUTH = "truth"  # simulator ground truth: judge process only / 仿真真值：仅裁判进程可用
    BELIEF = "belief"  # perceived or estimated: runtime may use, with time + uncertainty / 感知估计：运行时可用，须带时间与不确定性
    PREDICTED = "predicted"  # model prediction: candidate evaluation only, never a precondition / 模型预测：只用于候选评估，不作前置条件


class FactSource(StrEnum):
    """Where a fact came from. / 事实的来源。"""

    SENSOR = "sensor"
    MODEL = "model"
    HUMAN = "human"
    SIM_TRUTH = "sim_truth"


class Frame(ContractModel):
    """Coordinate frame plus map version; a position without both is not a fact.

    坐标系加地图版本；缺任一项的位置都不是事实。
    """

    frame_id: str = Field(min_length=1)
    map_version: str = Field(min_length=1)


class Position(ContractModel):
    """3D position with optional row-major 3x3 covariance. / 三维位置，可带行优先 3x3 协方差。"""

    x: float
    y: float
    z: float
    covariance: tuple[float, ...] | None = Field(default=None, description="row-major 3x3, 9 values / 行优先 3x3，共 9 个值")

    @field_validator("covariance")
    @classmethod
    def _covariance_shape(cls, value: tuple[float, ...] | None) -> tuple[float, ...] | None:
        # Only the shape is checked here; positive semi-definiteness is the producer's job.
        # 这里只检查形状；半正定性由生产方保证。
        if value is not None and len(value) != 9:
            raise ValueError("position covariance must be a row-major 3x3 (9 values)")
        return value


class Quaternion(ContractModel):
    """Orientation as (x, y, z, w) in the ENU convention. / ENU 约定下的 (x, y, z, w) 四元数。"""

    x: float
    y: float
    z: float
    w: float


class Pose(ContractModel):
    """Position (and optional orientation) in an explicit frame. / 显式坐标系下的位置（可选姿态）。"""

    frame: Frame
    position: Position
    orientation: Quaternion | None = None


class TimeWindow(ContractModel):
    """A timestamp with an optional expiry. / 带可选有效期的时间戳。"""

    timestamp: datetime
    valid_until: datetime | None = None

    def is_fresh(self, now: datetime | None = None) -> bool:
        """True while `now` is before `valid_until` (no expiry means always fresh).

        `now` 未超过 `valid_until` 时为真（无有效期视为一直新鲜）。
        """
        now = now or utcnow()
        return self.valid_until is None or now <= self.valid_until
