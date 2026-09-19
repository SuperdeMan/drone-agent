"""Authority / TaskLease, idempotency keys and the control-egress gate.

Solves: duplicate execution, replay of stale commands, and contention between control sources.
The gate is pure logic (no I/O) so the guardian can call it on every write and tests can pin
its behaviour. Authority priority, highest first:
fc_failsafe > manual_takeover > guardian_recovery > leased_executive > other.

控制权 / TaskLease、幂等键与控制出口闸门。

解决的问题：重复执行、旧指令重放、多个控制源争用。闸门是纯逻辑（无 I/O），
guardian 每次写入前都可调用，测试也能钉住其行为。控制权优先级从高到低：
fc_failsafe > manual_takeover > guardian_recovery > leased_executive > other。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from drone_agent.contracts.common import ContractModel

AUTHORITY_PRIORITY: tuple[str, ...] = (
    "fc_failsafe",
    "manual_takeover",
    "guardian_recovery",
    "leased_executive",
    "other",
)


class TaskLease(ContractModel):
    """Who may control a robot for which mission version, until when.

    谁在什么期限内、以哪个任务版本控制某台机器人。
    """

    robot_id: str
    mission_id: str
    mission_version: int
    lease_epoch: int = Field(
        ge=0, description="control-authority generation; bumps on every re-grant / 控制权代次，每次重新授予 +1"
    )
    holder: str
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    resources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_window(self) -> TaskLease:
        if self.expires_at <= self.issued_at:
            raise ValueError("lease expiry must follow issuance")
        return self

    def is_valid_at(self, now: datetime) -> bool:
        """Valid within [issued_at, expires_at). / 在 [issued_at, expires_at) 内有效。"""
        return self.issued_at <= now < self.expires_at


class IdempotencyKey(ContractModel):
    """The six-part identity of a command; never just skill name + params.

    命令的六元身份；绝不只用技能名 + 参数。
    """

    mission_id: str
    mission_version: int
    step_id: str
    command_id: str
    robot_id: str
    lease_epoch: int

    def as_string(self) -> str:
        """Stable string form used as the dedup key. / 用作去重键的稳定字符串形式。"""
        return json.dumps(
            [self.mission_id, self.mission_version, self.step_id, self.command_id, self.robot_id, self.lease_epoch],
            ensure_ascii=False,
            separators=(",", ":"),
        )


class ControlCommandEnvelope(ContractModel):
    """The only shape the control egress accepts. A MissionSpec can never be one of these.

    控制出口唯一接受的形态。MissionSpec 永远不可能是它。
    """

    key: IdempotencyKey
    command_seq: int = Field(ge=0, description="monotonic per lease epoch / 在同一租约代次内单调递增")
    issued_at: AwareDatetime
    valid_until: AwareDatetime
    intent_kind: str = Field(
        description="mode_request | route | local_goal | trajectory_segment / 模式请求 | 航线 | 局部目标 | 轨迹片段"
    )
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ordered_window(self) -> ControlCommandEnvelope:
        if self.valid_until <= self.issued_at:
            raise ValueError("intent expiry must follow issuance")
        return self


class EgressDecision(ContractModel):
    """Accept or reject, with a machine-readable reason. / 接受或拒绝，附机器可读的原因。"""

    accepted: bool
    reason: str = ""


class EgressGate:
    """Stateful per-robot gate used by the guardian before any write reaches the platform adapter.

    guardian 在任何写入到达平台适配器之前使用的、按机器人维护状态的闸门。
    """

    def __init__(self, lease: TaskLease | None = None) -> None:
        self._lease: TaskLease | None = None
        self._highest_epoch = -1
        self._robot_id: str | None = None
        self._last_seq: int = -1
        self._seen: set[str] = set()
        if lease is not None:
            self.grant(lease)

    @property
    def lease(self) -> TaskLease | None:
        return self._lease.model_copy(deep=True) if self._lease is not None else None

    def grant(self, lease: TaskLease) -> None:
        """Install a new lease; a higher epoch resets the sequence window and the dedup set.

        安装新租约；更高的代次会重置序号窗口与去重集合。
        """
        lease = TaskLease.model_validate(lease.model_dump())
        if self._robot_id is not None and lease.robot_id != self._robot_id:
            raise ValueError("a per-robot gate cannot change robots")
        if lease.lease_epoch < self._highest_epoch:
            raise ValueError("cannot grant a lease with a lower epoch than the current one")
        if lease.lease_epoch == self._highest_epoch:
            current = self._lease
            if current is None:
                raise ValueError("a revoked lease requires a higher epoch")
            identity = ("robot_id", "mission_id", "mission_version", "holder", "issued_at")
            if any(getattr(current, key) != getattr(lease, key) for key in identity):
                raise ValueError("same-epoch renewal cannot change lease identity")
            if set(current.resources) != set(lease.resources):
                raise ValueError("same-epoch renewal cannot change resources")
        else:
            self._last_seq = -1
            self._seen.clear()
        self._highest_epoch = lease.lease_epoch
        self._robot_id = lease.robot_id
        self._lease = lease

    def revoke(self) -> None:
        """Close the gate until a new lease is granted. / 关闭闸门直到授予新租约。"""
        self._lease = None

    def check(self, envelope: ControlCommandEnvelope, *, now: datetime) -> EgressDecision:
        """Run every check in order; the first failure wins. / 按顺序执行全部检查，首个失败即拒绝。"""
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
        if envelope.issued_at > now:
            return EgressDecision(accepted=False, reason="future_intent")
        if envelope.valid_until <= now:
            return EgressDecision(accepted=False, reason="expired_intent")
        if key.as_string() in self._seen:
            # Duplicate delivery: never re-execute; the caller reconciles against the recorded outcome.
            # 重复投递：绝不重新执行；调用方按已记录的结果做对账。
            return EgressDecision(accepted=False, reason="duplicate_command")
        if envelope.command_seq <= self._last_seq:
            return EgressDecision(accepted=False, reason="stale_seq")
        self._last_seq = envelope.command_seq
        self._seen.add(key.as_string())
        return EgressDecision(accepted=True)
