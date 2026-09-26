"""P2 workflow engine: triggers, schedules, node progress, outbox delivery, cancellation and business records (D057).

The engine lives in the mission-service process and only calls the service; it never builds or signs a package,
approves a mission or talks to a robot. Each pass takes or renews a lease per active run and moves nodes forward with
compare-and-set writes fenced by that lease: a node whose dependencies finished is skipped or started; an effect
(submit a mission, create a work order, start a reinspection) is written together with its outbox row and delivered
at least once to a consumer that deduplicates by the activity key; waits are polled from the mission ledger, the
review and work-order tables and durable deadlines. A cancel is persisted before anything else can dispatch; the
engine then reconciles claimed effects and reports the run cancelled only after its missions settled.

P2 工作流引擎：触发、排班、节点推进、outbox 投递、取消与业务记录（D057）。

引擎位于任务服务进程内，只调用服务；它从不构造或签名任务包、审批任务或与机器人通信。每一轮为每个活动运行获取或续租，
并以受该租约 fencing 约束的比较并交换写入推进节点：依赖已结束的节点被跳过或开始；外部效果（提交任务、创建工单、启动
复检）与其 outbox 行一起写入，并至少一次投递给按活动键去重的消费者；等待从任务账本、复核与工单表以及持久截止时刻轮询。
取消先于任何派遣持久化；随后引擎对已领取的效果对账，并在任务全部了结后才报告运行已取消。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from drone_agent.contracts import utcnow
from drone_agent.fleet.analysis import AnalysisResult, analyze
from drone_agent.fleet.provenance import AnalysisOrigin, ModelUse
from drone_agent.fleet.service import ServiceError
from drone_agent.fleet.workflow_models import (
    CANCELLING_RUN,
    EVENT_ID,
    LEGITIMATE_SKIPS,
    TERMINAL_NODE,
    TERMINAL_RUN,
    Activity,
    AnalysisFixture,
    AnalysisOutput,
    AwaitMissionNode,
    EventTrigger,
    InputRef,
    InspectionOutput,
    InternalTrigger,
    ManualTrigger,
    MissionOutput,
    NodeState,
    OrderOutput,
    ReinspectionOutput,
    RepairOutput,
    ReviewOutput,
    RunState,
    ScheduleTrigger,
    ScriptedAnalyzer,
    SkipReason,
    SubmitMissionNode,
    WaitReason,
    WorkflowCatalog,
    WorkflowSpec,
    WorkOrderNode,
    activity_key,
    due_occurrences,
    load_workflows,
    next_occurrence,
    predicate_holds,
    run_principal,
    tz_database_version,
)
from drone_agent.fleet.workflow_store import MISSION_TERMINAL, Fenced, StaleNode, WorkflowStore, migrate
from drone_agent.runtime.issues import issue
from drone_agent.runtime.permission import (
    TRUST_LEVEL_CAPS,
    WORKFLOW_DRAFT,
    WORKFLOW_READ,
    WORKFLOW_REVIEW,
    WORKFLOW_RUN,
    Caller,
    TrustLevel,
)

LEASE_S = 10.0
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
HOLDING = ("reserved", "occupied", "uncertain")
EFFECTS = (Activity.SUBMIT_MISSION, Activity.CREATE_WORK_ORDER, Activity.REQUEST_REINSPECTION)
# Consumer refusals that a retry cannot change. / 重试无法改变的消费者拒绝。
PERMANENT = ("service.not_found", "service.invalid_request", "dispatch.backend_mismatch", "workflow.invalid_draft",
             "workflow.invalid_inputs", "auth.project_denied")


@dataclass
class Workflows:
    """Everything the service needs for workflows. / 服务运行工作流所需的全部内容。"""

    catalog: WorkflowCatalog
    store: WorkflowStore
    fixtures: dict[str, str]
    migration: dict = field(default_factory=dict)


def build_workflows(root: Path, ledger, path: Path, operations, *, backups: Path | None, clock=utcnow) -> Workflows:
    """Load and check the workflow catalog, migrate the ledger (D058) and open the store.

    加载并核对工作流目录、迁移账本（D058）并打开存储。
    """
    catalog = load_workflows(path)
    fixtures = catalog.check(operations.catalog, operations.registries, root)
    migration = migrate(ledger, backups=backups, clock=clock)
    store = WorkflowStore(ledger, catalog, fixtures, clock=clock)
    return Workflows(catalog, store, fixtures, migration)


def first_party(identity: str) -> Caller:
    """The caller a manual starter or schedule enabler was; only first-party callers hold `workflow.run`.

    人工发起者或排班启用者当时的调用方；只有第一方调用方持有 `workflow.run`。
    """
    return Caller(identity, TrustLevel.FIRST_PARTY, TRUST_LEVEL_CAPS[TrustLevel.FIRST_PARTY])


class WorkflowEngine:
    """The P2 runner inside the mission service; `tick()` is its background pass. / 任务服务内的 P2 执行器；`tick()` 是后台处理。"""

    def __init__(self, service, workflows: Workflows, *, root: Path, worker_id: str | None = None,
                 lease_s: float = LEASE_S):
        self.service, self.workflows = service, workflows
        self.store, self.catalog, self.root, self.lease_s = workflows.store, workflows.catalog, root, lease_s
        self.worker = worker_id or "wfw-" + uuid.uuid4().hex[:12]
        self.touched: set[str] = set()
        self._fixtures: dict[str, AnalysisFixture] = {}

    @property
    def ledger(self):
        return self.service.ledger

    @property
    def ops(self):
        return self.service.ops

    def clock(self) -> datetime:
        return self.service.clock()

    # ── permissions / 权限 ──

    def _require(self, caller: Caller | None, project_id, scope: str) -> None:
        directory = self.ops.directory
        if caller is None or not isinstance(project_id, str) or project_id not in self.ops.catalog.projects \
                or not directory.allows(caller, project_id, WORKFLOW_READ):
            raise ServiceError("service.not_found", str(project_id)[:40])
        if scope != WORKFLOW_READ and not directory.allows(caller, project_id, scope):
            raise ServiceError("auth.project_denied", f"{scope} in {project_id}")

    def _run(self, caller: Caller | None, project_id, run_id, scope: str) -> dict:
        self._require(caller, project_id, WORKFLOW_READ)
        run = self.store.run(run_id) if isinstance(run_id, str) else None
        if run is None or run["project_id"] != project_id:
            raise ServiceError("service.not_found", str(run_id)[:40])
        self._require(caller, project_id, scope)
        return run

    def _authority(self, run: dict) -> str | None:
        """Why a run may no longer start an effect, or None (D057 §3). / 运行为何不能再开始效果，或 None（D057 §3）。"""
        if self.catalog.spec(run["project_id"], run["workflow_id"], run["version"]) is None:
            return "authority.version_retired"
        source = run["trigger_source"]
        if source.startswith(("manual:", "schedule:")):
            if not self.ops.directory.allows(first_party(run["started_by"]), run["project_id"], WORKFLOW_RUN):
                return "authority.role_revoked"
        elif source.startswith("event:"):
            spec = self.catalog.spec(run["project_id"], run["workflow_id"], run["version"])
            trigger = spec.trigger(source.rsplit(":", 1)[1])
            if not isinstance(trigger, EventTrigger) or trigger.source != run["started_by"]:
                return "authority.binding_revoked"
        elif source.startswith("internal:"):
            spec = self.catalog.spec(run["project_id"], run["workflow_id"], run["version"])
            if not any(isinstance(t, InternalTrigger) for t in spec.triggers):
                return "authority.binding_revoked"
        else:
            return "authority.unknown_trigger"
        return None

    # ── triggers / 触发 ──

    def start(self, caller: Caller | None, project_id, workflow_id, request_id, inputs) -> dict:
        """Manual start by an operator; a repeated request id returns the first run. / operator 人工启动；重复请求 ID 返回第一次的运行。"""
        self._require(caller, project_id, WORKFLOW_RUN)
        spec = self.catalog.latest(project_id, workflow_id) if isinstance(workflow_id, str) else None
        if spec is None:
            raise ServiceError("service.not_found", str(workflow_id)[:40])
        trigger = next((t for t in spec.triggers if isinstance(t, ManualTrigger)), None)
        if trigger is None:
            raise ServiceError("workflow.not_startable", f"{spec.workflow_id} has no manual trigger")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        try:
            values = spec.check_inputs(inputs)
        except ValueError as error:
            raise ServiceError("workflow.invalid_inputs", str(error)[:300]) from error
        run_id, _ = self.store.start_run(spec, catalog_sha256=self.catalog.sha256, source=f"manual:{caller.identity}",
                                         event_id=request_id, actor=caller.identity, started_by=caller.identity,
                                         inputs=values, body={"trigger_id": trigger.trigger_id})
        return self._view(self.store.run(run_id))

    def event(self, principal: str, project_id, workflow_id, trigger_id, event_type, event_id, payload) -> dict:
        """An authenticated event: only the bound source, only the declared inputs; a repeat returns the first run.

        经认证的事件：只接受绑定的来源与声明的输入；重复事件返回第一次的运行。
        """
        def refuse(reason: str):
            self.store.event(f"events:{principal}"[:120], "event.refused", principal,
                             {"project_id": str(project_id)[:40], "workflow_id": str(workflow_id)[:40],
                              "trigger_id": str(trigger_id)[:40], "event_id": str(event_id)[:120], "reason": reason})
            raise ServiceError("workflow.event_refused", reason)

        spec = self.catalog.latest(project_id, workflow_id) \
            if isinstance(project_id, str) and isinstance(workflow_id, str) else None
        trigger = spec.trigger(trigger_id) if spec is not None and isinstance(trigger_id, str) else None
        if not isinstance(trigger, EventTrigger) or trigger.source != principal:
            refuse("no such event trigger for this source")
        if event_type != trigger.event_type:
            refuse("event type differs from the trigger")
        if not isinstance(event_id, str) or not re.fullmatch(EVENT_ID, event_id):
            refuse("invalid event id")
        if not isinstance(payload, dict) or set(payload) != set(trigger.inputs_from_event):
            refuse("payload keys differ from the declared inputs")
        try:
            values = spec.check_inputs(payload)
        except ValueError:
            refuse("payload outside the declared choices")
        run_id, created = self.store.start_run(spec, catalog_sha256=self.catalog.sha256,
                                               source=f"{principal}:{trigger.trigger_id}", event_id=event_id,
                                               actor=principal, started_by=principal, inputs=values,
                                               body={"trigger_id": trigger.trigger_id, "event_type": event_type})
        return {"run_id": run_id, "created": created, "state": self.store.run(run_id)["state"]}

    def set_schedule(self, caller: Caller | None, project_id, workflow_id, trigger_id, action, reason) -> dict:
        """Enable (pinning the current version, cursor at the next occurrence) or disable a schedule; both audited.

        启用（固定当前版本，游标置于下一个发生时刻）或停用排班；两者都入审计。
        """
        self._require(caller, project_id, WORKFLOW_RUN)
        spec = self.catalog.latest(project_id, workflow_id) if isinstance(workflow_id, str) else None
        if spec is None:
            raise ServiceError("service.not_found", str(workflow_id)[:40])
        trigger = spec.trigger(trigger_id) if isinstance(trigger_id, str) else None
        if not isinstance(trigger, ScheduleTrigger):
            raise ServiceError("workflow.schedule_invalid", f"{trigger_id} is not a schedule of {spec.workflow_id}")
        reason = str(reason)[:300]
        if action == "enable":
            row = self.store.set_schedule(project_id, spec.workflow_id, trigger.trigger_id, state="enabled",
                                          version=spec.version, catalog_sha256=self.catalog.sha256,
                                          cursor=next_occurrence(trigger.schedule, self.clock()),
                                          actor=caller.identity, reason=reason)
        elif action == "disable":
            current = self.store.schedule(project_id, spec.workflow_id, trigger.trigger_id)
            if current is None or current["state"] != "enabled":
                raise ServiceError("workflow.schedule_invalid", "the schedule is not enabled")
            row = self.store.set_schedule(project_id, spec.workflow_id, trigger.trigger_id, state="disabled",
                                          version=current["version"], catalog_sha256=current["catalog_sha256"],
                                          cursor=datetime.fromisoformat(current["cursor_at"])
                                          if current["cursor_at"] else None, actor=caller.identity, reason=reason)
        else:
            raise ServiceError("service.invalid_request", "action must be enable or disable")
        return self._schedule_view(row)

    def fire_schedules(self) -> None:
        """Start due occurrences of enabled schedules; record missed and refused ones (09-operations §4.2).

        启动已启用排班的到期发生时刻；记录错过与拒绝的发生时刻（09-operations §4.2）。
        """
        now = self.clock()
        for row in self.store.schedules():
            if row["state"] != "enabled" or not row["cursor_at"] or datetime.fromisoformat(row["cursor_at"]) > now:
                continue
            spec = self.store.pinned(row["catalog_sha256"]).spec(row["project_id"], row["workflow_id"], row["version"])
            trigger = spec.trigger(row["trigger_id"]) if spec is not None else None
            if not isinstance(trigger, ScheduleTrigger):
                continue
            fire, missed, following = due_occurrences(trigger, datetime.fromisoformat(row["cursor_at"]), now)
            source = f"schedule:{trigger.trigger_id}"
            with self.store.transaction():
                if not self.store.advance_cursor(row["project_id"], row["workflow_id"], row["trigger_id"],
                                                 row["cursor_at"], following):
                    continue
                for occurrence in missed:
                    self.store.record_trigger(spec, source=source, event_id=occurrence.isoformat(),
                                              disposition="missed", actor="scheduler",
                                              body={"occurrence": occurrence.isoformat(),
                                                    "late_s": round((now - occurrence).total_seconds(), 3),
                                                    "start_window_s": trigger.start_window_s})
                for occurrence in fire:
                    body = {"trigger_id": trigger.trigger_id, "occurrence": occurrence.isoformat(),
                            "late_s": round((now - occurrence).total_seconds(), 3), "tzdata": tz_database_version()}
                    if not self.ops.directory.allows(first_party(row["changed_by"]), spec.project_id, WORKFLOW_RUN):
                        self.store.record_trigger(spec, source=source, event_id=occurrence.isoformat(),
                                                  disposition="refused", actor="scheduler",
                                                  body={**body, "reason": "the enabler no longer holds operator"})
                        continue
                    self.store.start_run(spec, catalog_sha256=row["catalog_sha256"], source=source,
                                         event_id=occurrence.isoformat(), actor="scheduler",
                                         started_by=row["changed_by"], inputs=dict(sorted(trigger.inputs.items())),
                                         body=body)

    # ── human actions / 人工操作 ──

    def cancel(self, caller: Caller | None, project_id, run_id, request_id, reason) -> dict:
        """Persist the cancel first; the engine then reconciles and settles (09-operations §4.3).

        先持久化取消；之后由引擎对账并收尾（09-operations §4.3）。
        """
        run = self._run(caller, project_id, run_id, WORKFLOW_RUN)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        result = self.store.request_cancel(run["run_id"], actor=caller.identity, request_id=request_id,
                                           reason=str(reason)[:300])
        if result["status"] == "final":
            raise ServiceError("workflow.not_cancellable", f"the run is already {result['state']}")
        self.touched.update(result.get("missions", []))
        self.service.dirty.update(result.get("missions", []))
        return self._view(self.store.run(run["run_id"]))

    def review(self, caller: Caller | None, project_id, run_id, node_id, decision, request_id, note) -> dict:
        """A reviewer's decision on a waiting review node; the first decision wins (D057 §7).

        reviewer 对等待中的复核节点作出决定；首个决定有效（D057 §7）。
        """
        run = self._run(caller, project_id, run_id, WORKFLOW_REVIEW)
        spec = self.store.spec_of(run)
        node_spec = next((n for n in spec.nodes if n.node_id == node_id), None)
        if node_spec is None or node_spec.activity != Activity.HUMAN_REVIEW.value:
            raise ServiceError("service.not_found", str(node_id)[:48])
        if decision not in ("confirmed", "dismissed"):
            raise ServiceError("service.invalid_request", "decision must be confirmed or dismissed")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        key = activity_key(run["run_id"], node_id)
        with self.store.transaction():
            known = self.store.review(key)
            if known is not None:
                if known["request_id"] != request_id:
                    raise ServiceError("workflow.already_decided", f"{node_id} was already {known['decision']}")
                return self._view(self.store.run(run["run_id"]))
            node = self.store.nodes(run["run_id"])[node_id]
            current = self.store.run(run["run_id"])
            if node["state"] != NodeState.WAITING.value or RunState(current["state"]) in CANCELLING_RUN | TERMINAL_RUN:
                raise ServiceError("workflow.not_waiting", f"{node_id} is {node['state']}")
            analysis = self.store.nodes(run["run_id"])[node_spec.params.analysis_from]["result"]
            self.store.record_review(activity_key=key, project_id=project_id, run_id=run["run_id"],
                                     analysis_id=analysis["analysis_id"], reviewer=caller.identity,
                                     request_id=request_id, decision=decision, note=str(note)[:300])
            self.store.event(f"workflow:{run['run_id']}", "review.recorded", caller.identity,
                             {"node_id": node_id, "decision": decision})
        return self._view(self.store.run(run["run_id"]))

    async def draft(self, caller: Caller | None, project_id, text) -> dict:
        """An inactive template draft from the operator's text; returned and audited, never stored or runnable (D057 §9).

        由操作者文本生成的未生效模板草案；只返回与审计，从不存储、也不可运行（D057 §9）。
        """
        from drone_agent.fleet.workflow_models import CATALOG_FORMAT

        self._require(caller, project_id, WORKFLOW_DRAFT)
        planner = getattr(self.service, "workflow_planner", None)
        if planner is None:
            raise ServiceError("service.invalid_request", "no planner is configured for workflow drafts")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ServiceError("service.invalid_request", "text must be 1-2000 characters")
        catalog = self.ops.catalog
        robot_id = sorted(r for r in catalog.robots if catalog.project_of(r) == project_id)[0]
        registry = self.ops.registry(robot_id).data
        volume_id = registry["approved_volume_id"]
        assets = sorted(a for a, entry in registry["assets"].items() if entry.get("volume") == volume_id)
        analyzers = sorted(self.catalog.analyzers)
        analyzer = next((a for a in analyzers if isinstance(self.catalog.analyzers[a], ScriptedAnalyzer)), analyzers[0])
        outcome = await planner.draft(text, project_id=project_id, robot_id=robot_id, volume_id=volume_id,
                                      assets=assets, analyzer=analyzer)
        result = outcome.model_dump(mode="json")
        if outcome.spec is not None:
            # The same checks a published catalog gets; a draft that fails them is reported, not repaired.
            # 与发布目录相同的检查；未通过的草案如实报告，不作修补。
            try:
                WorkflowCatalog.model_validate({
                    "format": CATALOG_FORMAT, "catalog_id": "draft_check", "operations_catalog": catalog.catalog_id,
                    "analyzers": {k: v.model_dump(mode="json") for k, v in self.catalog.analyzers.items()},
                    "workflows": [outcome.spec]}).check(catalog, self.ops.registries, self.root)
            except ValueError as error:
                result.update(status="failed", spec=None, spec_sha256="", errors=[str(error)[:300]])
        self.store.event(f"drafts:{project_id}", "draft.proposed", caller.identity,
                         {"status": result["status"], "spec_sha256": result["spec_sha256"],
                          "source": outcome.use.source, "input_sha256": outcome.use.input_sha256})
        return {**result, "active": False,
                "note": "drafts never run; a template takes effect only as a reviewed, versioned catalog change"}

    def repair(self, caller: Caller | None, project_id, order_id, request_id, note) -> dict:
        """Repair feedback on an open order: it waits for reinspection, it is not closed (D057 §7).

        对未结工单的维修反馈：工单进入待复检，不关单（D057 §7）。
        """
        self._require(caller, project_id, WORKFLOW_READ)
        order = self.store.order(order_id) if isinstance(order_id, str) else None
        if order is None or order["project_id"] != project_id:
            raise ServiceError("service.not_found", str(order_id)[:40])
        self._require(caller, project_id, WORKFLOW_RUN)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        if order["feedback"] is not None:
            if order["feedback"]["request_id"] == request_id:
                return self._order_view(order)
            raise ServiceError("workflow.order_state", "repair feedback was already recorded")
        if order["state"] != "open":
            raise ServiceError("workflow.order_state", f"the order is {order['state']}")
        feedback = {"feedback_id": "fb-" + uuid.uuid4().hex[:16], "request_id": request_id,
                    "reported_by": caller.identity, "reported_at": self.clock().isoformat(), "note": str(note)[:300]}
        order, applied = self.store.record_feedback(order["order_id"], feedback, caller.identity)
        if not applied and (order["feedback"] or {}).get("request_id") != request_id:
            raise ServiceError("workflow.order_state", f"the order is {order['state']}")
        return self._order_view(order)

    # ── the background pass / 后台处理 ──

    def tick(self) -> set[str]:
        """Fire schedules, advance every active run; returns missions to refresh. / 触发排班、推进活动运行；返回需刷新的任务。"""
        self.fire_schedules()
        for run in self.store.active_runs():
            try:
                self.advance(run["run_id"])
            except (Fenced, StaleNode):
                continue
            except Exception as error:  # keep the other runs moving; the problem is visible / 保持其他运行推进；问题可见
                self.ledger.record_issue(issue("service.degraded",
                                               f"workflow {run['run_id']}: {type(error).__name__}: {error}"[:300]))
        touched, self.touched = self.touched, set()
        return touched

    def advance(self, run_id: str) -> None:
        epoch = self.store.acquire(run_id, self.worker, self.lease_s)
        if epoch is None:
            return
        run = self.store.run(run_id)
        spec = self.store.spec_of(run)
        if RunState(run["state"]) in CANCELLING_RUN:
            self._cancelling(run, spec, epoch)
            return
        for _ in range(4 * len(spec.nodes) + 4):
            if not self._step(run, spec, epoch):
                break
            run = self.store.run(run_id)
            if RunState(run["state"]) in CANCELLING_RUN | TERMINAL_RUN:
                return
        self._derive(run, epoch)

    def _step(self, run: dict, spec: WorkflowSpec, epoch: int) -> bool:
        nodes = self.store.nodes(run["run_id"])
        for node_id in spec.order():
            node, node_spec = nodes[node_id], spec.node(node_id)
            state = NodeState(node["state"])
            if state is NodeState.PENDING:
                changed = self._begin(run, spec, node_spec, node, nodes, epoch)
            elif state is NodeState.RUNNING:
                changed = self._deliver(run, node_spec, node, epoch)
            elif state is NodeState.WAITING:
                changed = self._poll(run, spec, node_spec, node, nodes, epoch)
            else:
                changed = False
            if changed:
                return True
        return False

    def _move(self, run: dict, node: dict, epoch: int, state: NodeState, **values) -> None:
        self.store.transition(run["run_id"], node["node_id"], worker=self.worker, epoch=epoch,
                              version=node["state_version"], state=state, **values)

    @staticmethod
    def _outputs(nodes: dict) -> dict[str, dict]:
        return {node_id: node["result"] for node_id, node in nodes.items()
                if node["state"] == NodeState.COMPLETED.value and node["result"] is not None}

    @staticmethod
    def _readiness(spec: WorkflowSpec, node_spec, nodes: dict) -> tuple[NodeState, SkipReason | None] | None:
        """None while a dependency runs; otherwise start, or skip with the reason (D057 §8).

        依赖仍在运行时为 None；否则开始，或带原因跳过（D057 §8）。
        """
        dependencies = spec.dependencies(node_spec.node_id)
        if any(NodeState(nodes[d]["state"]) not in TERMINAL_NODE for d in dependencies):
            return None
        references = {reference for reference, _ in node_spec.references()}
        unknown = failed = skipped = False
        for dependency in dependencies:
            state, reason = NodeState(nodes[dependency]["state"]), nodes[dependency]["reason"]
            is_reference = dependency in references
            if state is NodeState.COMPLETED:
                continue
            if state is NodeState.SKIPPED and reason in LEGITIMATE_SKIPS:
                skipped |= is_reference
                continue
            if not is_reference and node_spec.requires == "done":
                continue
            if state is NodeState.OUTCOME_UNKNOWN or reason == SkipReason.UPSTREAM_UNKNOWN.value:
                unknown = True
            else:
                failed = True
        if unknown:
            return NodeState.SKIPPED, SkipReason.UPSTREAM_UNKNOWN
        if failed:
            return NodeState.SKIPPED, SkipReason.UPSTREAM_FAILED
        if skipped:
            return NodeState.SKIPPED, SkipReason.UPSTREAM_SKIPPED
        outputs = WorkflowEngine._outputs(nodes)
        if not all(predicate_holds(predicate, outputs) for predicate in node_spec.when):
            return NodeState.SKIPPED, SkipReason.CONDITION_FALSE
        return NodeState.RUNNING, None

    def _begin(self, run, spec, node_spec, node, nodes, epoch) -> bool:
        decision = self._readiness(spec, node_spec, nodes)
        if decision is None:
            return False
        state, skip = decision
        if state is NodeState.SKIPPED:
            self._move(run, node, epoch, NodeState.SKIPPED, reason=skip.value)
            return True
        activity = Activity(node_spec.activity)
        outputs = self._outputs(nodes)
        if activity in EFFECTS:
            problem = self._authority(run)
            if problem is not None:
                self._move(run, node, epoch, NodeState.FAILED, reason=problem)
                return True
            self._move(run, node, epoch, NodeState.RUNNING,
                       outbox={"key": activity_key(run["run_id"], node_spec.node_id), "kind": activity.value,
                               "payload": self._payload(run, spec, node_spec, outputs)})
        elif activity is Activity.AWAIT_MISSION:
            self._move(run, node, epoch, NodeState.WAITING, reason=WaitReason.DELIVERY.value)
        elif activity in (Activity.HUMAN_REVIEW, Activity.AWAIT_REPAIR):
            timeout = node_spec.params.timeout_s
            self._move(run, node, epoch, NodeState.WAITING,
                       reason=(WaitReason.REVIEW if activity is Activity.HUMAN_REVIEW else WaitReason.REPAIR).value,
                       deadline=self.clock() + timedelta(seconds=timeout) if timeout else None)
        elif activity is Activity.ANALYZE_EVIDENCE:
            self._analyze(run, node_spec, node, outputs, epoch)
        else:
            self._move(run, node, epoch, NodeState.COMPLETED, result=self._report(run, spec, nodes))
        return True

    def _asset(self, run: dict, submit: SubmitMissionNode) -> str:
        asset = submit.params.asset
        return run["inputs"][asset.input] if isinstance(asset, InputRef) else asset

    def _payload(self, run, spec: WorkflowSpec, node_spec, outputs) -> dict:
        project = run["project_id"]
        if isinstance(node_spec, SubmitMissionNode):
            params = node_spec.params
            return {"project_id": project, "robot_id": params.robot_id, "volume_id": params.volume_id,
                    "asset_id": self._asset(run, node_spec), "template": f"{spec.workflow_id}@v{spec.version}"}
        if isinstance(node_spec, WorkOrderNode):
            review = outputs[node_spec.params.review_from]
            analyze_id = spec.node(node_spec.params.review_from).params.analysis_from
            analysis = outputs[analyze_id]
            inspection = outputs[spec.node(analyze_id).params.inspection_from]
            return {"project_id": project, "asset_id": inspection["asset_id"], "review_id": review["review_id"],
                    "analysis_id": analysis["analysis_id"],
                    "finding": {"verdict": analysis["verdict"], "confidence": analysis["confidence"],
                                "source": analysis["source"], "decision": review["decision"],
                                "reviewer": review["reviewer"]},
                    "evidence": {k: inspection[k] for k in ("mission_id", "mission_version", "evidence_id",
                                                            "evidence_sha256", "captured_at")}}
        params = node_spec.params
        order = outputs[params.order_from]
        return {"project_id": project, "order_id": order["order_id"],
                "feedback_id": outputs[params.repair_from]["feedback_id"], "workflow_id": params.workflow_id,
                "version": params.version, "asset_input": params.asset_input,
                "asset_id": self.store.order(order["order_id"])["asset_id"]}

    # ── effects / 效果 ──

    def _deliver(self, run, node_spec, node, epoch) -> bool:
        row = next((r for r in self.store.outbox(run["run_id"]) if r["node_id"] == node_spec.node_id), None)
        if row is None:
            self._move(run, node, epoch, NodeState.FAILED, reason="outbox.missing")
            return True
        if row["state"] == "failed":
            self._move(run, node, epoch, NodeState.FAILED, reason=row["result"]["code"], detail=row["result"])
            return True
        if row["state"] == "void":
            self._move(run, node, epoch, NodeState.CANCELLED, reason="cancelled", detail=row["result"])
            return True
        claimed = self.store.claim(row["outbox_id"], worker=self.worker, epoch=epoch)
        if claimed is None:
            return True
        guard = self.store.claim_guard(row["outbox_id"], self.worker, epoch)
        try:
            output = self._consume(run, claimed, guard)
        except ServiceError as error:
            if error.issue.code not in PERMANENT:
                return False
            self.store.settle_outbox(row["outbox_id"], worker=self.worker, epoch=epoch, state="failed",
                                     result={"code": error.issue.code, "message": error.issue.message[:300]})
            return True
        if output is None:
            self.store.settle_outbox(row["outbox_id"], worker=self.worker, epoch=epoch, state="void",
                                     result={"reason": "refused_after_claim"})
            return True
        self.store.delivered(row["outbox_id"], worker=self.worker, epoch=epoch, result={"output": output},
                             node_version=node["state_version"], node_result=output)
        return True

    def _consume(self, run: dict, row: dict, guard) -> dict | None:
        """Deliver one effect to its deduplicating consumer; None when the guard refused. / 把效果投递给去重消费者。"""
        payload, key = row["payload"], row["idempotency_key"]
        if row["kind"] == Activity.SUBMIT_MISSION.value:
            view = self.service.submit_workflow(run_id=run["run_id"], key=key, project_id=payload["project_id"],
                                                robot_id=payload["robot_id"], volume_id=payload["volume_id"],
                                                asset_id=payload["asset_id"], template=payload["template"],
                                                guard=guard)
            return None if view is None else MissionOutput(mission_id=view["mission"]["mission_id"]).model_dump(
                mode="json")
        if row["kind"] == Activity.CREATE_WORK_ORDER.value:
            order, _ = self.store.create_order(idempotency_key=key, project_id=payload["project_id"],
                                               run_id=run["run_id"], asset_id=payload["asset_id"],
                                               review_id=payload["review_id"], analysis_id=payload["analysis_id"],
                                               body={"finding": payload["finding"], "evidence": payload["evidence"]},
                                               actor=run_principal(run["run_id"]), guard=guard)
            return None if order is None else OrderOutput(order_id=order["order_id"]).model_dump(mode="json")
        child = self._start_reinspection(run, payload, guard)
        return None if child is None else ReinspectionOutput(order_id=payload["order_id"], run_id=child).model_dump(
            mode="json")

    def _start_reinspection(self, run: dict, payload: dict, guard) -> str | None:
        """One reinspection run per order, pinned to the parent's catalog. / 每张工单一个复检运行，固定父运行的目录。"""
        target = self.store.pinned(run["catalog_sha256"]).spec(run["project_id"], payload["workflow_id"],
                                                               payload["version"])
        trigger = next(t for t in target.triggers if isinstance(t, InternalTrigger))
        source = f"internal:{payload['order_id']}"
        parent = run_principal(run["run_id"])
        with self.store.transaction() as db:
            known = self.store.trigger(target.project_id, target.workflow_id, target.version, source, "reinspection")
            if known is not None:
                return known["run_id"]
            if not guard(db):
                return None
            inputs = target.check_inputs({payload["asset_input"]: payload["asset_id"]})
            child, _ = self.store.start_run(target, catalog_sha256=run["catalog_sha256"], source=source,
                                            event_id="reinspection", actor=parent, started_by=parent, inputs=inputs,
                                            body={"trigger_id": trigger.trigger_id, "order_id": payload["order_id"],
                                                  "feedback_id": payload["feedback_id"],
                                                  "parent_run": run["run_id"]})
            self.store.link_reinspection(payload["order_id"], child, parent)
        return child

    def _reconcile(self, run: dict, row: dict) -> dict | None:
        """What a claimed effect actually produced, looked up by its key; None when nothing happened.

        按键查询已领取效果实际产生了什么；什么都没发生时为 None。
        """
        key, payload = row["idempotency_key"], row["payload"]
        if row["kind"] == Activity.SUBMIT_MISSION.value:
            mission_id = self.ledger.request_by_key(run_principal(run["run_id"]), key)
            return MissionOutput(mission_id=mission_id).model_dump(mode="json") if mission_id else None
        if row["kind"] == Activity.CREATE_WORK_ORDER.value:
            order = self.store.order_by_key(key)
            return OrderOutput(order_id=order["order_id"]).model_dump(mode="json") if order else None
        target = self.store.pinned(run["catalog_sha256"]).spec(run["project_id"], payload["workflow_id"],
                                                               payload["version"])
        known = self.store.trigger(target.project_id, target.workflow_id, target.version,
                                   f"internal:{payload['order_id']}", "reinspection")
        return ReinspectionOutput(order_id=payload["order_id"], run_id=known["run_id"]).model_dump(mode="json") \
            if known else None

    # ── waits / 等待 ──

    def _poll(self, run, spec, node_spec, node, nodes, epoch) -> bool:
        outputs = self._outputs(nodes)
        activity = Activity(node_spec.activity)
        deadline = datetime.fromisoformat(node["deadline_at"]) if node["deadline_at"] else None
        state, reason, detail, result = NodeState.WAITING, node["reason"], node["detail"], None
        if activity is Activity.AWAIT_MISSION:
            state, reason, detail, result = self._await(run, spec, node_spec, outputs)
        elif activity is Activity.HUMAN_REVIEW:
            review = self.store.review(activity_key(run["run_id"], node_spec.node_id))
            if review is not None:
                state, result = NodeState.COMPLETED, ReviewOutput(review_id=review["review_id"],
                                                                  decision=review["decision"],
                                                                  reviewer=review["reviewer"]).model_dump(mode="json")
            elif deadline is not None and self.clock() >= deadline:
                state, reason = NodeState.FAILED, "review.timeout"
        elif activity is Activity.AWAIT_REPAIR:
            order = self.store.order(outputs[node_spec.params.order_from]["order_id"])
            feedback = order["feedback"]
            if feedback is not None:
                state, result = NodeState.COMPLETED, RepairOutput(order_id=order["order_id"],
                                                                  feedback_id=feedback["feedback_id"],
                                                                  reported_at=feedback["reported_at"]).model_dump(
                    mode="json")
            elif deadline is not None and self.clock() >= deadline:
                state, reason = NodeState.FAILED, "repair.timeout"
        if state is NodeState.WAITING and (reason, detail) == (node["reason"], node["detail"]):
            return False
        self._move(run, node, epoch, state, reason=reason if state is not NodeState.COMPLETED else None,
                   detail=detail, result=result, deadline=deadline if state is NodeState.WAITING else None)
        return True

    def _mission_wait(self, mission: dict) -> tuple[WaitReason, dict]:
        """Why a mission is not settled yet, in the reasons operators see (09-operations §4.1).

        任务为何尚未了结，以操作者看到的原因表示（09-operations §4.1）。
        """
        status = mission["status"]
        detail = {"mission_id": mission["mission_id"], "status": status}
        if status in ("awaiting_approval", "approving"):
            return WaitReason.APPROVAL, detail
        if status in ("delivered", "running"):
            return WaitReason.FLIGHT, detail
        if status == "verifying":
            return WaitReason.EVIDENCE, detail
        if status in ("approved", "queued"):
            decisions = [d for d in self.ops.store.decisions(mission["mission_id"], 10)
                         if d["stage"] in ("prepare", "claim")]
            reasons = list(decisions[-1]["body"]["reasons"]) if decisions else []
            detail["reasons"] = reasons
            if any(code.startswith("environment.") for code in reasons):
                return WaitReason.ENVIRONMENT, detail
            return WaitReason.DEVICE, detail
        return WaitReason.DELIVERY, detail

    def _await(self, run, spec, node_spec: AwaitMissionNode, outputs) -> tuple:
        """(state, reason, detail, output): only a service-verified inspection of a settled mission completes it.

        （状态，原因，细节，输出）：只有已了结任务的服务复核通过巡检才使其完成。
        """
        mission_id = outputs[node_spec.params.mission_from]["mission_id"]
        asset = self._asset(run, spec.node(node_spec.params.mission_from))
        mission = self.ledger.mission(mission_id)
        if mission["status"] not in MISSION_TERMINAL:
            reason, detail = self._mission_wait(mission)
            return NodeState.WAITING, reason.value, detail, None
        holds = [r for r in self.ops.store.reservations(mission_id=mission_id) if r.state.value in HOLDING]
        detail = {"mission_id": mission_id, "status": mission["status"]}
        if holds:
            # Settled means reconciled on the ground too (D055): the next dispatch must not race the release.
            # 了结也包括地面对账（D055）：下一次派遣不能与释放赛跑。
            return NodeState.WAITING, WaitReason.DEVICE.value, {
                **detail, "reservations": [{"activity": h.activity_key, "state": h.state.value, "reason": h.reason}
                                           for h in holds]}, None
        report = self.ledger.report(mission_id) or {}
        column = (report.get("targets") or {}).get(asset)
        detail["column"] = column
        if mission["status"] == "completed" and column == "completed":
            evidence = self._verified_evidence(mission_id, asset)
            if evidence is not None:
                return NodeState.COMPLETED, None, detail, InspectionOutput(
                    mission_id=mission_id, mission_status="completed", mission_version=evidence["mission_version"],
                    asset_id=asset, inspection="verified", evidence_id=evidence["evidence_id"],
                    evidence_sha256=evidence["sha256"],
                    captured_at=evidence["body"]["time_window"]["timestamp"]).model_dump(mode="json")
            return NodeState.OUTCOME_UNKNOWN, "inspection.evidence_missing", detail, None
        if column == "uncertain" or mission["status"] == "completed":
            return NodeState.OUTCOME_UNKNOWN, "inspection.uncertain", detail, None
        return NodeState.FAILED, f"mission.{mission['status']}", detail, None

    def _verified_evidence(self, mission_id: str, asset: str) -> dict | None:
        """The newest acquisition of the asset whose service verification is verified. / 服务复核为已证实的该资产最新采集。"""
        verdicts = {v["evidence_id"]: v["body"]["final_verdict"] for v in self.ledger.verifications(mission_id)}
        found = None
        for row in self.ledger.evidence(mission_id):
            if asset in row["body"].get("subject_ids", []) and row["media_path"] \
                    and verdicts.get(row["evidence_id"]) == "verified":
                if found is None or row["mission_version"] >= found["mission_version"]:
                    found = row
        return found

    # ── analysis and report / 分析与报告 ──

    def _fixture(self, path: str, digest: str) -> AnalysisFixture | None:
        """The pinned fixture, or None if the file no longer has the pinned digest. / 固定夹具；文件摘要变化时为 None。"""
        if digest not in self._fixtures:
            raw = (self.root / path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                return None
            self._fixtures[digest] = AnalysisFixture.model_validate(yaml.safe_load(raw))
        return self._fixtures[digest]

    def _analyze(self, run, node_spec, node, outputs, epoch) -> None:
        inspection = outputs[node_spec.params.inspection_from]
        name = node_spec.params.analyzer
        analyzer = self.store.pinned(run["catalog_sha256"]).analyzers[name]
        digest = self.store.fixtures_of(run["catalog_sha256"]).get(name, "")
        row = next((r for r in self.ledger.evidence(inspection["mission_id"])
                    if r["evidence_id"] == inspection["evidence_id"]), None)
        media = self.service.hub.media(row["media_path"]) if row and row["media_path"] else None
        robot = self.ledger.mission(inspection["mission_id"])["robot_id"]
        asset = self.service._registry(robot).data["assets"].get(inspection["asset_id"], {})
        fixture = self._fixture(analyzer.fixture, digest) if isinstance(analyzer, ScriptedAnalyzer) else None
        if isinstance(analyzer, ScriptedAnalyzer) and fixture is None:
            result = AnalysisResult(analyzer=name, source="scripted", verdict="refused", confidence=0.0,
                                    reasons=("analysis.fixture_changed",), asset_id=inspection["asset_id"],
                                    evidence_id=inspection["evidence_id"],
                                    input_sha256=inspection["evidence_sha256"], fixture_sha256=digest)
        else:
            result = analyze(name, analyzer, asset_id=inspection["asset_id"], evidence_id=inspection["evidence_id"],
                             evidence_sha256=inspection["evidence_sha256"], media=media,
                             width=row["width"] if row else None, height=row["height"] if row else None, asset=asset,
                             fixture=fixture, fixture_sha256=digest)
        key = activity_key(run["run_id"], node_spec.node_id)
        with self.store.transaction():
            self.store.fence(run["run_id"], self.worker, epoch)
            record = self.store.record_analysis(
                activity_key=key, project_id=run["project_id"], run_id=run["run_id"],
                mission_id=inspection["mission_id"], mission_version=inspection["mission_version"],
                evidence_id=inspection["evidence_id"], asset_id=inspection["asset_id"], analyzer=name,
                source=result.source, verdict=result.verdict, body=result.model_dump(mode="json"))
            # The same record joins the mission version's immutable sources (D054). / 同一记录进入任务版本的不可变来源（D054）。
            self.ledger.record_source(inspection["mission_id"], inspection["mission_version"],
                                      f"analysis:{record['analysis_id']}", AnalysisOrigin(
                                          attempt_id=record["analysis_id"], evidence_id=inspection["evidence_id"],
                                          timestamp=datetime.fromisoformat(record["created_at"]),
                                          use=ModelUse(source=result.source, provider_id="workflow-analysis",
                                                       model_id=name, prompt_version=analyzer.kind,
                                                       input_sha256=inspection["evidence_sha256"],
                                                       recording_sha256=digest, outcome=result.verdict)))
            if result.verdict == "refused":
                self._move(run, node, epoch, NodeState.FAILED, reason=result.reasons[0],
                           detail=result.model_dump(mode="json"))
            else:
                self._move(run, node, epoch, NodeState.COMPLETED, result=AnalysisOutput(
                    analysis_id=record["analysis_id"], verdict=result.verdict,
                    suspected=result.verdict == "suspected", confidence=result.confidence,
                    source=result.source).model_dump(mode="json"))
        self.touched.add(inspection["mission_id"])

    def _report(self, run: dict, spec: WorkflowSpec, nodes: dict) -> dict:
        """The run's business report; a target is `verified` only if its await node completed (D057 §8).

        运行的业务报告；目标只有在其等待节点完成时才是 `verified`（D057 §8）。
        """
        inspections = []
        for node_spec in spec.nodes:
            if not isinstance(node_spec, AwaitMissionNode):
                continue
            node = nodes[node_spec.node_id]
            submitted = nodes[node_spec.params.mission_from]["result"] or {}
            mission_id = submitted.get("mission_id")
            mission = self.ledger.mission(mission_id) if mission_id else None
            report = (self.ledger.report(mission_id) if mission_id else None) or {}
            asset = self._asset(run, spec.node(node_spec.params.mission_from))
            inspections.append({
                "node_id": node_spec.node_id, "asset_id": asset, "mission_id": mission_id,
                "mission_status": mission["status"] if mission else None,
                "flight_column": (report.get("targets") or {}).get(asset),
                "inspection": {"completed": "verified", "failed": "not_completed",
                               "outcome_unknown": "unknown"}.get(node["state"], node["state"]),
                "reason": node["reason"], "evidence_id": (node["result"] or {}).get("evidence_id")})
        return {"format": "drone.workflow-report/v1", "run_id": run["run_id"], "workflow_id": spec.workflow_id,
                "version": spec.version, "generated_at": self.clock().isoformat(), "inspections": inspections,
                "analyses": [{"analysis_id": a["analysis_id"], "asset_id": a["asset_id"], "analyzer": a["analyzer"],
                              "source": a["source"], "verdict": a["verdict"],
                              "confidence": a["body"].get("confidence")} for a in self.store.analyses(run["run_id"])],
                "reviews": [{"review_id": r["review_id"], "decision": r["decision"], "reviewer": r["reviewer"]}
                            for r in self.store.reviews(run["run_id"])],
                "orders": [{"order_id": o["order_id"], "asset_id": o["asset_id"], "state": o["state"],
                            "reinspection_run": o["reinspection_run"]} for o in self.store.run_orders(run["run_id"])],
                "reinspections": [{"run_id": c["run_id"], "workflow_id": c["workflow_id"], "state": c["state"]}
                                  for c in self.store.children(run["run_id"])],
                "nodes": {node_id: {"state": node["state"], "reason": node["reason"]}
                          for node_id, node in nodes.items()},
                "note": "scripted and deterministic analyses are labelled test backends, not model findings"}

    # ── run state and cancellation / 运行状态与取消 ──

    @staticmethod
    def outcome(nodes: dict) -> dict:
        """completed only if every node completed or was skipped by a false condition (D057 §8).

        只有每个节点都完成或因条件为假被跳过才是 completed（D057 §8）。
        """
        states = {node_id: (node["state"], node["reason"]) for node_id, node in nodes.items()}
        unknown = [n for n, (s, r) in states.items() if s == "outcome_unknown" or r == SkipReason.UPSTREAM_UNKNOWN]
        failed = [n for n, (s, r) in states.items()
                  if s in ("failed", "cancelled") or (s == "skipped" and r == SkipReason.UPSTREAM_FAILED)]
        result = "outcome_unknown" if unknown else "failed" if failed else "completed"
        return {"result": result, "unknown": sorted(unknown), "failed": sorted(failed),
                "counts": {state: sum(1 for s, _ in states.values() if s == state)
                           for state in sorted({s for s, _ in states.values()})}}

    def _derive(self, run: dict, epoch: int) -> None:
        nodes = self.store.nodes(run["run_id"])
        states = [NodeState(node["state"]) for node in nodes.values()]
        if all(state in TERMINAL_NODE for state in states):
            summary = self.outcome(nodes)
            self.store.set_run_state(run["run_id"], worker=self.worker, epoch=epoch,
                                     state=RunState(summary["result"]), outcome=summary)
            return
        target = RunState.WAITING if NodeState.RUNNING not in states and NodeState.WAITING in states \
            else RunState.RUNNING
        self.store.set_run_state(run["run_id"], worker=self.worker, epoch=epoch, state=target)

    def _cancelling(self, run: dict, spec: WorkflowSpec, epoch: int) -> None:
        """Reconcile claimed effects, cancel what never started, and wait for missions and child runs to settle.

        对已领取的效果对账，取消从未开始的部分，并等待任务与子运行了结。
        """
        run_id = run["run_id"]
        if run["state"] == RunState.CANCEL_REQUESTED.value:
            self.store.set_run_state(run_id, worker=self.worker, epoch=epoch, state=RunState.CANCELLING,
                                     allowed_from=(RunState.CANCEL_REQUESTED,))
        nodes = self.store.nodes(run_id)
        for row in self.store.outbox(run_id, states=("pending", "claimed")):
            found = self._reconcile(run, row) if row["state"] == "claimed" else None
            if found is not None:
                self.store.delivered(row["outbox_id"], worker=self.worker, epoch=epoch,
                                     result={"output": found, "reconciled_after_cancel": True},
                                     node_version=nodes[row["node_id"]]["state_version"], node_result=found)
            else:
                self.store.settle_outbox(row["outbox_id"], worker=self.worker, epoch=epoch, state="void",
                                         result={"reason": "cancelled"})
            nodes = self.store.nodes(run_id)
        for node_id, node in nodes.items():
            state = NodeState(node["state"])
            if state in TERMINAL_NODE:
                continue
            node_spec = spec.node(node_id)
            if state is NodeState.WAITING and isinstance(node_spec, AwaitMissionNode):
                settled, reason, detail, _ = self._await(run, spec, node_spec, self._outputs(nodes))
                if settled is NodeState.WAITING:
                    if (reason, detail) != (node["reason"], node["detail"]):
                        self._move(run, node, epoch, NodeState.WAITING, reason=reason, detail=detail)
                    continue
                # A result that arrives after the cancel is recorded, never acted upon. / 取消后到达的结果只记录，不据此行动。
                self._move(run, node, epoch, NodeState.CANCELLED, reason="cancelled",
                           detail={"late_result": {"state": settled.value, "reason": reason, **(detail or {})}})
                continue
            self._move(run, node, epoch, NodeState.CANCELLED, reason="cancelled")
        unsettled = []
        for mission in self.store.child_missions(run_id):
            holds = [r.state.value for r in self.ops.store.reservations(mission_id=mission["mission_id"])
                     if r.state.value in HOLDING]
            if mission["status"] not in MISSION_TERMINAL or holds:
                unsettled.append(mission["mission_id"])
        children = [c["run_id"] for c in self.store.children(run_id) if RunState(c["state"]) not in TERMINAL_RUN]
        nodes = self.store.nodes(run_id)
        if not unsettled and not children and all(NodeState(n["state"]) in TERMINAL_NODE for n in nodes.values()):
            self.store.set_run_state(run_id, worker=self.worker, epoch=epoch, state=RunState.CANCELLED,
                                     outcome={**self.outcome(nodes), "result": "cancelled"},
                                     allowed_from=(RunState.CANCELLING,))

    # ── views / 视图 ──

    def list_view(self, caller: Caller | None, project_id) -> dict:
        """Templates, schedules, recent runs and orders of one project. / 一个项目的模板、排班、近期运行与工单。"""
        self._require(caller, project_id, WORKFLOW_READ)
        schedules = {(r["workflow_id"], r["trigger_id"]): r for r in self.store.schedules(project_id)}
        templates = []
        for spec in self.catalog.project_workflows(project_id):
            triggers = []
            for trigger in spec.triggers:
                item = {"trigger_id": trigger.trigger_id, "kind": trigger.kind}
                if isinstance(trigger, ScheduleTrigger):
                    item["schedule"] = trigger.schedule.model_dump(mode="json")
                    row = schedules.get((spec.workflow_id, trigger.trigger_id))
                    item["state"] = self._schedule_view(row) if row else {"state": "disabled"}
                if isinstance(trigger, EventTrigger):
                    item.update(source=trigger.source, event_type=trigger.event_type)
                triggers.append(item)
            templates.append({"workflow_id": spec.workflow_id, "version": spec.version, "title": spec.title,
                              "sha256": spec.sha256, "triggers": triggers,
                              "inputs": {name: value.model_dump(mode="json") for name, value in spec.inputs.items()},
                              "nodes": [{"node_id": n.node_id, "activity": n.activity} for n in spec.nodes]})
        return {"project_id": project_id, "catalog": {"catalog_id": self.catalog.catalog_id,
                                                      "sha256": self.catalog.sha256},
                "roles": sorted(r.value for r in self.ops.directory.roles(caller, project_id)),
                "templates": templates, "runs": [self._summary(r) for r in self.store.runs(project_id, 30)],
                "orders": [self._order_view(o) for o in self.store.orders(project_id, 30)]}

    def run_view(self, caller: Caller | None, project_id, run_id) -> dict:
        return self._view(self._run(caller, project_id, run_id, WORKFLOW_READ))

    def _summary(self, run: dict) -> dict:
        nodes = self.store.nodes(run["run_id"])
        return {"run_id": run["run_id"], "workflow_id": run["workflow_id"], "version": run["version"],
                "state": run["state"], "trigger_source": run["trigger_source"], "created_at": run["created_at"],
                "updated_at": run["updated_at"],
                "waiting": sorted({n["reason"] for n in nodes.values()
                                   if n["state"] == NodeState.WAITING.value and n["reason"]})}

    def _schedule_view(self, row: dict) -> dict:
        return {k: row[k] for k in ("project_id", "workflow_id", "trigger_id", "version", "state", "cursor_at",
                                    "changed_by", "changed_at", "reason")}

    def _order_view(self, order: dict) -> dict:
        return {k: order[k] for k in ("order_id", "project_id", "run_id", "asset_id", "review_id", "analysis_id",
                                      "state", "body", "feedback", "reinspection_run", "created_at", "updated_at")}

    def _view(self, run: dict) -> dict:
        spec = self.store.spec_of(run)
        nodes = self.store.nodes(run["run_id"])
        missions = []
        for mission in self.store.child_missions(run["run_id"]):
            node_id = mission["idempotency_key"].split(":")[2]
            holds = [{"state": r.state.value, "reason": r.reason}
                     for r in self.ops.store.reservations(mission_id=mission["mission_id"])]
            missions.append({"mission_id": mission["mission_id"], "node_id": node_id, "status": mission["status"],
                             "reservations": holds})
        return {"run": {k: run[k] for k in ("run_id", "project_id", "workflow_id", "version", "spec_sha256",
                                            "catalog_sha256", "trigger_source", "trigger_event", "started_by",
                                            "inputs", "state", "state_version", "cancel_epoch", "cancel", "owner",
                                            "owner_epoch", "outcome", "created_at", "updated_at")},
                "template": {"title": spec.title, "order": spec.order(),
                             "dependencies": {n.node_id: sorted(spec.dependencies(n.node_id)) for n in spec.nodes}},
                "nodes": [{"node_id": node_id, "activity": node["activity"], "state": node["state"],
                           "reason": node["reason"], "detail": node["detail"], "result": node["result"],
                           "deadline_at": node["deadline_at"], "started_at": node["started_at"],
                           "finished_at": node["finished_at"]} for node_id, node in nodes.items()],
                "waiting": sorted({n["reason"] for n in nodes.values()
                                   if n["state"] == NodeState.WAITING.value and n["reason"]}),
                "missions": missions,
                "analyses": [{k: a[k] for k in ("analysis_id", "asset_id", "analyzer", "source", "verdict",
                                                "mission_id", "mission_version", "evidence_id", "body")}
                             for a in self.store.analyses(run["run_id"])],
                "reviews": self.store.reviews(run["run_id"]),
                "orders": [self._order_view(o) for o in self.store.run_orders(run["run_id"])],
                "children": [self._summary(c) for c in self.store.children(run["run_id"])],
                "outbox": [{k: r[k] for k in ("node_id", "kind", "state", "attempts", "result")}
                           for r in self.store.outbox(run["run_id"])],
                "events": self.store.events(f"workflow:{run['run_id']}", 80)}
