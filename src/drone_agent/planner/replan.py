"""Bounded replanning and the approval policy for new mission versions (WP-M2-10, D032).

A replan only regenerates the affected subgraph: content nodes whose last outcome was not completed.
The default proposer retries them verbatim (same skill, version and parameters) inside the same
framework, scope, recovery policy and budget. Any proposal — deterministic or from the planner — is
then classified against configs/approval_policy.yaml: scope expansion, added unverified edges, a
changed recovery policy or exceeding the per-mission cap are rejected outright; any other change goes
to a human; only verbatim retries of eligible nodes are approved automatically, and the automatic
approval never outlives the human approval of the base version. A candidate event raised by an onboard
model may propose a verbatim re-run of a completed step; such a version always goes to a human (D044).
Switching the aircraft to the new version happens only after the previous version is finished and
grounded; that gate lives in the mission service and, independently, in the guardian's refusal to grant
a new epoch in the air.

有界重规划与新任务版本的审批策略（WP-M2-10，D032）。

重规划只重新生成受影响的子图：最近结果未完成的内容节点。默认提议器在同一框架、范围、恢复策略与预算
内原样重试它们（技能、版本与参数不变）。任何提议——确定性的或来自规划器的——都按
configs/approval_policy.yaml 分类：扩范围、新增未证实边、改变恢复策略或超出每任务上限直接拒绝；其他
改动交给人工；只有符合条件节点的原样重试可以自动批准，而且自动批准的有效期不超过基础版本的人工审批。
机载模型发起的候选事件可以提议原样重做已完成的步骤，这类版本一律交给人工（D044）。
飞行器切换到新版本只在上一版本结束并落地之后；这一闸门在任务服务中，同时独立地体现在 guardian 拒绝
在空中授予新代次。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from drone_agent.admission.compiler import FRAMEWORK
from drone_agent.contracts import (
    ApprovalRecord,
    EffectVerdict,
    ExecutionStatus,
    MissionPackage,
    MissionSpec,
    Provenance,
    StepOutcome,
    TargetRef,
    TaskNode,
    TemporalWindow,
)
from drone_agent.contracts.common import ContractModel
from drone_agent.runtime.issues import Issue, issue

REPLAN_PROMPT = "replan-retry-v1"


class ApprovalPolicy(ContractModel):
    """The approval policy file. / 审批策略文件。"""

    policy_id: str
    version: str
    max_replans_per_mission: int = Field(ge=0)
    approval_ttl_minutes: int = Field(gt=0)
    auto_approve: dict = Field(default_factory=dict)
    reject: list[str] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ApprovalPolicy:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    @property
    def ref(self) -> str:
        return f"{self.policy_id}@{self.version}"

    @property
    def eligible_outcomes(self) -> set[str]:
        return set(self.auto_approve.get("retry_unfinished_nodes", {}).get("eligible_outcomes", []))

    @model_validator(mode="after")
    def _model_candidates_never_auto_approve(self) -> ApprovalPolicy:
        # A candidate event comes from an onboard model; it may only lead to a version a human approves (D044).
        # 候选事件来自机载模型；它只能引出由人工批准的版本（D044）。
        if "candidate_event" in self.eligible_outcomes:
            raise ValueError("candidate_event can never be eligible for automatic approval")
        return self


class ReplanTrigger(ContractModel):
    """Why a node needs another attempt. / 某节点需要重试的原因。"""

    step_id: str
    kind: Literal["failed", "unverified", "refuted", "unknown", "not_run", "cancelled", "candidate_event"]
    detail: str = ""


class ReplanDecision(ContractModel):
    """How a proposed version may be approved. / 提议版本可以如何被批准。"""

    classification: Literal["auto_approvable", "requires_human", "rejected"]
    issues: list[Issue] = Field(default_factory=list)
    retried_steps: list[str] = Field(default_factory=list)


def outcome_kind(outcome: StepOutcome | None) -> str | None:
    """Classify a step's last outcome; None means it completed. / 分类步骤的最近结果；None 表示已完成。"""
    if outcome is None:
        return "not_run"
    if outcome.counts_as_completed:
        return None
    if outcome.execution_status is ExecutionStatus.CANCELLED:
        return "cancelled"
    if outcome.effect_verdict is EffectVerdict.REFUTED:
        return "refuted"
    if outcome.effect_verdict is EffectVerdict.UNVERIFIED:
        return "unverified"
    if outcome.effect_verdict is EffectVerdict.UNKNOWN and outcome.execution_status is not ExecutionStatus.FAILED:
        return "unknown"
    return "failed"


