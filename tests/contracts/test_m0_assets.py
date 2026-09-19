"""Validate cross-file skill and fault-matrix contracts without claiming SITL coverage.

检查跨文件技能与故障矩阵契约，不把静态校验宣称为 SITL 覆盖。
"""

from pathlib import Path

import yaml

from drone_agent.contracts import (
    CapabilityDescriptor,
    RecoveryPolicy,
    RecoveryTrigger,
    SkillManifest,
    find_resource_conflicts,
)

ROOT = Path(__file__).resolve().parents[2]


def load_yaml(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_five_skill_drafts_match_target_platform():
    platform = CapabilityDescriptor.model_validate(load_yaml("configs/platforms/px4_sitl_multirotor.yaml")["capability"])
    manifests = [SkillManifest.model_validate(load_yaml(path)) for path in sorted((ROOT / "configs/skills").glob("*.yaml"))]
    assert {m.skill_id: m.version for m in manifests} == {s.skill_id: s.version for s in platform.skills}
    assert len(manifests) == 5
    for manifest in manifests:
        assert platform.embodiment in manifest.embodiments
        assert not platform.missing_for(skills=[manifest.skill_id], control_modes=manifest.required_control_modes)
        assert manifest.preconditions and manifest.invariants and manifest.completion_evidence and manifest.failure_modes
        assert manifest.params_schema["additionalProperties"] is False
        assert set(manifest.params_schema["required"]) <= set(manifest.params_schema["properties"])
        assert 0 < manifest.estimated_duration_s <= manifest.timeout_s
        assert all(claim.resource_id.startswith(platform.robot_id + ".") for claim in manifest.resources)
        assert all(evidence.criteria["threshold_ref"].startswith("m1_campus_v1.") for evidence in manifest.completion_evidence)
        if manifest.pause.pausable:
            assert manifest.pause.safe_wait_condition and 0 < manifest.pause.max_wait_s <= manifest.timeout_s
    motion = [m for m in manifests if m.skill_id != "skill.flight.capture_image"]
    for index, first in enumerate(motion):
        for second in motion[index + 1:]:
            assert find_resource_conflicts(first.resources, second.resources) == ["uav_01.motion"]


def test_each_recovery_edge_has_a_matching_planned_injection():
    policy = RecoveryPolicy.from_yaml(ROOT / "configs/recovery_policies/multirotor_campus_v1.yaml")
    matrix = load_yaml("configs/scenarios/m0_fault_matrix.yaml")
    cases = {case["scenario_id"]: case for case in matrix["scenarios"]}
    assert len(cases) == len(matrix["scenarios"]) == len(policy.edges) == 15
    assert matrix["status"] == "planned"
    assert matrix["policy_ref"] == f"{policy.policy_id}@{policy.version}"
    assert len(set(matrix["seeds"])) >= 3
    for edge in policy.edges:
        case = cases[edge.fault_injection_scenario]
        assert policy.select(RecoveryTrigger(case["trigger"]), case["context"]) is edge
        assert case["expected"]["target"] == edge.target.value
        assert case["expected"].get("then") == (edge.then.value if edge.then else None)
        assert case["expected"].get("after_s") == edge.after_s
        assert case["inject"] and case["assertions"]
        assert not edge.is_verified
        if case["stage"] != "M1":
            assert case["m1_check"]
    assert {case["scenario_id"] for case in matrix["runtime_cases"]} >= {
        "fi.command.timeout", "fi.command.duplicate", "fi.command.stale_epoch", "fi.mode.manual_takeover",
        "fi.observation_stale.mission_upload", "fi.context.missing", "fi.resume.conditions", "fi.capture.invalid_evidence",
    }
