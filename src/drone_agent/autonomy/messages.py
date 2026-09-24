"""Domain models for `drone.autonomy.v1` (D039); every received frame is validated by one of these.

The proto only fixes the wire layout. These models carry the acceptance rules: finite numbers, bounded
horizons and time-to-live, uncertainty on every geometric claim, a declared implementation stage for every
trajectory source, and no ground truth anywhere. Field names match the proto exactly so the shared
presence-preserving codec (`runtime.wire`) can convert both ways.

`drone.autonomy.v1` 的领域模型（D039）；收到的每一帧都要由其中一个模型校验。

proto 只固定线路布局。这些模型承载接收规则：数值有限、时域与存活时间有界、每个几何断言都带不确定性、每个
轨迹来源都声明实现阶段，任何地方都不接受真值。字段名与 proto 完全一致，因此共享的存在性保留编解码器
（`runtime.wire`）可以双向转换。
"""

from __future__ import annotations

import math
import re
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

from drone_agent.contracts import ContractModel, FactSource, LearnedStage, WorldFact, WorldKind

TASK_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
MAX_SEGMENT_POINTS = 32
MAX_OBSTACLE_POINTS = 256
MAX_SETPOINT_TTL_MS = 500
MAX_SEGMENT_LIFETIME = timedelta(seconds=2)


def _finite(*values: float) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("non-finite value")


class Vector3(ContractModel):
    """A point or vector; the frame is carried by the enclosing message. / 点或向量；坐标系由外层消息给出。"""

    x: float
    y: float
    z: float

    @model_validator(mode="after")
    def _finite_components(self) -> Vector3:
        _finite(self.x, self.y, self.z)
        return self

    def as_list(self) -> list[float]:
        return [self.x, self.y, self.z]


class LocalTask(ContractModel):
    """What the local planner should do, within which scope, until when (guardian -> planner).

    局部规划节点应做什么、在什么范围内、到何时为止（guardian -> 规划节点）。
    """

    robot_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    mission_version: int = Field(ge=1)
    step_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=0)
    task_id: str = Field(pattern=TASK_ID.pattern)
    frame_id: str = Field(min_length=1)
    map_version: str = Field(min_length=1)
    goal: Vector3
    goal_tolerance_m: float = Field(gt=0, le=5)
    max_speed_mps: float = Field(gt=0, le=5)
    scope_min: Vector3
    scope_max: Vector3
    issued_at: AwareDatetime
    deadline: AwareDatetime
    active: bool = True

    @model_validator(mode="after")
    def _scope_contains_goal(self) -> LocalTask:
        low, high, goal = self.scope_min.as_list(), self.scope_max.as_list(), self.goal.as_list()
        if any(lo >= hi for lo, hi in zip(low, high, strict=True)):
            raise ValueError("empty task scope")
        if any(not lo <= g <= hi for lo, g, hi in zip(low, goal, high, strict=True)):
            raise ValueError("goal outside the task scope")
        if self.deadline <= self.issued_at:
            raise ValueError("task deadline must follow issuance")
        return self


class SegmentStatus(StrEnum):
    """Planner self-report; never a success claim. / 规划节点自报状态，从不代表成功。"""

    OK = "ok"
    REACHED = "reached"
    NO_PATH = "no_path"
    OVERLOADED = "overloaded"


class Implementation(StrEnum):
    """Deterministic planner or learned policy. / 确定性规划器或学习型策略。"""

    DETERMINISTIC = "deterministic"
    LEARNED = "learned"


class TrajectoryPoint(ContractModel):
    """One point of a short-horizon segment. / 短时域片段中的一个点。"""

    t_offset_s: float = Field(ge=0, le=5)
    position: Vector3


class TrajectorySegment(ContractModel):
    """A candidate short-horizon segment (planner -> guardian); the guardian filters it before any use.

    候选短时域轨迹片段（规划节点 -> guardian）；guardian 使用前必须过滤。
    """

    robot_id: str = Field(min_length=1)
    task_id: str = Field(pattern=TASK_ID.pattern)
    lease_epoch: int = Field(ge=0)
    segment_seq: int = Field(ge=0)
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    implementation: Implementation
    learned_stage: LearnedStage | None = None
    created_at: AwareDatetime
    valid_until: AwareDatetime
    frame_id: str = Field(min_length=1)
    map_version: str = Field(min_length=1)
    points: list[TrajectoryPoint] = Field(min_length=1, max_length=MAX_SEGMENT_POINTS)
    max_speed_mps: float = Field(gt=0, le=5)
    status: SegmentStatus
    cycle_ms: float = Field(ge=0)
    min_clearance_m: float | None = None

    @model_validator(mode="after")
    def _bounded_and_declared(self) -> TrajectorySegment:
        if self.implementation is Implementation.LEARNED and self.learned_stage is None:
            raise ValueError("a learned segment must declare its stage")
        if self.implementation is Implementation.DETERMINISTIC and self.learned_stage is not None:
            raise ValueError("a deterministic segment has no learned stage")
        if not self.created_at < self.valid_until <= self.created_at + MAX_SEGMENT_LIFETIME:
            raise ValueError("segment validity must be a short window after creation")
        offsets = [point.t_offset_s for point in self.points]
        if offsets != sorted(offsets) or len(set(offsets)) != len(offsets):
            raise ValueError("segment points must be strictly time-ordered")
        if self.min_clearance_m is not None:
            _finite(self.min_clearance_m)
        return self

    @property
    def may_execute(self) -> bool:
        """Shadow-stage policies only observe (D013). / 影子阶段策略只观察（D013）。"""
        return self.implementation is Implementation.DETERMINISTIC or self.learned_stage is not LearnedStage.SHADOW


