"""Delayed browser operations must not change a different skill or disrupt execution.

迟到的浏览器操作不能改变其他技能或打断任务执行。
"""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from drone_agent.contracts import SkillInstanceState, utcnow
from drone_agent.eval.fixture import make_package
from drone_agent.eval.judge import interactive_expectation
from drone_agent.mission.executive import Executive
from drone_agent.mission.registry import Registry
from drone_agent.runtime.ledger import Journal
from drone_agent.runtime.recording import Recorder

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def executive(tmp_path):
    registry = Registry(ROOT)

    class Client:
        def __init__(self):
            self.calls = []
            self.reject = False

        async def operate(self, operation):
            self.calls.append(operation)
            if self.reject:
                raise ValueError("resume conditions not satisfied")
            return {"safety_verdict": "hold", "reason": "user_pause"}

    journal, recorder = Journal(tmp_path / "events.jsonl"), Recorder(tmp_path / "events.mcap")
    agent = Executive(client=Client(), registry=registry, package=make_package(registry, 7, "test-mission"),
                      journal=journal, recorder=recorder, artifacts=tmp_path, executive_id="test-executive")
    agent.node = agent.package.nodes[1]
    agent.states[agent.node.task_id] = SkillInstanceState.RUNNING
    agent.lease = SimpleNamespace(robot_id="uav_01")
    yield agent
    journal.close()
    recorder.close()


def request(**changes):
    return {"request_id": "request-one", "action": "pause", "mission_id": "test-mission", "mission_version": 1,
            "lease_epoch": 1, "step_id": "fly_route", "valid_until": (utcnow() + timedelta(seconds=5)).isoformat(), **changes}


@pytest.mark.parametrize("changes", [
    {"mission_id": "other"}, {"mission_version": 2}, {"lease_epoch": 2}, {"step_id": "land"},
    {"valid_until": "2020-01-01T00:00:00Z"}, {"valid_until": "2020-01-01T00:00:00"}, {"action": "unsupported"},
])
async def test_expired_or_wrong_target_never_reaches_guardian(executive, changes):
    await executive.handle_operator(request(**changes))
    assert executive.client.calls == []
    assert executive.states["fly_route"] == SkillInstanceState.RUNNING
    assert executive.journal.rows[-1]["kind"] == "operator_rejected"


async def test_duplicate_or_already_pausing_request_cannot_crash_lifecycle(executive):
    await executive.handle_operator(request())
    await executive.handle_operator(request())
    await executive.handle_operator(request(request_id="different-request"))
    assert len(executive.client.calls) == 1
    assert executive.states["fly_route"] == SkillInstanceState.PAUSE_REQUESTED
    assert executive.journal.rows[-1]["kind"] == "operator_rejected"


async def test_guardian_rejection_leaves_existing_lifecycle_intact(executive):
    executive.states["fly_route"] = SkillInstanceState.PAUSED
    executive.client.reject = True
    await executive.handle_operator(request(action="resume"))
    assert executive.states["fly_route"] == SkillInstanceState.PAUSED
    assert executive.journal.rows[-1]["kind"] == "operator_rejected"


def test_interactive_expectation_requires_acceptance_and_guardian_cancellation():
    accepted = [{"kind": "operator_request", "data": {"action": "cancel", "accepted": True, "request_id": "cancel-1"}}]
    guardian = [{"kind": "cancel_requested", "data": {"request_id": "cancel-1"}}]
    assert interactive_expectation(accepted, guardian) == "safe_abort"
    assert interactive_expectation(accepted, []) == "completed"
    assert interactive_expectation([], guardian) == "completed"


@pytest.mark.parametrize("delay,reason,resumed,expected", [(121, "user_pause", False, "safe_abort"),
    (1, "user_pause", False, "completed"), (121, "energy_low", False, "completed"), (121, "user_pause", True, "completed")])
def test_pause_timeout_cannot_hide_an_unrelated_recovery(delay, reason, resumed, expected):
    start = utcnow()
    executive = [{"kind": "operator_request", "data": {"accepted": True, "action": "pause", "request_id": "pause-1"}}]
    guardian = [{"kind": "safety_intervention", "data": {"reason": reason}, "timestamp": start.isoformat()}]
    if resumed:
        guardian.append({"kind": "resume_authorized", "data": {}, "timestamp": (start + timedelta(seconds=3)).isoformat()})
    guardian.append({"kind": "recovery_receipt", "data": {"behavior": "rtl", "status": "accepted"},
                     "timestamp": (start + timedelta(seconds=delay)).isoformat()})
    assert interactive_expectation(executive, guardian) == expected
