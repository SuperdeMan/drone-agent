"""Service-side request and admission records (02-contracts §10); they never cross to a robot.

服务侧的请求与准入记录（02-contracts §10）；它们从不发送给机器人。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from drone_agent.contracts.common import ContractModel
from drone_agent.contracts.mission import MissionPackage
from drone_agent.runtime.issues import Issue, Severity
from drone_agent.runtime.permission import TrustLevel


class RequestChannel(StrEnum):
    """Where a mission request entered. / 任务请求的入口。"""

    CONSOLE = "console"
    A2A = "a2a"
    HARNESS = "harness"
    REPLAN = "replan"


class MissionRequest(ContractModel):
    """A natural-language mission request plus the scope the operator selected.

    The volume and optional asset list are chosen outside the model (console selection, A2A
    parameters or a harness case); admission rejects any plan that leaves this scope.

    自然语言任务请求以及操作者选定的范围。体积与可选资产列表在模型之外选定（控制台选择、A2A 参数
    或测试编排用例）；任何越出该范围的规划都被准入拒绝。
    """

    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=2000)
    requested_by: str = Field(min_length=1)
    trust_level: TrustLevel
    channel: RequestChannel
    approved_volume_id: str = Field(min_length=1)
    asset_ids: list[str] = Field(default_factory=list, description="empty = any asset in the volume / 为空表示体积内任意资产")
    idempotency_key: str = Field(min_length=1, max_length=120)
    received_at: AwareDatetime
    unverified_ack_by: str | None = Field(
        default=None,
        description=(
            "identified operator who explicitly accepted allow_unverified_from edges / "
            "明确接受 allow_unverified_from 边的已识别操作者"
        ),
    )


class CheckRecord(ContractModel):
    """One admission check and its result. / 一项准入检查及其结果。"""

    check: str
    passed: bool
    detail: str = ""


class CompileResult(ContractModel):
    """The compiled package, or the reasons it could not be compiled. / 编译得到的任务包，或无法编译的原因。"""

    package: MissionPackage | None = None
    package_hash: str = ""
    issues: list[Issue] = Field(default_factory=list)
    inserted_framework: list[str] = Field(default_factory=list)
    expanded_targets: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.package is not None and not any(i.severity is Severity.ERROR for i in self.issues)


class AdmissionResult(ContractModel):
    """Accepted only when no error-severity issue remains; unknown is never accepted.

    只有不存在错误级问题时才接受；未知永远不被接受。
    """

    accepted: bool
    package_hash: str = ""
    issues: list[Issue] = Field(default_factory=list)
    checks: list[CheckRecord] = Field(default_factory=list)
    requires_human_ack: list[str] = Field(
        default_factory=list, description="tasks with allow_unverified_from edges / 带 allow_unverified_from 边的任务"
    )
    energy_upper_fraction: float | None = None
    airspace: dict | None = None
    checked_at: AwareDatetime
