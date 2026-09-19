"""CapabilityDescriptor (static capability) and RobotStatus (dynamic availability).

Solves: upper layers must never assume every drone/robot has the same capabilities; vendor
differences are surfaced through capability negotiation, not hidden behind a fake uniform API.

CapabilityDescriptor（静态能力）与 RobotStatus（动态可用性）。

解决的问题：上层不能假设所有无人机 / 机器人能力相同；厂商差异通过能力协商暴露，
而不是藏在一个假的统一接口后面。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import Field

from drone_agent.contracts.common import ContractModel, ControlMode, Embodiment


class RecoveryBehavior(StrEnum):
    """Verified baseline behaviors a platform can execute (the Simplex baseline set).

    平台可执行的已验证基线行为（Simplex 架构中的基线集合）。
    """

    HOLD = "hold"
    LOITER = "loiter"
    RTL = "rtl"
    LAND_HERE = "land_here"
    LAND_AT = "land_at"
    HANDOVER_TO_FC_FAILSAFE = "handover_to_fc_failsafe"


class SkillRef(ContractModel):
    """A skill the robot really has, with its version. / 机器人真实具备的技能及其版本。"""

    skill_id: str
    version: str


class Limits(ContractModel):
    """Hard platform limits used by admission and the compiler. / 准入与编译器使用的平台硬限制。"""

    max_speed_mps: float
    max_altitude_m_agl: float | None = None
    endurance_s: float
    max_wind_mps: float | None = None
    max_payload_kg: float | None = None
    min_turn_radius_m: float | None = None
    can_hover: bool = True


class SensorSpec(ContractModel):
    """A sensor and the frame it reports in. / 传感器及其报告所用的坐标系。"""

    sensor_id: str
    kind: str
    frame_id: str
    nominal_accuracy: dict[str, float] = Field(default_factory=dict)


class PayloadSpec(ContractModel):
    """A payload and what it can do. / 载荷及其能力。"""

    payload_id: str
    kind: str
    capabilities: list[str] = Field(default_factory=list)


class CapabilityDescriptor(ContractModel):
    """Static description of what a robot can do, published by its platform adapter.

    机器人能做什么的静态描述，由其平台适配器发布。
    """

    robot_id: str
    embodiment: Embodiment
    interface_version: str
    control_modes: set[ControlMode] = Field(default_factory=set)
    skills: list[SkillRef] = Field(default_factory=list)
    sensors: list[SensorSpec] = Field(default_factory=list)
    payloads: list[PayloadSpec] = Field(default_factory=list)
    limits: Limits
    recovery_behaviors: set[RecoveryBehavior] = Field(default_factory=set)
    vendor: str = ""
    model: str = ""
    vendor_constraints: list[str] = Field(default_factory=list)

    def missing_for(self, *, skills: Iterable[str] = (), control_modes: Iterable[ControlMode] = ()) -> list[str]:
        """Return human-readable gaps; an empty list means the robot can take the assignment.

        返回可读的能力缺口；空列表表示该机器人可以接受这项分配。
        """
        have_skills = {s.skill_id for s in self.skills}
        gaps = [f"skill:{s}" for s in skills if s not in have_skills]
        gaps += [f"control_mode:{m.value}" for m in control_modes if m not in self.control_modes]
        return gaps


class LocalizationHealth(ContractModel):
    """Localization quality as the guardian sees it. / guardian 视角下的定位质量。"""

    fix_type: str = "none"
    position_std_m: float | None = None
    healthy: bool = False


class EnergyState(ContractModel):
    """Remaining energy against the required reserve. / 剩余能源与所需余量的关系。"""

    remaining_fraction: float = Field(ge=0.0, le=1.0)
    remaining_energy_wh: float | None = None
    reserve_required_fraction: float = Field(default=0.2, ge=0.0, le=1.0)

    @property
    def above_reserve(self) -> bool:
        """True while remaining energy exceeds the reserve. / 剩余能源高于余量时为真。"""
        return self.remaining_fraction > self.reserve_required_fraction


class CommsState(ContractModel):
    """Uplink health. / 上行链路健康状态。"""

    uplink_ok: bool
    latency_ms: float | None = None
    last_seen: datetime | None = None


class RobotStatus(ContractModel):
    """Dynamic state published at ~1 Hz; the coordinator combines it with CapabilityDescriptor.

    约 1 Hz 发布的动态状态；协调器把它与 CapabilityDescriptor 结合使用。
    """

    robot_id: str
    timestamp: datetime
    flight_phase: str = "unknown"
    control_mode: ControlMode = ControlMode.NONE
    energy: EnergyState
    localization: LocalizationHealth
    comms: CommsState
    lease_epoch: int | None = None
    active_skill_instance: str | None = None
    held_resources: list[str] = Field(default_factory=list)
    fc_failsafe_active: bool = False
