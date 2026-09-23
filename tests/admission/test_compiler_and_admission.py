"""Compiler purity and completion, and the fail-closed admission checks (WP-M2-04/05/06).

编译器的纯函数性与补全，以及 fail closed 的准入检查（WP-M2-04/05/06）。
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta

import pytest

from drone_agent.admission.admission import admit
from drone_agent.admission.airspace import AirspaceStatus, SimulatedAirspaceProvider, airspace_issues
from drone_agent.admission.compiler import INSPECT, compile_spec
from drone_agent.contracts import (
    ControlMode,
    EnergyBudget,
    Frame,
    Pose,
    Position,
    SkillRef,
    SpatialScope,
    TargetRef,
    TaskNode,
    TemporalWindow,
    utcnow,
)
from drone_agent.runtime.issues import codes
from tests.admission.scene import (
    admission_context,
    approve,
    compile_context,
    frame,
    inspect_task,
    registry,
    request,
    spec,
)


def compiled(**overrides):
    result = compile_spec(spec(**overrides), compile_context())
    assert result.ok, codes(result.issues)
    return result.package


def test_minimal_inspection_gets_framework_nodes_and_registry_bound_params():
    result = compile_spec(spec(), compile_context())
    package = result.package
    assert [n.task_id for n in package.nodes] == ["takeoff", "inspect_asset_red", "return_home", "land"]
    assert result.inserted_framework == ["takeoff", "return_home", "land"]
    inspect = package.nodes[1]
    assert inspect.params == {"asset_id": "asset_red", "camera_id": "cam_0", "quality_profile_ref": "m2_campus_v2.image",
                              "approach_route_id": "observe_red", "speed_mps": 2, "max_captures": 3}
    assert inspect.depends_on == ["takeoff"] and package.nodes[2].depends_on == ["inspect_asset_red"]
    assert {c.resource_id for c in inspect.resources} == {"uav_01.motion", "uav_01.camera"}
    assert package.recovery_policy_ref == "multirotor_m1@v1"
    assert package.package_hash == package.compute_hash()


def test_compiled_package_passes_the_onboard_registry_check():
    registry().validate_package(approve(compiled()))


def test_compilation_is_a_pure_function():
    first, second = spec(), spec()
    second = second.model_copy(update={"temporal_window": first.temporal_window, "provenance": first.provenance})
    assert compile_spec(first, compile_context()).package_hash == compile_spec(second, compile_context()).package_hash


def test_inspection_targets_expand_into_inspection_nodes():
    result = compile_spec(spec(tasks=[TaskNode(task_id="takeoff", skill_id="skill.flight.takeoff")],
                               targets=[TargetRef(asset_id="asset_blue")]), compile_context())
    assert result.expanded_targets == ["inspect_asset_blue"]
    assert [n.skill_id for n in result.package.nodes] == [
        "skill.flight.takeoff", INSPECT, "skill.flight.return_home", "skill.flight.land"]


def test_planner_chosen_recovery_policy_is_rejected():
    result = compile_spec(spec(recovery_policy_ref="multirotor_campus@v1"), compile_context())
    assert result.package is None and codes(result.issues) == ["compile.policy_mismatch"]


def test_unknown_skill_and_unknown_asset_are_compile_failures():
    result = compile_spec(spec(tasks=[TaskNode(task_id="t", skill_id="skill.flight.goto_local")]), compile_context())
    assert codes(result.issues) == ["compile.unknown_skill"]
    result = compile_spec(spec(tasks=[inspect_task("asset_green")]), compile_context())
    assert codes(result.issues) == ["compile.unknown_asset"]


@pytest.mark.parametrize(
    "tasks",
    [
        [TaskNode(task_id="land", skill_id="skill.flight.land"),
         TaskNode(task_id="inspect", skill_id=INSPECT, params={"asset_id": "asset_red"}, depends_on=["land"])],
        [TaskNode(task_id="takeoff", skill_id="skill.flight.takeoff"),
         TaskNode(task_id="inspect", skill_id=INSPECT, params={"asset_id": "asset_red"})],
        [TaskNode(task_id="inspect", skill_id=INSPECT, params={"asset_id": "asset_red"}),
         TaskNode(task_id="takeoff", skill_id="skill.flight.takeoff", depends_on=["inspect"])],
    ],
    ids=["content_after_land", "content_not_after_takeoff", "takeoff_after_content"],
)
def test_misordered_framework_is_rejected(tasks):
    result = compile_spec(spec(tasks=tasks), compile_context())
    assert result.package is None and "compile.invalid_order" in codes(result.issues)


def test_spec_values_are_never_overwritten_but_admission_checks_the_binding():
    package = compiled(tasks=[inspect_task(approach_route_id="observe_blue")])
    assert package.nodes[1].params["approach_route_id"] == "observe_blue"
    result = admit(package, admission_context())
    assert not result.accepted and "params.binding_mismatch" in codes(result.issues)


def test_nominal_single_asset_mission_is_admitted_with_its_energy_bound():
    result = admit(compiled(), admission_context(), spec=spec())
    assert result.accepted, codes(result.issues)
    assert result.energy_upper_fraction == pytest.approx(0.7696, abs=1e-3)
    assert result.airspace["filing_status"] == "simulated_unfiled"
    assert all(check.passed for check in result.checks)


def test_two_assets_exceed_the_simulated_battery_budget():
    package = compiled(tasks=[inspect_task("asset_red"), inspect_task("asset_blue", depends_on=["inspect_asset_red"])],
                       targets=[TargetRef(asset_id="asset_red"), TargetRef(asset_id="asset_blue")])
    result = admit(package, admission_context())
    assert codes(result.issues) == ["energy.budget_exceeded"] and not result.accepted


def test_rooftop_volume_is_real_airspace_and_fails_closed():
    package = compiled(spatial_scope=SpatialScope(approved_volume_id="campus_rooftop", frame=frame()))
    result = admit(package, admission_context(request=request(approved_volume_id="campus_rooftop")))
    assert "airspace.not_filed" in codes(result.issues)
    assert "scope.target_outside_volume" in codes(result.issues) and not result.accepted


def test_airspace_modes():
    status = SimulatedAirspaceProvider().query("v", now=utcnow())
    assert status.filing_status == "simulated_unfiled" and status.source.startswith("stub")
    assert airspace_issues("v", {"airspace_mode": "simulation"}, status) == []
    assert codes(airspace_issues("v", {"airspace_mode": "real"}, status)) == ["airspace.not_filed"]
    assert codes(airspace_issues("v", {}, status)) == ["airspace.mode_undeclared"]
    assert codes(airspace_issues("v", None, status)) == ["scope.unregistered_volume"]
    unknown = status.model_copy(update={"filing_status": "unknown"})
    assert codes(airspace_issues("v", {"airspace_mode": "real"}, unknown)) == ["airspace.unknown"]
    filed = AirspaceStatus(**{**status.model_dump(), "filing_status": "filed"})
    assert airspace_issues("v", {"airspace_mode": "real"}, filed) == []


def test_request_scope_bounds_volume_and_assets():
    result = admit(compiled(), admission_context(request=request(approved_volume_id="campus_rooftop")))
    assert "scope.volume_not_requested" in codes(result.issues)
    result = admit(compiled(), admission_context(request=request(asset_ids=["asset_blue"])), spec=spec())
    assert "scope.asset_not_requested" in codes(result.issues)


def test_wrong_frame_and_out_of_volume_target_pose_are_rejected():
    other = Frame(frame_id="map_enu", map_version="campus_v1")
    result = admit(compiled(spatial_scope=SpatialScope(approved_volume_id="campus_training", frame=other)),
                   admission_context())
    assert "scope.frame_mismatch" in codes(result.issues)
    far = TargetRef(description="pole", pose=Pose(frame=frame(), position=Position(x=120, y=0, z=0, covariance=(1,) * 9)))
    bad = spec(targets=[TargetRef(asset_id="asset_red"), far])
    result = admit(compile_spec(bad, compile_context()).package, admission_context(), spec=bad)
    assert "scope.target_outside_volume" in codes(result.issues)


def test_parameter_schema_violations_are_listed_per_field():
    result = admit(compiled(tasks=[inspect_task(speed_mps=99, max_captures=9)]), admission_context())
    assert codes(result.issues).count("params.schema_invalid") == 2


@pytest.mark.parametrize(
    "window",
    [
        lambda now: TemporalWindow(not_before=now - timedelta(hours=2), not_after=now - timedelta(minutes=1)),
        lambda now: TemporalWindow(not_before=now + timedelta(hours=1), not_after=now + timedelta(hours=2)),
        lambda now: TemporalWindow(not_before=now, not_after=now + timedelta(hours=5)),
        lambda now: TemporalWindow(not_before=now, not_after=now + timedelta(seconds=20)),
    ],
    ids=["ended", "too_far", "too_long", "too_short"],
)
def test_time_window_rules(window):
    result = admit(compiled(temporal_window=window(utcnow())), admission_context())
    assert "time.window_invalid" in codes(result.issues) and not result.accepted


def test_missing_capability_and_control_mode_are_rejected():
    caps = registry().capability.model_copy(deep=True)
    caps.skills = [s for s in caps.skills if s.skill_id != INSPECT]
    assert "capability.missing_skill" in codes(admit(compiled(), admission_context(capability=caps)).issues)
    caps = registry().capability.model_copy(deep=True)
    caps.control_modes = {ControlMode.OFFBOARD_VELOCITY}
    assert "capability.missing_control_mode" in codes(admit(compiled(), admission_context(capability=caps)).issues)
    caps = registry().capability.model_copy(deep=True)
    caps.skills = [SkillRef(skill_id=s.skill_id, version="9.9.9") for s in caps.skills]
    assert "capability.version_mismatch" in codes(admit(compiled(), admission_context(capability=caps)).issues)


def test_parallel_branches_with_exclusive_resources_conflict():
    package = compiled(tasks=[inspect_task("asset_red", task_id="a", depends_on=["takeoff"]),
                              inspect_task("asset_blue", task_id="b", depends_on=["takeoff"]),
                              TaskNode(task_id="takeoff", skill_id="skill.flight.takeoff")],
                       energy_budget=EnergyBudget(max_consumption_fraction=0.8, reserve_fraction=0.2))
    assert "resource.conflict" in codes(admit(package, admission_context()).issues)


def test_missing_energy_estimate_is_unknown_and_rejected():
    ctx = admission_context()
    manifests = dict(ctx.registry.manifests)
    manifests[INSPECT] = manifests[INSPECT].model_copy(update={"estimated_energy_fraction": None, "energy_estimate": None})
    changed = copy.copy(ctx.registry)
    changed.manifests = manifests
    result = admit(compiled(), replace(ctx, registry=changed))
    assert "energy.estimate_missing" in codes(result.issues) and not result.accepted


def test_reserve_below_site_minimum_is_invalid():
    result = admit(compiled(energy_budget=EnergyBudget(max_consumption_fraction=0.9, reserve_fraction=0.05)),
                   admission_context())
    assert "energy.budget_invalid" in codes(result.issues)


def test_unverified_edges_are_admitted_only_with_a_human_acknowledgement_flag():
    package = compiled(tasks=[inspect_task("asset_red", task_id="a", depends_on=["takeoff"]),
                              TaskNode(task_id="takeoff", skill_id="skill.flight.takeoff"),
                              TaskNode(task_id="return_home", skill_id="skill.flight.return_home", depends_on=["a"],
                                       allow_unverified_from=["a"])])
    result = admit(package, admission_context())
    assert not result.accepted and codes(result.issues) == ["approval.unverified_edge_requires_human"]
    result = admit(package, admission_context(request=request(unverified_ack_by="operator@example.test")))
    assert result.accepted and result.requires_human_ack == ["return_home"]
    assert codes(result.issues) == ["approval.unverified_edge_requires_human"]
    third_party = request(unverified_ack_by="agent", trust_level="third_party")
    assert not admit(package, admission_context(request=third_party)).accepted


def test_hash_mismatch_and_missing_boundaries_are_rejected():
    package = compiled()
    package.package_hash = "0" * 64
    assert "package.hash_mismatch" in codes(admit(package, admission_context()).issues)
    package = compiled()
    package.energy_budget = None
    package.package_hash = package.compute_hash()
    result = admit(package, admission_context())
    assert codes(result.issues) == ["package.boundary_missing"] and not result.accepted
