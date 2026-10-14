"""D088 handoff tasks inside the P3 scheduler: never served by the queue, rejected with the robot's reason, and a
cancel withdraws the open offer. Ground missions and their settlement are left to the X1 S0 matrix.

P3 调度器中的 D088 交接任务：队列从不处理、以地面车的原因码拒绝、取消撤回未结提议。地面任务版本及其结算交给 X1 S0 矩阵。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from drone_agent.contracts import WorldFact
from drone_agent.eval.p3_world import P3World
from drone_agent.fleet.business_store import migrate as migrate_business
from drone_agent.fleet.handoff import HandoffEngine
from drone_agent.fleet.handoff_store import HandoffStore
from drone_agent.fleet.handoff_store import migrate as migrate_handoffs
from drone_agent.fleet.scheduling_models import TaskState
from drone_agent.fleet.service import ServiceError

ROOT = Path(__file__).resolve().parents[2]
TARGET = WorldFact.model_validate({
    "fact_id": "f1", "subject": "finding-1", "predicate": "x1.target_point", "value": {"kind": "point"},
    "frame": {"frame_id": "map_enu", "map_version": "p3_site_a_v1"},
    "position": {"x": 4.0, "y": 2.0, "z": 0.0, "covariance": [0.5, 0, 0, 0, 0.5, 0, 0, 0, 0]},
    "timestamp": "2026-10-14T08:00:00+00:00", "source": "sensor"})


@pytest.fixture
def world(tmp_path):
    w = P3World(tmp_path / "case", ROOT, seed=7)
    backups = tmp_path / "case" / "service" / "backups"
    migrate_business(w.ledger, backups=backups, clock=w.clock)
    migrate_handoffs(w.ledger, backups=backups, clock=w.clock)
    store = HandoffStore(w.ledger, clock=w.clock)
    w.handoff_engine = HandoffEngine(store, reject_task=w.scheduler.reject_handoff_task, assign=lambda _: None,
                                     clock=w.clock)
    w.scheduler.handoffs = w.handoff_engine
    yield w
    w.ledger.close()


def submit(w: P3World, key: str = "recheck-1", robot: str | None = None) -> dict:
    robot = robot or sorted(w.service.ops.catalog.robots)[0]
    task, _ = w.scheduler.submit_handoff(requested_by="recheck:finding-1", key=key, project_id="campus_ops",
                                         asset_id="asset_mid", volume_id="campus_training", robot_id=robot,
                                         detail={"recheck_id": "r1"})
    return task


def offer(w: P3World, task: dict) -> dict:
    return w.handoff_engine.offer(task_id=task["task_id"], project_id="campus_ops", robot_id=task["candidates"][0],
                                  target=TARGET, evidence_refs=["ev-1"], ttl_s=60, actor="tester")


def test_a_handoff_task_is_never_served_by_the_p3_queue(world):
    async def scenario():
        await world.settle()
        task = submit(world)
        assert task["source"] == "handoff" and task["detail"] == {"mode": "handoff", "recheck_id": "r1"}
        assert task["task_id"] not in [t["task_id"] for t in world.tasks.queue()]
        for _ in range(20):
            await world.step()
        current = world.tasks.task(task["task_id"])
        assert (current["state"], current["robot_id"], world.tasks.live(task["task_id"])) == ("queued", None, None)
    asyncio.run(scenario())


def test_a_rejected_offer_rejects_the_task_with_the_robots_reason(world):
    task = submit(world)
    handoff = offer(world, task)
    world.handoff_engine.reply(handoff["to_robot_id"], {"handoff_id": handoff["handoff_id"], "accepted": False,
                                                        "reason_code": "unreachable"})
    current = world.tasks.task(task["task_id"])
    assert (current["state"], current["reason"]) == (TaskState.REJECTED.value, "handoff.unreachable")
    assert current["outcome"] == {"result": "rejected", "reason": "handoff.unreachable"}


def test_a_cancel_withdraws_the_open_offer(world):
    task = submit(world)
    handoff = offer(world, task)
    world.scheduler.cancel_task(task["task_id"], actor="operator", request_id="c-1", reason="no longer needed")
    assert world.handoff_engine.store.handoff(handoff["handoff_id"])["state"] == "withdrawn"
    assert world.tasks.task(task["task_id"])["state"] == TaskState.CANCELLED.value
    assert world.ledger.deliveries_after(handoff["to_robot_id"], 0) == []


def test_submission_checks_the_robot_and_keeps_its_key(world):
    first = submit(world)
    assert submit(world)["task_id"] == first["task_id"]
    with pytest.raises(ServiceError) as raised:
        submit(world, key="recheck-2", robot="ugv_nowhere")
    assert raised.value.issue.code == "task.candidate_outside_project"
