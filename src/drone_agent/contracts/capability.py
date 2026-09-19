"""CapabilityDescriptor (static capability) and RobotStatus (dynamic availability).

Solves: upper layers must never assume every drone/robot has the same capabilities; vendor
differences are surfaced through capability negotiation, not hidden behind a fake uniform API.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import Field

from drone_agent.contracts.common import ContractModel, ControlMode, Embodiment


class RecoveryBehavior(StrEnum):
    """Verified baseline behaviors a platform can execute (Simplex baseline set)."""

    HOLD = "hold"
    LOITER = "loiter"
    RTL = "rtl"
    LAND_HERE = "land_here"
    LAND_AT = "land_at"
    HANDOVER_TO_FC_FAILSAFE = "handover_to_fc_failsafe"


class SkillRef(ContractModel):
    skill_id: str
    version: str


class Limits(ContractModel):
    max_speed_mps: float
    max_altitude_m_agl: float | None = None
    endurance_s: float
    max_wind_mps: float | None = None
    max_payload_kg: float | None = None
    min_turn_radius_m: float | None = None
    can_hover: bool = True


class SensorSpec(ContractModel):
    sensor_id: str
    kind: str
    frame_id: str
    nominal_accuracy: dict[str, float] = Field(default_factory=dict)


class PayloadSpec(ContractModel):
    payload_id: str
    kind: str
    capabilities: list[str] = Field(default_factory=list)


class CapabilityDescriptor(ContractModel):
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
        """Return human-readable gaps; empty list means the robot can take the assignment."""
        have_skills = {s.skill_id for s in self.skills}
        gaps = [f"skill:{s}" for s in skills if s not in have_skills]
        gaps += [f"control_mode:{m.value}" for m in control_modes if m not in self.control_modes]
        return gaps


class LocalizationHealth(ContractModel):
    fix_type: str = "none"
    position_std_m: float | None = None
    healthy: bool = False


class EnergyState(ContractModel):
    remaining_fraction: float = Field(ge=0.0, le=1.0)
    remaining_energy_wh: float | None = None
    reserve_required_fraction: float = Field(default=0.2, ge=0.0, le=1.0)

    @property
    def above_reserve(self) -> bool:
        return self.remaining_fraction > self.reserve_required_fraction


class CommsState(ContractModel):
    uplink_ok: bool
    latency_ms: float | None = None
    last_seen: datetime | None = None


class RobotStatus(ContractModel):
    """Dynamic state published at ~1 Hz; the coordinator combines it with CapabilityDescriptor."""

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
