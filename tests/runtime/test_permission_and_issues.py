# Ported from embodied-agent tests/safety/test_permission.py @ e20fe33 (from car-agent
# security/tests/test_permission.py @ f0b08f8), changes: fixtures rewritten for the flight scope
# catalog and caller identities; issue-table tests added for the shared code table.
"""Scope coverage, trust caps, the actuation hard-deny and the shared issue code table.

scope 覆盖、信任上限、执行类硬拒绝与共用问题码表。
"""

import pytest
from pydantic import ValidationError

from drone_agent.runtime.issues import ISSUE_CODES, RECOVERY_KINDS, Issue, IssueLayer, codes, issue
from drone_agent.runtime.permission import (
    ALL_SCOPES,
    CAMERA_READ,
    FLIGHT_CONTROL,
    MISSION_APPROVE,
    MISSION_OPERATE,
    MISSION_READ,
    MISSION_SUBMIT,
    TRUST_LEVEL_CAPS,
    Caller,
    TrustLevel,
    authorize,
    is_actuation,
    is_scope_covered,
)


def test_parent_covers_child_and_siblings_do_not():
    assert is_scope_covered("mission.read", {"mission"})
    assert is_scope_covered("mission.read", {"mission.read"})
    assert not is_scope_covered("mission.approve", {"mission.read"})
    assert not is_scope_covered("mission", {"mission.read"})
    assert not is_scope_covered("mission.read", set())


def test_no_trust_level_cap_contains_actuation():
    for level, cap in TRUST_LEVEL_CAPS.items():
        assert not any(is_actuation(scope) for scope in cap), level
    assert {FLIGHT_CONTROL} <= ALL_SCOPES


def test_third_party_may_only_submit_and_read():
    assert TRUST_LEVEL_CAPS[TrustLevel.THIRD_PARTY] == {MISSION_SUBMIT, MISSION_READ}


@pytest.mark.parametrize("level", list(TrustLevel))
@pytest.mark.parametrize("scope", ["flight", "flight.control", "flight.arm", "gimbal.control", "payload.release"])
def test_actuation_is_denied_to_every_caller_even_when_granted(level, scope):
    caller = Caller("x", level, frozenset({scope, "flight", "mission"}))
    decision = authorize(caller, [scope])
    assert not decision.allowed
    assert decision.code == "auth.flight_scope_denied"


@pytest.mark.parametrize(
    "level,granted,required,allowed,code",
    [
        (TrustLevel.FIRST_PARTY, {"mission"}, [MISSION_APPROVE, MISSION_OPERATE], True, ""),
        (TrustLevel.FIRST_PARTY, {MISSION_READ}, [MISSION_APPROVE], False, "auth.scope_missing"),
        (TrustLevel.THIRD_PARTY, {"mission"}, [MISSION_SUBMIT], True, ""),
        (TrustLevel.THIRD_PARTY, {"mission"}, [MISSION_APPROVE], False, "auth.scope_missing"),
        (TrustLevel.THIRD_PARTY, {"mission"}, [MISSION_OPERATE], False, "auth.scope_missing"),
        (TrustLevel.TOOL, {"mission"}, [MISSION_SUBMIT], False, "auth.scope_missing"),
        (TrustLevel.ANONYMOUS, {"mission", "camera"}, [MISSION_READ, CAMERA_READ], True, ""),
        (TrustLevel.ANONYMOUS, {"mission"}, [CAMERA_READ], False, "auth.scope_missing"),
        (TrustLevel.ANONYMOUS, {"mission"}, [MISSION_SUBMIT], False, "auth.identity_missing"),
        (TrustLevel.FIRST_PARTY, set(), [], True, ""),
    ],
)
def test_authorize_contract(level, granted, required, allowed, code):
    decision = authorize(Caller("x", level, frozenset(granted)), required)
    assert decision.allowed is allowed
    assert decision.code == code


def test_grants_beyond_the_cap_are_ignored_not_widened():
    caller = Caller("agent", TrustLevel.THIRD_PARTY, frozenset({MISSION_APPROVE, MISSION_SUBMIT}))
    assert caller.effective() == {MISSION_SUBMIT}


def test_every_issue_code_names_its_layer():
    assert all(isinstance(layer, IssueLayer) for layer in ISSUE_CODES.values())
    assert all(code.count(".") == 1 for code in ISSUE_CODES)


def test_issue_derives_layer_and_rejects_unknown_codes():
    value = issue("scope.target_outside_volume", "target is outside", affected=["inspect_1"])
    assert value.layer is IssueLayer.ADMISSION
    assert value.affected == ["inspect_1"]
    with pytest.raises(ValidationError):
        Issue(code="made.up")


def test_uncontrolled_recovery_kinds_are_dropped_not_executed():
    value = issue("planner.refused", recovery=[("revise_request", "rephrase"), ("run_shell", "rm -rf")])
    assert [r.kind for r in value.recovery] == ["revise_request"]
    assert "run_shell" not in RECOVERY_KINDS


def test_codes_helper_preserves_order():
    assert codes([issue("time.window_invalid"), issue("energy.budget_exceeded")]) == [
        "time.window_invalid",
        "energy.budget_exceeded",
    ]
