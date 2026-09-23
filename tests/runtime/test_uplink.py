"""Onboard uplink checks: no rollback, bound operator requests, only accepted flights go up (D031).

机载 uplink 检查：不回退、操作请求受绑定、只上报已接受的飞行（D031）。
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

from drone_agent.contracts import MissionAction, OperatorRequest, utcnow
from drone_agent.runtime.ledger import Journal
from tests.fleet.harness import build_loop, request


async def delivered(tmp_path):
    loop = build_loop(tmp_path)
    view = await loop.service.submit(request())
    mission_id = view["mission"]["mission_id"]
    loop.service.approve(mission_id, 1, approver="tailnet:operator@example.test",
                         package_hash=view["versions"][0]["package_hash"])
    await loop.sync()
    return loop, loop.inbox_package()


async def test_versions_never_roll_back_and_a_version_never_changes_content(tmp_path):
    loop, package = await delivered(tmp_path)
    assert loop.uplink.accept_package(package) == (True, "accepted")
    newer = package.model_copy(update={"mission_version": 2})
    loop.uplink.state["accepted"][package.mission_id]["version"] = 2
    assert loop.uplink.accept_package(package) == (False, "onboard.version_rollback")
    loop.uplink.state["accepted"][package.mission_id].update(version=1, package_hash="f" * 64)
    assert loop.uplink.accept_package(package) == (False, "onboard.version_rollback")
    assert loop.uplink.accept_package(newer)[1] == "onboard.statement_mismatch"
    assert loop.uplink.accept_package(None) == (False, "onboard.unsigned")


def operation(package, **overrides) -> OperatorRequest:
    values = dict(request_id="op-uplink-001", robot_id="uav_01", mission_id=package.mission_id,
                  mission_version=package.mission_version, lease_epoch=1, step_id="takeoff",
                  action=MissionAction.PAUSE, valid_until=utcnow() + timedelta(seconds=10),
                  requested_by="tailnet:operator@example.test")
    values.update(overrides)
    return OperatorRequest(**values)


async def test_operator_requests_are_checked_before_the_mailbox(tmp_path):
    loop, package = await delivered(tmp_path)
    relay = loop.uplink.relay_operation
    assert relay(operation(package, robot_id="uav_02")) == (False, "auth.robot_mismatch")
    assert relay(operation(package, valid_until=utcnow() - timedelta(seconds=1))) == (False,
                                                                                      "operator_request_expired")
    assert relay(operation(package, mission_version=2)) == (False, "operator_target_changed")
    assert relay(operation(package)) == (True, "relayed")
    assert relay(operation(package)) == (True, "relayed")
    assert relay(operation(package, request_id="op-uplink-002")) == (None, "mailbox_busy")
    mailbox = json.loads((tmp_path / "mailbox/operator.json").read_text())
    assert set(mailbox) == {"request_id", "action", "mission_id", "mission_version", "lease_epoch", "step_id",
                            "valid_until", "requested_by"}


async def test_status_follows_the_most_recent_flight_across_missions(tmp_path):
    loop, package = await delivered(tmp_path)
    await loop.fly(package, epoch=1)
    flown = tmp_path / "aircraft" / package.mission_id / "v1/status.json"
    # An older flight of a mission whose id sorts last must not speak for the robot now.
    # 任务 ID 排在最后、但更早的飞行，不能代表机器人当前状态。
    other = "m-" + "f" * 12
    loop.uplink.state["accepted"][other] = {"version": 1, "versions": [1], "package_hash": "a" * 64,
                                            "signer_key_id": loop.key.key_id}
    stale = tmp_path / "aircraft" / other / "v1/status.json"
    stale.parent.mkdir(parents=True)
    value = json.loads(flown.read_text())
    value["observation"].update(in_air=True, armed=True)
    stale.write_text(json.dumps(value))
    os.utime(stale, ns=(1, 1))
    assert loop.uplink.status().flight_phase == "grounded"
    os.utime(stale)
    assert loop.uplink.status().flight_phase == "airborne"


async def test_only_flights_of_accepted_packages_are_forwarded(tmp_path):
    loop, package = await delivered(tmp_path)
    foreign = tmp_path / "aircraft" / package.mission_id / "v1"
    foreign.mkdir(parents=True)
    journal = Journal(foreign / "executive.jsonl")
    journal.append("mission_accepted", {"mission_id": package.mission_id, "package_hash": "e" * 64})
    journal.close()
    unrelated = tmp_path / "aircraft" / "m-local" / "v1"
    unrelated.mkdir(parents=True)
    local = Journal(unrelated / "executive.jsonl")
    local.append("mission_accepted", {"mission_id": "m-local"})
    local.close()
    await loop.sync()
    assert loop.ledger.events(package.mission_id) == [] and loop.ledger.events("m-local") == []