def triggers(package: MissionPackage, outcomes: dict[str, StepOutcome | None]) -> list[ReplanTrigger]:
    """Content nodes that did not complete, in package order. / 未完成的内容节点，按任务包顺序。"""
    found = []
    for node in package.nodes:
        if node.skill_id in FRAMEWORK:
            continue
        kind = outcome_kind(outcomes.get(node.task_id))
        if kind is not None:
            found.append(ReplanTrigger(step_id=node.task_id, kind=kind))
    return found


def propose_retry(base: MissionSpec, package: MissionPackage, found: list[ReplanTrigger], policy: ApprovalPolicy, *,
                  new_version: int, now: datetime, not_after_limit: datetime, window_minutes: int) -> MissionSpec | None:
    """The deterministic proposal: retry eligible unfinished nodes verbatim inside the base framework.

    确定性提议：在基础框架内原样重试符合条件的未完成节点。
    """
    eligible = [t.step_id for t in found if t.kind in policy.eligible_outcomes]
    # Candidate events propose a verbatim re-run too; classify() sends every such version to a human (D044).
    # 候选事件同样提议原样重做；classify() 把这类版本一律交给人工（D044）。
    eligible += [t.step_id for t in found if t.kind == "candidate_event" and t.step_id not in eligible]
    if not eligible:
        return None
    nodes = {n.task_id: n for n in package.nodes}
    framework = {n.skill_id: n for n in package.nodes if n.skill_id in FRAMEWORK}
    takeoff = framework["skill.flight.takeoff"]
    tasks = [TaskNode(task_id=takeoff.task_id, skill_id=takeoff.skill_id, params=dict(takeoff.params))]
    previous = takeoff.task_id
    for step in eligible:
        node = nodes[step]
        tasks.append(TaskNode(task_id=node.task_id, skill_id=node.skill_id, params=dict(node.params),
                              depends_on=[previous], completion_evidence=list(node.completion_evidence)))
        previous = node.task_id
    ret, land = framework["skill.flight.return_home"], framework["skill.flight.land"]
    tasks.append(TaskNode(task_id=ret.task_id, skill_id=ret.skill_id, params=dict(ret.params), depends_on=[previous]))
    tasks.append(TaskNode(task_id=land.task_id, skill_id=land.skill_id, params=dict(land.params),
                          depends_on=[ret.task_id]))
    digest = hashlib.sha256(json.dumps({"base": package.package_hash, "retry": eligible}, sort_keys=True).encode()).hexdigest()
    not_after = min(now + timedelta(minutes=window_minutes), not_after_limit)
    # Rebuild through validation so the new DAG is checked like any other spec. / 经校验重建，新 DAG 与其他规格同样受检。
    return MissionSpec.model_validate({
        **base.model_dump(),
        "mission_version": new_version,
        "tasks": [t.model_dump() for t in tasks],
        "targets": [TargetRef(asset_id=nodes[s].params["asset_id"]).model_dump() for s in eligible
                    if "asset_id" in nodes[s].params],
        "temporal_window": TemporalWindow(not_before=now - timedelta(seconds=30), not_after=not_after).model_dump(),
        "provenance": Provenance(model_id="deterministic-retry", prompt_version=REPLAN_PROMPT, input_hash=digest,
                                 generated_at=now).model_dump(),
    })


