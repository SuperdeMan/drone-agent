"""X1 handoff to ground mission in the S0 world (D088): an accepted offer becomes one assigned ground mission that a
human approves, the robot drives it, the task settles and the hold is released only once the robot is parked again; a
reply that disagrees with the robot's map withdraws the offer, and a hold conflict keeps the offer accepted until it
clears. The service entry refuses `--air-ground` without the scheduling and business catalogs.

X1 交接到地面任务（S0 世界，D088）：已接受的提议变为一个已分配的地面任务，经人工审批后地面车执行，任务结算，且只在地面车
再次停回停车位后才释放持有；与地面车地图不一致的回复撤回提议；持有冲突使提议保持 accepted 直到冲突解除。服务入口在没有
调度目录与业务目录时拒绝 `--air-ground`。
"""

from __future__ import annotations

import asyncio
import math
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.eval.logical_ground import Block
from drone_agent.eval.p1_world import TERMINAL
from drone_agent.eval.x1_world import X1World
from drone_agent.fleet.locator import COLOURS

ROOT = Path(__file__).resolve().parents[2]
MID = (3.0, 8.0)  # site a's asset_mid on the ground map / 地面地图中站点 a 的 asset_mid


@pytest.fixture
def world(tmp_path):
    w = X1World(tmp_path / "case", ROOT, seed=7)
    w.rovers["ugv_01"].body.blocks = [Block(*MID, 0.5, COLOURS["orange"])]
    yield w
    w.ledger.close()


def task_of(w: X1World, task_id: str) -> dict:
    return w.scheduler.store.task(task_id)


def test_an_accepted_offer_becomes_a_ground_mission_that_settles_and_releases(world):
    w = world

    async def scenario():
        await w.settle()
        await w.hold(0.5)
        task, handoff = w.offer(robot_id="ugv_01", target=MID)
        await w.until(lambda: task_of(w, task["task_id"])["mission_id"] is not None, "ground mission", 15)
        acked = w.handoff(handoff["handoff_id"])
        assert (acked["state"], acked["assignment_epoch"]) == ("acked", 1)
        assert acked["reply"]["accepted"] and acked["reply"]["map_version"] == "x1_ground_a_v1"
        mission_id = task_of(w, task["task_id"])["mission_id"]
        assert w.scheduler.store.live(task["task_id"])["decision_id"] == f"handoff:{handoff['handoff_id']}"
        assert w.status(mission_id) == "awaiting_approval"
        [hold] = w.service.ops.store.reservations(mission_id=mission_id)
        assert "ugv_01.motion" in hold.resources and hold.dock_id == ""
        # The path passes site a's apron, so the mission holds it too. / 路径经过站点 a 的起降区，任务也持有它。
        assert "shared.pad_a" in hold.resources
        assert all(r in ("ugv_01.motion", "shared.pad_a") or r.startswith("ground.campus.") for r in hold.resources)
        await w.approve(mission_id)
        await w.until(lambda: w.status(mission_id) in TERMINAL, "ground mission end", 90)
        assert w.status(mission_id) == "completed"
        await w.until(lambda: task_of(w, task["task_id"])["state"] == "completed", "task settled", 30)
        assert w.handoff(handoff["handoff_id"])["state"] == "completed"
        await w.until(lambda: w.service.ops.store.reservations(mission_id=mission_id)[0].state.value == "released",
                      "hold released", 30)
        assert w.service.ops.store.reservations(mission_id=mission_id)[0].reason == "reconciled"
        body = w.rovers["ugv_01"].body
        assert body.armed is False and math.dist(body.position, (0.0, 0.0)) <= 0.8
    asyncio.run(scenario())


def test_a_reply_that_disagrees_with_the_robots_map_withdraws_the_offer(world):
    w = world

    async def scenario():
        await w.settle()
        task, handoff = w.offer(robot_id="ugv_01", target=MID)
        # A reply the map does not support, recorded before the robot pulls the offer. / 地面车拉取之前记录一个地图不
        # 支持的回复。
        answer = w.service.ops.registry("ugv_01").answer(MID)
        w.handoffs.reply("ugv_01", {"handoff_id": handoff["handoff_id"], "accepted": True,
                                    "map_version": "x1_ground_a_v1", "path_length_m": answer.path_m + 3.0,
                                    "eta_s": answer.eta_s})
        await w.until(lambda: task_of(w, task["task_id"])["state"] != "queued", "task ended", 15)
        current = task_of(w, task["task_id"])
        assert (current["state"], current["reason"]) == ("rejected", "compile.reachability_mismatch")
        assert w.handoff(handoff["handoff_id"])["state"] == "withdrawn"
        assert w.scheduler.store.live(task["task_id"]) is None and current["mission_id"] is None
    asyncio.run(scenario())


def test_a_hold_conflict_keeps_the_offer_accepted_until_it_clears(world):
    w = world

    async def scenario():
        await w.settle()
        store = w.service.ops.store
        other = "mission:m-elsewhere:v1"
        held, conflicts = store.reserve(activity=other, project_id="campus_ops", mission_id="m-elsewhere", version=1,
                                        robot_id="ugv_01", expires_at=w.clock() + timedelta(minutes=5))
        assert held is not None and not conflicts
        task, handoff = w.offer(robot_id="ugv_01", target=MID)
        await w.until(lambda: w.handoff(handoff["handoff_id"])["state"] == "accepted", "offer accepted", 15)
        await w.hold(1.0)
        assert w.handoff(handoff["handoff_id"])["state"] == "accepted"
        assert task_of(w, task["task_id"])["state"] == "queued"
        store.transition(other, ("reserved",), "released", "test")
        await w.until(lambda: w.handoff(handoff["handoff_id"])["state"] == "acked", "offer acked", 15)
        assert task_of(w, task["task_id"])["state"] == "assigned"
    asyncio.run(scenario())


def test_the_service_entry_needs_scheduling_and_business_for_air_ground(monkeypatch):
    from drone_agent.fleet import main as entry

    monkeypatch.setattr(sys, "argv", ["mission-service", "--scene", "configs/scenarios/m2_campus_v2.yaml",
                                      "--catalog", "configs/sites/x1_campus_v1.yaml", "--air-ground"])
    with pytest.raises(SystemExit):
        entry.main()