class ObstacleSet(ContractModel):
    """Nearby occupied points from the local map, with uncertainty (map -> guardian).

    局部地图给出的邻近占据点及其不确定性（地图 -> guardian）。
    """

    robot_id: str = Field(min_length=1)
    frame_id: str = Field(min_length=1)
    map_version: str = Field(min_length=1)
    stamp: AwareDatetime
    valid_until: AwareDatetime
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    points: list[Vector3] = Field(default_factory=list, max_length=MAX_OBSTACLE_POINTS)
    point_radius_m: float = Field(ge=0, le=2)
    position_std_m: float = Field(gt=0, le=5, description="1-sigma position error / 位置误差 1σ")
    confidence: float = Field(ge=0, le=1)
    map_seq: int = Field(ge=0)
    coverage_radius_m: float = Field(gt=0, le=50, description="points beyond it are unknown, not free / 超出此半径为未知而非空闲")

    @model_validator(mode="after")
    def _ordered_window(self) -> ObstacleSet:
        if not self.stamp < self.valid_until <= self.stamp + MAX_SEGMENT_LIFETIME:
            raise ValueError("obstacle validity must be a short window after its stamp")
        return self


class LocalizationReport(ContractModel):
    """Localization health as the companion node sees it from PX4 estimator outputs.

    伴飞节点从 PX4 估计器输出看到的定位健康状态。
    """

    robot_id: str = Field(min_length=1)
    stamp: AwareDatetime
    valid_until: AwareDatetime
    gnss_ok: bool
    gnss_fix_type: int = Field(ge=0, le=8)
    gnss_satellites: int = Field(ge=0)
    gnss_eph_m: float | None = None
    visual_ok: bool
    ev_position_fused: bool
    gnss_position_fused: bool
    local_position_ok: bool
    position_std_m: float | None = None
    source_version: str = Field(min_length=1)
    px4_status_age_s: float = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> LocalizationReport:
        if not self.stamp < self.valid_until <= self.stamp + MAX_SEGMENT_LIFETIME:
            raise ValueError("report validity must be a short window after its stamp")
        if self.visual_ok and not (self.ev_position_fused and self.local_position_ok):
            raise ValueError("visual localization cannot be healthy without fused vision and a valid local position")
        for value in (self.gnss_eph_m, self.position_std_m):
            if value is not None:
                _finite(value)
        return self


class AuthorizedSetpoint(ContractModel):
    """The only message that lets a setpoint reach the flight controller (guardian -> egress node).

    唯一能让设定值到达飞控的消息（guardian -> 出口节点）。
    """

    robot_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    mission_version: int = Field(ge=1)
    step_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=0)
    command_seq: int = Field(ge=0)
    issued_at: AwareDatetime
    ttl_ms: int = Field(ge=1, le=MAX_SETPOINT_TTL_MS)
    position_ned: Vector3
    max_horizontal_speed_mps: float = Field(gt=0, le=5)
    max_vertical_speed_mps: float = Field(gt=0, le=3)


class EgressStatus(ContractModel):
    """What the egress node reports about its registration, link and counters.

    出口节点关于注册、链路与计数的报告。
    """

    robot_id: str = Field(min_length=1)
    stamp: AwareDatetime
    node_version: str = Field(min_length=1)
    registered: bool
    mode_nav_state: int = Field(ge=0, le=255)
    mode_active: bool
    fmu_link_ok: bool
    compatibility_ok: bool
    highest_epoch: int = Field(ge=-1)
    last_forwarded_seq: int = Field(ge=-1)
    forwarded_setpoints: int = Field(ge=0)
    rejected_stale: int = Field(ge=0)
    rejected_epoch: int = Field(ge=0)
    rejected_seq: int = Field(ge=0)
    watchdog_exits: int = Field(ge=0)
    last_watchdog_reason: str = ""
    armed: bool = False
    hold_commands: int = Field(ge=0, default=0)
    cant_run_reports: int = Field(ge=0, default=0)

    @property
    def external_nav_state(self) -> int | None:
        """The PX4 external nav state if it is in the valid range 23..30. / 若在 23..30 内则为 PX4 外部模式 nav_state。"""
        return self.mode_nav_state if self.registered and 23 <= self.mode_nav_state <= 30 else None


class BeliefFact(ContractModel):
    """A belief or candidate fact for the executive; truth, uncertainty-free positions and unversioned models are refused.

    交给 executive 的信念或候选事实；真值、缺不确定性的位置与未标版本的模型输出一律拒收。
    """

    producer_id: str = Field(min_length=1)
    fact: dict[str, Any]
    latency_ms: float = Field(ge=0)
    replan_trigger: bool = False

    @field_validator("fact")
    @classmethod
    def _belief_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        fact = WorldFact.model_validate(value)
        if fact.world_kind is not WorldKind.BELIEF or fact.source is FactSource.SIM_TRUTH:
            raise ValueError("only belief facts from sensors, models or humans may enter the runtime")
        if fact.valid_until is None:
            raise ValueError("a runtime fact needs an expiry")
        return value

    def world_fact(self) -> WorldFact:
        return WorldFact.model_validate(self.fact)


MODELS: dict[str, type[ContractModel]] = {
    "local_task": LocalTask,
    "trajectory_segment": TrajectorySegment,
    "obstacle_set": ObstacleSet,
    "localization_report": LocalizationReport,
    "authorized_setpoint": AuthorizedSetpoint,
    "egress_status": EgressStatus,
    "belief_fact": BeliefFact,
}
