"""X1 ground compile and admission (D088): the skeleton around one recheck, the path from the robot's own map, the
face leg, refusals, the reply check, admission, and the onboard re-check of the same package.

X1 地面编译与准入（D088）：围绕一次复核的骨架、来自地面车自身地图的路径、正对段、各类拒绝、回复核对、准入，以及对同一
任务包的机载复核。
"""

from __future__ import annotations

import copy
import math

import pytest

from drone_agent.admission.compiler import CompileContext
from drone_agent.admission.ground import ORDER, admit_ground, compile_ground
from drone_agent.admission.pipeline import evaluate
from drone_agent.contracts import PackageNode
from tests.admission.ground_scene import (
    COMPOUND,
    MID,
    admission_context,
    approve,
    ground,
    ground_package,
    recheck_spec,
)


def compiled(target=MID, **overrides):
    return compile_ground(recheck_spec(target, **overrides), ground(), robot_id="ugv_01")


def test_a_point_recheck_compiles_into_the_ground_skeleton():
    result = compiled()
    assert result.ok and result.package.package_hash == result.package.compute_hash()
    nodes = result.package.nodes
    assert tuple(n.skill_id for n in nodes) == ORDER
    assert [n.depends_on for n in nodes[1:]] == [[n.task_id] for n in nodes[:-1]] and nodes[0].depends_on == []
    nav, recheck, home = nodes[1].params, nodes[2].params, nodes[3].params
    assert nav["waypoints"][0] == [0.0, 0.0] and nav["goal"] == nav["waypoints"][-1] == recheck["goal"]
    # The face leg ends at a viewing distance and points at the target. / 正对段终点在观察距离内，且指向目标。
    face = recheck["face_point"]
    assert 1.0 <= math.dist(face, MID) <= 2.0
    leg = (face[0] - recheck["goal"][0], face[1] - recheck["goal"][1])
    aim = (MID[0] - face[0], MID[1] - face[1])
    cosine = (leg[0] * aim[0] + leg[1] * aim[1]) / (math.hypot(*leg) * math.hypot(*aim))
    assert cosine > math.cos(math.radians(15))
    # Home is the outbound path reversed, from the face point. / 回程是去程反向，从正对点出发。
    assert home["waypoints"] == [face, *nav["waypoints"][::-1]] and home["waypoints"][-1] == [0.0, 0.0]
    assert recheck["quality_profile_ref"] == "x1_ground_a_v1.image" and recheck["signature"] == "orange"
    assert result.inserted_framework == ["arm", "nav_to", "return_home", "stop", "disarm"]


def test_a_search_region_is_viewed_from_far_enough_to_span_it():
    result = compiled(COMPOUND, kind="search_region", radius_m=2.0)
    assert result.ok
    face = result.package.nodes[2].params["face_point"]
    near = 2.0 / math.tan(math.radians(80) / 2)
    assert near - 0.2 <= math.dist(face, COMPOUND) <= near + 1.0 + 0.2


@pytest.mark.parametrize(("overrides", "code"), [
    ({"recovery_policy_ref": "multirotor_m1@v1"}, "compile.policy_mismatch"),
    ({"mission_id": "x1-test", "goal": "g", "tasks": None}, "compile.ground_shape"),
])
def test_wrong_policy_or_shape_is_refused(overrides, code):
    if overrides.get("tasks", 1) is None:
        spec = recheck_spec()
        spec = spec.model_copy(update={"tasks": [spec.tasks[0], spec.tasks[0].model_copy(update={"task_id": "x"})]})
        result = compile_ground(spec, ground(), robot_id="ugv_01")
    else:
        result = compiled(**overrides)
    assert not result.ok and result.issues[0].code == code


def test_an_unreachable_target_never_compiles():
    result = compiled(COMPOUND)
    assert not result.ok and result.issues[0].code == "compile.ground_unreachable"


def test_a_reply_that_disagrees_with_the_compiled_path_is_refused():
    answer = ground().answer(MID)
    good = {"accepted": True, "map_version": "x1_ground_a_v1", "path_length_m": answer.path_m}
    assert compile_ground(recheck_spec(), ground(), robot_id="ugv_01", reply=good).ok
    for bad in ({**good, "map_version": "x1_ground_a_v0"}, {**good, "path_length_m": answer.path_m + 3},
                {**good, "accepted": False}, {"accepted": True}):
        result = compile_ground(recheck_spec(), ground(), robot_id="ugv_01", reply=bad)
        assert not result.ok and result.issues[0].code == "compile.reachability_mismatch"


def test_admission_accepts_the_compiled_package_and_the_pipeline_dispatches_ground_sites():
    spec = recheck_spec()
    package = compile_ground(spec, ground(), robot_id="ugv_01").package
    admitted = admit_ground(package, admission_context())
    assert admitted.accepted, admitted.issues
    outcome = evaluate(spec, CompileContext(registry=ground(), robot_id="ugv_01"), admission_context())
    assert outcome.blocked_at is None and outcome.package.package_hash == package.package_hash


def tampered(change) -> list[str]:
    package = copy.deepcopy(compiled().package)
    change(package)
    package.package_hash = package.compute_hash()
    return [i.code for i in admit_ground(package, admission_context()).issues if i.severity.value == "error"]


def test_admission_refuses_tampered_ground_packages():
    def through_the_building(package):
        package.nodes[1].params["waypoints"] = [[0.0, 0.0], [0.0, 9.0], [0.0, 14.0]]
        package.nodes[1].params["goal"] = [0.0, 14.0]

    def swapped(package):
        package.nodes[3], package.nodes[4] = package.nodes[4], package.nodes[3]

    def aerial(package):
        package.nodes.insert(1, PackageNode(task_id="takeoff", skill_id="skill.flight.takeoff", skill_version="0.1.0",
                                            robot_id="ugv_01", params={"altitude_m_agl": 4}, depends_on=["arm"],
                                            timeout_s=30))

    assert "scope.ground_path" in tampered(through_the_building)
    assert "compile.invalid_order" in tampered(swapped)
    assert {"capability.missing_skill", "compile.invalid_order"} <= set(tampered(aerial))


def test_the_onboard_registry_re_checks_the_same_package():
    package = ground_package()
    ground().validate_package(package)
    broken = copy.deepcopy(package)
    broken.nodes[3].params["waypoints"][-1] = [5.0, 0.0]  # home no longer at the parking spot / 回程不再回到停车位
    broken.package_hash = broken.compute_hash()
    approve(broken)
    with pytest.raises(ValueError):
        ground().validate_package(broken)
