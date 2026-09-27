"""P4 work orders: one order per confirmed finding, rounds of repair feedback, reinspection runs and settlement (D063).

An order is created only from a finding a reviewer confirmed, and pins the reinspection template its repair feedback
will start. Each round of feedback, the reinspection run it starts and the order's move to `reinspection_requested`
commit in one transaction; the run's `settle_reinspection` node then applies `reinspection-v1` (`settle_round`) and,
only when every condition holds, closes the order and resolves the finding in the same transaction. A round whose
run ends unsettled is unknown. Nothing here approves a flight: the reinspection run's mission is approved like any
other.

P4 工单：每个已确认发现一张工单、维修反馈轮次、复检运行与结算（D063）。

工单只能由 reviewer 确认的发现创建，并固定其维修反馈将启动的复检模板。每一轮反馈、它启动的复检运行与工单转为
`reinspection_requested` 在一个事务中提交；随后运行的 `settle_reinspection` 节点应用 `reinspection-v1`（`settle_round`），
只有全部条件成立时才在同一事务中关单并解决发现。运行未经结算就结束的轮次为 unknown。这里不批准任何飞行：复检运行的任务
与其他任务一样逐个审批。
"""

from __future__ import annotations

import uuid

from drone_agent.fleet.business_models import (
    DECISIONS,
    REPAIRABLE,
    REQUEST_ID,
    Conclusion,
    Feedback,
    FindingState,
    JobInputs,
    JobResult,
    OrderState,
    ReviewSubject,
    RoundState,
    round_subject,
    settle_round,
)
from drone_agent.fleet.service import ServiceError
from drone_agent.fleet.workflow_models import (
    TERMINAL_RUN,
    InternalTrigger,
    RunState,
    SettleOutput,
    activity_key,
    run_principal,
)
from drone_agent.runtime.permission import ANALYSIS_READ, WORKFLOW_REVIEW, WORKFLOW_RUN


