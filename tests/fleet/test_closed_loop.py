"""The M2 loop in one process: request, approval and signature, uplink, flight, evidence, report, replan.

M2 闭环在单进程内：请求、审批与签名、uplink、飞行、证据、报告、重规划。
"""

from __future__ import annotations

import json

import pytest

from drone_agent.fleet.service import ServiceError
from tests.fleet.harness import build_loop, draft, request, scripted_planner


async def approved(loop):
    view = await loop.service.submit(request())
    version = view["versions"][0]
    assert view["mission"]["status"] == "awaiting_approval", view["issues"]
    assert version["planner"]["model_id"] == "scripted-fixture" and version["admission"]["accepted"]
    return loop.service.approve(view["mission"]["mission_id"], 1, approver="tailnet:operator@example.test",
                                package_hash=version["package_hash"])


async def test_request_to_completed_report_through_signature_uplink_and_flight(tmp_path):
    loop = build_loop(tmp_path)
    view = await approved(loop)
    mission_id = view["mission"]["mission_id"]
    assert view["versions"][0]["status"] == "queued"
    assert view["versions"][0]["approval"]["signer_key_id"] == loop.key.key_id
    missions = await loop.sync()
    assert loop.ledger.version(mission_id, 1)["status"] == "delivered"
    package = loop.inbox_package()
    assert package.mission_id == mission_id and package.approval.approver == "tailnet:operator@example.test"

    result = await loop.fly(package, epoch=1)
    assert result["completed"], result
    missions = await loop.sync()
    assert missions[mission_id]["status"] == "completed"
    report = loop.ledger.report(mission_id)
    assert report["targets"] == {"asset_red": "completed"} and report["all_targets_completed"]
    inspect = next(r for r in report["rows"] if r["step_id"] == "inspect_asset_red")
    assert (inspect["execution_status"], inspect["effect_verdict"], inspect["service_verdict"]) == (
        "succeeded", "verified", "verified")
    # The mirror is the aircraft's log: every row forwarded, chains intact. / 镜像即飞行器日志：每行都转发、链完整。
    final = loop.service.view(mission_id)
    journals = final["versions"][0]["journals"]
    for name in ("executive", "guardian"):
        rows = (tmp_path / "aircraft" / mission_id / "v1" / f"{name}.jsonl").read_text().splitlines()
        assert journals[name] == {"rows": len(rows), "chain": "ok"}
    assert [v["agrees"] for v in (e["verification"] for e in final["evidence"])] == [True]
    assert loop.ledger.robot("uav_01")["status"]["flight_phase"] == "grounded"


async def test_unverified_inspection_is_retried_as_a_policy_approved_version_after_landing(tmp_path):
    loop = build_loop(tmp_path)
    mission_id = (await approved(loop))["mission"]["mission_id"]
    await loop.sync()
    result = await loop.fly(loop.inbox_package(), epoch=1, frames=[None, None, None])
    assert not result["completed"]
    missions = await loop.sync()
    report = loop.ledger.report(mission_id)
    assert report["targets"] == {"asset_red": "uncertain"}
    v2 = loop.ledger.version(mission_id, 2)
    assert v2["origin"] == "replan" and v2["decision"]["classification"] == "auto_approvable"
    assert v2["approval"]["approver"] == "policy:m2_approval@v1"
    assert v2["approval"]["expires_at"] <= loop.ledger.version(mission_id, 1)["approval"]["expires_at"]
    assert missions[mission_id]["replans"] == 1 and v2["status"] in ("queued", "delivered")

    package = loop.inbox_package()
    assert package.mission_version == 2
    assert [n.task_id for n in package.nodes] == ["takeoff", "inspect_asset_red", "return_home", "land"]
    result = await loop.fly(package, epoch=2)
    assert result["completed"]
    missions = await loop.sync()
    assert missions[mission_id]["status"] == "completed"
    report = loop.ledger.report(mission_id)
    assert report["versions"] == [1, 2] and report["targets"] == {"asset_red": "completed"}
    columns = {(r["mission_version"], r["step_id"]): r["column"] for r in report["rows"]}
    assert columns[(1, "inspect_asset_red")] == "uncertain" and columns[(2, "inspect_asset_red")] == "completed"


