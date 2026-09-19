"""Onboard acceptance: only an approved, hash-matching, in-scope, unexpired package may run.

机载接受规则：只有已审批、哈希匹配、机器人在范围内且未过期的任务包才能运行。
"""

from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from drone_agent.contracts import MissionPackage
from tests.contracts.factories import NOW, package


def test_approved_package_is_authorized_for_listed_robot():
    pkg = package()
    assert pkg.is_authorized(robot_id="uav_01", now=NOW)


def test_unapproved_package_is_rejected():
    assert not package(approved=False).is_authorized(robot_id="uav_01", now=NOW)


def test_hash_mismatch_is_rejected():
    assert not package(hash_override="0" * 64).is_authorized(robot_id="uav_01", now=NOW)


def test_tampering_after_approval_breaks_the_hash():
    pkg = package()
    pkg.nodes[0].params["altitude_m_agl"] = 120  # someone edits the uplinked package / 有人改了上行后的任务包
    assert not pkg.is_authorized(robot_id="uav_01", now=NOW)


def test_robot_outside_scope_is_rejected():
    assert not package().is_authorized(robot_id="uav_02", now=NOW)


def test_expired_approval_is_rejected():
    assert not package().is_authorized(robot_id="uav_01", now=NOW + timedelta(hours=3))


def test_hash_is_canonical_and_stable():
    a, b = package(), package()
    assert a.compute_hash() == b.compute_hash() == a.package_hash


def test_package_hash_itself_must_match_approval():
    pkg = package()
    pkg.package_hash = "0" * 64
    assert not pkg.is_authorized(robot_id="uav_01", now=NOW)


def test_future_approval_is_not_yet_authority():
    pkg = package()
    pkg.approval.approved_at = NOW + timedelta(minutes=1)
    assert not pkg.is_authorized(robot_id="uav_01", now=NOW)


def test_empty_robot_scope_grants_no_authority():
    pkg = package()
    pkg.approval.allowed_robots = []
    assert not pkg.is_authorized(robot_id="uav_01", now=NOW)


@pytest.mark.parametrize("defect", ["unknown_dependency", "cycle", "duplicate_id", "unapproved_edge"])
def test_compiled_dag_is_validated_again(defect):
    data = package().model_dump()
    node = data["nodes"][0]
    if defect == "unknown_dependency":
        node["depends_on"] = ["missing"]
    elif defect == "cycle":
        node["depends_on"] = [node["task_id"]]
    elif defect == "duplicate_id":
        data["nodes"].append(node.copy())
    else:
        node["allow_unverified_from"] = ["missing"]
    with pytest.raises(ValidationError):
        MissionPackage.model_validate(data)


@pytest.mark.parametrize("field", ["spatial_scope", "temporal_window", "energy_budget"])
def test_missing_boundary_cannot_be_authorized_even_with_matching_hash(field):
    pkg = package()
    setattr(pkg, field, None)
    pkg.package_hash = pkg.compute_hash()
    pkg.approval.package_hash = pkg.package_hash
    assert not pkg.is_authorized(robot_id="uav_01", now=NOW)


@pytest.mark.parametrize("field", ["spatial_scope", "temporal_window", "energy_budget"])
def test_approval_binds_all_mission_boundaries(field):
    pkg = package()
    if field == "spatial_scope":
        pkg.spatial_scope.approved_volume_id = "different_volume"
    elif field == "temporal_window":
        pkg.temporal_window.not_after += timedelta(hours=1)
    else:
        pkg.energy_budget.max_consumption_fraction = 0.7
    assert not pkg.is_authorized(robot_id="uav_01", now=NOW)


@pytest.mark.parametrize("offset", [-1, 3600, 3601])
def test_mission_time_window_is_checked_separately_from_approval(offset):
    pkg = package()
    pkg.approval.approved_at = NOW - timedelta(hours=1)
    assert not pkg.is_authorized(robot_id="uav_01", now=NOW + timedelta(seconds=offset))


def test_timestamp_utc_normalization_preserves_approved_hash():
    pkg = package()
    local_tz = timezone(timedelta(hours=8))
    pkg.temporal_window.not_before = pkg.temporal_window.not_before.astimezone(local_tz)
    pkg.temporal_window.not_after = pkg.temporal_window.not_after.astimezone(local_tz)
    assert pkg.compute_hash() == pkg.package_hash
    assert pkg.is_authorized(robot_id="uav_01", now=NOW)
