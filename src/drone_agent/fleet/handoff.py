"""X1 handoff engine (D088): offers of ground recheck tasks, the receiver's reply, the ack that assigns, and expiry.

The coordinator names the receiver; the receiver answers from its own map. The engine lives in the mission-service
process: it never signs or approves anything and never connects to a robot (D031). An offer is a delivery of kind
`handoff_offer`, fetched by the robot like a package; it carries the target fact and the aerial evidence ids, and no
mission exists yet, so its delivery names no mission. The robot replies over `ReplyHandoff` with the answer of its
own reachability check. A reply only records: an acceptance is not ownership. Each engine pass then
- turns every open offer past its expiry into `timed_out` and rejects its task (nothing physical happened: an offer
  carries no mission, so there is nothing to reconcile);
- acks every accepted offer through the injected `assign`, which writes the assignment with the next epoch, the ground
  mission (still waiting for a human approval) and the holds in one transaction; the offer turns `acked` with that
  epoch only when `assign` succeeded, and stays accepted for the next pass otherwise.
A rejection rejects the task with the robot's reason code; nothing is re-offered automatically in the first version.
Duplicate, late and foreign replies are recorded on the offer's history and refused.

X1 交接引擎（D088）：地面复核任务的提议、接收方的回复、完成分配的确认，以及过期。

协调器点名接收方；接收方依据自己的地图作答。引擎位于任务服务进程内：从不签名或批准任何东西，也从不连接机器人（D031）。
提议是 `handoff_offer` 类型的投递，机器人像取任务包一样取到它；提议带目标事实与空中证据 id，此时还没有任务版本，因此投递
不指向任何任务。机器人经 `ReplyHandoff` 回复自身可达性检查的结论。回复只做记录：接受不等于所有权。之后引擎每一轮：
- 把每个已过期的未结提议改为 `timed_out` 并拒绝其任务（没有发生任何物理动作：提议不带任务版本，无需对账）；
- 经注入的 `assign` 确认每个已接受的提议：在一个事务内写入下一代次的分配、地面任务版本（仍需人工审批）与持有；只有
  `assign` 成功，提议才以该代次变为 `acked`，否则保持 accepted，留待下一轮。
拒绝会以机器人的原因码拒绝该任务；第一版不自动重新提议。重复、迟到与冒名的回复记入提议历史并被拒绝。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from drone_agent.contracts import WorldFact, utcnow
from drone_agent.fleet.handoff_store import HandoffStore, StaleHandoff

HANDOFF_OFFER = "handoff_offer"
COORDINATOR = "coordinator"
# Controlled reason codes of a reply (contracts §13). / 回复的受控原因码（契约 §13）。
REASON_CODES = frozenset({"unreachable", "outside_map", "alignment_insufficient", "busy", "energy", "expired"})


class HandoffRefused(ValueError):
    """A reply the engine does not take; `code` is the controlled reason. / 引擎不接受的回复；`code` 是受控原因码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value)


