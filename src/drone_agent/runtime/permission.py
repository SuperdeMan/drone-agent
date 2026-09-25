# Ported from embodied-agent src/embodied/safety/{scopes,permission}.py @ e20fe33 (itself ported from
# car-agent security/{scopes,permission}.py @ f0b08f8), changes: the two modules merged; scope catalog
# rewritten for flight missions (mission/flight/gimbal/camera/payload); actuation prefixes are now
# flight/gimbal/payload and are denied to every request-level caller (no console, A2A or tool identity
# may hold them); trust levels add `tool`; PermissionEngine/check_action dropped in favour of
# `authorize(caller, required)` because callers here are identities, not agent manifests.
"""Scope catalog and the single permission decision for console, A2A and planner-tool callers.

Scopes are `<resource>.<action>`; a parent covers its children (`mission` covers `mission.read`).
Flight actuation (`flight.*`, `gimbal.*`, `payload.*`) is never granted to a request-level caller:
only the onboard executive/guardian pair acts on the aircraft, through the leased control egress.
Operators pause, resume and cancel through `mission.operate`, which the executive and guardian
re-check; A2A callers are third-party and may only submit requests and read status and reports.
Project roles (P1, D055) map to scopes inside one project; the trust cap still clips them, so a role
never widens what an identity channel may do. Dock backends report status through `resource.report` only.

console、A2A 与规划工具调用方共用的 scope 目录与唯一权限决策。

scope 形如 `<resource>.<action>`；父 scope 覆盖子 scope（`mission` 覆盖 `mission.read`）。
飞行执行类（`flight.*`、`gimbal.*`、`payload.*`）永不授予请求层调用方：只有机载 executive 与
guardian 经带租约的控制出口作用于飞行器。操作者通过 `mission.operate` 暂停、恢复与取消，
executive 与 guardian 会再次复核；A2A 调用方是第三方，只能提交请求、读取状态与报告。
项目角色（P1，D055）映射为单个项目内的 scope；信任上限仍会裁剪，角色永远不会放宽身份通道的能力。
机场后端只能经 `resource.report` 报告状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ─── Scope catalog / scope 全集 ───
MISSION_SUBMIT = "mission.submit"
MISSION_READ = "mission.read"
MISSION_APPROVE = "mission.approve"
MISSION_OPERATE = "mission.operate"
CAMERA_READ = "camera.read"
FLIGHT_CONTROL = "flight.control"
FLIGHT_ARM = "flight.arm"
FLIGHT_MODE = "flight.mode"
GIMBAL_CONTROL = "gimbal.control"
PAYLOAD_RELEASE = "payload.release"
# P1 operations resources (D055). / P1 运营资源（D055）。
RESOURCE_READ = "resource.read"
RESOURCE_MAINTAIN = "resource.maintain"
RESOURCE_REPORT = "resource.report"

ALL_SCOPES: frozenset[str] = frozenset(
    {
        MISSION_SUBMIT,
        MISSION_READ,
        MISSION_APPROVE,
        MISSION_OPERATE,
        CAMERA_READ,
        FLIGHT_CONTROL,
        FLIGHT_ARM,
        FLIGHT_MODE,
        GIMBAL_CONTROL,
        PAYLOAD_RELEASE,
        RESOURCE_READ,
        RESOURCE_MAINTAIN,
        RESOURCE_REPORT,
    }
)

# Physical actuation prefixes: request-level callers can never hold these, whatever a token says.
# 物理执行类前缀：请求层调用方无论 token 如何声明都不能持有。
ACTUATION_PREFIXES: tuple[str, ...] = ("flight", "gimbal", "payload")


class TrustLevel(StrEnum):
    """How much a caller is trusted. / 调用方的信任级别。"""

    FIRST_PARTY = "first_party"  # identified human operator on the console / 控制台上已识别身份的人类操作者
    THIRD_PARTY = "third_party"  # external agent over A2A / 经 A2A 接入的外部 agent
    TOOL = "tool"  # planner tool or model-side caller / 规划工具或模型侧调用方
    ANONYMOUS = "anonymous"  # reachable but unidentified, e.g. a tagged tailnet device / 可达但无身份，如 tagged 设备
    BACKEND = "backend"  # bound resource backend such as a dock simulator (P1) / 已绑定的资源后端，如机场模拟器（P1）


# Hard upper bounds per trust level; grants outside the cap are ignored, never widened.
# 每个信任级别的硬上限；超出上限的授予被忽略，永不放宽。
TRUST_LEVEL_CAPS: dict[TrustLevel, frozenset[str]] = {
    TrustLevel.FIRST_PARTY: frozenset({MISSION_SUBMIT, MISSION_READ, MISSION_APPROVE, MISSION_OPERATE, CAMERA_READ,
                                       RESOURCE_READ, RESOURCE_MAINTAIN}),
    TrustLevel.THIRD_PARTY: frozenset({MISSION_SUBMIT, MISSION_READ}),
    TrustLevel.TOOL: frozenset({MISSION_READ}),
    TrustLevel.ANONYMOUS: frozenset({MISSION_READ, CAMERA_READ}),
    TrustLevel.BACKEND: frozenset({RESOURCE_REPORT}),
}


class Role(StrEnum):
    """Project roles (D055); admin manages resources but never approves flights.

    项目角色（D055）；admin 管理资源，但从不审批飞行。
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    REVIEWER = "reviewer"  # business review arrives in P2/P4; read-only in P1 / 业务复核在 P2/P4，P1 只读
    ADMIN = "admin"


