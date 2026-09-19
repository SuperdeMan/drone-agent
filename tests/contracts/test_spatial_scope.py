"""MissionSpec must reference an approved volume with a frame and map version; the planner cannot mint geometry.

MissionSpec 必须引用带坐标系与地图版本的已批准体积；规划器不能自造几何。
"""

import pytest
from pydantic import ValidationError

from drone_agent.contracts import Frame, MissionSpec, SpatialScope
from tests.contracts.factories import frame, mission_spec


def test_spatial_scope_requires_approved_volume_reference():
    with pytest.raises(ValidationError):
        SpatialScope(approved_volume_id="", frame=frame())


def test_spatial_scope_requires_frame_and_map_version():
    with pytest.raises(ValidationError):
        Frame(frame_id="campus_enu", map_version="")
    with pytest.raises(ValidationError):
        SpatialScope(approved_volume_id="inspection_volume_A", frame=Frame(frame_id="", map_version="v1"))


def test_spatial_scope_has_no_free_geometry_fields():
    # Only a reference and a frame: geometry is resolved by the compiler. / 只有引用与坐标系：几何由编译器解析。
    names = set(SpatialScope.model_fields)
    assert names == {"schema_version", "approved_volume_id", "frame"}


def test_mission_spec_round_trips_scope():
    spec = mission_spec()
    again = MissionSpec.model_validate_json(spec.model_dump_json())
    assert again.spatial_scope.approved_volume_id == "inspection_volume_A"
    assert again.spatial_scope.frame.map_version == "2026-09-01"


def test_temporal_window_must_be_ordered():
    with pytest.raises(ValidationError):
        mission_spec(temporal_window={"not_before": "2026-09-19T12:00:00Z", "not_after": "2026-09-19T11:00:00Z"})


def test_task_dag_rejects_unknown_dependency_and_cycles():
    with pytest.raises(ValidationError):
        mission_spec(tasks=[{"task_id": "a", "skill_id": "skill.flight.takeoff", "depends_on": ["zzz"]}])
    with pytest.raises(ValidationError):
        mission_spec(
            tasks=[
                {"task_id": "a", "skill_id": "skill.flight.takeoff", "depends_on": ["b"]},
                {"task_id": "b", "skill_id": "skill.flight.land", "depends_on": ["a"]},
            ]
        )


def test_allow_unverified_must_name_a_direct_dependency():
    with pytest.raises(ValidationError):
        mission_spec(
            tasks=[
                {"task_id": "a", "skill_id": "skill.flight.takeoff"},
                {"task_id": "b", "skill_id": "skill.flight.land", "depends_on": ["a"], "allow_unverified_from": ["zzz"]},
            ]
        )
