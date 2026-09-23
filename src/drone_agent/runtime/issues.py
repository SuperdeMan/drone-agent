# Ported from car-agent runtime/issues.py @ e3528ee7, changes: dict builder replaced by a validated
# Pydantic model; code table rewritten for flight missions (planner/compiler/admission/onboard/auth/
# service layers) with a `layer` field; recovery kinds reduced to operator-console actions; proto
# conversion dropped (issues travel as JSON to the console and A2A, not over drone.*.v1).
"""Structured issues: the one controlled code table shared by admission, the console and A2A.

Every rejection anywhere in the mission service carries a code from ISSUE_CODES, so the console,
the A2A caller and the adversarial tests all read the same reason. Recovery hints are a small
closed set of console actions; a client never executes an arbitrary command, URL or script sent
by the service.

结构化问题：准入、控制台与 A2A 共用的唯一受控码表。

任务服务中任何一处拒绝都带 ISSUE_CODES 里的码，控制台、A2A 调用方与对抗性测试读到的是同一个
原因。恢复提示是一小组封闭的控制台动作；客户端永远不执行服务端下发的任意命令、链接或脚本。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator

from drone_agent.contracts.common import ContractModel


class IssueLayer(StrEnum):
    """Which layer produced the issue; adversarial cases assert the expected layer.

    产生问题的层；对抗性用例据此断言期望的拦截层。
    """

    PLANNER = "planner"
    COMPILER = "compiler"
    ADMISSION = "admission"
    APPROVAL = "approval"
    ONBOARD = "onboard"
    AUTH = "auth"
    SERVICE = "service"


class Severity(StrEnum):
    """How bad the issue is. / 问题的严重程度。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueScope(StrEnum):
    """What the issue is about. / 问题针对的对象。"""

    REQUEST = "request"
    MISSION = "mission"
    OPERATION = "operation"
    SESSION = "session"
    CAPABILITY = "capability"


# Controlled codes, grouped by the layer that normally raises them. Adding a code is a contract change:
# the console, A2A and the adversarial corpus all key on these strings.
# 受控码，按通常产生它的层分组。新增码属于契约变更：控制台、A2A 与对抗语料都以这些字符串为键。
ISSUE_CODES: dict[str, IssueLayer] = {
    # Planner / 规划层
    "planner.refused": IssueLayer.PLANNER,
    "planner.invalid_output": IssueLayer.PLANNER,
    "planner.technical_failure": IssueLayer.PLANNER,
    "planner.replay_mismatch": IssueLayer.PLANNER,
    "planner.tool_failure": IssueLayer.PLANNER,
    # Compiler / 编译层
    "compile.unknown_skill": IssueLayer.COMPILER,
    "compile.framework_skill_misplaced": IssueLayer.COMPILER,
    "compile.invalid_order": IssueLayer.COMPILER,
    "compile.invalid_dag": IssueLayer.COMPILER,
    "compile.policy_mismatch": IssueLayer.COMPILER,
    "compile.unknown_asset": IssueLayer.COMPILER,
    "compile.unknown_route": IssueLayer.COMPILER,
    "compile.no_capable_robot": IssueLayer.COMPILER,
    "compile.params_invalid": IssueLayer.COMPILER,
    "compile.goal_uncovered": IssueLayer.COMPILER,
    # Admission / 准入层
    "scope.unregistered_volume": IssueLayer.ADMISSION,
    "scope.volume_not_requested": IssueLayer.ADMISSION,
    "scope.frame_mismatch": IssueLayer.ADMISSION,
    "scope.target_outside_volume": IssueLayer.ADMISSION,
    "scope.asset_not_requested": IssueLayer.ADMISSION,
    "scope.route_outside_volume": IssueLayer.ADMISSION,
    "capability.missing_skill": IssueLayer.ADMISSION,
    "capability.missing_control_mode": IssueLayer.ADMISSION,
    "capability.version_mismatch": IssueLayer.ADMISSION,
    "capability.robot_not_allowed": IssueLayer.ADMISSION,
    "params.schema_invalid": IssueLayer.ADMISSION,
    "params.control_key": IssueLayer.ADMISSION,
    "params.binding_mismatch": IssueLayer.ADMISSION,
    "order.invalid": IssueLayer.ADMISSION,
    "time.window_invalid": IssueLayer.ADMISSION,
    "energy.estimate_missing": IssueLayer.ADMISSION,
    "energy.budget_exceeded": IssueLayer.ADMISSION,
    "energy.budget_invalid": IssueLayer.ADMISSION,
    "airspace.mode_undeclared": IssueLayer.ADMISSION,
    "airspace.not_filed": IssueLayer.ADMISSION,
    "airspace.unknown": IssueLayer.ADMISSION,
    "resource.conflict": IssueLayer.ADMISSION,
    "approval.unverified_edge_requires_human": IssueLayer.ADMISSION,
    "policy.recovery_mismatch": IssueLayer.ADMISSION,
    "package.hash_mismatch": IssueLayer.ADMISSION,
    "package.boundary_missing": IssueLayer.ADMISSION,
    # Approval and replanning / 审批与重规划
    "approval.identity_missing": IssueLayer.APPROVAL,
    "approval.stale_version": IssueLayer.APPROVAL,
    "approval.not_admitted": IssueLayer.APPROVAL,
    "replan.limit_exceeded": IssueLayer.APPROVAL,
    "replan.requires_human": IssueLayer.APPROVAL,
    "replan.scope_expanded": IssueLayer.APPROVAL,
    "replan.unverified_edge_added": IssueLayer.APPROVAL,
    # Onboard acceptance / 机载接受
    "onboard.unsigned": IssueLayer.ONBOARD,
    "onboard.untrusted_signer": IssueLayer.ONBOARD,
    "onboard.signature_invalid": IssueLayer.ONBOARD,
    "onboard.statement_mismatch": IssueLayer.ONBOARD,
    "onboard.version_rollback": IssueLayer.ONBOARD,
    "onboard.package_unauthorized": IssueLayer.ONBOARD,
    "onboard.registry_mismatch": IssueLayer.ONBOARD,
    # Identity and permissions (console, A2A) / 身份与权限（控制台、A2A）
    "auth.identity_missing": IssueLayer.AUTH,
    "auth.token_invalid": IssueLayer.AUTH,
    "auth.scope_missing": IssueLayer.AUTH,
    "auth.flight_scope_denied": IssueLayer.AUTH,
    "auth.control_intent_rejected": IssueLayer.AUTH,
    "auth.method_not_allowed": IssueLayer.AUTH,
    # Service / 服务
    "service.degraded": IssueLayer.SERVICE,
    "service.transport_error": IssueLayer.SERVICE,
    "service.verification_mismatch": IssueLayer.SERVICE,
    "service.not_found": IssueLayer.SERVICE,
    "service.invalid_request": IssueLayer.SERVICE,
}

