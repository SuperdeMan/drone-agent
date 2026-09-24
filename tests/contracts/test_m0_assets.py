"""Validate cross-file skill and fault-matrix contracts without claiming SITL coverage.

检查跨文件技能与故障矩阵契约，不把静态校验宣称为 SITL 覆盖。
"""

from pathlib import Path

import yaml

from drone_agent.contracts import (
    CapabilityDescriptor,
    ControlMode,
    RecoveryPolicy,
    RecoveryTrigger,
    SkillManifest,
    find_resource_conflicts,
)

ROOT = Path(__file__).resolve().parents[2]


def load_yaml(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


# The five M1 skills keep their M1 thresholds; the M2 inspection skill references the M2 scene (D034); the M3
# external-mode skills reference the M3 scene (D039).
# 五个 M1 技能保持 M1 阈值；M2 巡检技能引用 M2 场景（D034）；M3 外部模式技能引用 M3 场景（D039）。
SCENE_OF_SKILL = {
    "skill.flight.takeoff": "m1_campus_v1",
    "skill.flight.fly_route": "m1_campus_v1",
    "skill.flight.capture_image": "m1_campus_v1",
    "skill.flight.return_home": "m1_campus_v1",
    "skill.flight.land": "m1_campus_v1",
    "skill.inspect.asset": "m2_campus_v2",
    "skill.flight.goto_local": "m3_campus_v3",
    "skill.inspect.asset_local": "m3_campus_v3",
}


def test_skill_manifests_match_target_platform():
    platform_file = load_yaml("configs/platforms/px4_sitl_multirotor.yaml")
    platform = CapabilityDescriptor.model_validate(platform_file["capability"])
    # Optional modes exist only while a companion process is healthy; they are never in the static capability (D039).
    # 可选模式只在伴飞进程健康时存在，从不写进静态能力（D039）。
    optional = {ControlMode(mode) for mode in platform_file.get("optional_control_modes", [])}
    assert not optional & platform.control_modes
    offered = platform.model_copy(update={"control_modes": platform.control_modes | optional})
    manifests = [SkillManifest.model_validate(load_yaml(path)) for path in sorted((ROOT / "configs/skills").glob("*.yaml"))]
    assert {m.skill_id: m.version for m in manifests} == {s.skill_id: s.version for s in platform.skills}
    assert {m.skill_id for m in manifests} == set(SCENE_OF_SKILL)
    for manifest in manifests:
        assert platform.embodiment in manifest.embodiments
        assert not offered.missing_for(skills=[manifest.skill_id], control_modes=manifest.required_control_modes)
        if manifest.required_control_modes & optional:
            # Without the live optional mode the static platform must not satisfy the skill. / 没有在线可选模式时静态平台不得满足该技能。
            assert platform.missing_for(skills=[manifest.skill_id], control_modes=manifest.required_control_modes)
        assert manifest.preconditions and manifest.invariants and manifest.completion_evidence and manifest.failure_modes
        assert manifest.params_schema["additionalProperties"] is False
        assert set(manifest.params_schema["required"]) <= set(manifest.params_schema["properties"])
        assert 0 < manifest.estimated_duration_s <= manifest.timeout_s
        assert all(claim.resource_id.startswith(platform.robot_id + ".") for claim in manifest.resources)
        prefix = SCENE_OF_SKILL[manifest.skill_id] + "."
        assert all(evidence.criteria["threshold_ref"].startswith(prefix) for evidence in manifest.completion_evidence)
        if manifest.pause.pausable:
            assert manifest.pause.safe_wait_condition and 0 < manifest.pause.max_wait_s <= manifest.timeout_s
        # Every skill carries a simulation-only estimate whose scalar is the conservative bound (D034).
        # 每个技能都带仅仿真的能耗估计，标量取保守上界（D034）。
        estimate = manifest.energy_estimate
        assert estimate is not None and estimate.basis == "sim_only" and estimate.source_runs
        assert manifest.estimated_energy_fraction == estimate.upper_fraction >= estimate.mean_fraction
        # A phased skill declares preconditions for every phase and no phase may be empty.
        # 多相位技能为每个相位声明前置条件，且不能为空。
        assert all(phase.preconditions for phase in manifest.intent_phases)
    motion = [m for m in manifests if m.skill_id != "skill.flight.capture_image"]
    local_inspect = next(m for m in manifests if m.skill_id == "skill.inspect.asset_local")
    assert [p.phase for p in local_inspect.intent_phases] == ["approach", "capture"]
    assert {ControlMode.EXTERNAL_MODE} == local_inspect.required_control_modes
    for index, first in enumerate(motion):
        for second in motion[index + 1:]:
            # Motion is exclusive across every motion skill; two camera-claiming skills also share the camera.
            # 所有运动技能之间运动资源互斥；两个都占用相机的技能还共享相机冲突。
            shared = {c.resource_id for c in first.resources} & {c.resource_id for c in second.resources}
            assert find_resource_conflicts(first.resources, second.resources) == sorted(shared)
            assert "uav_01.motion" in shared
    inspect = next(m for m in manifests if m.skill_id == "skill.inspect.asset")
    assert [p.phase for p in inspect.intent_phases] == ["approach", "capture"]
    assert {c.resource_id for c in inspect.resources} == {"uav_01.motion", "uav_01.camera"}


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