class WorkOrders:
    """Orders, rounds and settlement of one business engine. / 一个业务引擎的工单、轮次与结算。"""

    def __init__(self, engine):
        self.engine = engine

    @property
    def store(self):
        return self.engine.store

    @staticmethod
    def round_of(run: dict) -> tuple[str, int] | None:
        """(order, round) of a reinspection run, from its trigger identity. / 从触发身份得出复检运行的（工单，轮次）。"""
        source, event = run["trigger_source"], run["trigger_event"]
        if not source.startswith("internal:ord-") or not event.startswith("r") or not event[1:].isdigit():
            return None
        return source.split(":", 1)[1], int(event[1:])

    # ── creation / 创建 ──

    def create(self, run: dict, payload: dict, key: str, guard) -> dict | None:
        """The outbox consumer of a P4 `create_work_order`: one order per confirmed finding (D063 §8).

        P4 `create_work_order` 的 outbox 消费者：每个已确认发现一张工单（D063 §8）。
        """
        with self.store.transaction() as db:
            known = self.store.order_by_key(key)
            if known is not None:
                return known
            if guard is not None and not guard(db):
                return None
            finding = self.store.finding(payload["finding_id"])
            review = self.store.review(ReviewSubject.FINDING.value, payload["finding_id"])
            if finding is None or finding["state"] != FindingState.CONFIRMED.value or review is None \
                    or review["decision"] != "confirmed" \
                    or not self.engine.reviewer_holds(review["reviewer"], finding["project_id"]):
                # A model finding never becomes an order without a human confirmation (D052).
                # 没有人工确认，模型发现永远不会成为工单（D052）。
                raise ServiceError("business.not_confirmed", "the finding has no reviewer confirmation")
            target = payload["reinspection"]
            order, _ = self.store.create_order(
                key=key, finding=finding, review_id=review["review_id"],
                reinspection={**target, "catalog_sha256": run["catalog_sha256"],
                              "asset_id": finding["body"]["asset_id"]},
                body={"asset_id": finding["body"]["asset_id"], "family": finding["family"],
                      "defect_types": finding["body"].get("defect_types", []), "run_id": run["run_id"],
                      "evidence": finding["body"].get("evidence", [])[-5:]},
                actor=run_principal(run["run_id"]))
        return order

    # ── repair feedback / 维修反馈 ──

    def _order(self, caller, project_id, order_id, scope: str) -> dict:
        self.engine.require(caller, project_id, ANALYSIS_READ)
        order = self.store.order(order_id) if isinstance(order_id, str) else None
        if order is None or order["project_id"] != project_id:
            raise ServiceError("service.not_found", str(order_id)[:40])
        self.engine.require(caller, project_id, scope)
        return order

    def repair(self, caller, project_id, order_id, request_id, note) -> dict:
        """A new round of repair feedback and, in the same transaction, its reinspection run (D063 §8).

        新一轮维修反馈，并在同一事务中启动其复检运行（D063 §8）。
        """
        order = self._order(caller, project_id, order_id, WORKFLOW_RUN)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        workflows = self.engine.workflow_store
        with self.store.transaction():
            order = self.store.order(order["order_id"])
            for existing in self.store.rounds(order["order_id"]):
                if existing["feedback"]["request_id"] == request_id:
                    return self.view(order)
            if OrderState(order["state"]) not in REPAIRABLE:
                raise ServiceError("workflow.order_state", f"the order is {order['state']}")
            number = order["round"] + 1
            feedback = Feedback(feedback_id="fb-" + uuid.uuid4().hex[:16], request_id=request_id,
                                reported_by=caller.identity, reported_at=self.engine.clock(), note=str(note or "")[:300])
            self.store.create_round(order["order_id"], number, feedback.model_dump(mode="json"))
            target = order["reinspection"]
            spec = workflows.pinned(target["catalog_sha256"]).spec(project_id, target["workflow_id"],
                                                                   target["version"])
            trigger = next(t for t in spec.triggers if isinstance(t, InternalTrigger))
            inputs = spec.check_inputs({target["asset_input"]: target["asset_id"]})
            run_id, _ = workflows.start_run(spec, catalog_sha256=target["catalog_sha256"],
                                            source=f"internal:{order['order_id']}", event_id=f"r{number}",
                                            actor=caller.identity, started_by=caller.identity, inputs=inputs,
                                            body={"trigger_id": trigger.trigger_id, "order_id": order["order_id"],
                                                  "round": number, "feedback_id": feedback.feedback_id,
                                                  "finding_id": order["finding_id"]})
            self.store.link_round(order["order_id"], number, run_id)
            self.store.set_order(order["order_id"], version=order["state_version"],
                                 state=OrderState.REINSPECTION_REQUESTED, round=number)
            self.store.event(f"order:{order['order_id']}", "order.repair_reported", caller.identity,
                             {"round": number, "feedback_id": feedback.feedback_id, "run_id": run_id})
        return self.view(self.store.order(order["order_id"]))

    # ── the round's review / 本轮复核 ──

    def review(self, caller, project_id, order_id, round_number, decision, request_id, note) -> dict:
        """A reviewer confirms (or rejects) that the round's unsuspected capture shows the repair.

        reviewer 确认（或否定）本轮未疑似的采集显示已修复。
        """
        order = self._order(caller, project_id, order_id, WORKFLOW_REVIEW)
        if decision not in DECISIONS:
            raise ServiceError("service.invalid_request", "decision must be confirmed or dismissed")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        if not isinstance(round_number, int) or isinstance(round_number, bool):
            raise ServiceError("service.invalid_request", "round must be an integer")
        self.decide(order, round_number, reviewer=caller.identity, decision=decision, request_id=request_id,
                    note=str(note or ""))
        return self.view(self.store.order(order["order_id"]))

    def decide(self, order: dict, round_number: int, *, reviewer: str, decision: str, request_id: str,
               note: str) -> dict:
        subject = round_subject(order["order_id"], round_number)
        with self.store.transaction():
            known = self.store.review(ReviewSubject.ROUND.value, subject)
            if known is not None:
                if known["request_id"] == request_id and known["reviewer"] == reviewer:
                    return known
                raise ServiceError("workflow.already_decided", f"the round was already {known['decision']}")
            current = self.store.round(order["order_id"], round_number)
            job = self.store.job_by_key(self._analysis_key(current)) if current and current["reinspection_run"] \
                else None
            if current is None or current["state"] != RoundState.REINSPECTING.value or job is None \
                    or job["verdict"] != "normal":
                raise ServiceError("workflow.not_waiting", "the round has no unsuspected reinspection capture to review")
            review, _ = self.store.record_review(subject_kind=ReviewSubject.ROUND.value, subject_id=subject,
                                                 project_id=order["project_id"], reviewer=reviewer,
                                                 request_id=request_id, decision=decision, note=note[:300],
                                                 basis={"job_id": job["job_id"], "evidence_id": job["evidence_id"],
                                                        "source": job["source"]})
            self.store.event(f"order:{order['order_id']}", "order.round_reviewed", reviewer,
                             {"round": round_number, "decision": decision})
        return review

    def _analysis_key(self, round_row: dict) -> str:
        """The activity key of the round's reinspection analysis node. / 本轮复检分析节点的活动键。"""
        run = self.engine.workflow_store.run(round_row["reinspection_run"])
        spec = self.engine.workflow_store.spec_of(run)
        node = next(n for n in spec.nodes if n.activity == "analyze_evidence" and n.params.purpose == "reinspection")
        return activity_key(run["run_id"], node.node_id)

    def persisting(self, inputs: JobInputs, job_id: str, result: JobResult) -> str | None:
        """A suspected reinspection result is more evidence of the order's finding, never a new finding.

        疑似的复检结果是工单发现的更多证据，从不是新发现。
        """
        order = self.store.order(inputs.order_id) if inputs.order_id else None
        if order is None:
            return None
        finding = self.store.finding(order["finding_id"])
        body = dict(finding["body"])
        body["persisting"] = ([*body.get("persisting", []), {"job_id": job_id, "round": inputs.round,
                                                              "evidence_id": inputs.evidence.evidence_id,
                                                              "score": result.score}])[-10:]
        self.store.attach(finding["finding_id"], job_id, body)
        return finding["finding_id"]

    # ── settlement / 结算 ──

    def settle(self, run: dict, spec, node_spec, nodes: dict) -> SettleOutput:
        """Apply `reinspection-v1` to the run's round; closing happens only on `passed` (D063 §9).

        对运行的轮次应用 `reinspection-v1`；只有 `passed` 才关单（D063 §9）。
        """
        found = self.round_of(run)
        if found is None:
            raise ServiceError("business.not_reinspection", "the run belongs to no order round")
        order_id, number = found
        analysis_node = spec.node(node_spec.params.analysis_from)
        await_id = analysis_node.params.inspection_from
        inspection = nodes[await_id]["result"] if nodes[await_id]["state"] == "completed" else None
        with self.store.transaction():
            current = self.store.round(order_id, number)
            if current is None or current["state"] != RoundState.REINSPECTING.value:
                conclusion = (current or {}).get("conclusion") or {}
                return SettleOutput(order_id=order_id, round=number, status=conclusion.get("status", "unknown"),
                                    reasons=tuple(conclusion.get("reasons", ())))
            order = self.store.order(order_id)
            job = self.store.job_by_key(activity_key(run["run_id"], analysis_node.node_id))
            if job is not None and job["state"] not in ("completed", "refused"):
                job = None
            review = self.store.review(ReviewSubject.ROUND.value, round_subject(order_id, number))
            missions = {m["mission_id"] for m in self.engine.workflow_store.child_missions(run["run_id"])}
            seen_evidence, seen_media = self.engine.index.seen(order["project_id"], order["asset_key"],
                                                               exclude_missions=missions)
            catalog = self.store.pinned(order["catalog_sha256"])
            conclusion = settle_round(
                feedback=Feedback.model_validate(current["feedback"]), inspection=inspection, run_missions=missions,
                job={"job_id": job["job_id"], "verdict": job["verdict"], "source": job["source"]} if job else None,
                review=review, seen_evidence=seen_evidence, seen_media=seen_media,
                allowed_sources=catalog.closure.allowed_sources, now=self.engine.clock())
            self._conclude(order, number, conclusion)
        return SettleOutput(order_id=order_id, round=number, status=conclusion.status, reasons=conclusion.reasons)

    def _conclude(self, order: dict, number: int, conclusion: Conclusion) -> None:
        """Write the round's conclusion and move the order (and finding); called inside a transaction.

        写入本轮结论并推进工单（与发现）；在事务内调用。
        """
        if not self.store.conclude_round(order["order_id"], number, RoundState(conclusion.status),
                                         conclusion.model_dump(mode="json")):
            return
        state = {"passed": OrderState.CLOSED, "failed": OrderState.REINSPECTION_FAILED,
                 "unknown": OrderState.REINSPECTION_UNKNOWN}[conclusion.status]
        fields = {"closure": conclusion.model_dump(mode="json")} if state is OrderState.CLOSED else {}
        self.store.set_order(order["order_id"], version=order["state_version"], state=state, **fields)
        if state is OrderState.CLOSED:
            finding = self.store.finding(order["finding_id"])
            self.store.set_finding(finding["finding_id"], version=finding["state_version"],
                                   state=FindingState.RESOLVED)
        self.store.event(f"order:{order['order_id']}", f"order.round_{conclusion.status}", self.engine.worker,
                         {"round": number, "reasons": list(conclusion.reasons)})

    def run_ended(self, run: dict) -> None:
        """A reinspection run that ended without settling leaves its round unknown (D063 §9).

        未经结算就结束的复检运行使其轮次为 unknown（D063 §9）。
        """
        found = self.round_of(run)
        if found is None or RunState(run["state"]) not in TERMINAL_RUN:
            return
        order_id, number = found
        with self.store.transaction():
            current = self.store.round(order_id, number)
            if current is None or current["state"] != RoundState.REINSPECTING.value \
                    or current["reinspection_run"] != run["run_id"]:
                return
            conclusion = Conclusion(status="unknown", reasons=("reinspection.run_ended",),
                                    feedback_id=current["feedback"]["feedback_id"], decided_at=self.engine.clock())
            self._conclude(self.store.order(order_id), number, conclusion)

    # ── views / 视图 ──

    def view(self, order: dict) -> dict:
        rounds = []
        for item in self.store.rounds(order["order_id"]):
            run = self.engine.workflow_store.run(item["reinspection_run"]) if item["reinspection_run"] else None
            rounds.append({**item, "run_state": run["state"] if run else None,
                           "review": self.store.review(ReviewSubject.ROUND.value,
                                                       round_subject(order["order_id"], item["round"]))})
        finding = self.store.finding(order["finding_id"])
        return {"order": order, "rounds": rounds,
                "finding": {k: finding[k] for k in ("finding_id", "state", "asset_key", "family", "jobs")},
                "events": self.store.events(f"order:{order['order_id']}", 60)}

    def get(self, caller, project_id, order_id) -> dict:
        return self.view(self._order(caller, project_id, order_id, ANALYSIS_READ))

    def list(self, caller, project_id) -> list[dict]:
        self.engine.require(caller, project_id, ANALYSIS_READ)
        return [{k: o[k] for k in ("order_id", "finding_id", "asset_key", "state", "round", "created_at", "updated_at",
                                   "closed_at")} for o in self.store.orders(project_id)]
