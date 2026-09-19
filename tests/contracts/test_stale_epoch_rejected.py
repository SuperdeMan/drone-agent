"""Red line D005: the control egress rejects stale epochs, stale sequence numbers, duplicates and expired leases."""

from datetime import timedelta

import pytest

from drone_agent.contracts import EgressGate
from tests.contracts.factories import NOW, envelope, lease


def test_no_lease_rejects_everything():
    gate = EgressGate()
    assert gate.check(envelope(), now=NOW).reason == "no_lease"


def test_stale_epoch_rejected_after_regrant():
    gate = EgressGate(lease(epoch=1))
    assert gate.check(envelope(epoch=1, seq=0), now=NOW).accepted
    gate.grant(lease(epoch=2))
    old = gate.check(envelope(epoch=1, seq=1, command_id="c-old"), now=NOW)
    assert not old.accepted and old.reason == "stale_epoch"
    assert gate.check(envelope(epoch=2, seq=0, command_id="c-new"), now=NOW).accepted


def test_unknown_future_epoch_rejected():
    gate = EgressGate(lease(epoch=1))
    assert gate.check(envelope(epoch=5), now=NOW).reason == "unknown_epoch"


def test_sequence_must_be_monotonic():
    gate = EgressGate(lease(epoch=1))
    assert gate.check(envelope(seq=3, command_id="a"), now=NOW).accepted
    assert gate.check(envelope(seq=2, command_id="b"), now=NOW).reason == "stale_seq"
    assert gate.check(envelope(seq=3, command_id="c"), now=NOW).reason == "stale_seq"
    assert gate.check(envelope(seq=4, command_id="d"), now=NOW).accepted


def test_duplicate_delivery_is_not_re_executed():
    gate = EgressGate(lease(epoch=1))
    assert gate.check(envelope(seq=0, command_id="same"), now=NOW).accepted
    dup = gate.check(envelope(seq=1, command_id="same"), now=NOW)
    assert not dup.accepted and dup.reason == "duplicate_command"


def test_expired_lease_rejected():
    gate = EgressGate(lease(epoch=1, ttl_s=10))
    assert gate.check(envelope(), now=NOW + timedelta(seconds=11)).reason == "lease_expired"


def test_expired_intent_rejected():
    gate = EgressGate(lease(epoch=1))
    late = NOW + timedelta(seconds=2)  # envelope valid for 1 s
    assert gate.check(envelope(), now=late).reason == "expired_intent"


def test_robot_and_mission_binding():
    gate = EgressGate(lease(epoch=1, robot_id="uav_02"))
    assert gate.check(envelope(robot_id="uav_01"), now=NOW).reason == "robot_mismatch"


def test_cannot_regrant_lower_epoch():
    gate = EgressGate(lease(epoch=3))
    with pytest.raises(ValueError):
        gate.grant(lease(epoch=2))


def test_revoke_closes_the_gate():
    gate = EgressGate(lease(epoch=1))
    gate.revoke()
    assert gate.check(envelope(), now=NOW).reason == "no_lease"
