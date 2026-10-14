"""The D088 handoff engine: offers as deliveries, replies that record but never transfer ownership, acks only after a
successful assignment, expiry, and the refusals of duplicate, late, foreign and uncontrolled replies.

D088 交接引擎：以投递方式下发提议；回复只做记录、从不转移所有权；只有分配成功才确认；过期；拒绝重复、迟到、冒名与
非受控原因码的回复。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from drone_agent.contracts import WorldFact
from drone_agent.fleet.handoff import HANDOFF_OFFER, HandoffEngine, HandoffRefused
from drone_agent.fleet.handoff_store import HandoffStore, migrate
from drone_agent.fleet.ledger import BusinessLedger
from tests.fleet.test_handoff_store import prepared

START = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
TARGET = WorldFact.model_validate({
    "fact_id": "f1", "subject": "finding-1", "predicate": "x1.target_point", "value": {"kind": "point"},
    "frame": {"frame_id": "map_enu", "map_version": "ground_a_v1"},
    "position": {"x": 4.0, "y": 2.0, "z": 0.0, "covariance": [0.5, 0, 0, 0, 0.5, 0, 0, 0, 0]},
    "timestamp": START.isoformat(), "source": "sensor"})


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Engine:
    """An engine over a prepared ledger whose scheduler side is recorded. / 调度一侧被记录下来的引擎。"""

    def __init__(self, ledger: BusinessLedger, *, epoch: int | None = 1) -> None:
        self.clock, self.rejected, self.assigned, self.epoch = Clock(), [], [], epoch
        self.store = HandoffStore(ledger, clock=self.clock)
        self.engine = HandoffEngine(self.store, reject_task=lambda task, reason: self.rejected.append((task, reason)),
                                    assign=self._assign, clock=self.clock)

    def _assign(self, handoff: dict) -> int | None:
        self.assigned.append(handoff["handoff_id"])
        return self.epoch

    def offer(self, task_id: str = "task-1", ttl_s: float = 60) -> dict:
        return self.engine.offer(task_id=task_id, project_id="p1", robot_id="ugv_01", target=TARGET,
                                 evidence_refs=["ev-1"], ttl_s=ttl_s, actor="tester")


async def engine_for(tmp_path, **options) -> tuple[BusinessLedger, Engine]:
    path = await prepared(tmp_path)
    ledger = BusinessLedger(path)
    migrate(ledger, backups=path.parent / "backups")
    return ledger, Engine(ledger, **options)


def accept(reply_to: dict, robot: str = "ugv_01", **extra) -> dict:
    return {"handoff_id": reply_to["handoff_id"], "accepted": True, "robot_id": robot, "path_length_m": 12.0,
            "eta_s": 12.0, "map_version": "ground_a_v1", **extra}


async def test_an_offer_is_a_delivery_without_a_mission_and_only_open_offers_are_handed_out(tmp_path):
    ledger, x = await engine_for(tmp_path)
    try:
        handoff = x.offer()
        delivery = ledger.delivery(handoff["delivery_id"])
        assert (delivery["kind"], delivery["mission_id"], delivery["robot_id"]) == (HANDOFF_OFFER, "", "ugv_01")
        assert delivery["payload"]["target"]["predicate"] == "x1.target_point"
        assert x.engine.deliverable("ugv_01", delivery["payload"])
        assert not x.engine.deliverable("uav_01", delivery["payload"])
        x.clock.now += timedelta(seconds=61)
        assert not x.engine.deliverable("ugv_01", delivery["payload"])
        with pytest.raises(HandoffRefused) as raised:
            x.offer()
        assert raised.value.code == "handoff.already_open"
    finally:
        ledger.close()


async def test_acceptance_records_and_only_a_successful_assignment_acks(tmp_path):
    ledger, x = await engine_for(tmp_path, epoch=None)
    try:
        handoff = x.offer()
        accepted = x.engine.reply("ugv_01", accept(handoff))
        assert accepted["state"] == "accepted" and accepted["assignment_epoch"] is None
        # The scheduler side could not assign yet: the offer stays accepted. / 调度一侧暂时无法分配：提议保持 accepted。
        assert x.engine.tick() == {"expired": [], "acked": [], "withdrawn": []}
        assert x.store.handoff(handoff["handoff_id"])["state"] == "accepted"
        x.epoch = 3
        assert x.engine.tick()["acked"] == [handoff["handoff_id"]]
        acked = x.store.handoff(handoff["handoff_id"])
        assert (acked["state"], acked["assignment_epoch"], acked["reply"]["path_length_m"]) == ("acked", 3, 12.0)
        x.engine.complete(handoff["handoff_id"], mission_id="m-ground", outcome="relocated")
        assert x.store.handoff(handoff["handoff_id"])["state"] == "completed"
    finally:
        ledger.close()


async def test_a_rejection_rejects_the_task_with_the_robots_reason(tmp_path):
    ledger, x = await engine_for(tmp_path)
    try:
        handoff = x.offer()
        rejected = x.engine.reply("ugv_01", {"handoff_id": handoff["handoff_id"], "accepted": False,
                                             "reason_code": "unreachable", "map_version": "ground_a_v1"})
        assert rejected["state"] == "rejected" and x.rejected == [("task-1", "handoff.unreachable")]
        assert x.engine.tick() == {"expired": [], "acked": [], "withdrawn": []} and x.assigned == []
    finally:
        ledger.close()


@pytest.mark.parametrize(("robot", "reply", "code"), [
    ("uav_01", {}, "auth.robot_mismatch"),
    ("ugv_01", {"robot_id": "ugv_02"}, "auth.robot_mismatch"),
    ("ugv_01", {"accepted": False, "reason_code": "tired"}, "handoff.reason_invalid"),
])
async def test_foreign_and_uncontrolled_replies_are_refused_and_recorded(tmp_path, robot, reply, code):
    ledger, x = await engine_for(tmp_path)
    try:
        handoff = x.offer()
        with pytest.raises(HandoffRefused) as raised:
            x.engine.reply(robot, {**accept(handoff), **reply})
        assert raised.value.code == code
        assert x.store.handoff(handoff["handoff_id"])["state"] == "offered"
        assert x.store.events(handoff["handoff_id"])[-1]["kind"] == "reply_refused"
    finally:
        ledger.close()


async def test_duplicate_late_and_after_timeout_replies_are_ignored(tmp_path):
    ledger, x = await engine_for(tmp_path)
    try:
        first = x.offer()
        x.engine.reply("ugv_01", accept(first))
        with pytest.raises(HandoffRefused, match="handoff.not_open"):
            x.engine.reply("ugv_01", accept(first))  # duplicate / 重复
        # An offer nobody answers times out and its task is rejected; a reply after that is ignored.
        # 无人回复的提议过期并拒绝其任务；之后的回复被忽略。
        second = x.offer("task-2", ttl_s=30)
        x.clock.now += timedelta(seconds=31)
        result = x.engine.tick()
        assert second["handoff_id"] in result["expired"] and ("task-2", "handoff.timed_out") in x.rejected
        with pytest.raises(HandoffRefused, match="handoff.not_open"):
            x.engine.reply("ugv_01", accept(second))
        kinds = [e["kind"] for e in x.store.events(second["handoff_id"])]
        assert kinds == ["offered", "timed_out", "reply_ignored"]
    finally:
        ledger.close()


async def test_a_reply_arriving_after_expiry_but_before_the_pass_expires_the_offer(tmp_path):
    ledger, x = await engine_for(tmp_path)
    try:
        handoff = x.offer(ttl_s=10)
        x.clock.now += timedelta(seconds=11)
        with pytest.raises(HandoffRefused, match="handoff.expired"):
            x.engine.reply("ugv_01", accept(handoff))
        assert x.store.handoff(handoff["handoff_id"])["state"] == "timed_out"
        assert x.rejected == [("task-1", "handoff.timed_out")]
    finally:
        ledger.close()


async def test_an_offer_needs_a_positioned_target(tmp_path):
    ledger, x = await engine_for(tmp_path)
    try:
        bare = WorldFact.model_validate({**TARGET.model_dump(mode="json"), "position": None, "frame": None})
        with pytest.raises(HandoffRefused, match="handoff.target_invalid"):
            x.engine.offer(task_id="task-1", project_id="p1", robot_id="ugv_01", target=bare, evidence_refs=[],
                           ttl_s=60, actor="tester")
    finally:
        ledger.close()
