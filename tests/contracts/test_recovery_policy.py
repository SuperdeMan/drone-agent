"""D009: recovery policies are validated configuration; edges select by context, not by 'always hover'."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from drone_agent.contracts import (
    CapabilityDescriptor,
    ControlMode,
    Embodiment,
    RecoveryBehavior,
    RecoveryPolicy,
    RecoveryTrigger,
)
from tests.contracts.factories import capability

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "configs" / "recovery_policies" / "multirotor_campus_v1.yaml"
PLATFORM = REPO / "configs" / "platforms" / "px4_sitl_multirotor.yaml"


def load_policy() -> RecoveryPolicy:
    return RecoveryPolicy.from_yaml(POLICY)


def test_policy_loads_and_matches_platform_capability():
    policy = load_policy()
    platform = yaml.safe_load(PLATFORM.read_text(encoding="utf-8"))
    caps = CapabilityDescriptor.model_validate(platform["capability"])
    assert policy.validate_against(caps) == []
    assert platform["recovery_policy_ref"] == f"{policy.policy_id}@{policy.version}"


def test_platform_capability_is_honest_about_m1_control_modes():
    platform = yaml.safe_load(PLATFORM.read_text(encoding="utf-8"))
    caps = CapabilityDescriptor.model_validate(platform["capability"])
    assert caps.control_modes == {ControlMode.MISSION_UPLOAD}


def test_heartbeat_loss_depends_on_flight_phase():
    policy = load_policy()
    takeoff = policy.select(RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST, {"flight_phase": "takeoff"})
    cruise = policy.select(
        RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST, {"flight_phase": "cruise", "localization_healthy": True}
    )
    assert takeoff is not None and takeoff.target is RecoveryBehavior.LAND_HERE
    assert cruise is not None and cruise.target is RecoveryBehavior.HOLD and cruise.then is RecoveryBehavior.RTL


def test_energy_low_depends_on_reachability():
    policy = load_policy()
    assert policy.select(RecoveryTrigger.ENERGY_LOW, {"rtl_reachable": True}).target is RecoveryBehavior.RTL
    assert policy.select(RecoveryTrigger.ENERGY_LOW, {"rtl_reachable": False}).target is RecoveryBehavior.LAND_AT


def test_missing_context_key_means_no_match():
    policy = load_policy()
    assert policy.select(RecoveryTrigger.ENERGY_LOW, {}) is None


def test_fc_failsafe_always_hands_over():
    policy = load_policy()
    edge = policy.select(RecoveryTrigger.FC_FAILSAFE_ACTIVE, {"anything": 1})
    assert edge is not None and edge.target is RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE


def test_draft_edges_are_visible():
    # The uplink_lost/authorized_to_continue edge is deliberately unverified until its M1 scenario exists.
    policy = load_policy()
    drafts = policy.unverified_edges()
    assert [e.trigger for e in drafts] == [RecoveryTrigger.UPLINK_LOST]
    assert drafts[0].guard.get("authorized_to_continue") is True


def test_edges_must_target_declared_nodes():
    data = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    data["nodes"] = ["hold"]
    with pytest.raises(ValidationError):
        RecoveryPolicy.model_validate(data)


def test_then_requires_delay():
    data = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    data["edges"] = [{"trigger": "user_cancel", "guard": {}, "target": "hold", "then": "rtl"}]
    with pytest.raises(ValidationError):
        RecoveryPolicy.model_validate(data)


def test_policy_rejects_platform_that_cannot_hover_or_lacks_behaviour():
    policy = load_policy()
    fixed_wing = capability(embodiment=Embodiment.AERIAL_FIXED_WING, can_hover=False)
    errors = policy.validate_against(fixed_wing)
    assert any("embodiment mismatch" in e for e in errors)
    assert any("cannot hover" in e for e in errors)
    limited = capability()
    limited.recovery_behaviors = {RecoveryBehavior.RTL}
    assert any("not supported" in e for e in policy.validate_against(limited))