def classify(base: MissionPackage, proposed: MissionPackage, found: list[ReplanTrigger], policy: ApprovalPolicy, *,
             replans_so_far: int, base_approval: ApprovalRecord) -> ReplanDecision:
    """Apply the approval policy to a compiled proposal. / 对已编译的提议应用审批策略。"""
    rejected, human = [], []
    if replans_so_far >= policy.max_replans_per_mission:
        rejected.append(issue("replan.limit_exceeded", f"{replans_so_far} replans already used"))
    if proposed.mission_id != base.mission_id or proposed.mission_version <= base.mission_version:
        rejected.append(issue("replan.scope_expanded", "a replan must be a newer version of the same mission"))
    if proposed.spatial_scope != base.spatial_scope:
        rejected.append(issue("replan.scope_expanded", "the spatial scope changed"))
    if {n.robot_id for n in proposed.nodes} != {n.robot_id for n in base.nodes}:
        rejected.append(issue("replan.scope_expanded", "the robot set changed"))
    if proposed.recovery_policy_ref != base.recovery_policy_ref:
        rejected.append(issue("replan.scope_expanded", "the recovery policy changed"))
    base_assets = {n.params.get("asset_id") for n in base.nodes if "asset_id" in n.params}
    for node in proposed.nodes:
        if "asset_id" in node.params and node.params["asset_id"] not in base_assets:
            rejected.append(issue("replan.scope_expanded", f"{node.params['asset_id']} was not in the base plan",
                                  affected=[node.task_id]))
        if node.allow_unverified_from:
            rejected.append(issue("replan.unverified_edge_added", "a replan cannot add allow_unverified_from",
                                  affected=[node.task_id]))
    by_id = {n.task_id: n for n in base.nodes}
    eligible = {t.step_id for t in found if t.kind in policy.eligible_outcomes}
    retried = []
    for node in proposed.nodes:
        original = by_id.get(node.task_id)
        bindings = ("skill_id", "skill_version", "params", "robot_id", "resources", "timeout_s", "completion_evidence")
        same = original is not None and all(getattr(original, field) == getattr(node, field) for field in bindings)
        if node.skill_id in FRAMEWORK:
            if not same:
                human.append(issue("replan.requires_human", f"framework node {node.task_id} changed",
                                   affected=[node.task_id]))
            continue
        if not same:
            human.append(issue("replan.requires_human", f"{node.task_id} is not a verbatim retry", affected=[node.task_id]))
        elif node.task_id not in eligible:
            human.append(issue("replan.requires_human", f"{node.task_id} did not fail and is not eligible for retry",
                               affected=[node.task_id]))
        else:
            retried.append(node.task_id)
    budget, base_budget = proposed.energy_budget, base.energy_budget
    if (budget.max_consumption_fraction > base_budget.max_consumption_fraction
            or budget.reserve_fraction < base_budget.reserve_fraction):
        human.append(issue("replan.requires_human", "the energy budget grew or the reserve shrank"))
    if proposed.temporal_window.not_after > base_approval.expires_at:
        human.append(issue("replan.requires_human", "the new window outlives the base approval"))
    if rejected:
        return ReplanDecision(classification="rejected", issues=rejected + human, retried_steps=retried)
    if human:
        return ReplanDecision(classification="requires_human", issues=human, retried_steps=retried)
    return ReplanDecision(classification="auto_approvable", retried_steps=retried)


def auto_approval(package: MissionPackage, decision: ReplanDecision, policy: ApprovalPolicy, *,
                  base_approval: ApprovalRecord, now: datetime) -> ApprovalRecord:
    """Policy approval for an auto-approvable version; never valid beyond the base human approval.

    对可自动批准版本的策略审批；有效期永不超过基础版本的人工审批。
    """
    if decision.classification != "auto_approvable":
        raise ValueError("only auto-approvable replans may be approved by policy")
    return ApprovalRecord(
        approver=f"policy:{policy.ref}",
        approved_at=now,
        mission_id=package.mission_id,
        mission_version=package.mission_version,
        package_hash=package.package_hash,
        expires_at=min(now + timedelta(minutes=policy.approval_ttl_minutes), base_approval.expires_at),
        allowed_robots=list(base_approval.allowed_robots),
    )
