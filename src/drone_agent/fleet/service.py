"""Mission service: request -> plan -> compile/admit -> approve and sign -> deliver -> ingest -> verify -> report.

The service is the only place that calls a model (D029) and the only holder of the approval signing key
(D030). It never talks to a guardian: packages and operator requests are queued as deliveries that the
robot's uplink pulls over mTLS (D031), and everything it knows about a flight comes from the forwarded,
hash-chained journal rows. Derived state (step outcomes, verifications, the three-column report, replans)
is recomputed from those rows by `refresh`, so a service restart or a late sync converges to the same
result. A replanned version is approved automatically only when it is a verbatim retry (D032) and is
delivered only after the previous version finished and the robot reported itself grounded.

任务服务：请求 → 规划 → 编译 / 准入 → 审批并签名 → 投递 → 入账 → 复核 → 报告。

服务是唯一调用模型的地方（D029），也是审批签名密钥的唯一持有者（D030）。它从不与 guardian 通信：任务包
与操作请求作为投递排队，由机器人的 uplink 经 mTLS 拉取（D031）；它对一次飞行的全部了解都来自转发的
哈希链账本行。派生状态（步骤结果、复核、三列报告、重规划）由 `refresh` 从这些行重新计算，因此服务
重启或迟到的同步都收敛到同一结果。重规划版本只有在原样重试时才自动批准（D032），并且只在上一版本
结束、机器人报告已在地面之后才投递。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.admission.admission import AdmissionContext
from drone_agent.admission.airspace import SimulatedAirspaceProvider
from drone_agent.admission.compiler import CompileContext
from drone_agent.admission.models import MissionRequest
from drone_agent.admission.pipeline import evaluate
from drone_agent.contracts import (
    ApprovalRecord,
    EffectVerdict,
    Evidence,
    ExecutionStatus,
    MissionAction,
    MissionPackage,
    MissionSpec,
    OperatorRequest,
    StepOutcome,
    utcnow,
)
from drone_agent.fleet.catalog import Catalog
from drone_agent.fleet.coordinator import PassthroughCoordinator
from drone_agent.fleet.events import event_to_row, outcomes_from_rows, verify_chain
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.report import build_report
from drone_agent.fleet.transport import FleetHub
from drone_agent.fleet.verifier import accept_fact, business_judgment, verify
from drone_agent.mission.registry import Registry
from drone_agent.planner.replan import ApprovalPolicy, auto_approval, classify, propose_retry, triggers
from drone_agent.runtime.issues import ISSUE_CODES, Issue, issue
from drone_agent.runtime.signing import SigningKey

# Version states after which the robot may hold the package. / 机器人可能已持有任务包的版本状态。
DELIVERED = ("queued", "delivered", "delivery_rejected", "running", "finished")
OPERATOR_TTL = timedelta(seconds=15)
ALLOWED_ACTIONS = {"running": ["pause", "cancel"], "paused": ["resume", "cancel"]}


class ServiceError(ValueError):
    """A refused service call carrying a controlled issue. / 带受控问题的被拒服务调用。"""

    def __init__(self, code: str, message: str = ""):
        self.issue = issue(code, message)
        super().__init__(f"{code}: {message}" if message else code)


def package_diff(before: dict | None, after: dict) -> dict:
    """Node-level difference between two package versions. / 两个任务包版本之间的节点级差异。"""
    old = {n["task_id"]: n for n in (before or {}).get("nodes", [])}
    new = {n["task_id"]: n for n in after.get("nodes", [])}
    keys = ("skill_id", "skill_version", "params", "depends_on", "allow_unverified_from")
    return {"added": [k for k in new if k not in old], "removed": [k for k in old if k not in new],
            "changed": [k for k in new if k in old and any(old[k].get(f) != new[k].get(f) for f in keys)]}


class MissionService:
    def __init__(self, *, root: Path, scene: Path, ledger: BusinessLedger, hub: FleetHub, signing_key: SigningKey,
                 approval_policy: ApprovalPolicy, planner=None, robot_id: str = "uav_01", airspace=None,
                 clock=utcnow, vision=None):
        self.registry = Registry(root, scene=scene)
        self.ledger, self.hub, self.key, self.policy = ledger, hub, signing_key, approval_policy
        self.planner, self.robot_id, self.clock, self.vision = planner, robot_id, clock, vision
        self.airspace = airspace or SimulatedAirspaceProvider()
        self.catalog = Catalog(ledger, static=self.registry.capability)
        self.coordinator = PassthroughCoordinator(robot_id, self.catalog)
        self.defaults = self.registry.data.get("mission_defaults", {})
        # Reconcile persisted input even when the uplink already acknowledged every item before a restart.
        # 重启后复核持久化输入，即使 uplink 已在重启前确认了每条上传。
        self.dirty: set[str] = {m["mission_id"] for m in ledger.missions(-1) if m["current_version"] > 0}
        self.status_changed = False
        hub.listeners.append(self._ingested)

    def _ingested(self, robot_id: str, kind: str, mission_id: str | None) -> None:
        if mission_id:
            self.dirty.add(mission_id)
        if kind == "status":
            self.status_changed = True

    def _issues(self, found: list[Issue], mission_id: str, request_id: str = "") -> None:
        for item in found:
            self.ledger.record_issue(item, mission_id=mission_id, request_id=request_id or None)

    # ── submit and plan / 提交与规划 ──

    async def submit(self, request: MissionRequest) -> dict:
        """Plan, compile and admit a request; a repeated idempotency key returns the first mission.

        规划、编译并准入一个请求；重复的幂等键返回第一次的任务。
        """
        mission_id, created = self.ledger.record_request(request, "m-" + uuid.uuid4().hex[:12])
        if not created:
            return self.view(mission_id)
        operation = self.ledger.begin_operation(mission_id, "plan", request.idempotency_key)
        try:
            await self._plan(mission_id, request)
        finally:
            self.ledger.finish_operation(operation, "done")
        return self.view(mission_id)

    async def _plan(self, mission_id: str, request: MissionRequest) -> None:
        def stop(status: str, found: list[Issue], **records) -> None:
            self.ledger.record_version(mission_id, 1, status=status, origin=request.channel.value, **records)
            self.ledger.update_mission(mission_id, status=status, current_version=1)
            self._issues(found, mission_id, request.request_id)

        if request.approved_volume_id not in self.registry.data.get("volumes", {}):
            # No model call for a scope the registry does not know. / 登记表不认识的范围不调用模型。
            return stop("rejected", [issue("scope.unregistered_volume", request.approved_volume_id)])
        if self.planner is None:
            return stop("planning_failed", [issue("planner.technical_failure", "no planner is configured")])
        outcome = await self.planner.plan(request, mission_id=mission_id, mission_version=1)
        if outcome.status != "planned":
            return stop("refused" if outcome.status == "refused" else "planning_failed", outcome.issues,
                        planner=outcome)
        spec = outcome.spec
        robot, found = self.coordinator.assign(spec)
        if robot is None:
            return stop("rejected", found, spec=spec, planner=outcome)
        result = self._evaluate(spec, robot, request)
        decision = {"blocked_at": result.blocked_at, "codes": result.codes,
                    "collaboration": self.coordinator.collaboration(spec)}
        package = result.compile.package if result.compile else None
        status = "awaiting_approval" if result.blocked_at is None else "rejected"
        self.ledger.record_version(mission_id, 1, status=status, origin=request.channel.value, spec=spec,
                                   planner=outcome, compile=result.compile, admission=result.admission,
                                   package=package, package_hash=package.package_hash if package else None,
                                   decision=decision)
        self.ledger.update_mission(mission_id, status=status, robot_id=robot, current_version=1)
        self._issues(result.issues, mission_id, request.request_id)

    def _evaluate(self, spec: MissionSpec, robot: str, request: MissionRequest):
        return evaluate(spec, CompileContext(registry=self.registry, robot_id=robot),
                        AdmissionContext(registry=self.registry, capability=self.catalog.capability(robot)[0],
                                         airspace=self.airspace, now=self.clock(), request=request))

    def _request(self, mission: dict) -> MissionRequest:
        return MissionRequest.model_validate(self.ledger.request(mission["request_id"])["body"])

    # ── approval and delivery / 审批与投递 ──

    def _mission(self, mission_id: str) -> dict:
        mission = self.ledger.mission(mission_id)
        if mission is None:
            raise ServiceError("service.not_found", mission_id)
        return mission

    def _awaiting(self, mission_id: str, version: int) -> tuple[dict, dict]:
        mission = self._mission(mission_id)
        record = self.ledger.version(mission_id, version)
        if record is None:
            raise ServiceError("service.not_found", f"{mission_id} v{version}")
        if version != mission["current_version"] or record["status"] != "awaiting_approval":
            raise ServiceError("approval.stale_version", f"v{version} is {record['status']}")
        return mission, record

    def approve(self, mission_id: str, version: int, *, approver: str, package_hash: str) -> dict:
        """Human approval bound to the package hash the approver saw; signs and queues the package.

        绑定审批人所见任务包哈希的人工审批；签名并排队投递。
        """
        if not approver or approver.startswith(("a2a:", "policy:")):
            raise ServiceError("approval.identity_missing", "an identified operator must approve")
        mission, record = self._awaiting(mission_id, version)
        if record["package_hash"] != package_hash:
            raise ServiceError("approval.stale_version", "the package changed since it was shown")
        admission = record["admission"] or {}
        if not admission.get("accepted"):
            raise ServiceError("approval.not_admitted")
        if admission.get("requires_human_ack") and not self._request(mission).unverified_ack_by:
            raise ServiceError("approval.unverified_edge_requires_human", ", ".join(admission["requires_human_ack"]))
        package = MissionPackage.model_validate(record["package"])
        if package.compute_hash() != package_hash:
            raise ServiceError("package.hash_mismatch")
        now = self.clock()
        approval = ApprovalRecord(
            approver=approver, approved_at=now, mission_id=mission_id, mission_version=version,
            package_hash=package_hash, allowed_robots=[mission["robot_id"]],
            expires_at=min(now + timedelta(minutes=self.policy.approval_ttl_minutes), package.temporal_window.not_after))
        self._approved(mission_id, version, package, self.key.sign(approval))
        return self.view(mission_id)

    def decline(self, mission_id: str, version: int, *, approver: str, reason: str = "") -> dict:
        if not approver or approver.startswith(("a2a:", "policy:")):
            raise ServiceError("approval.identity_missing", "an identified operator must decline")
        self._awaiting(mission_id, version)
        self.ledger.update_version(mission_id, version, status="declined",
                                   decision={"declined_by": approver, "reason": reason[:300]})
        self.ledger.update_mission(mission_id, status="declined")
        return self.view(mission_id)

    def _approved(self, mission_id: str, version: int, package: MissionPackage, signed: ApprovalRecord) -> None:
        package = package.model_copy(update={"approval": signed})
        self.ledger.update_version(mission_id, version, status="approved", approval=signed, package=package)
        self.ledger.update_mission(mission_id, status="approved")
        self._deliver_ready(mission_id)

    def _finished_at(self, mission_id: str, version: int) -> datetime | None:
        journals = self._journals(mission_id, version)
        if not self._intact(journals):
            return None
        rows = journals.get("executive", [])
        return next((datetime.fromisoformat(r["timestamp"]) for r in rows if r["kind"] == "mission_result"), None)

    def _deliver_ready(self, mission_id: str) -> bool:
        """Queue the current approved version once the previous flight ended on the ground (D032).

        上一次飞行在地面结束后，把当前已批准版本排队投递（D032）。
        """
        mission = self._mission(mission_id)
        version = mission["current_version"]
        record = self.ledger.version(mission_id, version)
        if record is None or record["status"] != "approved":
            return False
        earlier = [v for v in self.ledger.versions(mission_id) if v["version"] < version and v["status"] in DELIVERED]
        if earlier:
            finished = self._finished_at(mission_id, earlier[-1]["version"])
            if finished is None or not self.catalog.grounded_since(mission["robot_id"], finished):
                return False
        self.ledger.queue_delivery(mission["robot_id"], "mission_package", mission_id, version, record["package"])
        self.ledger.update_version(mission_id, version, status="queued")
        self.ledger.update_mission(mission_id, status="queued")
        return True

    # ── operator requests / 操作请求 ──

    def operate(self, mission_id: str, action: str, *, requested_by: str, request_id: str) -> dict:
        """Queue a pause/resume/cancel bound to the running step the service last saw.

        排队一个绑定到服务最近所见运行步骤的暂停 / 恢复 / 取消请求。
        """
        if not requested_by or requested_by.startswith(("a2a:", "policy:")):
            raise ServiceError("auth.identity_missing", "an identified operator must operate")
        mission = self._mission(mission_id)
        if any(d["payload"].get("request_id") == request_id for d in self.ledger.deliveries(mission_id)
               if d["kind"] == "operator_request"):
            return self.view(mission_id)
        live = self._live(mission_id, mission["current_version"])
        if live is None or action not in live["allowed_actions"]:
            raise ServiceError("service.invalid_request", f"{action} is not available now")
        request = OperatorRequest(request_id=request_id, robot_id=mission["robot_id"], mission_id=mission_id,
                                  mission_version=live["version"], lease_epoch=live["lease_epoch"],
                                  step_id=live["step_id"], action=MissionAction(action),
                                  valid_until=self.clock() + OPERATOR_TTL, requested_by=requested_by)
        self.ledger.queue_delivery(mission["robot_id"], "operator_request", mission_id, live["version"], request)
        return self.view(mission_id)

    # ── derived state from forwarded journals / 由转发账本派生的状态 ──

    def _journals(self, mission_id: str, version: int) -> dict[str, list[dict]]:
        rows: dict[str, list[dict]] = {}
        for event in self.ledger.events(mission_id, version):
            rows.setdefault(event["journal"], []).append(event_to_row(event["body"]))
        return rows

    def _live(self, mission_id: str, version: int) -> dict | None:
        rows = self._journals(mission_id, version)
        if not self._intact(rows):
            return None
        executive = rows.get("executive", [])
        lease = next((r["data"] for r in reversed(rows.get("guardian", [])) if r["kind"] == "lease"), None)
        if lease is None or any(r["kind"] == "mission_result" for r in executive):
            return None
        concluded = {r["data"]["outcome"]["step_id"] for r in executive if r["kind"] == "step_outcome"}
        state = None
        for row in executive:
            if row["kind"] == "skill_state":
                state = (row["data"]["step_id"], row["data"]["state"])
        if state is None or state[0] in concluded:
            return None
        allowed = ALLOWED_ACTIONS.get(state[1], ["cancel"])
        if "pause" in allowed and not self._pausable(mission_id, version, state[0]):
            # The guardian refuses to pause a skill its manifest does not declare pausable; do not offer it.
            # guardian 拒绝暂停清单未声明可暂停的技能；不提供该操作。
            allowed = [action for action in allowed if action != "pause"]
        return {"version": version, "lease_epoch": lease["lease_epoch"], "step_id": state[0], "state": state[1],
                "allowed_actions": allowed}

    @staticmethod
    def _intact(journals: dict[str, list[dict]]) -> bool:
        """Both authoritative journals must be contiguous and checked. / 两本权威账本都必须连续且通过检查。"""
        return all(journals.get(name) and verify_chain(journals[name])[0] for name in ("executive", "guardian"))

    def _pausable(self, mission_id: str, version: int, step_id: str) -> bool:
        """The same manifest rule the guardian applies; the aircraft still decides. / 与 guardian 相同的清单规则；仍由机载决定。"""
        record = self.ledger.version(mission_id, version) or {}
        nodes = (record.get("package") or {}).get("nodes", [])
        skill = next((node["skill_id"] for node in nodes if node["task_id"] == step_id), None)
        manifest = self.registry.manifests.get(skill) if skill else None
        return bool(manifest and manifest.pause.pausable)

    def _verifications(self, mission_id: str, version: int, package: MissionPackage,
                       outcomes: dict[str, StepOutcome]) -> dict[tuple[int, str], EffectVerdict]:
        latest: dict[str, tuple[Evidence, dict]] = {}
        for row in self.ledger.evidence(mission_id, version):
            if not row["media_path"]:
                continue
            evidence = Evidence.model_validate(row["body"])
            step = evidence.produced_by_skill_instance
            if step not in latest or evidence.time_window.timestamp > latest[step][0].time_window.timestamp:
                latest[step] = (evidence, row)
        stored = {r["evidence_id"]: r["body"] for r in self.ledger.verifications(mission_id)}
        # Image success needs a positive service recheck, even before metadata arrives. / 影像元数据未到也不能缺省成功。
        verdicts = {(version, node.task_id): EffectVerdict.UNKNOWN for node in package.nodes
                    if "image" in node.completion_evidence}
        for step, (evidence, row) in latest.items():
            outcome = outcomes.get(step)
            result = verify(evidence, self.hub.media(row["media_path"]), row["width"], row["height"], package,
                            self.registry, outcome.effect_verdict if outcome else None)
            body = result.model_dump(mode="json")
            previous = stored.get(evidence.evidence_id)
            if previous is None or {k: v for k, v in previous.items() if k != "verified_at"} != {
                    k: v for k, v in body.items() if k != "verified_at"}:
                self.ledger.record_verification(body)
                if not result.agrees:
                    self.ledger.record_issue(issue(
                        "service.verification_mismatch",
                        f"onboard {result.onboard_verdict} vs service {result.service_verdict.value}",
                        affected=[step], mission_id=mission_id), mission_id=mission_id)
            if outcome is not None:
                verdicts[(version, step)] = result.final_verdict
        return verdicts

    def refresh(self, mission_id: str) -> dict:
        """Recompute derived state from the ledger; idempotent. / 从账本重新计算派生状态；幂等。"""
        self._mission(mission_id)
        for delivery in self.ledger.deliveries(mission_id):
            record = self.ledger.version(mission_id, delivery["version"])
            if delivery["kind"] != "mission_package" or delivery["acked_at"] is None or record["status"] != "queued":
                continue
            if delivery["ack_accepted"]:
                self.ledger.update_version(mission_id, delivery["version"], status="delivered")
            else:
                reason = delivery["ack_reason"] or ""
                self.ledger.update_version(mission_id, delivery["version"], status="delivery_rejected")
                self.ledger.record_issue(issue(reason if reason in ISSUE_CODES else "service.transport_error", reason,
                                               mission_id=mission_id), mission_id=mission_id)
        packages, outcomes, verdicts = {}, {}, {}
        syncing = False
        for record in self.ledger.versions(mission_id):
            if record["status"] not in DELIVERED:
                continue
            version = record["version"]
            journals = self._journals(mission_id, version)
            executive = journals.get("executive", [])
            package = MissionPackage.model_validate(record["package"])
            if record["status"] != "delivery_rejected" and journals and not self._intact(journals):
                # Never make decisions from a partial or corrupt mirror. / 不从部分或损坏的镜像作出决定。
                packages[version] = package
                outcomes[version] = {node.task_id: StepOutcome(
                    mission_id=mission_id, mission_version=version, step_id=node.task_id, robot_id=node.robot_id,
                    execution_status=ExecutionStatus.UNKNOWN, effect_verdict=EffectVerdict.UNKNOWN)
                    for node in package.nodes}
                syncing = True
                continue
            status = record["status"]
            if status in ("queued", "delivered") and any(r["kind"] == "mission_accepted" for r in executive):
                status = "running"
            if status in ("queued", "delivered", "running") and any(r["kind"] == "mission_result" for r in executive):
                status = "finished"
            if status != record["status"]:
                self.ledger.update_version(mission_id, version, status=status)
            if status == "delivery_rejected":
                continue
            packages[version], outcomes[version] = package, outcomes_from_rows(executive)
            verdicts.update(self._verifications(mission_id, version, package, outcomes[version]))
            verified_steps = {row["step_id"] for row in self.ledger.evidence(mission_id, version) if row["media_path"]}
            syncing |= any("image" in node.completion_evidence and node.task_id not in verified_steps
                           and outcomes[version].get(node.task_id) is not None
                           and outcomes[version][node.task_id].counts_as_completed for node in package.nodes)
        report = None
        if packages:
            report = build_report(mission_id, packages, outcomes, verdicts, self.ledger.facts(mission_id))
            previous = self.ledger.report(mission_id)
            body = report.model_dump(mode="json")
            if previous is None or {k: v for k, v in previous.items() if k != "generated_at"} != {
                    k: v for k, v in body.items() if k != "generated_at"}:
                self.ledger.record_report(mission_id, report)
        if syncing:
            self.ledger.update_mission(mission_id, status="verifying")
        else:
            self._advance(self._mission(mission_id), bool(report and report.all_targets_completed), outcomes)
            self._deliver_ready(mission_id)
        return self.view(mission_id)

    def _advance(self, mission: dict, completed: bool, outcomes: dict[int, dict[str, StepOutcome]]) -> None:
        mission_id, version = mission["mission_id"], mission["current_version"]
        record = self.ledger.version(mission_id, version)
        if record is None:
            return
        if record["status"] in ("queued", "delivered", "running", "delivery_rejected"):
            self.ledger.update_mission(mission_id, status=record["status"])
            return
        if record["status"] != "finished":
            return
        if completed:
            if mission["status"] != "completed":
                self.ledger.update_mission(mission_id, status="completed")
            return
        if mission["status"] in ("completed", "incomplete"):
            if mission["status"] == "completed":
                self.ledger.update_mission(mission_id, status="incomplete")
            return
        self._replan(mission, record, outcomes[version])

    def _replan(self, mission: dict, record: dict, outcomes: dict[str, StepOutcome]) -> None:
        """Propose, compile, admit and classify a retry version (D032). / 提议、编译、准入并分类重试版本（D032）。"""
        mission_id, version = mission["mission_id"], record["version"]

        def incomplete(found: list[Issue]) -> None:
            self._issues(found, mission_id)
            self.ledger.update_mission(mission_id, status="incomplete")

        cancelled = sorted(step for step, outcome in outcomes.items()
                           if outcome is not None and outcome.execution_status is ExecutionStatus.CANCELLED)
        if cancelled:
            # Only an operator cancel yields `cancelled`; flying again would override that decision (D035).
            # 只有操作者取消会产生 `cancelled`；再次起飞会推翻这一决定（D035）。
            return incomplete([issue("replan.cancelled_by_operator", ", ".join(cancelled), affected=cancelled)])
        if mission["replans"] >= self.policy.max_replans_per_mission:
            return incomplete([issue("replan.limit_exceeded", f"{mission['replans']} replans used")])
        package = MissionPackage.model_validate(record["package"])
        base_approval = ApprovalRecord.model_validate(record["approval"])
        found = triggers(package, outcomes)
        now = self.clock()
        proposal = propose_retry(MissionSpec.model_validate(record["spec"]), package, found, self.policy,
                                 new_version=version + 1, now=now, not_after_limit=base_approval.expires_at,
                                 window_minutes=int(self.defaults.get("window_minutes", 30)))
        if proposal is None:
            return incomplete([])
        result = self._evaluate(proposal, mission["robot_id"], self._request(mission))
        common = {"origin": "replan", "spec": proposal, "compile": result.compile, "admission": result.admission}
        if result.blocked_at is not None:
            self.ledger.record_version(mission_id, version + 1, status="rejected",
                                       decision={"blocked_at": result.blocked_at, "codes": result.codes,
                                                 "triggers": [t.model_dump() for t in found]}, **common)
            return incomplete(result.issues)
        proposed = result.package
        decision = classify(package, proposed, found, self.policy, replans_so_far=mission["replans"],
                            base_approval=base_approval)
        details = {**decision.model_dump(mode="json"), "triggers": [t.model_dump() for t in found]}
        status = {"auto_approvable": "approving", "requires_human": "awaiting_approval"}.get(
            decision.classification, "rejected")
        self.ledger.record_version(mission_id, version + 1, status=status, package=proposed,
                                   package_hash=proposed.package_hash, decision=details, **common)
        if status == "rejected":
            self.ledger.update_mission(mission_id, current_version=version + 1)
            return incomplete(decision.issues)
        self.ledger.update_mission(mission_id, current_version=version + 1, replans=mission["replans"] + 1,
                                   status=status)
        self._issues(decision.issues, mission_id)
        if status == "approving":
            approval = auto_approval(proposed, decision, self.policy, base_approval=base_approval, now=now)
            self._approved(mission_id, version + 1, proposed, self.key.sign(approval))

    async def enrich(self, mission_id: str) -> int:
        """Optional VLM notes on service-verified images; never changes a verdict. / 对服务复核通过影像的可选 VLM 备注。"""
        if self.vision is None:
            return 0
        provider, model = self.vision
        known = {fact["fact_id"] for fact in self.ledger.facts(mission_id)}
        added = 0
        for verification in self.ledger.verifications(mission_id):
            body = verification["body"]
            if body["final_verdict"] != "verified" or f"vlm:{body['evidence_id']}" in known:
                continue
            row = next(r for r in self.ledger.evidence(mission_id) if r["evidence_id"] == body["evidence_id"])
            evidence = Evidence.model_validate(row["body"])
            fact = await business_judgment(evidence, self.hub.media(row["media_path"]), row["width"], row["height"],
                                           provider, model, asset_id=evidence.subject_ids[0])
            if fact is not None and accept_fact(fact):
                self.ledger.record_fact(mission_id, fact)
                added += 1
        return added

    async def run(self, stop: asyncio.Event, period_s: float = 0.5) -> None:
        """Background refresh of missions touched by ingest. / 后台刷新被入账触及的任务。"""
        while not stop.is_set():
            if self.status_changed:
                self.status_changed = False
                self.dirty.update(m["mission_id"] for m in self.ledger.missions(200) if m["status"] == "approved")
            for mission_id in sorted(self.dirty):
                self.dirty.discard(mission_id)
                try:
                    self.refresh(mission_id)
                    await self.enrich(mission_id)
                except Exception as error:  # keep ingesting; the issue is visible to operators / 继续入账；问题对操作者可见
                    self.ledger.record_issue(issue("service.degraded", f"{type(error).__name__}: {error}"[:300],
                                                   mission_id=mission_id), mission_id=mission_id)
            try:
                await asyncio.wait_for(stop.wait(), period_s)
            except TimeoutError:
                pass

    # ── views / 视图 ──

    def view(self, mission_id: str) -> dict:
        """Everything the console shows for one mission. / 控制台为一个任务展示的全部内容。"""
        mission = self._mission(mission_id)
        request = self.ledger.request(mission["request_id"])["body"]
        versions, previous = [], None
        for record in self.ledger.versions(mission_id):
            planner = record["planner"] or {}
            admission = record["admission"] or {}
            approval = record["approval"] or {}
            journals = self._journals(mission_id, record["version"])
            versions.append({
                "version": record["version"], "status": record["status"], "origin": record["origin"],
                "created_at": record["created_at"], "package_hash": record["package_hash"], "spec": record["spec"],
                "package": record["package"],
                "diff": package_diff(previous, record["package"]) if record["package"] else None,
                "planner": {k: planner.get(k) for k in ("status", "provider_id", "model_id", "prompt_version",
                                                        "input_hash", "decline_reason", "notes")}
                | {"attempts": len(planner.get("attempts", [])),
                   "channels": [a["channel"] for a in planner.get("attempts", [])]} if planner else None,
                "admission": {"accepted": admission.get("accepted"),
                              "codes": [i["code"] for i in admission.get("issues", [])],
                              "energy_upper_fraction": admission.get("energy_upper_fraction"),
                              "checks": admission.get("checks", [])} if admission else None,
                "approval": {k: approval.get(k) for k in ("approver", "approved_at", "expires_at", "signer_key_id")}
                if approval else None,
                "decision": record["decision"],
                "journals": {name: {"rows": len(rows), "chain": verify_chain(rows)[1] or "ok"}
                             for name, rows in journals.items()},
                "events": [{"journal": name, "seq": r["seq"], "kind": r["kind"], "timestamp": r["timestamp"],
                            "data": r["data"]} for name, rows in journals.items() for r in rows
                           if r["kind"] in ("mission_accepted", "skill_state", "step_outcome", "command_rejected",
                                            "operator_request", "operator_rejected", "safety_intervention",
                                            "recovered_to", "mission_result", "package_verified")],
            })
            if record["package"]:
                previous = record["package"]
        live = self._live(mission_id, mission["current_version"])
        verifications = {v["evidence_id"]: v["body"] for v in self.ledger.verifications(mission_id)}
        evidence = [{"evidence_id": r["evidence_id"], "version": r["mission_version"], "step_id": r["step_id"],
                     "sha256": r["sha256"], "media": bool(r["media_path"]), "width": r["width"], "height": r["height"],
                     "captured_at": r["body"]["time_window"]["timestamp"],
                     "verification": verifications.get(r["evidence_id"])} for r in self.ledger.evidence(mission_id)]
        operations = [{"request_id": d["payload"]["request_id"], "action": d["payload"]["action"],
                       "requested_by": d["payload"]["requested_by"], "version": d["version"],
                       "acked": d["acked_at"] is not None, "accepted": d["ack_accepted"], "reason": d["ack_reason"]}
                      for d in self.ledger.deliveries(mission_id) if d["kind"] == "operator_request"]
        return {"mission": mission, "request": {k: request.get(k) for k in (
                    "request_id", "text", "requested_by", "channel", "approved_volume_id", "asset_ids", "received_at")},
                "versions": versions, "live": live, "evidence": evidence, "operations": operations,
                "report": self.ledger.report(mission_id), "issues": self.ledger.issues(mission_id),
                "facts": self.ledger.facts(mission_id)}

    def summary(self, mission_id: str) -> dict:
        """The read-only view an external agent may receive (D033). / 外部 agent 可以得到的只读视图（D033）。"""
        mission = self._mission(mission_id)
        report = self.ledger.report(mission_id)
        return {"mission_id": mission_id, "status": mission["status"], "current_version": mission["current_version"],
                "updated_at": mission["updated_at"],
                "report": {k: report[k] for k in ("targets", "summary", "all_targets_completed", "versions")}
                if report else None,
                "issues": [i["code"] for i in self.ledger.issues(mission_id)]}
