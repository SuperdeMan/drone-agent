"""Concurrency is physical safety: two motion-exclusive skills never run together on one robot.

并发属于物理安全：同一机器人上两个独占运动的技能永远不能同时运行。
"""

import pytest
from pydantic import ValidationError

from drone_agent.contracts import (
    Implementation,
    ImplementationKind,
    LearnedStage,
    ResourceClaim,
    ResourceMode,
    SkillManifest,
    find_resource_conflicts,
)
from tests.contracts.factories import motion_skill


def test_two_exclusive_motion_skills_conflict():
    a, b = motion_skill("skill.flight.fly_route"), motion_skill("skill.inspect.asset")
    assert find_resource_conflicts(a.resources, b.resources) == ["uav_01.motion"]


def test_shared_camera_same_mode_does_not_conflict():
    a = [ResourceClaim(resource_id="uav_01.camera", mode=ResourceMode.SHARED, shared_mode="still")]
    b = [ResourceClaim(resource_id="uav_01.camera", mode=ResourceMode.SHARED, shared_mode="still")]
    assert find_resource_conflicts(a, b) == []


def test_shared_camera_different_mode_conflicts():
    a = [ResourceClaim(resource_id="uav_01.camera", mode=ResourceMode.SHARED, shared_mode="still")]
    b = [ResourceClaim(resource_id="uav_01.camera", mode=ResourceMode.SHARED, shared_mode="video")]
    assert find_resource_conflicts(a, b) == ["uav_01.camera"]


def test_exclusive_beats_shared():
    a = [ResourceClaim(resource_id="uav_01.gimbal", mode=ResourceMode.EXCLUSIVE)]
    b = [ResourceClaim(resource_id="uav_01.gimbal", mode=ResourceMode.SHARED, shared_mode="track")]
    assert find_resource_conflicts(a, b) == ["uav_01.gimbal"]


def test_different_robots_do_not_conflict():
    a = [ResourceClaim(resource_id="uav_01.motion", mode=ResourceMode.EXCLUSIVE)]
    b = [ResourceClaim(resource_id="uav_02.motion", mode=ResourceMode.EXCLUSIVE)]
    assert find_resource_conflicts(a, b) == []


def test_skill_id_format_enforced():
    with pytest.raises(ValidationError):
        motion_skill("fly_route")
    with pytest.raises(ValidationError):
        motion_skill("skill.Flight.fly")


def test_learned_implementation_must_declare_stage():
    # Shadow observes only; limited and full may act. / 影子阶段只观察；limited 与 full 可执行。
    with pytest.raises(ValidationError):
        Implementation(kind=ImplementationKind.LEARNED)
    shadow = Implementation(kind=ImplementationKind.LEARNED, learned_stage=LearnedStage.SHADOW, model_version="v0")
    assert not shadow.may_execute
    assert Implementation(kind=ImplementationKind.LEARNED, learned_stage=LearnedStage.LIMITED, model_version="v0").may_execute


def test_skill_requires_cancel_procedure_and_timeout():
    with pytest.raises(ValidationError):
        SkillManifest(
            skill_id="skill.flight.land",
            version="0.1.0",
            embodiments=["aerial_multirotor"],
            cancel={"cancel_procedure": ""},
            timeout_s=30,
        )
    with pytest.raises(ValidationError):
        SkillManifest(
            skill_id="skill.flight.land",
            version="0.1.0",
            embodiments=["aerial_multirotor"],
            cancel={"cancel_procedure": "continue landing; landing is not cancellable below 2 m"},
            timeout_s=0,
        )
