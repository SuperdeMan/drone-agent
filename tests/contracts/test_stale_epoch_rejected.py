"""Red line D005: the control egress rejects stale epochs, stale sequence numbers, duplicates and expired leases.

红线 D005：控制出口拒绝旧代次、旧序号、重复命令与过期租约。
"""

from datetime import timedelta

import pytest

from drone_agent.contracts import EgressGate
from tests.contracts.factories import NOW, envelope, lease


def test_no_lease_rejects_everything():
    gate = EgressGate()
    assert gate.check(envelope(), now=NOW).reason == "no_lease"


def test_stale_epoch_rejected_after_regrant():
    # After a re-grant, a command minted under the old epoch is replay, not authority.
    # 重新授予后，旧代次下签发的命令是重放，不是控制权。
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
    # Same idempotency key twice: reconcile, never execute twice. / 同一幂等键两次：对账，绝不执行两次。
    gate = EgressGate(lease(epoch=1))
    assert gate.check(envelope(seq=0, command_id="same"), now=NOW).accepted
    dup = gate.check(envelope(seq=1, command_id="same"), now=NOW)
    assert not dup.accepted and dup.reason == "duplicate_command"


def test_expired_lease_rejected():
    gate = EgressGate(lease(epoch=1, ttl_s=10))
    assert gate.check(envelope(), now=NOW + timedelta(seconds=11)).reason == "lease_expired"


def test_expired_intent_rejected():
    gate = EgressGate(lease(epoch=1))
    late = NOW + timedelta(seconds=2)  # envelope is valid for 1 s / 信封只有 1 秒有效期
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


@pytest.mark.parametrize("epoch", [1, 3])
def test_revocation_does_not_erase_epoch_history(epoch):
    gate = EgressGate(lease(epoch=3))
    gate.revoke()
    with pytest.raises(ValueError):
        gate.grant(lease(epoch=epoch))
    gate.grant(lease(epoch=4))
    assert gate.check(envelope(epoch=3), now=NOW).reason == "stale_epoch"


@pytest.mark.parametrize(
    ("field", "value"),
    [("holder", "other"), ("mission_id", "m-002"), ("mission_version", 2), ("resources", ["uav_01.motion"])],
)
def test_same_epoch_cannot_transfer_authority(field, value):
    gate = EgressGate(lease())
    changed = lease()
    setattr(changed, field, value)
    with pytest.raises(ValueError):
        gate.grant(changed)


def test_renewal_preserves_dedup_and_sequence_window():
    gate = EgressGate(lease())
    assert gate.check(envelope(seq=4), now=NOW).accepted
    gate.grant(lease(ttl_s=120))
    assert gate.check(envelope(seq=5), now=NOW).reason == "duplicate_command"
    assert gate.check(envelope(seq=3, command_id="new"), now=NOW).reason == "stale_seq"


def test_external_mutation_does_not_change_installed_lease():
    original = lease()
    gate = EgressGate(original)
    original.lease_epoch = 2
    snapshot = gate.lease
    snapshot.lease_epoch = 3
    assert gate.check(envelope(epoch=1), now=NOW).accepted
    assert gate.check(envelope(epoch=2), now=NOW).reason == "unknown_epoch"


def test_gate_remains_bound_to_robot_after_revocation():
    gate = EgressGate(lease())
    gate.revoke()
    with pytest.raises(ValueError):
        gate.grant(lease(epoch=2, robot_id="uav_02"))


def test_namespaced_idempotency_components_do_not_collide():
    a, b = envelope(), envelope()
    a.key.step_id, a.key.command_id = "step:part", "command"
    b.key.step_id, b.key.command_id = "step", "part:command"
    assert a.key.as_string() != b.key.as_string()
    gate = EgressGate(lease())
    b.command_seq = 1
    assert gate.check(a, now=NOW).accepted
    assert gate.check(b, now=NOW).accepted


def test_future_intent_is_rejected_without_consuming_sequence():
    gate = EgressGate(lease())
    future = envelope()
    future.valid_until = NOW + timedelta(seconds=3)
    future.issued_at = NOW + timedelta(seconds=1)
    assert gate.check(future, now=NOW).reason == "future_intent"
    assert gate.check(envelope(), now=NOW).accepted
