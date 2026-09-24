"""Policy v2 extends v1 without changing what v1 already decided (D042).

策略 v2 扩展 v1，但不改变 v1 已经作出的决定（D042）。
"""

import itertools
from pathlib import Path

import yaml

from drone_agent.contracts import RecoveryBehavior, RecoveryPolicy, RecoveryTrigger

ROOT = Path(__file__).resolve().parents[2]
V1 = RecoveryPolicy.from_yaml(ROOT / "configs/recovery_policies/multirotor_m1_v1.yaml")
V2 = RecoveryPolicy.from_yaml(ROOT / "configs/recovery_policies/multirotor_m3_v2.yaml")
NEW_TRIGGERS = {RecoveryTrigger.PROGRESS_STALLED, RecoveryTrigger.COMPUTE_OVERLOADED, RecoveryTrigger.AUTONOMY_UNAVAILABLE}


def outcome(edge):
    return None if edge is None else (edge.target, edge.then, edge.after_s)


def test_every_v2_edge_is_planned_and_none_claims_verification():
    assert (V2.policy_id, V2.version) == ("multirotor_m3", "v2")
    assert RecoveryBehavior.LAND_AT in V2.nodes
    assert all(edge.fault_injection_scenario and not edge.is_verified for edge in V2.edges)
    assert NEW_TRIGGERS <= {edge.trigger for edge in V2.edges}


def test_inherited_edges_keep_their_exact_content():
    v1 = {edge.fault_injection_scenario: edge.validation_hash() for edge in V1.edges}
    inherited = [edge for edge in V2.edges if not edge.fault_injection_scenario.startswith("fi.m3.")]
    assert {edge.fault_injection_scenario for edge in inherited} == set(v1) - {"fi.energy.low", "fi.energy.critical"}
    for edge in inherited:
        assert edge.validation_hash() == v1[edge.fault_injection_scenario]


def test_mission_upload_contexts_select_the_same_behaviour_as_v1():
    for trigger in RecoveryTrigger:
        if trigger in NEW_TRIGGERS or trigger is RecoveryTrigger.TRAJECTORY_STALE:
            continue
        for phase, localized, reachable, authorized in itertools.product(
            ["takeoff", "hover", "cruise", "inspect", "landing"], [True, False], [True, False], [True, False]
        ):
            v1_context = {"flight_phase": phase, "localization_healthy": localized, "rtl_reachable": reachable,
                          "authorized_to_continue": authorized}
            # The v2 context always carries these keys; visual health is absent when unknown. / v2 上下文总带这些键；视觉健康未知时缺席。
            v2_context = {**v1_context, "control_mode": "mission_upload", "nearest_site_reachable": False}
            assert outcome(V2.select(trigger, v2_context)) == outcome(V1.select(trigger, v1_context)), (
                trigger, v1_context)


def test_external_mode_and_energy_edges_select_the_v2_behaviours():
    external = {"flight_phase": "cruise", "localization_healthy": True, "rtl_reachable": True,
                "authorized_to_continue": True, "control_mode": "external_mode", "nearest_site_reachable": True}
    for trigger in (RecoveryTrigger.OBSERVATION_STALE, RecoveryTrigger.TRAJECTORY_STALE, *NEW_TRIGGERS):
        edge = V2.select(trigger, external)
        assert (edge.target, edge.then, edge.after_s) == (RecoveryBehavior.HOLD, RecoveryBehavior.RTL, 10)
    assert V2.select(RecoveryTrigger.TRAJECTORY_STALE, {**external, "control_mode": "mission_upload"}) is None
    far = {**external, "rtl_reachable": False}
    assert V2.select(RecoveryTrigger.ENERGY_LOW, far).target is RecoveryBehavior.LAND_AT
    assert V2.select(RecoveryTrigger.ENERGY_LOW, {**far, "nearest_site_reachable": False}).target is RecoveryBehavior.LAND_HERE
    visual = V2.select(RecoveryTrigger.LOCALIZATION_DEGRADED, {**external, "visual_localization_ok": True})
    assert (visual.target, visual.then) == (RecoveryBehavior.HOLD, RecoveryBehavior.LAND_HERE)
    blind = V2.select(RecoveryTrigger.LOCALIZATION_DEGRADED, {**external, "visual_localization_ok": False})
    assert blind.target is RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE
    # Unknown visual health never matches the optimistic edge. / 视觉健康未知时绝不匹配乐观边。
    assert V2.select(RecoveryTrigger.LOCALIZATION_DEGRADED, external).target is RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE


def test_the_m0_matrix_m3_draft_edges_are_covered_by_v2():
    matrix = yaml.safe_load((ROOT / "configs/scenarios/m0_fault_matrix.yaml").read_text(encoding="utf-8"))
    drafts = [case for case in matrix["scenarios"] if case["stage"] == "M3"]
    assert {case["scenario_id"] for case in drafts} == {
        "fi.observation_stale.offboard", "fi.trajectory_stale.offboard", "fi.localization.gnss_lost_visual_ok"}
    for case in drafts:
        context = {"flight_phase": "cruise", "localization_healthy": True, "authorized_to_continue": True,
                   "rtl_reachable": True, "nearest_site_reachable": False, **case["context"]}
        if str(context.get("control_mode", "")).startswith("offboard"):
            # M3 realizes the drafted Offboard path as the px4_ros2 external mode (D039). / M3 以外部模式实现草案中的 Offboard 路径（D039）。
            context["control_mode"] = "external_mode"
        if "visual_localization_ok" in context:
            context["visual_localization_ok"] = context.pop("visual_localization_ok")
        edge = V2.select(RecoveryTrigger(case["trigger"]), context)
        assert edge is not None and edge.target.value == case["expected"]["target"]
