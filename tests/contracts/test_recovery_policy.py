"""D009: recovery policies are validated configuration; edges select by context, not by 'always hover'.

D009：恢复策略是经校验的配置；边按上下文选择，而不是「一律悬停」。
"""

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
    RecoveryValidation,
)
from tests.contracts.factories import NOW, capability

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
    # M1 enables mission upload only; offboard must not be declared yet. / M1 只开航线上传，offboard 不得提前声明。
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
    # Every M0 edge is a draft; scenario names alone are not passed evidence.
    # 所有 M0 策略边都是草案；场景名本身不是通过证据。
    policy = load_policy()
    drafts = policy.unverified_edges()
    assert drafts == policy.edges
    assert len(drafts) == 15
    assert any(e.trigger is RecoveryTrigger.UPLINK_LOST and e.guard.get("authorized_to_continue") is True for e in drafts)
    with pytest.raises(ValueError, match="unverified"):
        policy.require_verified()


def test_validation_must_bind_the_exact_edge_and_scenario():
    edge = load_policy().edges[0]
    edge.validation = RecoveryValidation(
        scenario_id=edge.fault_injection_scenario,
        edge_hash=edge.validation_hash(),
        software_revision="test-only-revision",
        tested_at=NOW,
        evidence_ref="test-only://passed-injection",
    )
    assert edge.is_verified
    edge.validation.scenario_id = "different-scenario"
    assert not edge.is_verified
    edge.validation.scenario_id = edge.fault_injection_scenario
    edge.guard["flight_phase"] = ["cruise"]
    assert not edge.is_verified


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