# Console actions a client may offer; anything else is dropped, never executed.
# 客户端可以提供的控制台动作；其余一律丢弃，永不执行。
RECOVERY_KINDS: frozenset[str] = frozenset(
    {"revise_request", "request_human_approval", "retry_request", "refresh_state", "contact_operator", "dismiss"}
)


class RecoveryAction(ContractModel):
    """One controlled follow-up a client may offer. / 客户端可以提供的一个受控后续动作。"""

    kind: str
    label: str = ""

    @field_validator("kind")
    @classmethod
    def _controlled(cls, value: str) -> str:
        if value not in RECOVERY_KINDS:
            raise ValueError(f"uncontrolled recovery kind: {value}")
        return value


class Issue(ContractModel):
    """One structured problem with a controlled code, the layer that raised it and what it affects.

    带受控码、产生层与影响对象的一条结构化问题。
    """

    code: str
    message: str = ""
    layer: IssueLayer | None = None
    severity: Severity = Severity.ERROR
    scope: IssueScope = IssueScope.REQUEST
    request_id: str = ""
    mission_id: str = ""
    affected: list[str] = Field(default_factory=list, description="task ids, fields or scopes / 任务 ID、字段或 scope")
    recovery: list[RecoveryAction] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def _known_code(cls, value: str) -> str:
        if value not in ISSUE_CODES:
            raise ValueError(f"unknown issue code: {value}")
        return value

    def model_post_init(self, _context) -> None:
        # The layer is derived from the table unless a caller explicitly records another layer.
        # 除非调用方显式记录其他层，层由码表推出。
        if self.layer is None:
            object.__setattr__(self, "layer", ISSUE_CODES[self.code])


def issue(
    code: str,
    message: str = "",
    *,
    affected=(),
    recovery=(),
    layer: IssueLayer | None = None,
    severity: Severity = Severity.ERROR,
    scope: IssueScope = IssueScope.REQUEST,
    request_id: str = "",
    mission_id: str = "",
) -> Issue:
    """Build an issue; unknown recovery kinds are dropped so the message itself is never lost.

    构造一条问题；未知的恢复动作被丢弃，而不是让整条问题丢失。
    """
    actions = [RecoveryAction(kind=kind, label=label) for kind, label in recovery if kind in RECOVERY_KINDS]
    return Issue(
        code=code,
        message=message,
        layer=layer,
        severity=severity,
        scope=scope,
        request_id=request_id,
        mission_id=mission_id,
        affected=[str(a) for a in affected],
        recovery=actions,
    )


def codes(issues) -> list[str]:
    """Issue codes in order, for assertions and reports. / 按顺序返回问题码，供断言与报告使用。"""
    return [item.code for item in issues]
