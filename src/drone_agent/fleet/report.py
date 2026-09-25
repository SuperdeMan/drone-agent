"""Three-column mission report built from onboard outcomes and service verifications (WP-M2-13).

Completed means `succeeded ∧ verified` onboard and, where the service re-checked the evidence, a service
verdict that agrees. Unknown, unverified, timeouts and verification disagreements are uncertain; failures
with refuted effects, cancellations and steps that never ran are not completed. The report is built from
the onboard journal rows (forwarded events or the original files) plus verifications; model facts are
attached as notes and never move a row between columns. Across mission versions a target is completed if
any version completed it.

由机载结果与服务验证生成的三列任务报告（WP-M2-13）。

已完成表示机载 `succeeded ∧ verified`，且服务复核过证据时服务判定一致。unknown、unverified、超时与验证
不一致属于不确定；效果被推翻的失败、取消与从未执行的步骤属于未完成。报告由机载账本行（转发事件或原始
文件）加上验证结果生成；模型事实只作为备注附上，从不在列之间移动任何一行。跨任务版本时，任一版本完成
的目标即算完成。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from drone_agent.contracts import EffectVerdict, ExecutionStatus, MissionPackage, StepOutcome, utcnow
from drone_agent.contracts.common import ContractModel

Column = Literal["completed", "not_completed", "uncertain"]
_RANK = {"completed": 2, "uncertain": 1, "not_completed": 0}


class ReportRow(ContractModel):
    """One step of one mission version. / 一个任务版本中的一个步骤。"""

    mission_version: int
    step_id: str
    skill_id: str
    target: str | None = None
    column: Column
    execution_status: ExecutionStatus | None = None
    effect_verdict: EffectVerdict | None = None
    service_verdict: EffectVerdict | None = None
    reason: str = ""


class MissionReport(ContractModel):
    """The report: per-step rows, per-target columns and a summary. / 报告：逐步骤行、逐目标列与汇总。"""

    mission_id: str
    versions: list[int]
    rows: list[ReportRow]
    targets: dict[str, Column] = Field(default_factory=dict)
    summary: dict[str, int] = Field(default_factory=dict)
    all_targets_completed: bool
    facts: list[dict] = Field(default_factory=list)
    generated_at: datetime
    provenance: list[dict] = Field(default_factory=list)


def classify(outcome: StepOutcome | None, service: EffectVerdict | None) -> tuple[Column, str]:
    """Column for one step. / 单个步骤所属的列。"""
    if outcome is None:
        return "not_completed", "not run"
    if outcome.counts_as_completed:
        if service is None or service is EffectVerdict.VERIFIED:
            return "completed", ""
        return "uncertain", f"service verification {service.value}"
    if outcome.execution_status is ExecutionStatus.CANCELLED:
        return "not_completed", "cancelled"
    if outcome.effect_verdict is EffectVerdict.REFUTED:
        return "not_completed", "effect refuted"
    return "uncertain", f"{outcome.execution_status.value} / {outcome.effect_verdict.value}"


def build_report(mission_id: str, packages: dict[int, MissionPackage], outcomes: dict[int, dict[str, StepOutcome]],
                 service_verdicts: dict[tuple[int, str], EffectVerdict] | None = None,
                 facts: list[dict] | None = None, *, provenance: list[dict] | None = None) -> MissionReport:
    """Build the report from per-version packages, onboard outcomes and final service verdicts.

    由各版本任务包、机载结果与服务最终判定生成报告。
    """
    service_verdicts = service_verdicts or {}
    rows, targets = [], {}
    for version in sorted(packages):
        for node in packages[version].nodes:
            outcome = outcomes.get(version, {}).get(node.task_id)
            service = service_verdicts.get((version, node.task_id))
            column, reason = classify(outcome, service)
            target = node.params.get("asset_id")
            rows.append(ReportRow(mission_version=version, step_id=node.task_id, skill_id=node.skill_id,
                                  target=target, column=column,
                                  execution_status=outcome.execution_status if outcome else None,
                                  effect_verdict=outcome.effect_verdict if outcome else None,
                                  service_verdict=service, reason=reason))
            if target is not None and _RANK[column] >= _RANK.get(targets.get(target, "not_completed"), 0):
                targets[target] = column
    summary = {name: sum(1 for c in targets.values() if c == name) for name in _RANK}
    return MissionReport(mission_id=mission_id, versions=sorted(packages), rows=rows, targets=targets,
                         summary=summary, all_targets_completed=bool(targets) and all(
                             c == "completed" for c in targets.values()),
                         facts=list(facts or []), generated_at=utcnow(), provenance=list(provenance or []))
