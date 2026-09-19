"""Onboard acceptance: only an approved, hash-matching, in-scope, unexpired package may run.

机载接受规则：只有已审批、哈希匹配、机器人在范围内且未过期的任务包才能运行。
"""

from datetime import timedelta

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
