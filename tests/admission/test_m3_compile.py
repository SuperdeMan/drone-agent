"""The same inspection compiles to the external-mode or the route version by live capability, or is refused (D039).

同一巡检依实时能力编译为外部模式版本或航线版本，否则被拒（D039）。
"""

from pathlib import Path

import pytest

from drone_agent.admission.admission import admit
from drone_agent.admission.compiler import INSPECT, INSPECT_LOCAL, compile_spec
from drone_agent.contracts import ControlMode, EnergyBudget, Frame, SpatialScope, TargetRef
from drone_agent.mission.registry import M3_SCENE, Registry
from tests.admission.scene import admission_context, approve, compile_context, inspect_task, spec

ROOT = Path(__file__).resolve().parents[2]
M3 = Registry(ROOT, scene=ROOT / M3_SCENE)
EXTERNAL = frozenset({ControlMode.MISSION_UPLOAD, ControlMode.EXTERNAL_MODE})
ROUTE_ONLY = frozenset({ControlMode.MISSION_UPLOAD})


def m3_spec(asset="asset_green"):
    return spec(
        targets=[TargetRef(asset_id=asset)], tasks=[inspect_task(asset)], recovery_policy_ref="multirotor_m3@v2",
        spatial_scope=SpatialScope(approved_volume_id="campus_training",
                                   frame=Frame(frame_id="map_enu", map_version="campus_v3")),
        energy_budget=EnergyBudget(max_consumption_fraction=0.5, reserve_fraction=0.2),
    )


def compiled(modes, asset="asset_green"):
    return compile_spec(m3_spec(asset), compile_context(registry=M3, control_modes=modes))


def node(result, skill):
    return next(n for n in result.package.nodes if n.skill_id == skill)


def test_an_external_mode_robot_gets_the_obstacle_aware_approach():
    result = compiled(EXTERNAL)
    assert result.package is not None, result.issues
    local = node(result, INSPECT_LOCAL)
    assert local.params["approach_goal_id"] == "green_observe" and "approach_route_id" not in local.params
    assert node(result, "skill.flight.return_home").params["return_route_id"] == "return_green"
    M3.validate_package(approve(result.package), control_modes=EXTERNAL)


def test_without_external_mode_the_same_task_compiles_to_the_registered_detour_route():
    result = compiled(ROUTE_ONLY)
    assert result.package is not None, result.issues
    route = node(result, INSPECT)
    assert route.params["approach_route_id"] == "observe_green"
    assert node(result, "skill.flight.return_home").params["return_route_id"] == "return_green"
    M3.validate_package(approve(result.package), control_modes=ROUTE_ONLY)


def test_an_asset_without_a_local_goal_keeps_its_route_even_with_external_mode():
    result = compiled(EXTERNAL, asset="asset_red")
    assert result.package is not None
    assert node(result, INSPECT).params["approach_route_id"] == "observe_red"
    assert node(result, "skill.flight.return_home").params["return_route_id"] == "return"


def test_no_usable_approach_is_a_compile_failure():
    result = compiled(frozenset())
    assert result.package is None
    assert [i.code for i in result.issues] == ["compile.no_approach_for_capability"]


def test_the_m2_compile_path_is_unchanged_without_live_modes():
    result = compile_spec(spec(), compile_context())
    assert result.package is not None
    assert [n.skill_id for n in result.package.nodes] == [
        "skill.flight.takeoff", INSPECT, "skill.flight.return_home", "skill.flight.land"]


@pytest.mark.parametrize(("modes", "blocked"), [(EXTERNAL, False), (ROUTE_ONLY, True)])
def test_admission_rechecks_the_live_capability_of_an_external_package(modes, blocked):
    package = compiled(EXTERNAL).package
    capability = M3.capability.model_copy(update={"control_modes": set(modes)})
    result = admit(package, admission_context(registry=M3, capability=capability, request=None))
    missing = "capability.missing_control_mode" in [i.code for i in result.issues]
    assert missing is blocked
