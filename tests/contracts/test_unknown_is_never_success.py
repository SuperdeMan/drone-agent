"""Red line D006: UNKNOWN / UNVERIFIED never unlock a dependent step; reports only accept succeeded+verified."""

import pytest

from drone_agent.contracts import EffectVerdict, ExecutionStatus, SafetyVerdict, StepOutcome, may_run_successor


def outcome(status: ExecutionStatus, effect: EffectVerdict) -> StepOutcome:
    return StepOutcome(
        mission_id="m-001", mission_version=1, step_id="t1", robot_id="uav_01", execution_status=status, effect_verdict=effect
    )


@pytest.mark.parametrize("status", list(ExecutionStatus))
@pytest.mark.parametrize("effect", [EffectVerdict.UNKNOWN, EffectVerdict.REFUTED])
def test_unknown_or_refuted_effect_never_unlocks_successor(status, effect):
    pred = outcome(status, effect)
    assert not may_run_successor(pred, current_safety=SafetyVerdict.PROCEED)
    assert not may_run_successor(pred, current_safety=SafetyVerdict.PROCEED, edge_allows_unverified=True)


@pytest.mark.parametrize("status", [s for s in ExecutionStatus if s is not ExecutionStatus.SUCCEEDED])
def test_non_succeeded_execution_never_unlocks_successor(status):
    pred = outcome(status, EffectVerdict.VERIFIED)
    assert not may_run_successor(pred, current_safety=SafetyVerdict.PROCEED)


def test_timeout_without_resend_is_unknown_not_success():
    # "takeoff command timed out but was not resent" must block the next leg (GPT-6 Pro review §1.2).
    pred = outcome(ExecutionStatus.TIMEOUT, EffectVerdict.UNKNOWN)
    assert not pred.counts_as_completed
    assert not may_run_successor(pred, current_safety=SafetyVerdict.PROCEED, edge_allows_unverified=True)


def test_unverified_passes_only_on_explicitly_allowed_edge():
    pred = outcome(ExecutionStatus.SUCCEEDED, EffectVerdict.UNVERIFIED)
    assert not may_run_successor(pred, current_safety=SafetyVerdict.PROCEED)
    assert may_run_successor(pred, current_safety=SafetyVerdict.PROCEED, edge_allows_unverified=True)
    assert not pred.counts_as_completed


@pytest.mark.parametrize("safety", [SafetyVerdict.HOLD, SafetyVerdict.RECOVER, SafetyVerdict.ABORT])
def test_guardian_verdict_overrides_verified_success(safety):
    pred = outcome(ExecutionStatus.SUCCEEDED, EffectVerdict.VERIFIED)
    assert not may_run_successor(pred, current_safety=safety)


def test_only_succeeded_and_verified_counts_as_completed():
    assert outcome(ExecutionStatus.SUCCEEDED, EffectVerdict.VERIFIED).counts_as_completed
    assert may_run_successor(outcome(ExecutionStatus.SUCCEEDED, EffectVerdict.VERIFIED), current_safety=SafetyVerdict.PROCEED)
