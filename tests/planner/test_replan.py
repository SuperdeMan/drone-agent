"""Bounded replanning under the approval policy (WP-M2-10, D032).

审批策略约束下的有界重规划（WP-M2-10，D032）。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.admission.admission import admit
from drone_agent.admission.compiler import compile_spec
from drone_agent.contracts import EffectVerdict, ExecutionStatus, SafetyVerdict, StepOutcome, TaskNode, utcnow
from drone_agent.planner.replan import ApprovalPolicy, auto_approval, classify, propose_retry, triggers
from drone_agent.runtime.issues import codes
from tests.admission.scene import admission_context, approve, compile_context, inspect_task, spec

ROOT = Path(__file__).resolve().parents[2]
POLICY = ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml")


def outcome(step, status=ExecutionStatus.SUCCEEDED, effect=EffectVerdict.VERIFIED):
    return StepOutcome(mission_id="m2-test", mission_version=1, step_id=step, robot_id="uav_01",
                       execution_status=status, effect_verdict=effect, safety_verdict=SafetyVerdict.PROCEED)


def base():
    package = approve(compile_spec(spec(), compile_context()).package)
    return spec(), package


def aborted_after_unverified_inspection():
    return {"takeoff": outcome("takeoff"),
            "inspect_asset_red": outcome("inspect_asset_red", ExecutionStatus.FAILED, EffectVerdict.UNVERIFIED)}


def retry_package(base_spec, package, found, **overrides):
    now = utcnow()
    values = dict(new_version=2, now=now, not_after_limit=package.approval.expires_at, window_minutes=30)
    values.update(overrides)
    proposed = propose_retry(base_spec, package, found, POLICY, **values)
    result = compile_spec(proposed, compile_context())
    assert result.ok, codes(result.issues)
    return proposed, result.package


def test_only_unfinished_content_nodes_trigger_a_replan():
    _, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    assert [(t.step_id, t.kind) for t in found] == [("inspect_asset_red", "unverified")]
    completed = {n.task_id: outcome(n.task_id) for n in package.nodes}
    assert triggers(package, completed) == []


def test_verbatim_retry_is_auto_approvable_and_admitted():
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    proposed, new_package = retry_package(base_spec, package, found)
    assert [t.task_id for t in proposed.tasks] == ["takeoff", "inspect_asset_red", "return_home", "land"]
    assert proposed.provenance.model_id == "deterministic-retry" and proposed.mission_version == 2
    assert [n.params for n in new_package.nodes] == [n.params for n in package.nodes]
    decision = classify(package, new_package, found, POLICY, replans_so_far=0, base_approval=package.approval)
    assert decision.classification == "auto_approvable" and decision.retried_steps == ["inspect_asset_red"]
    assert admit(new_package, admission_context()).accepted
    record = auto_approval(new_package, decision, POLICY, base_approval=package.approval, now=utcnow())
    assert record.approver == "policy:m2_approval@v1" and record.package_hash == new_package.package_hash
    assert record.expires_at <= package.approval.expires_at and record.allowed_robots == ["uav_01"]


def test_operator_cancelled_nodes_are_never_retried_automatically():
    base_spec, package = base()
    cancelled = {"takeoff": outcome("takeoff"),
                 "inspect_asset_red": outcome("inspect_asset_red", ExecutionStatus.CANCELLED, EffectVerdict.UNKNOWN)}
    found = triggers(package, cancelled)
    assert [t.kind for t in found] == ["cancelled"]
    assert propose_retry(base_spec, package, found, POLICY, new_version=2, now=utcnow(),
                         not_after_limit=package.approval.expires_at, window_minutes=30) is None


def test_scope_expansion_by_a_planner_proposal_is_rejected():
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    wider = spec(mission_version=2, tasks=[inspect_task("asset_red"),
                                           inspect_task("asset_blue", depends_on=["inspect_asset_red"])])
    proposed = compile_spec(wider, compile_context()).package
    decision = classify(package, proposed, found, POLICY, replans_so_far=0, base_approval=package.approval)
    assert decision.classification == "rejected" and "replan.scope_expanded" in codes(decision.issues)


def test_adding_an_unverified_edge_is_rejected():
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    _, proposed = retry_package(base_spec, package, found)
    proposed.nodes[2].allow_unverified_from = ["inspect_asset_red"]
    decision = classify(package, proposed, found, POLICY, replans_so_far=0, base_approval=package.approval)
    assert decision.classification == "rejected" and "replan.unverified_edge_added" in codes(decision.issues)


def test_parameter_change_goes_to_a_human():
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    # Same window as the approved package, so the parameter is the only difference.
    # 与已批准任务包相同的时间窗，使参数成为唯一差异。
    tuned = spec(mission_version=2, tasks=[inspect_task(max_captures=2)], temporal_window=package.temporal_window)
    proposed = compile_spec(tuned, compile_context()).package
    decision = classify(package, proposed, found, POLICY, replans_so_far=0, base_approval=package.approval)
    assert decision.classification == "requires_human" and codes(decision.issues) == ["replan.requires_human"]


@pytest.mark.parametrize("field,value", [("completion_evidence", []), ("timeout_s", 1), ("resources", [])])
def test_execution_or_evidence_binding_change_requires_human(field, value):
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    _, proposed = retry_package(base_spec, package, found)
    setattr(proposed.nodes[1], field, value)
    decision = classify(package, proposed, found, POLICY, replans_so_far=0, base_approval=package.approval)
    assert decision.classification == "requires_human"


def test_retrying_a_completed_node_goes_to_a_human():
    base_spec, package = base()
    completed = {n.task_id: outcome(n.task_id) for n in package.nodes}
    proposed = compile_spec(spec(mission_version=2, temporal_window=package.temporal_window),
                            compile_context()).package
    decision = classify(package, proposed, triggers(package, completed), POLICY, replans_so_far=0,
                        base_approval=package.approval)
    assert decision.classification == "requires_human" and codes(decision.issues) == ["replan.requires_human"]
    assert "not eligible" in decision.issues[0].message


def test_the_replan_cap_is_a_hard_limit():
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    _, proposed = retry_package(base_spec, package, found)
    decision = classify(package, proposed, found, POLICY, replans_so_far=POLICY.max_replans_per_mission,
                        base_approval=package.approval)
    assert decision.classification == "rejected" and "replan.limit_exceeded" in codes(decision.issues)


def test_auto_approval_cannot_outlive_the_base_human_approval():
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    _, proposed = retry_package(base_spec, package, found,
                                not_after_limit=package.approval.expires_at + timedelta(hours=3), window_minutes=90)
    decision = classify(package, proposed, found, POLICY, replans_so_far=0, base_approval=package.approval)
    assert decision.classification == "requires_human"
    with pytest.raises(ValueError):
        auto_approval(proposed, decision, POLICY, base_approval=package.approval, now=utcnow())


def test_framework_change_goes_to_a_human():
    base_spec, package = base()
    found = triggers(package, aborted_after_unverified_inspection())
    higher = spec(mission_version=2, tasks=[TaskNode(task_id="takeoff", skill_id="skill.flight.takeoff",
                                                     params={"altitude_m_agl": 6}),
                                            inspect_task(depends_on=["takeoff"])],
                  temporal_window=package.temporal_window)
    proposed = compile_spec(higher, compile_context()).package
    decision = classify(package, proposed, found, POLICY, replans_so_far=0, base_approval=package.approval)
    assert decision.classification == "requires_human" and codes(decision.issues) == ["replan.requires_human"]
