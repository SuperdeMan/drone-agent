"""Skill instance lifecycle: cancel and safe-state are distinct events; terminal states are final."""

import pytest

from drone_agent.contracts import ALLOWED_TRANSITIONS, TERMINAL_STATES, can_transition
from drone_agent.contracts import SkillInstanceState as S


def test_terminal_states_have_no_exits():
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_every_state_is_covered():
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_happy_path():
    path = [S.ACCEPTED, S.PREPARING, S.RUNNING, S.VERIFYING, S.COMPLETED]
    assert all(can_transition(a, b) for a, b in zip(path, path[1:]))


def test_cancel_goes_through_recovering_not_straight_to_safe():
    # "user cancelled" (cancel_requested) and "aircraft is in a safe state" (recovered_to event) differ.
    assert can_transition(S.RUNNING, S.CANCEL_REQUESTED)
    assert can_transition(S.CANCEL_REQUESTED, S.RECOVERING)
    assert can_transition(S.RECOVERING, S.CANCELLED)
    assert not can_transition(S.RUNNING, S.CANCELLED)


def test_unknown_outcome_is_reachable_from_running_and_verifying_only_forward():
    assert can_transition(S.RUNNING, S.OUTCOME_UNKNOWN)
    assert can_transition(S.VERIFYING, S.OUTCOME_UNKNOWN)
    assert not can_transition(S.OUTCOME_UNKNOWN, S.COMPLETED)


@pytest.mark.parametrize("state", [S.COMPLETED, S.CANCELLED, S.FAILED])
def test_no_resurrection(state):
    assert not can_transition(state, S.RUNNING)


def test_verifying_can_fall_back_to_running_for_retake():
    # e.g. image quality check failed -> re-capture inside the same skill instance
    assert can_transition(S.VERIFYING, S.RUNNING)