_READ = frozenset({MISSION_READ, RESOURCE_READ, CAMERA_READ})
# Scopes one role grants inside its project. / 单个角色在其项目内授予的 scope。
ROLE_SCOPES: dict[Role, frozenset[str]] = {
    Role.VIEWER: _READ,
    Role.OPERATOR: _READ | {MISSION_SUBMIT, MISSION_OPERATE},
    Role.APPROVER: _READ | {MISSION_APPROVE},
    Role.REVIEWER: _READ,
    Role.ADMIN: _READ | {RESOURCE_MAINTAIN},
}


def role_scopes(roles) -> frozenset[str]:
    """Union of the scopes of `roles`. / `roles` 的 scope 并集。"""
    return frozenset().union(*(ROLE_SCOPES[Role(role)] for role in roles)) if roles else frozenset()


def is_scope_covered(required: str, effective) -> bool:
    """Whether `required` is covered by `effective`, parent scopes covering their children.

    判断 `required` 是否被 `effective` 覆盖（父 scope 覆盖子 scope）。
    """
    parts = required.split(".")
    return any(".".join(parts[:i]) in effective for i in range(len(parts), 0, -1))


def is_actuation(scope: str) -> bool:
    """True for flight/gimbal/payload scopes or their parents. / flight/gimbal/payload 及其父 scope 返回真。"""
    return any(scope == p or scope.startswith(p + ".") for p in ACTUATION_PREFIXES)


@dataclass(frozen=True)
class Caller:
    """An authenticated identity with its trust level and granted scopes.

    已认证的调用方身份，带信任级别与授予的 scope。
    """

    identity: str
    trust_level: TrustLevel
    granted: frozenset[str] = field(default_factory=frozenset)

    def effective(self) -> frozenset[str]:
        """Granted scopes clipped to the trust cap, parents expanded against the cap.

        授予的 scope 与信任上限求交；父 scope 按上限展开。
        """
        cap = TRUST_LEVEL_CAPS[self.trust_level]
        return frozenset(scope for scope in cap if is_scope_covered(scope, self.granted))


@dataclass(frozen=True)
class Decision:
    """The permission decision. / 权限判定结果。"""

    allowed: bool
    missing: tuple[str, ...] = ()
    code: str = ""
    reason: str = ""


def authorize(caller: Caller, required) -> Decision:
    """The only runtime permission decision; actuation is refused before any grant is considered.

    运行时唯一的权限判定；在考虑任何授予之前先拒绝执行类 scope。
    """
    required = [str(scope) for scope in (required or [])]
    actuation = [scope for scope in required if is_actuation(scope)]
    if actuation:
        return Decision(False, tuple(actuation), "auth.flight_scope_denied",
                        "request-level callers cannot hold flight, gimbal or payload scopes")
    if caller.trust_level is TrustLevel.ANONYMOUS and any(scope != MISSION_READ and scope != CAMERA_READ
                                                           for scope in required):
        return Decision(False, tuple(required), "auth.identity_missing", "an identified operator is required")
    effective = caller.effective()
    missing = tuple(scope for scope in required if not is_scope_covered(scope, effective))
    if missing:
        return Decision(False, missing, "auth.scope_missing", "missing permissions: " + ", ".join(missing))
    return Decision(True)
