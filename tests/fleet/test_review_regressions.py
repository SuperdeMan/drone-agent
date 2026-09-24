"""M2 review: delayed evidence, journal integrity and idempotent uploads.

M2 评审：迟到证据、账本完整性与幂等上传。
"""

from __future__ import annotations

import pytest

from drone_agent.contracts import Evidence
from drone_agent.fleet.transport import Receipt
from tests.fleet.harness import build_loop
from tests.fleet.test_closed_loop import approved


async def flown(tmp_path):
    loop = build_loop(tmp_path)
    mission_id = (await approved(loop))["mission"]["mission_id"]
    await loop.sync()
    assert (await loop.fly(loop.inbox_package(), epoch=1))["completed"]
    return loop, mission_id


async def test_delayed_media_cannot_complete_a_mission_and_later_converges(tmp_path, monkeypatch):
    loop, mission_id = await flown(tmp_path)
    original = loop.uplink.client.publish_media

    async def unavailable(_media):
        return Receipt(False, "temporary outage")

    monkeypatch.setattr(loop.uplink.client, "publish_media", unavailable)
    await loop.sync()
    assert loop.ledger.report(mission_id)["targets"] == {"asset_red": "uncertain"}
    assert loop.ledger.mission(mission_id)["status"] == "verifying"
    assert len(loop.ledger.versions(mission_id)) == 1
    monkeypatch.setattr(loop.uplink.client, "publish_media", original)
    await loop.sync()
    assert loop.ledger.mission(mission_id)["status"] == "completed"
    assert loop.ledger.report(mission_id)["targets"] == {"asset_red": "completed"}


async def test_journal_gap_blocks_completion_until_the_missing_row_arrives(tmp_path, monkeypatch):
    loop, mission_id = await flown(tmp_path)
    original = loop.uplink.client.publish_event
    withheld = []

    async def with_gap(event):
        if event.data["journal"] == "executive" and event.data["seq"] == 1:
            withheld.append(event)
            return Receipt(True)
        return await original(event)

    monkeypatch.setattr(loop.uplink.client, "publish_event", with_gap)
    await loop.sync()
    assert withheld
    view = loop.service.view(mission_id)
    assert view["live"] is None
    assert not view["report"]["all_targets_completed"]
    assert view["mission"]["status"] == "verifying"
    assert len(view["versions"]) == 1
    assert (await original(withheld[0])).accepted
    loop.service.refresh(mission_id)
    assert loop.ledger.mission(mission_id)["status"] == "completed"


async def test_duplicate_evidence_keeps_uploaded_media_and_rejects_conflicts(tmp_path):
    loop, mission_id = await flown(tmp_path)
    await loop.sync()
    stored = loop.ledger.evidence(mission_id)[0]
    item = Evidence.model_validate(stored["body"])
    assert loop.hub.publish_evidence("uav_01", item).accepted
    assert loop.ledger.evidence(mission_id)[0]["media_path"] == stored["media_path"]
    changed = item.model_copy(update={"subject_ids": ["asset_blue"]})
    assert not loop.hub.publish_evidence("uav_01", changed).accepted
    assert loop.ledger.evidence(mission_id)[0]["body"] == stored["body"]


async def test_restart_reconciles_uploaded_inputs_without_waiting_for_new_events(tmp_path):
    import asyncio

    from drone_agent.fleet.service import MissionService
    from tests.fleet.harness import ROOT, SCENE

    loop, mission_id = await flown(tmp_path)
    await loop.uplink.cycle()
    restarted = MissionService(root=ROOT, scene=SCENE, ledger=loop.ledger, hub=loop.hub,
                               signing_key=loop.key, approval_policy=loop.service.policy)
    stop = asyncio.Event()
    task = asyncio.create_task(restarted.run(stop, period_s=0.01))
    try:
        for _ in range(100):
            if loop.ledger.mission(mission_id)["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert loop.ledger.mission(mission_id)["status"] == "completed"
    finally:
        stop.set()
        await task


async def test_identical_pixels_in_two_missions_keep_two_independent_evidence_records(tmp_path):
    from tests.fleet.harness import request

    loop, first = await flown(tmp_path)
    await loop.sync()
    view = await loop.service.submit(request(key="idem-second-image"))
    second = view["mission"]["mission_id"]
    loop.service.approve(second, 1, approver="tailnet:operator@example.test",
                         package_hash=view["versions"][0]["package_hash"])
    await loop.sync()
    assert (await loop.fly(loop.inbox_package(), epoch=2))["completed"]
    await loop.sync()
    a, b = loop.ledger.evidence(first)[0], loop.ledger.evidence(second)[0]
    assert a["sha256"] == b["sha256"]
    assert a["evidence_id"] != b["evidence_id"]
    for mission_id in (first, second):
        assert loop.ledger.mission(mission_id)["status"] == "completed"
        assert loop.ledger.evidence(mission_id)[0]["media_path"]


@pytest.mark.parametrize("field", ["row_timestamp", "sha256"])
async def test_invalid_forwarded_row_is_rejected_before_persistence(tmp_path, field):
    from drone_agent.fleet.events import journal_row_to_event
    from drone_agent.runtime.ledger import Journal

    loop = build_loop(tmp_path)
    mission_id = (await approved(loop))["mission"]["mission_id"]
    journal = Journal(tmp_path / "row.jsonl")
    try:
        row = journal.append("skill_state", {"step_id": "takeoff", "state": "running"})
    finally:
        journal.close()
    event = journal_row_to_event(row, journal="executive", robot_id="uav_01", mission_id=mission_id, version=1)
    if field == "row_timestamp":
        event.data.pop(field)
    else:
        event.data[field] = "0" * 64
    assert not loop.hub.publish_event("uav_01", event).accepted
    assert loop.ledger.events(mission_id) == []