class HandoffEngine:
    """Offers, replies, acks and expiry over the handoff store and the ledger's deliveries.

    `reject_task(task_id, reason)` and `assign(handoff) -> int | None` come from the scheduler side: the first ends a
    task for good, the second performs the assignment transaction and returns its epoch (None to retry later).

    基于交接存储与账本投递的提议、回复、确认与过期。`reject_task(task_id, reason)` 与 `assign(handoff) -> int | None`
    来自调度一侧：前者终结任务，后者执行分配事务并返回代次（返回 None 表示稍后重试）。
    """

    def __init__(self, store: HandoffStore, *, reject_task: Callable[[str, str], None],
                 assign: Callable[[dict], int | None], clock=utcnow):
        self.store, self.reject_task, self.assign, self.clock = store, reject_task, assign, clock
        self.ledger = store.ledger

    def offer(self, *, task_id: str, project_id: str, robot_id: str, target: WorldFact, evidence_refs: list[str],
              ttl_s: float, actor: str) -> dict:
        """Offer a ground task to one robot; a second open offer for the task is refused by the store's index.

        向一台机器人提议一个地面任务；同一任务的第二个未结提议被存储的索引拒绝。
        """
        if target.position is None or target.frame is None:
            raise HandoffRefused("handoff.target_invalid", "an offered target needs a frame and a position")
        handoff_id = "ho-" + uuid.uuid4().hex[:16]
        expires_at = (self.clock() + timedelta(seconds=ttl_s)).isoformat()
        try:
            with self.store.transaction():
                handoff = self.store.insert_offer(handoff_id=handoff_id, task_id=task_id, project_id=project_id,
                                                  from_party=COORDINATOR, to_robot_id=robot_id,
                                                  target=target.model_dump(mode="json"), evidence_refs=evidence_refs,
                                                  expires_at=expires_at, actor=actor)
                payload = {"schema_version": "0.1.0", "handoff_id": handoff_id, "task_id": task_id,
                           "from_robot_id": COORDINATOR, "to_robot_id": robot_id, "expires_at": expires_at,
                           "evidence_refs": list(evidence_refs), "target": target.model_dump(mode="json")}
                delivery = self.ledger.queue_delivery(robot_id, HANDOFF_OFFER, "", handoff["offer_seq"], payload)
                self.store.set_delivery(handoff_id, delivery["delivery_id"])
        except sqlite3.IntegrityError as error:
            raise HandoffRefused("handoff.already_open", task_id) from error
        return self.store.handoff(handoff_id)

    def deliverable(self, robot_id: str, payload: dict) -> bool:
        """Only an offer still open, unexpired and addressed to this robot is handed out. / 只交付仍未结、未过期且发给
        本机的提议。"""
        handoff = self.store.handoff(str(payload.get("handoff_id", "")))
        return (handoff is not None and handoff["state"] == "offered" and handoff["to_robot_id"] == robot_id
                and _when(handoff["expires_at"]) > self.clock())

    def reply(self, robot_id: str, reply: dict) -> dict:
        """Record a robot's reply; acceptance is not ownership. Refusals raise `HandoffRefused` after recording.

        记录机器人的回复；接受不等于所有权。拒绝时先记录再抛出 `HandoffRefused`。
        """
        handoff_id = str(reply.get("handoff_id", ""))
        handoff = self.store.handoff(handoff_id)
        if handoff is None:
            raise HandoffRefused("handoff.not_found", handoff_id)
        body = {"robot_id": robot_id, "accepted": bool(reply.get("accepted")), "reason_code": reply.get("reason_code"),
                "path_length_m": reply.get("path_length_m"), "eta_s": reply.get("eta_s"),
                "map_version": reply.get("map_version"), "received_at": self.clock().isoformat()}
        if handoff["to_robot_id"] != robot_id or reply.get("robot_id") not in (None, "", robot_id):
            self.store.note(handoff_id, "reply_refused", robot_id, {**body, "reason": "not_the_receiver"})
            raise HandoffRefused("auth.robot_mismatch", handoff_id)
        if handoff["state"] != "offered":
            self.store.note(handoff_id, "reply_ignored", robot_id, {**body, "reason": f"offer is {handoff['state']}"})
            raise HandoffRefused("handoff.not_open", handoff["state"])
        if _when(handoff["expires_at"]) <= self.clock():
            self._expire(handoff)
            self.store.note(handoff_id, "reply_ignored", robot_id, {**body, "reason": "expired"})
            raise HandoffRefused("handoff.expired", handoff_id)
        if not body["accepted"] and body["reason_code"] not in REASON_CODES:
            self.store.note(handoff_id, "reply_refused", robot_id, {**body, "reason": "uncontrolled reason code"})
            raise HandoffRefused("handoff.reason_invalid", str(body["reason_code"]))
        try:
            with self.store.transaction():
                self._close_delivery(handoff, True, "handoff.replied")
                if body["accepted"]:
                    return self.store.transition(handoff_id, ("offered",), "accepted", actor=robot_id, body=body,
                                                 reply=body)
                rejected = self.store.transition(handoff_id, ("offered",), "rejected", actor=robot_id, body=body,
                                                 reply=body)
                self.reject_task(handoff["task_id"], f"handoff.{body['reason_code']}")
                return rejected
        except StaleHandoff as error:
            self.store.note(handoff_id, "reply_ignored", robot_id, {**body, "reason": "changed while recording"})
            raise HandoffRefused("handoff.not_open", handoff_id) from error

    def _close_delivery(self, handoff: dict, accepted: bool, reason: str) -> None:
        # An answered or expired offer leaves the pull queue, so it is never scanned again. / 已回复或已过期的提议离开
        # 拉取队列，不再被反复扫描。
        if handoff.get("delivery_id"):
            self.ledger.ack_delivery(handoff["to_robot_id"], handoff["delivery_id"], accepted, reason)

    def _expire(self, handoff: dict) -> bool:
        try:
            with self.store.transaction():
                self.store.transition(handoff["handoff_id"], ("offered", "accepted"), "timed_out", actor=COORDINATOR,
                                      body={"expires_at": handoff["expires_at"]})
                self._close_delivery(handoff, False, "handoff.timed_out")
                self.reject_task(handoff["task_id"], "handoff.timed_out")
        except StaleHandoff:
            return False
        return True

    def tick(self) -> dict:
        """One pass: expire overdue offers, then ack accepted ones. `assign` returning None retries on the next pass;
        raising `HandoffRefused` (a deterministic refusal) withdraws the offer and ends its task with the code.

        一轮：先让逾期提议过期，再确认已接受的提议。`assign` 返回 None 时下一轮重试；抛出 `HandoffRefused`（确定性拒绝）
        时撤回提议并以该原因码终结其任务。
        """
        expired = [h["handoff_id"] for h in self.store.expired(self.clock().isoformat()) if self._expire(h)]
        acked, withdrawn = [], []
        for handoff in self.store.open_offers():
            if handoff["state"] != "accepted":
                continue
            try:
                epoch = self.assign(handoff)
            except HandoffRefused as refusal:
                if self.withdraw(handoff["task_id"], actor=COORDINATOR, reason=refusal.code):
                    self.reject_task(handoff["task_id"], refusal.code)
                    withdrawn.append(handoff["handoff_id"])
                continue
            if epoch is None:
                continue
            try:
                self.store.transition(handoff["handoff_id"], ("accepted",), "acked", actor=COORDINATOR,
                                      body={"assignment_epoch": epoch}, assignment_epoch=epoch)
            except StaleHandoff:
                continue
            acked.append(handoff["handoff_id"])
        return {"expired": expired, "acked": acked, "withdrawn": withdrawn}

    def withdraw(self, task_id: str, *, actor: str, reason: str) -> str | None:
        """Withdraw the task's open offer (the task was cancelled); its delivery leaves the queue.

        撤回该任务的未结提议（任务已取消）；其投递离开拉取队列。
        """
        handoff = self.store.open_offer(task_id)
        if handoff is None:
            return None
        try:
            with self.store.transaction():
                self.store.transition(handoff["handoff_id"], ("offered", "accepted"), "withdrawn", actor=actor,
                                      body={"reason": reason})
                self._close_delivery(handoff, False, "handoff.withdrawn")
        except StaleHandoff:
            return None
        return handoff["handoff_id"]

    def complete(self, handoff_id: str, *, mission_id: str, outcome: str) -> None:
        """The ground mission of an acked offer settled; the offer is done. / 已确认提议的地面任务已了结；提议完成。"""
        self.store.transition(handoff_id, ("acked",), "completed", actor=COORDINATOR,
                              body={"mission_id": mission_id, "outcome": outcome})