async def test_a_new_version_waits_until_the_robot_reports_grounded(tmp_path):
    loop = build_loop(tmp_path)
    mission_id = (await approved(loop))["mission"]["mission_id"]
    await loop.sync()
    await loop.fly(loop.inbox_package(), epoch=1, frames=[None, None, None])
    status = tmp_path / "aircraft" / mission_id / "v1" / "status.json"
    airborne = json.loads(status.read_text())
    airborne["observation"]["in_air"] = airborne["observation"]["armed"] = True
    status.write_text(json.dumps(airborne))
    await loop.sync()
    assert loop.ledger.version(mission_id, 2)["status"] == "approved"
    assert loop.inbox_package().mission_version == 1
    assert not [d for d in loop.ledger.deliveries(mission_id) if d["version"] == 2]


async def test_a_package_altered_after_approval_is_rejected_by_the_uplink(tmp_path):
    loop = build_loop(tmp_path)
    mission_id = (await approved(loop))["mission"]["mission_id"]
    delivery = loop.ledger.deliveries(mission_id)[0]
    payload = delivery["payload"]
    payload["nodes"][1]["params"]["max_captures"] = 9
    loop.ledger._exec("UPDATE deliveries SET payload=? WHERE delivery_id=?",
                      (json.dumps(payload), delivery["delivery_id"]))
    await loop.sync()
    assert loop.ledger.version(mission_id, 1)["status"] == "delivery_rejected"
    assert loop.ledger.delivery(delivery["delivery_id"])["ack_reason"] == "onboard.statement_mismatch"
    assert "onboard.statement_mismatch" in [i["code"] for i in loop.ledger.issues(mission_id)]
    assert not (tmp_path / "inbox/package.json").exists()


async def test_approval_is_bound_to_identity_hash_and_current_version(tmp_path):
    loop = build_loop(tmp_path)
    view = await loop.service.submit(request())
    mission_id, package_hash = view["mission"]["mission_id"], view["versions"][0]["package_hash"]
    for approver, digest, code in (("", package_hash, "approval.identity_missing"),
                                   ("a2a:partner", package_hash, "approval.identity_missing"),
                                   ("tailnet:operator@example.test", "0" * 64, "approval.stale_version")):
        with pytest.raises(ServiceError) as caught:
            loop.service.approve(mission_id, 1, approver=approver, package_hash=digest)
        assert caught.value.issue.code == code
    loop.service.approve(mission_id, 1, approver="tailnet:operator@example.test", package_hash=package_hash)
    with pytest.raises(ServiceError) as caught:
        loop.service.approve(mission_id, 1, approver="tailnet:operator@example.test", package_hash=package_hash)
    assert caught.value.issue.code == "approval.stale_version"
    # The same idempotency key returns the first mission and plans nothing new. / 相同幂等键返回第一次的任务。
    again = await loop.service.submit(request())
    assert again["mission"]["mission_id"] == mission_id and len(loop.ledger.missions()) == 1


