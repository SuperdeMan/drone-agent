"""Mission service: request -> plan -> compile/admit -> approve and sign -> deliver -> ingest -> verify -> report.

The service is the only place that calls a model (D029) and the only holder of the approval signing key
(D030). It never talks to a guardian: packages and operator requests are queued as deliveries that the
robot's uplink pulls over mTLS (D031), and everything it knows about a flight comes from the forwarded,
hash-chained journal rows. Derived state (step outcomes, verifications, the three-column report, replans)
is recomputed from those rows by `refresh`, so a service restart or a late sync converges to the same
result. A replanned version is approved automatically only when it is a verbatim retry (D032) and is
delivered only after the previous version finished and the robot reported itself grounded.

With an operations catalog (P1, D055) every new mission is bound to a project, site, dock and robot in the same
transaction as the mission row; each call checks the caller's project role; approval takes the activity's
reservation; the robot's pull passes the claim gate; and reconciliation alone releases resources. Without a
catalog the service behaves exactly as in M2. A P2 workflow activity submits through `submit_workflow`: a narrow draft
built from the template, the same compilation, admission and soft hold, and one transaction for the whole mission,
keyed by the activity so that a retry after a lost response finds the same mission (D057).

任务服务：请求 → 规划 → 编译 / 准入 → 审批并签名 → 投递 → 入账 → 复核 → 报告。

服务是唯一调用模型的地方（D029），也是审批签名密钥的唯一持有者（D030）。它从不与 guardian 通信：任务包
与操作请求作为投递排队，由机器人的 uplink 经 mTLS 拉取（D031）；它对一次飞行的全部了解都来自转发的
哈希链账本行。派生状态（步骤结果、复核、三列报告、重规划）由 `refresh` 从这些行重新计算，因此服务
重启或迟到的同步都收敛到同一结果。重规划版本只有在原样重试时才自动批准（D032），并且只在上一版本
结束、机器人报告已在地面之后才投递。

带运营目录时（P1，D055），每个新任务与任务行在同一事务中绑定到项目、站点、机场与机器人；每次调用检查调用方的
项目角色；审批获取活动预约；机器人拉取须经领取闸门；只有对账才能释放资源。不带目录时服务行为与 M2 完全相同。P2 工作流
活动经 `submit_workflow` 提交：由模板生成的窄草案、相同的编译、准入与软预约，整个任务在一个事务中写入，并以活动为键，
使响应丢失后的重试找到同一任务（D057）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.admission.admission import AdmissionContext
from drone_agent.admission.airspace import SimulatedAirspaceProvider
from drone_agent.admission.compiler import FRAMEWORK, CompileContext
from drone_agent.admission.models import MissionRequest, RequestChannel
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
    Provenance,
    StepOutcome,
    utcnow,
)
from drone_agent.fleet.catalog import Catalog
from drone_agent.fleet.coordinator import PassthroughCoordinator
from drone_agent.fleet.dispatch import Dispatch, Operations
from drone_agent.fleet.events import event_to_row, outcomes_from_rows, verify_chain
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import (
    NAMESPACE,
    AnalysisOrigin,
    EvidenceOrigin,
    ModelUse,
    SourceContext,
    digest,
    export,
    planning_use,
    project,
    provider_source,
    seal,
    source_context,
)
from drone_agent.fleet.report import build_report
from drone_agent.fleet.resources import Stage
from drone_agent.fleet.transport import FleetHub
from drone_agent.fleet.verifier import VLM_QUESTION, accept_fact, business_judgment, verify
from drone_agent.mission.registry import Registry
from drone_agent.planner.draft import draft_schema, draft_to_spec, validate_draft
from drone_agent.planner.replan import (
    ApprovalPolicy,
    ReplanTrigger,
    auto_approval,
    classify,
    propose_retry,
    triggers,
)
from drone_agent.runtime.issues import ISSUE_CODES, Issue, Severity, issue
from drone_agent.runtime.permission import (
    MISSION_APPROVE,
    MISSION_OPERATE,
    MISSION_READ,
    MISSION_SUBMIT,
    RESOURCE_MAINTAIN,
    RESOURCE_READ,
    Caller,
    TrustLevel,
)
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
                 clock=utcnow, vision=None, provenance_context: SourceContext | None = None,
                 operations: Operations | None = None):
        self.registry = Registry(root, scene=scene)
        self.ledger, self.hub, self.key, self.policy = ledger, hub, signing_key, approval_policy
        self.planner, self.robot_id, self.clock, self.vision = planner, robot_id, clock, vision
        self.airspace = airspace or SimulatedAirspaceProvider()
        self.catalog = Catalog(ledger, static=self.registry.capability)
        self.coordinator = PassthroughCoordinator(robot_id, self.catalog)
        self.defaults = self.registry.data.get("mission_defaults", {})
        self.source = provenance_context or source_context(root, scene, self.registry.sha256)
        self.ops, self.dispatch = operations, None
        self.sources: dict[str, SourceContext] = {}
        if operations is not None:
            import hashlib

            self.catalog = Catalog(ledger, static=self.registry.capability, statics=operations.capabilities)
            self.dispatch = Dispatch(operations, ledger, self.catalog, clock=clock, terminal=self._terminal)
            hub.claim_gate = self._gate
            # A bound mission's source names its own site map. / 已绑定任务的来源记录其站点地图。
            self.sources = {site_id: self.source.model_copy(update={
                "scenario_sha256": hashlib.sha256((root / site.scene).read_bytes()).hexdigest(),
                "registry_sha256": operations.registries[site_id].sha256})
                for site_id, site in operations.catalog.sites.items()}
        # Reconcile persisted input even when the uplink already acknowledged every item before a restart.
        # 重启后复核持久化输入，即使 uplink 已在重启前确认了每条上传。
        self.dirty: set[str] = {m["mission_id"] for m in ledger.missions(-1) if m["current_version"] > 0}
        self.status_changed = False
        # P2 (D057): the workflow engine and its draft planner, attached by the process entry when a workflow
        # catalog is loaded. / P2（D057）：工作流引擎及其草案规划器；加载了工作流目录时由进程入口挂上。
        self.workflows = None
        self.workflow_planner = None
        # P3 (D059): the scheduler, attached by the process entry when a scheduling catalog is loaded.
        # P3（D059）：调度器；加载了调度目录时由进程入口挂上。
        self.scheduler = None
        hub.listeners.append(self._ingested)

    # ── P1 bindings and project checks / P1 绑定与项目检查 ──

    def _binding(self, mission_id: str) -> dict | None:
        return self.ops.store.binding(mission_id) if self.ops is not None else None

    def _robot_of(self, mission_id: str) -> str:
        binding = self._binding(mission_id)
        return binding["robot_id"] if binding else self.robot_id

    def _registry(self, robot_id: str) -> Registry:
        if self.ops is not None and robot_id in self.ops.catalog.robots:
            return self.ops.registry(robot_id)
        return self.registry

    def _coordinator(self, robot_id: str) -> PassthroughCoordinator:
        return self.coordinator if robot_id == self.robot_id and self.ops is None else \
            PassthroughCoordinator(robot_id, self.catalog)

    def project_of(self, mission_id: str) -> str | None:
        """The mission's project; missions without a binding belong to the legacy project. / 无绑定的任务属于 legacy 项目。"""
        if self.ops is None:
            return None
        binding = self._binding(mission_id)
        return binding["project_id"] if binding else self.ops.catalog.legacy_project.project_id

    def _check(self, caller: Caller | None, mission_id: str, scope: str) -> None:
        """Catalog mode: an unreadable mission looks absent; a readable one needs the scope's role.

        目录模式：不可读的任务如同不存在；可读的任务还需要相应 scope 的角色。
        """
        if self.ops is None:
            return
        if caller is None:
            raise ServiceError("auth.identity_missing", "catalog mode needs an authenticated caller")
        self._mission(mission_id)
        project = self.project_of(mission_id)
        if not self.ops.directory.allows(caller, project, MISSION_READ):
            raise ServiceError("service.not_found", mission_id)
        if scope != MISSION_READ and not self.ops.directory.allows(caller, project, scope):
            raise ServiceError("auth.project_denied", f"{scope} in {project}")

    def _gate(self, robot_id: str, delivery: dict) -> bool:
        handed = self.dispatch.gate(robot_id, delivery)
        self.dirty.update(self.dispatch.touched)
        return handed

    def _terminal(self, mission_id: str, version: int) -> tuple[str, datetime] | None:
        """The authoritative end of one activity: the robot's rejection or the mission result. / 活动的权威结束。"""
        for delivery in self.ledger.deliveries(mission_id):
            if delivery["kind"] == "mission_package" and delivery["version"] == version and delivery["acked_at"] \
                    and not delivery["ack_accepted"]:
                return "delivery_rejected", datetime.fromisoformat(delivery["acked_at"])
        finished = self._finished_at(mission_id, version)
        return ("mission_result", finished) if finished else None

    def _ingested(self, robot_id: str, kind: str, mission_id: str | None) -> None:
        if mission_id:
            self.dirty.add(mission_id)
        if kind == "status":
            self.status_changed = True

    def _issues(self, found: list[Issue], mission_id: str, request_id: str = "") -> None:
        for item in found:
            self.ledger.record_issue(item, mission_id=mission_id, request_id=request_id or None)

    def _record_version(self, mission_id: str, version: int, *, status: str, origin: str, **records) -> None:
        """Persist the version and its trusted source in the same database write.

        在同一次数据库写入中保存版本及其受信来源。
        """
        planning = records.pop("planning", None) or planning_use(self.planner, records.get("planner"))
        if origin == "replan":
            spec = records.get("spec")
            planning = ModelUse(source="deterministic", provider_id="runtime", model_id="bounded_retry",
                                input_sha256=digest(spec.model_dump(mode="json")) if spec else "", outcome=status)
        decision = dict(records.pop("decision", None) or {})
        binding = self._binding(mission_id)
        source = self.sources.get(binding["site_id"], self.source) if binding else self.source
        decision[NAMESPACE] = {"run": seal(source.header(mission_id, version, planning))}
        self.ledger.record_version(mission_id, version, status=status, origin=origin, decision=decision, **records)

    def provenance(self, mission_id: str) -> list[dict]:
        """Saved sources only; upgrades cannot relabel historical runs. / 只读已保存来源；升级不能重标历史运行。"""
        return [export(record) for record in self.ledger.versions(mission_id)]

    def _evidence_sources(self, mission_id: str, version: int) -> None:
        """Bind each acquisition to the saved deployment, never the robot's self-declared label.

        每份采集绑定已保存部署，不采信机器人自报来源标签。
        """
        record = self.ledger.version(mission_id, version)
        run = project(record).run
        if run is None:
            return
        saved = (record["decision"] or {}).get(NAMESPACE, {})
        for row in self.ledger.evidence(mission_id, version):
            key = f"evidence:{row['evidence_id']}"
            if key not in saved:
                self.ledger.record_source(mission_id, version, key, EvidenceOrigin(
                    evidence_id=row["evidence_id"], media_sha256=row["sha256"],
                    source=run.default_image_source, run_id=run.run_id))

    # ── submit and plan / 提交与规划 ──

    async def submit(self, request: MissionRequest, caller: Caller | None = None) -> dict:
        """Plan, compile and admit a request; a repeated idempotency key returns the first mission.

        In catalog mode the unscoped request goes to the catalog's default binding, membership checked (D055).

        规划、编译并准入一个请求；重复的幂等键返回第一次的任务。目录模式下未带项目的请求进入目录默认绑定，
        仍检查成员资格（D055）。
        """
        if self.ops is not None:
            default = self.ops.catalog.default_binding
            if default is None:
                raise ServiceError("auth.project_denied", "this deployment needs an explicit project and robot")
            return await self.submit_bound(request, project_id=default.project_id, robot_id=default.robot_id,
                                           caller=caller)
        mission_id, created = self.ledger.record_request(request, "m-" + uuid.uuid4().hex[:12])
        if not created:
            return self._view(mission_id)
        operation = self.ledger.begin_operation(mission_id, "plan", request.idempotency_key)
        try:
            await self._plan(mission_id, request)
        finally:
            self.ledger.finish_operation(operation, "done")
        return self._view(mission_id)

    async def submit_bound(self, request: MissionRequest, *, project_id: str, robot_id: str,
                           caller: Caller | None) -> dict:
        """Submit into a project for one fixed robot; the binding is written with the mission (D055).

        向项目内一台固定机器人提交；绑定与任务一起写入（D055）。
        """
        if self.ops is None:
            raise ServiceError("service.invalid_request", "no operations catalog is configured")
        catalog, directory = self.ops.catalog, self.ops.directory
        if caller is None or project_id not in catalog.projects or \
                not directory.allows(caller, project_id, MISSION_READ):
            raise ServiceError("service.not_found", project_id)
        if not directory.allows(caller, project_id, MISSION_SUBMIT):
            raise ServiceError("auth.project_denied", f"{MISSION_SUBMIT} in {project_id}")
        if catalog.project_of(robot_id) != project_id:
            raise ServiceError("service.not_found", robot_id)
        if catalog.robots[robot_id].execution_backend != self.source.execution_backend:
            raise ServiceError("dispatch.backend_mismatch",
                               f"{robot_id} runs {catalog.robots[robot_id].execution_backend}")
        mission_id = "m-" + uuid.uuid4().hex[:12]
        mission_id, created = self.ledger.record_request(
            request, mission_id, also=self.ops.store.binding_writer(mission_id, project_id=project_id,
                                                                    robot_id=robot_id))
        if not created:
            binding = self._binding(mission_id)
            if binding is None or (binding["project_id"], binding["robot_id"]) != (project_id, robot_id):
                raise ServiceError("service.idempotency_conflict", "the key was used for another project or robot")
            return self._view(mission_id)
        operation = self.ledger.begin_operation(mission_id, "plan", request.idempotency_key)
        try:
            await self._plan(mission_id, request)
        finally:
            self.ledger.finish_operation(operation, "done")
        return self._view(mission_id)

    async def _plan(self, mission_id: str, request: MissionRequest) -> None:
        def stop(status: str, found: list[Issue], **records) -> None:
            self._record_version(mission_id, 1, status=status, origin=request.channel.value, **records)
            self.ledger.update_mission(mission_id, status=status, current_version=1)
            self._issues(found, mission_id, request.request_id)

        robot_id = self._robot_of(mission_id)
        if request.approved_volume_id not in self._registry(robot_id).data.get("volumes", {}):
            # No model call for a scope the registry does not know. / 登记表不认识的范围不调用模型。
            return stop("rejected", [issue("scope.unregistered_volume", request.approved_volume_id)])
        if self.planner is None:
            return stop("planning_failed", [issue("planner.technical_failure", "no planner is configured")])
        outcome = await self.planner.plan(request, mission_id=mission_id, mission_version=1)
        if outcome.status != "planned":
            return stop("refused" if outcome.status == "refused" else "planning_failed", outcome.issues,
                        planner=outcome)
        self._admit(mission_id, request, outcome.spec, planner=outcome)

    def _admit(self, mission_id: str, request: MissionRequest, spec: MissionSpec, *, planner=None,
               planning: ModelUse | None = None, strict_hold: bool = False) -> None:
        """Assign, compile and admit a planned v1, then soft-hold the resources while a human decides.

        With `strict_hold` (a P3 assignment, D059) a conflicting hold raises so the caller's transaction rolls back.

        分配、编译并准入已规划的 v1，然后在人工决定期间软预约资源。`strict_hold`（P3 分配，D059）时持有冲突即抛出，
        使调用方的事务回滚。
        """
        robot_id = self._robot_of(mission_id)
        coordinator = self._coordinator(robot_id)
        robot, found = coordinator.assign(spec)
        if robot is None:
            self._record_version(mission_id, 1, status="rejected", origin=request.channel.value, spec=spec,
                                 planner=planner, planning=planning)
            self.ledger.update_mission(mission_id, status="rejected", current_version=1)
            self._issues(found, mission_id, request.request_id)
            return
        result = self._evaluate(spec, robot, request)
        decision = {"blocked_at": result.blocked_at, "codes": result.codes,
                    "collaboration": coordinator.collaboration(spec)}
        package = result.compile.package if result.compile else None
        status = "awaiting_approval" if result.blocked_at is None else "rejected"
        self._record_version(mission_id, 1, status=status, origin=request.channel.value, spec=spec,
                             planner=planner, planning=planning, compile=result.compile, admission=result.admission,
                             package=package, package_hash=package.package_hash if package else None,
                             decision=decision)
        self.ledger.update_mission(mission_id, status=status, robot_id=robot, current_version=1)
        self._issues(result.issues, mission_id, request.request_id)
        if status == "awaiting_approval" and self._binding(mission_id) is not None:
            # A soft hold while the human decides; approval refreshes or retakes it (D055). / 人工决定期间的软预约。
            _, conflicts = self.dispatch.reserve(mission_id, 1)
            if conflicts and strict_hold:
                raise ServiceError("dispatch.reservation_conflict",
                                   "held by " + ", ".join(sorted(set(conflicts.values()))))
            if conflicts:
                self.ledger.record_issue(issue("dispatch.reservation_conflict",
                                               "held by " + ", ".join(sorted(set(conflicts.values()))),
                                               severity=Severity.WARNING, mission_id=mission_id),
                                         mission_id=mission_id)
            self.dispatch.preview(mission_id, 1)

    def submit_workflow(self, *, run_id: str, key: str, project_id: str, robot_id: str, volume_id: str, asset_id: str,
                        template: str, guard) -> dict | None:
        """Submit one workflow activity deterministically; None when `guard` refuses inside the transaction (D057).

        The template contributes a narrow single-asset draft; ids, window, budget and policy come from the site map as
        for a planned request, and compilation, admission and the soft hold are the same. The request, mission,
        binding, version and hold are written in one transaction whose first step is `guard` (the outbox claim is
        still ours and no cancel committed since), so a cancel that committed first leaves nothing behind. The
        activity key is the idempotency key: a retry after a lost response returns the first mission. The version
        still needs a human approval; nothing here signs or queues a package.

        确定性地提交一个工作流活动；`guard` 在事务内拒绝时返回 None（D057）。模板只提供一个窄的单资产草案；ID、时间窗、
        预算与策略与规划请求一样来自站点地图，编译、准入与软预约相同。请求、任务、绑定、版本与预约在一个事务中写入，
        事务的第一步是 `guard`（outbox 领取仍属于我方，且自那以后没有提交取消），因此先提交的取消不会留下任何东西。活动键
        即幂等键：响应丢失后的重试返回第一次的任务。该版本仍需人工审批；这里不签名也不排队任何任务包。
        """
        return self._submit_deterministic(principal=f"workflow:{run_id}", key=key, project_id=project_id,
                                          robot_id=robot_id, volume_id=volume_id, asset_id=asset_id, template=template,
                                          provider="workflow", channel=RequestChannel.WORKFLOW, guard=guard)

    def submit_assigned(self, *, principal: str, key: str, project_id: str, robot_id: str, volume_id: str,
                        asset_id: str, template: str, guard) -> dict | None:
        """The mission of one P3 assignment, inside the scheduler's transaction (D059).

        Same deterministic draft, compilation, admission and soft hold as a workflow activity, keyed by the task and
        its assignment epoch; the hold is strict: a conflict raises, so the whole assignment rolls back. The version
        still needs a human approval.

        一次 P3 分配的任务，在调度器的事务内写入（D059）。与工作流活动相同的确定性草案、编译、准入与软预约，以任务及其分配
        代次为键；预约是严格的：冲突即抛出，整个分配回滚。该版本仍需人工审批。
        """
        return self._submit_deterministic(principal=principal, key=key, project_id=project_id, robot_id=robot_id,
                                          volume_id=volume_id, asset_id=asset_id, template=template,
                                          provider="scheduler", channel=RequestChannel.SCHEDULER, guard=guard,
                                          strict_hold=True)

    def _submit_deterministic(self, *, principal: str, key: str, project_id: str, robot_id: str, volume_id: str,
                              asset_id: str, template: str, provider: str, channel: RequestChannel, guard,
                              strict_hold: bool = False) -> dict | None:
        if self.ops is None:
            raise ServiceError("service.invalid_request", "no operations catalog is configured")
        known = self.ledger.request_by_key(principal, key)
        if known is not None:
            return self._view(known)
        catalog = self.ops.catalog
        if catalog.project_of(robot_id) != project_id:
            raise ServiceError("service.not_found", robot_id)
        if catalog.robots[robot_id].execution_backend != self.source.execution_backend:
            raise ServiceError("dispatch.backend_mismatch",
                               f"{robot_id} runs {catalog.robots[robot_id].execution_backend}")
        registry = self._registry(robot_id)
        data = registry.data
        draft = {"decision": "plan", "decline_reason": "", "goal": f"Inspect {asset_id}", "goal_type": "inspect",
                 "approved_volume_id": volume_id,
                 "tasks": [{"task_id": f"inspect_{asset_id}", "skill_id": "skill.inspect.asset", "asset_id": asset_id}],
                 "notes": f"{provider} activity {key}"}
        errors = validate_draft(draft, draft_schema(list(data.get("volumes", {})), list(data["assets"]),
                                                    data.get("mission_defaults", {}).get("planner_skills", [])))
        if errors:
            raise ServiceError("workflow.invalid_draft", "; ".join(errors)[:300])
        now = self.clock()
        request = MissionRequest(request_id="req-" + uuid.uuid4().hex[:16],
                                 text=f"{provider.capitalize()} {template}: inspect {asset_id} in {volume_id}",
                                 requested_by=principal, trust_level=TrustLevel.FIRST_PARTY, channel=channel,
                                 approved_volume_id=volume_id, asset_ids=[asset_id], idempotency_key=key,
                                 received_at=now)
        planning = ModelUse(source="deterministic", provider_id=provider, model_id=template,
                            input_sha256=digest(draft), outcome="planned")
        mission_id = "m-" + uuid.uuid4().hex[:12]
        with self.ledger.transaction() as db:
            if not guard(db):
                return None
            mission_id, created = self.ledger.record_request(
                request, mission_id, also=self.ops.store.binding_writer(mission_id, project_id=project_id,
                                                                        robot_id=robot_id))
            if created:
                spec = draft_to_spec(draft, request, registry, mission_id=mission_id, mission_version=1,
                                     provenance=Provenance(model_id=f"{provider}/{template}",
                                                           prompt_version=f"{provider}-template-v1",
                                                           input_hash=planning.input_sha256, generated_at=now),
                                     now=now)
                self._admit(mission_id, request, spec, planning=planning, strict_hold=strict_hold)
        return self._view(mission_id)

    def _evaluate(self, spec: MissionSpec, robot: str, request: MissionRequest):
        registry = self._registry(robot)
        return evaluate(spec, CompileContext(registry=registry, robot_id=robot),
                        AdmissionContext(registry=registry, capability=self.catalog.capability(robot)[0],
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

    def approve(self, mission_id: str, version: int, *, approver: str, package_hash: str,
                caller: Caller | None = None) -> dict:
        """Human approval bound to the package hash the approver saw; signs and queues the package.

        In catalog mode the approver needs the project's approver role and the activity's reservation (D055).

        绑定审批人所见任务包哈希的人工审批；签名并排队投递。目录模式下审批人需要项目 approver 角色，并须取得
        活动预约（D055）。
        """
        self._check(caller, mission_id, MISSION_APPROVE)
        if not approver or approver.startswith(("a2a:", "policy:")):
            raise ServiceError("approval.identity_missing", "an identified operator must approve")
        mission, record = self._awaiting(mission_id, version)
        bound = self._binding(mission_id) is not None
        if bound and self.ops.store.cancel_intent(mission_id) is not None:
            raise ServiceError("dispatch.cancelled", "the mission was cancelled")
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
        if bound:
            reservation, conflicts = self.dispatch.reserve(mission_id, version)
            if reservation is None:
                raise ServiceError("dispatch.reservation_conflict",
                                   "held by " + ", ".join(sorted(set(conflicts.values()))))
        now = self.clock()
        approval = ApprovalRecord(
            approver=approver, approved_at=now, mission_id=mission_id, mission_version=version,
            package_hash=package_hash, allowed_robots=[mission["robot_id"]],
            expires_at=min(now + timedelta(minutes=self.policy.approval_ttl_minutes), package.temporal_window.not_after))
        self._approved(mission_id, version, package, self.key.sign(approval))
        return self._view(mission_id)

    def decline(self, mission_id: str, version: int, *, approver: str, reason: str = "",
                caller: Caller | None = None) -> dict:
        self._check(caller, mission_id, MISSION_APPROVE)
        if not approver or approver.startswith(("a2a:", "policy:")):
            raise ServiceError("approval.identity_missing", "an identified operator must decline")
        _, record = self._awaiting(mission_id, version)
        found = (record.get("decision") or {}).get("triggers") or []
        # Declining a re-run proposed only by candidate events leaves the completed mission completed (D044).
        # 拒绝仅由候选事件提议的重做时，已完成的任务仍为已完成（D044）。
        restored = bool(found) and all(t.get("kind") == "candidate_event" for t in found)
        self.ledger.update_version(mission_id, version, status="declined",
                                   decision={"declined_by": approver, "reason": reason[:300], "triggers": found})
        self.ledger.update_mission(mission_id, status="completed" if restored else "declined")
        if self._binding(mission_id) is not None:
            self.dispatch.release_unclaimed(mission_id, version, "declined")
        return self._view(mission_id)

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
        if self._binding(mission_id) is not None:
            # Queue only with the activity's hold; the claim gate still decides the hand-out (D055).
            # 只在持有活动预约时排队；是否交付仍由领取闸门决定（D055）。
            if self.ops.store.cancel_intent(mission_id) is not None:
                return False
            reservation, _ = self.dispatch.reserve(mission_id, version)
            if reservation is None:
                return False
        self.ledger.queue_delivery(mission["robot_id"], "mission_package", mission_id, version, record["package"])
        self.ledger.update_version(mission_id, version, status="queued")
        self.ledger.update_mission(mission_id, status="queued")
        return True

    # ── operator requests / 操作请求 ──

    def operate(self, mission_id: str, action: str, *, requested_by: str, request_id: str,
                caller: Caller | None = None) -> dict:
        """Queue a pause/resume/cancel bound to the running step the service last saw.

        For a bound mission a cancel is first persisted as the mission's cancel intent: an unclaimed delivery is voided,
        a claimed one is cancelled as soon as the flight has a running step, and no later version flies (D055).

        排队一个绑定到服务最近所见运行步骤的暂停 / 恢复 / 取消请求。对已绑定任务，取消先持久化为任务的取消意图：
        未领取的投递作废，已领取的在飞行出现运行步骤后立即取消，之后任何版本都不再起飞（D055）。
        """
        self._check(caller, mission_id, MISSION_OPERATE)
        if not requested_by or requested_by.startswith(("a2a:", "policy:")):
            raise ServiceError("auth.identity_missing", "an identified operator must operate")
        mission = self._mission(mission_id)
        if any(d["payload"].get("request_id") == request_id for d in self.ledger.deliveries(mission_id)
               if d["kind"] == "operator_request"):
            return self._view(mission_id)
        bound = self._binding(mission_id) is not None
        if bound and action == "cancel":
            if mission["status"] in ("completed", "incomplete", "declined", "rejected", "refused", "planning_failed",
                                     "delivery_rejected", "cancelled", "dispatch_expired"):
                raise ServiceError("service.invalid_request", f"{mission['status']} missions cannot be cancelled")
            self.ops.store.record_cancel(mission_id, requested_by=requested_by, request_id=request_id,
                                         reason="operator")
        live = self._live(mission_id, mission["current_version"])
        if live is None or action not in live["allowed_actions"]:
            if bound and action == "cancel":
                # Persisted; the claim gate voids unclaimed work and the relay cancels claimed work (D055).
                # 已持久化；领取闸门作废未领取工作，转发机制取消已领取工作（D055）。
                self.dispatch.prepare()
                self.refresh(mission_id)
                return self._view(mission_id)
            raise ServiceError("service.invalid_request", f"{action} is not available now")
        request = OperatorRequest(request_id=request_id, robot_id=mission["robot_id"], mission_id=mission_id,
                                  mission_version=live["version"], lease_epoch=live["lease_epoch"],
                                  step_id=live["step_id"], action=MissionAction(action),
                                  valid_until=self.clock() + OPERATOR_TTL, requested_by=requested_by)
        self.ledger.queue_delivery(mission["robot_id"], "operator_request", mission_id, live["version"], request)
        if bound and action == "cancel":
            self.ops.store.mark_cancel_relayed(mission_id, request_id)
        return self._view(mission_id)

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
        # The mission's own site map: an asset exists only where it is registered (P3, D059).
        # 任务自己的站点地图：资产只在其登记处存在（P3，D059）。
        registry = self._registry(self._robot_of(mission_id))
        for step, (evidence, row) in latest.items():
            outcome = outcomes.get(step)
            result = verify(evidence, self.hub.media(row["media_path"]), row["width"], row["height"], package,
                            registry, outcome.effect_verdict if outcome else None)
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

    def _dispatch_state(self, mission_id: str) -> None:
        """Bound missions: voided deliveries end their version; a cancel intent ends or cancels the mission.

        已绑定任务：作废的投递结束其版本；取消意图结束或取消任务。
        """
        store = self.ops.store
        codes = {"cancelled": "dispatch.cancelled", "approval_expired": "dispatch.approval_expired",
                 "backend_mismatch": "dispatch.backend_mismatch",
                 "assignment_superseded": "dispatch.assignment_superseded"}
        expired = False
        for claim in store.claims(mission_id):
            if claim["state"] != "void":
                continue
            record = self.ledger.version(mission_id, claim["mission_version"])
            expired |= claim["reason"] != "cancelled"
            if record is not None and record["status"] in ("approved", "queued"):
                self.ledger.update_version(mission_id, claim["mission_version"], status="withdrawn")
                self.ledger.record_issue(issue(codes.get(claim["reason"], "dispatch.blocked"),
                                               f"v{claim['mission_version']} was never handed out: {claim['reason']}",
                                               mission_id=mission_id), mission_id=mission_id)
        mission = self._mission(mission_id)
        record = self.ledger.version(mission_id, mission["current_version"])
        intent = store.cancel_intent(mission_id)
        if intent is None:
            if expired and record is not None and record["status"] == "withdrawn" and \
                    mission["status"] != "dispatch_expired":
                self.ledger.update_mission(mission_id, status="dispatch_expired")
            return
        if record is not None and record["status"] in ("awaiting_approval", "approving", "approved"):
            self.ledger.update_version(mission_id, mission["current_version"], status="withdrawn")
            self.dispatch.release_unclaimed(mission_id, mission["current_version"], "cancelled")
            record = self.ledger.version(mission_id, mission["current_version"])
        if record is not None and record["status"] == "withdrawn" and mission["status"] != "cancelled":
            self.ledger.update_mission(mission_id, status="cancelled")
            return
        if intent["relayed_request_id"] is None:
            live = self._live(mission_id, mission["current_version"])
            if live is not None and "cancel" in live["allowed_actions"]:
                # The operator's persisted cancel reaches a flight that started after it was recorded.
                # 操作者已持久化的取消送达记录之后才开始的飞行。
                relayed = f"{intent['request_id']}-relay"[:80]
                request = OperatorRequest(request_id=relayed, robot_id=mission["robot_id"], mission_id=mission_id,
                                          mission_version=live["version"], lease_epoch=live["lease_epoch"],
                                          step_id=live["step_id"], action=MissionAction.CANCEL,
                                          valid_until=self.clock() + OPERATOR_TTL,
                                          requested_by=intent["requested_by"])
                self.ledger.queue_delivery(mission["robot_id"], "operator_request", mission_id, live["version"],
                                           request)
                store.mark_cancel_relayed(mission_id, relayed)
                store.event(f"mission:{mission_id}", "cancel.relayed", "service", {"request_id": relayed})

    def refresh(self, mission_id: str) -> dict:
        """Recompute derived state from the ledger; idempotent. / 从账本重新计算派生状态；幂等。"""
        self._mission(mission_id)
        if self._binding(mission_id) is not None:
            self._dispatch_state(mission_id)
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
            self._evidence_sources(mission_id, version)
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
            report = build_report(mission_id, packages, outcomes, verdicts, self.ledger.facts(mission_id),
                                  provenance=self.provenance(mission_id))
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
        return self._view(mission_id)

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
            candidates = self._candidate_events(mission_id, version, record)
            if candidates and mission["status"] not in ("completed", "incomplete"):
                # Every target is done, but an onboard model raised a candidate event: a human decides (D044).
                # 全部目标已完成，但机载模型发起了候选事件：由人决定（D044）。
                return self._replan(mission, record, outcomes[version], extra=candidates)
            if mission["status"] != "completed":
                self.ledger.update_mission(mission_id, status="completed")
            return
        if mission["status"] in ("completed", "incomplete"):
            if mission["status"] == "completed":
                self.ledger.update_mission(mission_id, status="incomplete")
            return
        self._replan(mission, record, outcomes[version])

    def _candidate_events(self, mission_id: str, version: int, record: dict) -> list[ReplanTrigger]:
        """Content steps an onboard model flagged with a replan trigger, once each (D044).

        机载模型以重规划触发标记的内容步骤，每步一次（D044）。
        """
        package = MissionPackage.model_validate(record["package"])
        content = {node.task_id for node in package.nodes if node.skill_id not in FRAMEWORK}
        found: dict[str, ReplanTrigger] = {}
        for row in self._journals(mission_id, version).get("executive", []):
            data = row["data"]
            step = data.get("step_id")
            if row["kind"] == "belief_fact" and data.get("replan_trigger") and step in content and step not in found:
                value = (data.get("fact") or {}).get("value") or {}
                label = value.get("label", "") if isinstance(value, dict) else ""
                found[step] = ReplanTrigger(step_id=step, kind="candidate_event",
                                            detail=f"{data.get('producer', '')}:{label}"[:200])
        return list(found.values())

    def _replan(self, mission: dict, record: dict, outcomes: dict[str, StepOutcome],
                extra: list[ReplanTrigger] | None = None) -> None:
        """Propose, compile, admit and classify a retry version (D032). / 提议、编译、准入并分类重试版本（D032）。"""
        mission_id, version = mission["mission_id"], record["version"]

        def incomplete(found: list[Issue]) -> None:
            self._issues(found, mission_id)
            self.ledger.update_mission(mission_id, status="incomplete")

        cancelled = {step for step, outcome in outcomes.items()
                     if outcome is not None and outcome.execution_status is ExecutionStatus.CANCELLED}
        cancelled.update(row["data"].get("step_id", "mission")
                         for row in self._journals(mission_id, version).get("executive", [])
                         if row["kind"] == "operator_request" and row["data"].get("accepted") is True
                         and row["data"].get("action") == "cancel")
        cancelled.update(delivery["payload"].get("step_id", "mission")
                         for delivery in self.ledger.deliveries(mission_id)
                         if delivery["kind"] == "operator_request" and delivery["payload"].get("action") == "cancel")
        if self._binding(mission_id) is not None and self.ops.store.cancel_intent(mission_id) is not None:
            # A persisted cancel intent is a barrier for every later version (D050/D055). / 持久化取消意图阻止之后所有版本。
            cancelled.add("mission")
        if cancelled:
            # Cancel intent survives an unknown outcome or acknowledgement; never override it with another flight.
            # 即使结果或回执未知也保留取消意图，绝不自动重飞推翻操作者决定。
            return incomplete([issue("replan.cancelled_by_operator", ", ".join(sorted(cancelled)),
                                      affected=sorted(cancelled))])
        if mission["replans"] >= self.policy.max_replans_per_mission:
            return incomplete([issue("replan.limit_exceeded", f"{mission['replans']} replans used")])
        package = MissionPackage.model_validate(record["package"])
        base_approval = ApprovalRecord.model_validate(record["approval"])
        found = triggers(package, outcomes) + list(extra or [])
        now = self.clock()
        proposal = propose_retry(MissionSpec.model_validate(record["spec"]), package, found, self.policy,
                                 new_version=version + 1, now=now, not_after_limit=base_approval.expires_at,
                                 window_minutes=int(self.defaults.get("window_minutes", 30)))
        if proposal is None:
            return incomplete([])
        result = self._evaluate(proposal, mission["robot_id"], self._request(mission))
        common = {"origin": "replan", "spec": proposal, "compile": result.compile, "admission": result.admission}
        if result.blocked_at is not None:
            self._record_version(mission_id, version + 1, status="rejected",
                                       decision={"blocked_at": result.blocked_at, "codes": result.codes,
                                                 "triggers": [t.model_dump() for t in found]}, **common)
            return incomplete(result.issues)
        proposed = result.package
        decision = classify(package, proposed, found, self.policy, replans_so_far=mission["replans"],
                            base_approval=base_approval)
        details = {**decision.model_dump(mode="json"), "triggers": [t.model_dump() for t in found]}
        status = {"auto_approvable": "approving", "requires_human": "awaiting_approval"}.get(
            decision.classification, "rejected")
        self._record_version(mission_id, version + 1, status=status, package=proposed,
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
            fact, outcome = None, "failed"
            try:
                fact = await business_judgment(evidence, self.hub.media(row["media_path"]), row["width"], row["height"],
                                               provider, model, asset_id=evidence.subject_ids[0])
                outcome = "returned" if fact is not None else "no_fact"
            finally:
                source, recording = provider_source(provider)
                attempt = AnalysisOrigin(attempt_id=uuid.uuid4().hex, evidence_id=evidence.evidence_id,
                                         use=ModelUse(source=source, model_id=model,
                                                      reported_model_id=fact.source_version if fact else "",
                                                      prompt_version="business-judgment-v1",
                                                      prompt_sha256=digest(VLM_QUESTION),
                                                      input_sha256=evidence.sha256, recording_sha256=recording,
                                                      outcome=outcome))
                self.ledger.record_source(mission_id, row["mission_version"], f"analysis:{attempt.attempt_id}", attempt)
                report = self.ledger.report(mission_id)
                if report is not None:
                    self.ledger.record_report(mission_id, {**report, "provenance": self.provenance(mission_id)})
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
            if self.dispatch is not None:
                self.tick_operations()
            if self.workflows is not None:
                self.tick_workflows()
            if self.scheduler is not None:
                self.tick_scheduler()
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

    def tick_operations(self) -> None:
        """One P1 background pass: prepare docks, reconcile holds, do dock chores. / 一次 P1 后台处理。"""
        try:
            self.dirty.update(self.dispatch.tick())
        except Exception as error:  # keep serving; the problem is visible / 继续服务；问题可见
            self.ledger.record_issue(issue("service.degraded", f"dispatch: {type(error).__name__}: {error}"[:300]))

    def tick_workflows(self) -> None:
        """One P2 background pass: schedules and every active run. / 一次 P2 后台处理：排班与全部活动运行。"""
        try:
            self.dirty.update(self.workflows.tick())
        except Exception as error:  # keep serving; the problem is visible / 继续服务；问题可见
            self.ledger.record_issue(issue("service.degraded", f"workflows: {type(error).__name__}: {error}"[:300]))

    def tick_scheduler(self) -> None:
        """One P3 background pass: settle, withdraw, assign (D059). / 一次 P3 后台处理：结算、撤回、分配（D059）。"""
        try:
            self.dirty.update(self.scheduler.tick())
        except Exception as error:  # keep serving; the problem is visible / 继续服务；问题可见
            self.ledger.record_issue(issue("service.degraded", f"scheduler: {type(error).__name__}: {error}"[:300]))

    # ── views / 视图 ──

    def view(self, mission_id: str, caller: Caller | None = None) -> dict:
        """Everything the console shows for one mission; catalog mode checks project read.

        控制台为一个任务展示的全部内容；目录模式检查项目读权限。
        """
        self._check(caller, mission_id, MISSION_READ)
        return self._view(mission_id)

    def _dispatch_view(self, mission_id: str) -> dict | None:
        binding = self._binding(mission_id)
        if binding is None:
            return None
        store = self.ops.store
        decisions = [{"decision_id": d["decision_id"], "version": d["mission_version"], "stage": d["stage"],
                      "verdict": d["verdict"], "reasons": d["body"]["reasons"], "created_at": d["created_at"],
                      "snapshot_sha256": d["body"]["snapshot_sha256"], "policy_version": d["body"]["policy_version"]}
                     for d in store.decisions(mission_id, 40)]
        actions = [row for row in store.actions(binding["dock_id"])
                   if row["activity_key"].startswith(f"mission:{mission_id}:")]
        return {"reservations": [r.model_dump(mode="json") for r in store.reservations(mission_id=mission_id)],
                "decisions": decisions, "claims": store.claims(mission_id), "cancel": store.cancel_intent(mission_id),
                "dock_actions": actions, "events": store.events(prefix=f"mission:{mission_id}", limit=60)}

    def _view(self, mission_id: str) -> dict:
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
                "provenance": export(record),
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
        origins = {image["evidence_id"]: image for v in versions for image in v["provenance"]["imagery"]}
        for item in evidence:
            item["provenance"] = origins.get(item["evidence_id"], {"source": "legacy_unknown"})
        operations = [{"request_id": d["payload"]["request_id"], "action": d["payload"]["action"],
                       "requested_by": d["payload"]["requested_by"], "version": d["version"],
                       "acked": d["acked_at"] is not None, "accepted": d["ack_accepted"], "reason": d["ack_reason"]}
                      for d in self.ledger.deliveries(mission_id) if d["kind"] == "operator_request"]
        value = {"mission": mission, "request": {k: request.get(k) for k in (
                    "request_id", "text", "requested_by", "channel", "approved_volume_id", "asset_ids", "received_at")},
                 "versions": versions, "live": live, "evidence": evidence, "operations": operations,
                 "report": self.ledger.report(mission_id), "issues": self.ledger.issues(mission_id),
                 "facts": self.ledger.facts(mission_id)}
        if self.ops is not None:
            binding = self._binding(mission_id)
            value["binding"] = {k: binding[k] for k in ("project_id", "site_id", "dock_id", "robot_id",
                                                        "execution_backend", "catalog_sha256")} if binding else \
                {"project_id": self.ops.catalog.legacy_project.project_id, "legacy": True}
            value["dispatch"] = self._dispatch_view(mission_id)
        if self.scheduler is not None:
            value["task"] = self.scheduler.mission_task(mission_id)
        return value

    def summary(self, mission_id: str, caller: Caller | None = None) -> dict:
        """The read-only view an external agent may receive (D033). / 外部 agent 可以得到的只读视图（D033）。"""
        self._check(caller, mission_id, MISSION_READ)
        mission = self._mission(mission_id)
        report = self.ledger.report(mission_id)
        return {"mission_id": mission_id, "status": mission["status"], "current_version": mission["current_version"],
                "updated_at": mission["updated_at"],
                "report": {k: report[k] for k in ("targets", "summary", "all_targets_completed", "versions")}
                if report else None,
                "issues": [i["code"] for i in self.ledger.issues(mission_id)],
                "provenance": self.provenance(mission_id)}

    # ── project-scoped reads (P1) / 按项目限定的读取（P1）──

    def missions(self, caller: Caller | None = None, limit: int = 50) -> list[dict]:
        """Recent missions the caller may read; M2 mode lists everything as before. / 调用方可读的近期任务。"""
        fields = ("mission_id", "status", "current_version", "robot_id", "created_at", "updated_at")
        rows = self.ledger.missions(limit if self.ops is None else 500)
        if self.ops is None:
            return [{k: m[k] for k in fields} for m in rows]
        readable = set(self.ops.directory.projects(caller)) if caller is not None else set()
        found = []
        for mission in rows:
            project = self.project_of(mission["mission_id"])
            if project in readable and self.ops.directory.allows(caller, project, MISSION_READ):
                found.append({**{k: mission[k] for k in fields}, "project_id": project})
            if len(found) >= limit:
                break
        return found

    def robots_view(self, caller: Caller | None = None) -> list[dict]:
        if self.ops is None:
            return self.catalog.view()
        readable = [p for p in self.ops.directory.projects(caller)
                    if self.ops.directory.allows(caller, p, RESOURCE_READ)] if caller is not None else []
        robots = {r for r in self.ops.catalog.robots if self.ops.catalog.project_of(r) in readable}
        return self.catalog.view(only=robots)

    def media_row(self, mission_id: str, evidence_id: str, caller: Caller | None = None) -> dict | None:
        """One evidence row after the project read check; others look absent. / 经项目读检查后的一条证据。"""
        self._check(caller, mission_id, MISSION_READ)
        return next((r for r in self.ledger.evidence(mission_id) if r["evidence_id"] == evidence_id), None)

    def _require(self, caller: Caller | None, project_id: str, scope: str) -> None:
        if self.ops is None:
            raise ServiceError("service.invalid_request", "no operations catalog is configured")
        if caller is None or project_id not in self.ops.catalog.projects or \
                not self.ops.directory.allows(caller, project_id, RESOURCE_READ):
            raise ServiceError("service.not_found", project_id)
        if not self.ops.directory.allows(caller, project_id, scope):
            raise ServiceError("auth.project_denied", f"{scope} in {project_id}")

    def projects(self, caller: Caller | None = None) -> list[dict]:
        if self.ops is None or caller is None:
            return []
        catalog, legacy = self.ops.catalog, self.ops.catalog.legacy_project.project_id
        return [{"project_id": p, "name": catalog.projects[p].name if p in catalog.projects else "legacy M2 history",
                 "legacy": p == legacy, "roles": sorted(r.value for r in self.ops.directory.roles(caller, p)),
                 "robots": sorted(r for r in catalog.robots if catalog.project_of(r) == p)}
                for p in self.ops.directory.projects(caller)]

    def _dock_row(self, dock_id: str) -> dict:
        store, catalog = self.ops.store, self.ops.catalog
        dock, entry, now = store.dock_status(dock_id), catalog.docks[dock_id], self.clock()
        holders = store.holders([f"{dock_id}.pad"])
        pad = "free"
        if holders:
            reservation = store.reservation(holders[f"{dock_id}.pad"])
            pad = reservation.state.value if reservation else "uncertain"
        row = {"dock_id": dock_id, "site_id": entry.site_id, "vendor": entry.vendor, "model": entry.model,
               "source": entry.backend.kind, "serves": list(entry.serves), "pad": pad,
               "holder": holders.get(f"{dock_id}.pad"), "status": None}
        if dock is not None:
            age = (now - dock.report.observed_at).total_seconds()
            report = dock.report
            row["status"] = {
                "session": dock.session.value, "boot_id": report.boot_id, "seq": report.seq,
                "observed_at": report.observed_at.isoformat(), "received_at": dock.received_at.isoformat(),
                "age_s": round(age, 3), "fresh": -catalog.policy.future_skew_s <= age <= catalog.policy.freshness_s,
                "link": report.link.value, "lid": report.lid.value, "aircraft": report.aircraft.value,
                "energy": report.energy.model_dump(mode="json"), "environment": report.environment.model_dump(mode="json"),
                "upkeep": report.upkeep.value, "lock": dock.lock.model_dump(mode="json") if dock.lock else None}
        return row

    def _robot_row(self, robot_id: str) -> dict:
        capability, source = self.catalog.capability(robot_id)
        status, entry = self.catalog.status(robot_id), self.ops.catalog.robots[robot_id]
        eligibility = self.dispatch.judge(robot_id, stage=Stage.PREVIEW)
        return {"robot_id": robot_id, "site_id": entry.site_id, "dock_id": entry.dock_id,
                "execution_backend": entry.execution_backend, "capability_source": source,
                "skills": sorted(s.skill_id for s in capability.skills) if capability else [],
                "status": {"flight_phase": status.flight_phase, "timestamp": status.timestamp.isoformat(),
                           "age_s": round((self.clock() - status.timestamp).total_seconds(), 3),
                           "energy": status.energy.remaining_fraction} if status else None,
                "eligibility": {k: v for k, v in eligibility.model_dump(mode="json").items()
                                if k in ("verdict", "reasons", "evaluated_at", "valid_until", "snapshot_sha256",
                                         "policy_version")}}

    def resources(self, project_id: str, caller: Caller | None = None) -> dict:
        """Sites, docks and robots of one project with status age, source and preview eligibility (never authority).

        一个项目的站点、机场与机器人，附状态年龄、来源与预览判定（从不构成授权）。
        """
        self._require(caller, project_id, RESOURCE_READ)
        catalog = self.ops.catalog
        sites = []
        for site_id in catalog.projects[project_id].sites:
            site = catalog.sites[site_id]
            docks = [d for d, entry in catalog.docks.items() if entry.site_id == site_id]
            robots = [r for r, entry in catalog.robots.items() if entry.site_id == site_id]
            sites.append({"site_id": site_id, "scene": site.scene, "max_wind_mps": site.max_wind_mps,
                          "docks": [self._dock_row(d) for d in docks], "robots": [self._robot_row(r) for r in robots]})
        return {"project_id": project_id, "name": catalog.projects[project_id].name,
                "roles": sorted(r.value for r in self.ops.directory.roles(caller, project_id)),
                "catalog": {"catalog_id": catalog.catalog_id, "sha256": catalog.sha256,
                            "policy": catalog.policy.model_dump(mode="json")},
                "evaluated_at": self.clock().isoformat(), "sites": sites}

    def resource(self, project_id: str, resource_id: str, caller: Caller | None = None) -> dict:
        self._require(caller, project_id, RESOURCE_READ)
        catalog, store = self.ops.catalog, self.ops.store
        if resource_id in catalog.docks and catalog.sites[catalog.docks[resource_id].site_id].project_id == project_id:
            return {"kind": "dock", **self._dock_row(resource_id),
                    "actions": store.actions(resource_id)[-20:], "events": store.events(f"dock:{resource_id}", limit=40),
                    "reservations": [r.model_dump(mode="json") for r in store.reservations(dock_id=resource_id)][-20:]}
        if resource_id in catalog.robots and catalog.project_of(resource_id) == project_id:
            return {"kind": "robot", **self._robot_row(resource_id)}
        raise ServiceError("service.not_found", resource_id)

    def eligibility(self, project_id: str, robot_id: str, caller: Caller | None = None) -> dict:
        self._require(caller, project_id, RESOURCE_READ)
        if self.ops.catalog.project_of(robot_id) != project_id:
            raise ServiceError("service.not_found", robot_id)
        return self.dispatch.judge(robot_id, stage=Stage.PREVIEW).model_dump(mode="json")

    def maintenance(self, project_id: str, dock_id: str, action: str, reason: str,
                    caller: Caller | None = None) -> dict:
        """Admin lock or release of a dock; telemetry never releases a lock (D055). / admin 加锁或解锁；遥测从不解锁。"""
        from drone_agent.fleet import docks

        self._require(caller, project_id, RESOURCE_MAINTAIN)
        entry = self.ops.catalog.docks.get(dock_id)
        if entry is None or self.ops.catalog.sites[entry.site_id].project_id != project_id:
            raise ServiceError("service.not_found", dock_id)
        if action == "release":
            if not docks.release_lock(self.ops.store, dock_id, caller.identity, reason):
                raise ServiceError("dispatch.not_locked", dock_id)
        elif action == "set":
            docks.set_lock(self.ops.store, dock_id, caller.identity, reason)
        else:
            raise ServiceError("service.invalid_request", f"unknown maintenance action {action}")
        return self._dock_row(dock_id)

    # ── dock backends (P1) / 机场后端（P1）──

    def dock_report(self, principal: str, report: dict) -> dict:
        from drone_agent.fleet import docks

        if self.ops is None:
            raise ServiceError("service.invalid_request", "no operations catalog is configured")
        result = docks.ingest(self.ops.store, principal, report, self.clock())
        if result["accepted"]:
            self.dirty.update(m["mission_id"] for m in self.ledger.missions(50)
                              if m["status"] in ("approved", "queued", "delivered", "running", "verifying"))
        return result

    def dock_actions(self, principal: str, dock_id: str) -> list[dict]:
        from drone_agent.fleet import docks

        if self.ops is None:
            raise ServiceError("service.invalid_request", "no operations catalog is configured")
        try:
            return docks.pending_actions(self.ops.store, principal, dock_id)
        except docks.ReportRejected as error:
            raise ServiceError("auth.backend_mismatch", str(error)) from error

    def dock_ack(self, principal: str, action_id: str, accepted: bool, reason: str) -> dict:
        from drone_agent.fleet import docks

        if self.ops is None:
            raise ServiceError("service.invalid_request", "no operations catalog is configured")
        try:
            return docks.acknowledge(self.ops.store, principal, action_id, accepted, reason)
        except docks.ReportRejected as error:
            raise ServiceError("auth.backend_mismatch", str(error)) from error
