"""Authority / TaskLease, idempotency keys and the control-egress gate.

Solves: duplicate execution, replay of stale commands, and contention between control sources.
The gate is pure logic (no I/O) so the guardian can call it on every write and tests can pin
its behaviour. Authority priority, highest first:
fc_failsafe > manual_takeover > guardian_recovery > leased_executive > other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from drone_agent.contracts.common import ContractModel

AUTHORITY_PRIORITY: tuple[str, ...] = (
    "fc_failsafe",
    "manual_takeover",
    "guardian_recovery",
    "leased_executive",
    "other",
)


class TaskLease(ContractModel):
    robot_id: str
    mission_id: str
    mission_version: int
    lease_epoch: int = Field(ge=0, description="control-authority generation; bumps on every re-grant")
    holder: str
    issued_at: datetime
    expires_at: datetime
    resources: list[str] = Field(default_factory=list)

    def is_valid_at(self, now: datetime) -> bool:
        return self.issued_at <= now < self.expires_at


class IdempotencyKey(ContractModel):
    mission_id: str
    mission_version: int
    step_id: str
    command_id: str
    robot_id: str
    lease_epoch: int

    def as_string(self) -> str:
        return (
            f"{self.mission_id}:{self.mission_version}:{self.step_id}:{self.command_id}:{self.robot_id}:{self.lease_epoch}"
        )


class ControlCommandEnvelope(ContractModel):
    """The only shape the control egress accepts. A MissionSpec can never be one of these."""

    key: IdempotencyKey
    command_seq: int = Field(ge=0, description="monotonic per lease epoch")
    issued_at: datetime
    valid_until: datetime
    intent_kind: str = Field(description="mode_request | route | local_goal | trajectory_segment")
    payload: dict[str, Any] = Field(default_factory=dict)


class EgressDecision(ContractModel):
    accepted: bool
    reason: str = ""


class EgressGate:
    """Stateful per-robot gate used by the guardian before any write reaches the platform adapter."""

    def __init__(self, lease: TaskLease | None = None) -> None:
        self._lease = lease
        self._last_seq: int = -1
        self._seen: set[str] = set()

    @property
    def lease(self) -> TaskLease | None:
        return self._lease

    def grant(self, lease: TaskLease) -> None:
        """Install a new lease; a higher epoch resets the sequence window and the dedup set."""
        if self._lease is not None and lease.lease_epoch < self._lease.lease_epoch:
            raise ValueError("cannot grant a lease with a lower epoch than the current one")
        if self._lease is None or lease.lease_epoch != self._lease.lease_epoch:
            self._last_seq = -1
            self._seen.clear()
        self._lease = lease

    def revoke(self) -> None:
        self._lease = None

    def check(self, envelope: ControlCommandEnvelope, *, now: datetime) -> EgressDecision:
        lease = self._lease
        if lease is None:
            return EgressDecision(accepted=False, reason="no_lease")
        if not lease.is_valid_at(now):
            return EgressDecision(accepted=False, reason="lease_expired")
        key = envelope.key
        if key.robot_id != lease.robot_id:
            return EgressDecision(accepted=False, reason="robot_mismatch")
        if key.mission_id != lease.mission_id or key.mission_version != lease.mission_version:
            return EgressDecision(accepted=False, reason="mission_mismatch")
        if key.lease_epoch < lease.lease_epoch:
            return EgressDecision(accepted=False, reason="stale_epoch")
        if key.lease_epoch > lease.lease_epoch:
            return EgressDecision(accepted=False, reason="unknown_epoch")
        if envelope.valid_until <= now:
            return EgressDecision(accepted=False, reason="expired_intent")
        if key.as_string() in self._seen:
            # Duplicate delivery: never re-execute; the caller reconciles against recorded outcome.
            return EgressDecision(accepted=False, reason="duplicate_command")
        if envelope.command_seq <= self._last_seq:
            return EgressDecision(accepted=False, reason="stale_seq")
        self._last_seq = envelope.command_seq
        self._seen.add(key.as_string())
        return EgressDecision(accepted=True)