async def test_refusals_and_admission_rejections_never_reach_approval(tmp_path):
    decline = {"decision": "decline", "decline_reason": "outside the registered site", "goal": "", "goal_type": "other",
               "approved_volume_id": "campus_training", "tasks": [], "notes": ""}
    loop = build_loop(tmp_path, planner=scripted_planner({"Fly to the stadium downtown.": decline,
                                                          "Inspect the blue marker.": draft("asset_blue")}))
    refused = await loop.service.submit(request("Fly to the stadium downtown.", key="idem-refuse-01"))
    assert refused["mission"]["status"] == "refused"
    scoped = await loop.service.submit(request("Inspect the blue marker.", key="idem-scope-001",
                                               asset_ids=["asset_red"]))
    assert scoped["mission"]["status"] == "rejected"
    assert "scope.asset_not_requested" in [i["code"] for i in scoped["issues"]]
    unknown = await loop.service.submit(request(key="idem-volume-01", approved_volume_id="downtown"))
    assert unknown["mission"]["status"] == "rejected" and unknown["versions"][0]["planner"] is None
    for view in (refused, scoped, unknown):
        with pytest.raises(ServiceError):
            loop.service.approve(view["mission"]["mission_id"], 1, approver="tailnet:operator@example.test",
                                 package_hash=view["versions"][0]["package_hash"] or "")
    assert loop.ledger.deliveries(refused["mission"]["mission_id"]) == []


async def test_operator_requests_bind_to_the_running_step_and_relay_one_at_a_time(tmp_path):
    loop = build_loop(tmp_path)
    mission_id = (await approved(loop))["mission"]["mission_id"]
    with pytest.raises(ServiceError):
        loop.service.operate(mission_id, "pause", requested_by="tailnet:operator@example.test",
                             request_id="op-pause-0001")
    await loop.sync()
    # Pretend the flight started: forward a lease and a running skill as the aircraft would.
    # 假装飞行已开始：像飞行器一样转发一个租约与一个运行中的技能。
    from drone_agent.runtime.ledger import Journal

    artifacts = tmp_path / "aircraft" / mission_id / "v1"
    artifacts.mkdir(parents=True)
    package = loop.inbox_package()
    guardian, executive = Journal(artifacts / "guardian.jsonl"), Journal(artifacts / "executive.jsonl")
    executive.append("mission_accepted", {"mission_id": mission_id, "package_hash": package.package_hash})
    guardian.append("lease", {"mission_id": mission_id, "lease_epoch": 1})
    executive.append("skill_state", {"mission_id": mission_id, "step_id": "takeoff", "previous": "accepted",
                                     "state": "running"})
    await loop.sync()
    assert loop.service.view(mission_id)["live"]["allowed_actions"] == ["pause", "cancel"]
    loop.service.operate(mission_id, "pause", requested_by="tailnet:operator@example.test", request_id="op-pause-0001")
    loop.service.operate(mission_id, "cancel", requested_by="tailnet:operator@example.test", request_id="op-cancel-001")
    await loop.sync()
    mailbox = json.loads((tmp_path / "mailbox/operator.json").read_text())
    assert (mailbox["request_id"], mailbox["action"], mailbox["lease_epoch"], mailbox["step_id"]) == (
        "op-pause-0001", "pause", 1, "takeoff")
    pending = [d for d in loop.ledger.deliveries(mission_id) if d["kind"] == "operator_request" and not d["acked_at"]]
    assert [d["payload"]["request_id"] for d in pending] == ["op-cancel-001"]
    executive.append("operator_request", {"mission_id": mission_id, "request_id": "op-pause-0001", "accepted": True})
    await loop.sync()
    assert json.loads((tmp_path / "mailbox/operator.json").read_text())["request_id"] == "op-cancel-001"
    with pytest.raises(ServiceError):
        loop.service.operate(mission_id, "pause", requested_by="a2a:partner", request_id="op-pause-0002")
    guardian.close()
    executive.close()


async def test_service_outage_is_caught_up_after_the_flight(tmp_path):
    # The flight needs neither the service nor the uplink once the package is in the inbox.
    # 任务包进入 inbox 后，飞行既不需要服务也不需要 uplink。
    loop = build_loop(tmp_path)
    mission_id = (await approved(loop))["mission"]["mission_id"]
    await loop.sync()
    result = await loop.fly(loop.inbox_package(), epoch=1)
    assert result["completed"] and loop.ledger.events(mission_id) == []
    missions = await loop.sync()
    assert missions[mission_id]["status"] == "completed"
