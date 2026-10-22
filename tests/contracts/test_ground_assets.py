"""X1 ground assets (D088) beside the multirotor ones: the ground manifests and the rover platform match one to one,
no skill crosses embodiments, the ground recovery policy has no hold and no RTL, and every ground edge names a planned
injection. The multirotor contract test stays as it is.

X1 地面资产（D088）与多旋翼资产并列：地面技能清单与地面车平台逐一对应，没有技能跨本体，地面恢复策略既无悬停也无 RTL，
每条地面边都对应一个计划中的注入场景。多旋翼的契约测试保持不变。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from drone_agent.contracts import (
    CapabilityDescriptor,
    Embodiment,
    RecoveryBehavior,
    RecoveryPolicy,
    RecoveryTrigger,
    SkillManifest,
)
from drone_agent.contracts.skill import find_resource_conflicts

ROOT = Path(__file__).resolve().parents[2]
SKELETON = {"skill.ground.arm", "skill.ground.stop", "skill.ground.disarm"}


def load(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def ground_manifests() -> list[SkillManifest]:
    return [SkillManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            for path in sorted((ROOT / "configs/skills/ground").glob("*.yaml"))]


def test_ground_manifests_match_the_rover_platform():
    platform = CapabilityDescriptor.model_validate(load("configs/platforms/px4_sitl_rover.yaml")["capability"])
    manifests = ground_manifests()
    assert {m.skill_id: m.version for m in manifests} == {s.skill_id: s.version for s in platform.skills}
    assert platform.embodiment is Embodiment.GROUND_WHEELED and not platform.limits.can_hover
    for manifest in manifests:
        assert manifest.skill_id.startswith("skill.ground.") and manifest.embodiments == [Embodiment.GROUND_WHEELED]
        assert not platform.missing_for(skills=[manifest.skill_id], control_modes=manifest.required_control_modes)
        assert manifest.preconditions and manifest.invariants and manifest.completion_evidence and manifest.failure_modes
        assert manifest.params_schema["additionalProperties"] is False
        assert set(manifest.params_schema["required"]) <= set(manifest.params_schema["properties"])
        assert 0 < manifest.estimated_duration_s <= manifest.timeout_s
        assert all(claim.resource_id.startswith(platform.robot_id + ".") for claim in manifest.resources)
        assert all(e.criteria["threshold_ref"].startswith("x1_ground_a_v1.") for e in manifest.completion_evidence)
        estimate = manifest.energy_estimate
        assert estimate is not None and estimate.basis == "sim_only" and estimate.source_runs
        assert manifest.estimated_energy_fraction == estimate.upper_fraction >= estimate.mean_fraction
        assert all(phase.preconditions for phase in manifest.intent_phases)
        # Ground predicates only: an aerial one would be missing on the ground registry, so always false.
        # 只用地面谓词：空中谓词在地面登记表上缺失，恒为假。
        assert not {"airborne", "on_ground", "above_asset", "above_landing_site"} & set(manifest.preconditions)
    motion = [m for m in manifests]
    for index, first in enumerate(motion):
        for second in motion[index + 1:]:
            shared = {c.resource_id for c in first.resources} & {c.resource_id for c in second.resources}
            assert "ugv_01.motion" in shared
            assert find_resource_conflicts(first.resources, second.resources) == sorted(shared)


def test_no_skill_crosses_embodiments():
    aerial = CapabilityDescriptor.model_validate(load("configs/platforms/px4_sitl_multirotor.yaml")["capability"])
    rover = CapabilityDescriptor.model_validate(load("configs/platforms/px4_sitl_rover.yaml")["capability"])
    assert not any(s.skill_id.startswith("skill.ground.") for s in aerial.skills)
    assert all(s.skill_id.startswith("skill.ground.") for s in rover.skills)
    # The skeleton skills are never offered to the planner. / 骨架技能从不提供给规划器。
    defaults = load("configs/scenarios/x1_ground_a_v1.yaml")["mission_defaults"]
    assert not SKELETON & set(defaults["planner_skills"])


def test_the_disarm_skill_needs_a_stationary_armed_robot():
    disarm = next(m for m in ground_manifests() if m.skill_id == "skill.ground.disarm")
    assert {"armed", "stationary"} <= set(disarm.preconditions)
    assert not disarm.pause.pausable


def test_the_ground_policy_stops_hands_over_and_never_returns_by_itself():
    policy = RecoveryPolicy.from_yaml(ROOT / "configs/recovery_policies/rover_campus_v1.yaml")
    rover = CapabilityDescriptor.model_validate(load("configs/platforms/px4_sitl_rover.yaml")["capability"])
    assert policy.validate_against(rover) == []
    assert set(policy.nodes) == {RecoveryBehavior.STOP, RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE}
    assert RecoveryBehavior.RTL not in rover.recovery_behaviors and RecoveryBehavior.HOLD not in policy.nodes
    assert all(edge.then is None for edge in policy.edges)
    scenarios = [edge.fault_injection_scenario for edge in policy.edges]
    assert len(set(scenarios)) == len(scenarios) and all(s.startswith("fi.ground.") for s in scenarios)
    covered = {edge.trigger for edge in policy.edges}
    assert {RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST, RecoveryTrigger.OBSERVATION_STALE, RecoveryTrigger.LEASE_EXPIRED,
            RecoveryTrigger.GEOFENCE_PREDICTED_BREACH, RecoveryTrigger.FC_FAILSAFE_ACTIVE, RecoveryTrigger.USER_PAUSE,
            RecoveryTrigger.USER_CANCEL} <= covered
    fc = policy.select(RecoveryTrigger.FC_FAILSAFE_ACTIVE, {})
    assert fc is not None and fc.target is RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE
