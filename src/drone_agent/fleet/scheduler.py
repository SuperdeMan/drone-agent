"""P3 scheduler: tasks, deterministic assignment, withdrawal before the claim, settlement and task-level relay (D059).

The scheduler lives in the mission-service process and only uses the service: it never signs or approves anything and
never talks to a robot. Each pass settles tasks whose mission ended and was reconciled on the ground, withdraws an
unclaimed assignment whose robot stayed blocked while another candidate is ready, and serves the queue in priority
order: it takes one snapshot per task, applies the pure `decide`, records the decision when its conclusion changed,
and for an assignment writes the assignment row, the robot's mission (deterministic draft, compilation, admission) and
the holds of the robot and its airspace cells in one transaction that first re-judges the chosen robot on the current
state. Every mission still waits for a human approval (D032); a claimed flight is never reassigned, only reconciled.

P3 调度器：任务、确定性分配、领取前撤回、结算与任务级接力（D059）。

调度器位于任务服务进程内，只使用服务：它从不签名或批准任何东西，也从不与机器人通信。每一轮：结算任务已结束并在地面完成
对账的任务；当某个未领取分配的机器人持续被阻断且另有候选就绪时撤回该分配；按优先级顺序处理队列——每个任务取一个快照，
应用纯函数 `decide`，结论变化时记录判定；分配时在一个事务中写入分配行、该机器人的任务（确定性草案、编译、准入）以及
机器人与其空域单元的持有，该事务首先按当前状态重判所选机器人。每个任务仍需人工审批（D032）；已领取的飞行从不改派，
只做对账。
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.admission.compiler import FRAMEWORK, INSPECT
from drone_agent.contracts import utcnow
from drone_agent.fleet.coordinator import candidate_reasons, decide, same_decision
from drone_agent.fleet.dispatch import needs_of
from drone_agent.fleet.reservation import Airspace, is_cell
from drone_agent.fleet.resources import Stage, Verdict, activity_key
from drone_agent.fleet.scheduling_models import (
    CANCELLING_TASK,
    TERMINAL_TASK,
    AssignmentState,
    SchedulingCatalog,
    TaskState,
    TaskVerdict,
    load_scheduling,
    task_key_ok,
)
from drone_agent.fleet.scheduling_store import SchedulingStore, StaleTask, migrate
from drone_agent.fleet.service import ServiceError
from drone_agent.runtime.issues import issue
from drone_agent.runtime.permission import MISSION_OPERATE, MISSION_READ, MISSION_SUBMIT, Caller

SCHEDULER = "scheduler"
HOLDING = ("reserved", "occupied", "uncertain")
# Mission statuses after which a mission is settled for its task. / 对任务而言任务已了结的状态。
MISSION_TERMINAL = ("completed", "incomplete", "declined", "rejected", "refused", "planning_failed",
                    "delivery_rejected", "cancelled", "dispatch_expired")
UNCLAIMED = ("awaiting_approval", "approving", "approved", "queued")
ASSET = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
# Refusals of a mission for one robot that retrying cannot change (configuration, the robot's site map).
# 重试也改变不了的、针对某台机器人的任务拒绝（配置、该机站点地图）。
PERMANENT_REFUSALS = frozenset({"dispatch.backend_mismatch", "workflow.invalid_draft", "service.not_found"})


@dataclass
class Scheduling:
    """Everything the service needs for P3. / 服务运行 P3 所需的全部内容。"""

    catalog: SchedulingCatalog
    store: SchedulingStore
    migration: dict = field(default_factory=dict)


def build_scheduling(ledger, path: Path, operations, *, backups: Path | None, clock=utcnow) -> Scheduling:
    """Load and check the scheduling catalog, migrate the ledger (D060) and open the store.

    加载并核对调度目录、迁移账本（D060）并打开存储。
    """
    catalog = load_scheduling(path)
    catalog.check(operations.catalog, operations.registries)
    migration = migrate(ledger, backups=backups, clock=clock)
    store = SchedulingStore(ledger, catalog, clock=clock)
    return Scheduling(catalog, store, migration)


def verified_evidence(ledger, mission_id: str, asset: str) -> dict | None:
    """The newest acquisition of the asset whose service verification is verified. / 服务复核为已证实的该资产最新采集。"""
    verdicts = {v["evidence_id"]: v["body"]["final_verdict"] for v in ledger.verifications(mission_id)}
    found = None
    for row in ledger.evidence(mission_id):
        if asset in row["body"].get("subject_ids", []) and row["media_path"] \
                and verdicts.get(row["evidence_id"]) == "verified":
            if found is None or row["mission_version"] >= found["mission_version"]:
                found = row
    return found


class Scheduler:
    """The P3 runner inside the mission service; `tick()` is its background pass. / 任务服务内的 P3 执行器；`tick()` 是后台处理。"""

    def __init__(self, service, scheduling: Scheduling, *, worker_id: str | None = None):
        if service.ops is None or service.dispatch is None:
            raise ValueError("the scheduler needs the operations catalog")
        self.service, self.catalog, self.store = service, scheduling.catalog, scheduling.store
        self.worker = worker_id or "sch-" + uuid.uuid4().hex[:10]
        self.airspace = Airspace(self.catalog, service.ops, service.catalog, self.store, clock=service.clock)
        service.dispatch.airspace = self.airspace
        service.dispatch.assignment_problem = self.assignment_problem
        if getattr(service, "workflows", None) is not None:
            # A run's cancel reaches its tasks in the same transaction (D059 §8). / 运行取消在同一事务到达其任务单。
            service.workflows.store.cancel_hooks.append(self.cancel_run)
        self.touched: set[str] = set()

    @property
    def ops(self):
        return self.service.ops

    @property
    def ledger(self):
        return self.service.ledger

    def clock(self) -> datetime:
        return self.service.clock()

    # ── permissions / 权限 ──

    def _require(self, caller: Caller | None, project_id, scope: str) -> None:
        directory = self.ops.directory
        if caller is None or not isinstance(project_id, str) or project_id not in self.ops.catalog.projects \
                or not directory.allows(caller, project_id, MISSION_READ):
            raise ServiceError("service.not_found", str(project_id)[:40])
        if scope != MISSION_READ and not directory.allows(caller, project_id, scope):
            raise ServiceError("auth.project_denied", f"{scope} in {project_id}")

    def _task(self, caller: Caller | None, project_id, task_id, scope: str) -> dict:
        self._require(caller, project_id, MISSION_READ)
        task = self.store.task(task_id) if isinstance(task_id, str) else None
        if task is None or task["project_id"] != project_id:
            raise ServiceError("service.not_found", str(task_id)[:40])
        self._require(caller, project_id, scope)
        return task

    def project_robots(self, project_id: str) -> list[str]:
        catalog = self.ops.catalog
        return sorted(r for r in catalog.robots if catalog.project_of(r) == project_id)

    # ── submission / 提交 ──

    def submit(self, caller: Caller | None, project_id, asset_id, volume_id, candidates, priority,
               idempotency_key) -> dict:
        """An operator's task; a repeated key returns the first task. / 操作者的任务；重复的键返回第一次的任务。"""
        self._require(caller, project_id, MISSION_SUBMIT)
        task, created = self.submit_internal(requested_by=caller.identity, key=idempotency_key, project_id=project_id,
                                             asset_id=asset_id, volume_id=volume_id, candidates=candidates,
                                             priority=priority, source="api")
        return self._task_view(task)

    def submit_internal(self, *, requested_by: str, key, project_id: str, asset_id, volume_id, candidates, priority,
                        source: str, guard=None) -> tuple[dict | None, bool]:
        """Validate and create a task; the workflow engine calls this for assignment-mode nodes (its own authority
        check already ran). None when `guard` refuses inside the transaction.

        校验并创建任务；工作流引擎为分配模式节点调用它（其自身的授权检查已执行）。`guard` 在事务内拒绝时返回 None。
        """
        if not isinstance(asset_id, str) or not ASSET.fullmatch(asset_id):
            raise ServiceError("task.invalid_request", "asset_id must be a registered asset id")
        if not isinstance(volume_id, str) or not ASSET.fullmatch(volume_id):
            raise ServiceError("task.invalid_request", "volume_id must be a registered volume id")
        if not isinstance(candidates, list) or any(not isinstance(c, str) for c in candidates) \
                or len(candidates) != len(set(candidates)) or len(candidates) > 50:
            raise ServiceError("task.invalid_request", "candidates must be distinct robot ids")
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 9:
            raise ServiceError("task.invalid_request", "priority must be an integer 0-9")
        if not task_key_ok(key):
            raise ServiceError("task.invalid_request", "idempotency_key must be 1-160 safe characters")
        robots = self.project_robots(project_id)
        outside = sorted(set(candidates) - set(robots))
        if outside:
            raise ServiceError("task.candidate_outside_project", ", ".join(outside)[:200])
        not_after = (self.clock() + timedelta(seconds=self.catalog.policy.task_window_s)).isoformat()
        task, created = self.store.create_task(requested_by=requested_by, key=key, project_id=project_id,
                                               asset_id=asset_id, volume_id=volume_id, candidates=sorted(candidates),
                                               priority=priority, not_after=not_after, source=source, guard=guard)
        if task is not None and not created and (task["project_id"], task["asset_id"], task["volume_id"],
                                                 task["candidates"], task["priority"]) != (
                project_id, asset_id, volume_id, sorted(candidates), priority):
            raise ServiceError("service.idempotency_conflict", "the key was used for another task")
        return task, created

    # ── snapshots and decisions / 快照与判定 ──

    def _candidates(self, task: dict) -> list[str]:
        robots = self.project_robots(task["project_id"])
        return [r for r in (task["candidates"] or robots) if r in robots]

    def _candidate(self, task: dict, robot_id: str, now: datetime, ignore: set[str], envelopes: dict) -> dict:
        catalog, service = self.ops.catalog, self.service
        entry = catalog.robots[robot_id]
        data = self.ops.registry(robot_id).data
        volume = data.get("volumes", {}).get(task["volume_id"])
        volume_registered = volume is not None and volume.get("airspace_mode") == "simulation"
        asset = data.get("assets", {}).get(task["asset_id"])
        asset_registered = asset is not None and asset.get("volume") == task["volume_id"] \
            and asset.get("observation_route") in data.get("routes", {})
        fp = self.airspace.task_footprint(robot_id, task["asset_id"], task["volume_id"]) \
            if asset_registered and volume_registered else None
        cells = list(fp.cells) if fp else []
        capability = service.catalog.capability(robot_id)[0]
        status = service.catalog.status(robot_id)
        dock = self.ops.store.dock_status(entry.dock_id)
        holders = {r: a for r, a in self.ops.store.holders(catalog.resources(robot_id)).items() if a not in ignore}
        budget = data.get("mission_defaults", {}).get("energy_budget") or {}
        energy = min(1.0, float(budget["max_consumption_fraction"]) + float(budget.get("reserve_fraction", 0.0))) \
            if "max_consumption_fraction" in budget else None
        origin = self.catalog.origin(entry.site_id)
        home = data["home"]["position"]
        window = timedelta(seconds=self.catalog.ranking.usage_window_s)
        return {"robot_id": robot_id, "site_id": entry.site_id, "dock_id": entry.dock_id,
                "volume_registered": volume_registered, "asset_registered": asset_registered,
                "pad": [origin[0] + float(home[0]), origin[1] + float(home[1])],
                "asset": [origin[0] + float(asset["position"][0]), origin[1] + float(asset["position"][1])]
                if asset else None,
                "capability": capability.model_dump(mode="json") if capability else None,
                "robot_status": status.model_dump(mode="json") if status else None,
                "dock": dock.model_dump(mode="json") if dock else None, "holders": holders,
                "needs": {"skills": sorted({*FRAMEWORK, INSPECT}), "energy_fraction": energy,
                          "not_after": task["not_after"]},
                "cells": cells, "held": self.airspace.held(cells, exclude=ignore),
                "envelope": {cell: envelopes[cell] for cell in cells if cell in envelopes},
                "usage": self.store.usage(robot_id, (now - window).isoformat())}

    def snapshot(self, task: dict, *, only: set[str] | None = None, skip: set[str] | None = None,
                 ignore: set[str] | None = None) -> dict:
        """Everything `decide` reads for one task, as plain data. / `decide` 为一个任务读取的全部内容，均为普通数据。

        `ignore` drops holds of the given activities (the task's own current assignment when weighing a withdrawal).
        `ignore` 去掉给定活动的持有（权衡撤回时去掉任务自己当前分配的持有）。
        """
        now = self.clock()
        ignore = set(ignore or ())
        candidates = [r for r in self._candidates(task) if (only is None or r in only) and r not in (skip or ())]
        envelopes = self.airspace.envelopes(exclude=ignore)
        return {"now": now.isoformat(),
                "task": {k: task[k] for k in ("task_id", "project_id", "asset_id", "volume_id", "priority",
                                              "not_after")}
                | {"candidates": candidates, "excluded": sorted({e["robot_id"] for e in task["excluded"]})},
                "ranking": self.catalog.ranking.model_dump(mode="json"), "policy_version": self.catalog.policy.version,
                "catalog_sha256": self.ops.catalog.sha256, "scheduling_sha256": self.catalog.sha256,
                "candidates": [self._candidate(task, robot_id, now, ignore, envelopes)
                               for robot_id in sorted(candidates)]}

    def _decide(self, task: dict, **options) -> dict:
        snapshot = self.snapshot(task, **options)
        started = time.perf_counter()
        decision = decide(self.ops.catalog, snapshot)
        decision["compute_ms"] = round((time.perf_counter() - started) * 1000, 3)
        decision["snapshot"] = snapshot
        return decision

    def _record(self, task: dict, decision: dict) -> str:
        """Append a decision only when its conclusion changed; returns the decision it matches.

        只在结论变化时追加判定；返回与之一致的判定。
        """
        last = self.store.last_decision(task["task_id"])
        if last is not None and last["epoch"] == task["epoch"] + 1 and same_decision(last["body"], decision):
            return last["decision_id"]
        return self.store.record_decision(task["task_id"], decision, epoch=task["epoch"] + 1)

    # ── the background pass / 后台处理 ──

    def tick(self) -> set[str]:
        """Finish cancels, settle, withdraw, assign; returns missions to refresh. / 收尾取消、结算、撤回、分配。"""
        for step in (self.finish_cancels, self.settle, self.withdraw, self.assign):
            try:
                step()
            except StaleTask:
                continue
        touched, self.touched = self.touched, set()
        return touched

    def assign(self) -> None:
        for task in self.store.queue()[: self.catalog.policy.max_tasks_per_pass]:
            try:
                self._serve(task)
            except StaleTask:
                continue

    def _serve(self, task: dict) -> None:
        decision = self._decide(task)
        decision_id = self._record(task, decision)
        if decision["verdict"] == TaskVerdict.REJECT.value:
            reasons = sorted({code for c in decision["candidates"] for code in c["permanent"]}) \
                or [decision["reason"] or "task.no_candidates"]
            self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                   state=TaskState.REJECTED.value, reason=",".join(reasons)[:300],
                                   outcome={"result": "rejected", "decision_id": decision_id, "reasons": reasons})
        elif decision["verdict"] == TaskVerdict.ASSIGN.value:
            self._assign(task, decision, decision_id)

    def _assign(self, task: dict, decision: dict, decision_id: str) -> None:
        """One transaction: re-judge, assignment row, mission and holds; any conflict rolls it all back.

        一个事务：重判、分配行、任务与持有；任何冲突整体回滚。
        """
        robot, epoch = decision["robot_id"], task["epoch"] + 1
        refused: dict = {}

        def guard(_db) -> bool:
            current = self.store.task(task["task_id"])
            if current is None or current["state"] != TaskState.QUEUED.value or current["cancel"] is not None \
                    or current["state_version"] != task["state_version"]:
                refused["reason"] = "task_changed"
                return False
            snapshot = self.snapshot(current, only={robot})
            if not snapshot["candidates"]:
                refused["reason"] = "candidate_gone"
                return False
            verdict, reasons, _ = candidate_reasons(self.ops.catalog, snapshot["task"], snapshot["candidates"][0],
                                                    datetime.fromisoformat(snapshot["now"]))
            if verdict is not Verdict.ELIGIBLE:
                refused.update(reason="no_longer_eligible", codes=list(reasons))
                return False
            return True

        try:
            with self.store.transaction():
                view = self.service.submit_assigned(
                    principal=task["requested_by"], key=f"{task['idempotency_key']}#a{epoch}",
                    project_id=task["project_id"], robot_id=robot, volume_id=task["volume_id"],
                    asset_id=task["asset_id"], template=f"task:{task['task_id']}", guard=guard)
                if view is None:
                    return
                mission_id, status = view["mission"]["mission_id"], view["mission"]["status"]
                if status == "rejected":
                    # Admission refused this robot: exclude it for this task and serve the queue again.
                    # 准入拒绝了该机器人：本任务排除它，重新排队。
                    self.store.insert_assignment(task_id=task["task_id"], epoch=epoch, robot_id=robot,
                                                 mission_id=mission_id, decision_id=decision_id,
                                                 state=AssignmentState.ENDED.value, reason="admission_rejected")
                    self._requeue(task, epoch, exclude=(robot, "admission_rejected"), mission_id=mission_id)
                else:
                    self.store.insert_assignment(task_id=task["task_id"], epoch=epoch, robot_id=robot,
                                                 mission_id=mission_id, decision_id=decision_id)
                    self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                           state=TaskState.ASSIGNED.value, epoch=epoch, robot_id=robot,
                                           mission_id=mission_id, reason=None)
                self.touched.add(mission_id)
        except (ServiceError, sqlite3.IntegrityError, StaleTask) as error:
            refused["reason"] = getattr(getattr(error, "issue", None), "code", type(error).__name__)
        finally:
            if refused:
                self.store.event(f"task:{task['task_id']}", "assignment.refused", self.worker,
                                 {"robot_id": robot, "epoch": epoch, **refused})
        if refused.get("reason") in PERMANENT_REFUSALS:
            self._exclude(task, robot, refused["reason"])

    def _exclude(self, task: dict, robot: str, reason: str) -> None:
        """Exclude a robot that can never take this task, so the queue moves on instead of retrying every pass; no
        assignment happened, so the epoch stays. / 排除永远不能执行本任务的机器人，队列继续推进而不是每轮重试；没有发生
        分配，代次不变。"""
        current = self.store.task(task["task_id"])
        if current is None or current["state"] != TaskState.QUEUED.value or current["cancel"] is not None:
            return
        excluded = [*current["excluded"], {"robot_id": robot, "reason": reason, "epoch": current["epoch"],
                                           "mission_id": None}]
        try:
            self.store.update_task(task["task_id"], current["state_version"], actor=SCHEDULER, excluded=excluded,
                                   reason=reason)
        except StaleTask:
            return

    def _requeue(self, task: dict, epoch: int, *, exclude: tuple[str, str] | None, mission_id: str | None,
                 version: int | None = None) -> dict:
        """Back to the queue, or failed once the assignment budget is used. / 回到队列，或在分配预算用尽时失败。"""
        excluded = list(task["excluded"])
        if exclude is not None:
            excluded.append({"robot_id": exclude[0], "reason": exclude[1], "epoch": epoch, "mission_id": mission_id})
        exhausted = epoch >= self.catalog.policy.max_assignments
        values = {"epoch": epoch, "excluded": excluded, "robot_id": None, "mission_id": None}
        if exhausted:
            values.update(state=TaskState.FAILED.value, reason="task.assignments_exhausted",
                          outcome={"result": "failed", "reason": "assignments_exhausted", "epoch": epoch})
        else:
            values.update(state=TaskState.QUEUED.value, reason=exclude[1] if exclude else None)
        return self.store.update_task(task["task_id"], task["state_version"] if version is None else version,
                                      actor=SCHEDULER, **values)

    # ── settlement / 结算 ──

    def _holding(self, mission_id: str) -> list:
        return [r for r in self.ops.store.reservations(mission_id=mission_id) if r.state.value in HOLDING]

    def _claimed(self, mission_id: str) -> bool:
        return any(c["state"] == "claimed" for c in self.ops.store.claims(mission_id))

    def outcome(self, task: dict, mission: dict) -> tuple[str, dict]:
        """How a settled mission ends its task (D059 §7). / 已了结的任务如何结束其任务（D059 §7）。"""
        mission_id, status = mission["mission_id"], mission["status"]
        report = self.ledger.report(mission_id) or {}
        column = (report.get("targets") or {}).get(task["asset_id"])
        detail = {"mission_id": mission_id, "status": status, "column": column}
        if status == "completed":
            if column == "completed":
                evidence = verified_evidence(self.ledger, mission_id, task["asset_id"])
                if evidence is not None:
                    return "completed", {**detail, "mission_version": evidence["mission_version"],
                                         "evidence_id": evidence["evidence_id"], "evidence_sha256": evidence["sha256"],
                                         "captured_at": evidence["body"]["time_window"]["timestamp"]}
                return "unknown", {**detail, "reason": "inspection.evidence_missing"}
            return "unknown", {**detail, "reason": "inspection.uncertain"}
        if status == "incomplete":
            if column == "uncertain":
                return "unknown", {**detail, "reason": "inspection.uncertain"}
            return ("flight_failed" if self._claimed(mission_id) else "not_flown"), detail
        if status == "declined":
            return "declined", detail
        if status == "rejected":
            return "admission_rejected", detail
        if status == "cancelled":
            intent = self.ops.store.cancel_intent(mission_id) or {}
            if task["cancel"] is not None:
                return "cancelled", detail
            if intent.get("reason") == "assignment_withdrawn":
                return "withdrawn", detail
            return "operator_cancelled", detail
        if status in ("dispatch_expired", "delivery_rejected"):
            return "expired", detail
        return "unknown", {**detail, "reason": f"mission.{status}"}

    def settle(self) -> None:
        for task in self.store.tasks(states=(TaskState.ASSIGNED.value,)):
            live = self.store.live(task["task_id"])
            if live is None:
                # An assigned task always has a live assignment; say so rather than guess. / 已分配的任务总有有效分配。
                self.ledger.record_issue(issue("service.degraded", f"task {task['task_id']} lost its assignment"))
                continue
            mission = self.ledger.mission(live["mission_id"])
            if mission is None or mission["status"] not in MISSION_TERMINAL or self._holding(live["mission_id"]):
                continue
            kind, detail = self.outcome(task, mission)
            try:
                self._settle(task, live, kind, detail)
            except StaleTask:
                continue

    def _settle(self, task: dict, live: dict, kind: str, detail: dict) -> None:
        epoch, robot = live["epoch"], live["robot_id"]
        with self.store.transaction():
            current = self.store.task(task["task_id"])
            if current["state_version"] != task["state_version"]:
                raise StaleTask(task["task_id"])
            self.store.end_assignment(task["task_id"], epoch, state=AssignmentState.ENDED.value, reason=kind)
            summary = {"result": kind, "epoch": epoch, "robot_id": robot, **detail}
            if kind == "completed":
                self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                       state=TaskState.COMPLETED.value, reason=None, outcome=summary)
            elif kind == "unknown":
                self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                       state=TaskState.OUTCOME_UNKNOWN.value, reason=detail.get("reason"),
                                       outcome=summary)
            elif kind in ("declined", "operator_cancelled"):
                self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                       state=TaskState.FAILED.value, reason=f"mission.{kind}", outcome=summary)
            elif kind in ("flight_failed", "admission_rejected"):
                self._requeue(task, epoch, exclude=(robot, kind), mission_id=live["mission_id"])
            elif kind in ("not_flown", "expired", "withdrawn"):
                self._requeue(task, epoch, exclude=None, mission_id=live["mission_id"])
            else:
                return
        self.touched.add(live["mission_id"])

    # ── withdrawal before the claim / 领取前撤回 ──

    def withdraw(self) -> None:
        now = self.clock()
        for task in self.store.tasks(states=(TaskState.ASSIGNED.value,)):
            live = self.store.live(task["task_id"])
            mission = self.ledger.mission(live["mission_id"]) if live else None
            if mission is None or mission["status"] not in UNCLAIMED or self._claimed(live["mission_id"]):
                if live is not None and live["blocked_since"]:
                    self.store.mark_blocked(task["task_id"], live["epoch"], None)
                continue
            version = mission["current_version"]
            record = self.ledger.version(live["mission_id"], version)
            activity = activity_key(live["mission_id"], version)
            eligibility = self.service.dispatch.judge(live["robot_id"], stage=Stage.PREVIEW,
                                                      needs=needs_of(record) if record and record["package"] else None,
                                                      activity=activity)
            if eligibility.verdict is Verdict.ELIGIBLE:
                if live["blocked_since"]:
                    self.store.mark_blocked(task["task_id"], live["epoch"], None)
                continue
            if not live["blocked_since"]:
                self.store.mark_blocked(task["task_id"], live["epoch"], now.isoformat())
                continue
            if (now - datetime.fromisoformat(live["blocked_since"])).total_seconds() < \
                    self.catalog.policy.reassign_after_s:
                continue
            other = self._decide(task, skip={live["robot_id"]}, ignore={activity})
            if other["verdict"] != TaskVerdict.ASSIGN.value:
                continue
            try:
                self._withdraw(task, live, version, list(eligibility.reasons), other)
            except StaleTask:
                continue

    def _withdraw(self, task: dict, live: dict, version: int, reasons: list[str], other: dict) -> None:
        mission_id, epoch = live["mission_id"], live["epoch"]
        with self.store.transaction():
            current = self.store.task(task["task_id"])
            if current["state_version"] != task["state_version"] or self._claimed(mission_id):
                # The claim committed first: the robot owns the flight and nothing is withdrawn.
                # 领取先提交：机器人拥有这次飞行，不撤回任何东西。
                self.store.event(f"task:{task['task_id']}", "withdrawal.refused", self.worker,
                                 {"epoch": epoch, "mission_id": mission_id, "claimed": self._claimed(mission_id)})
                return
            self.ops.store.record_cancel(mission_id, requested_by=f"scheduler:{self.worker}",
                                         request_id=f"withdraw-{task['task_id']}-a{epoch}",
                                         reason="assignment_withdrawn")
            self.store.end_assignment(task["task_id"], epoch, state=AssignmentState.WITHDRAWN.value,
                                      reason=",".join(reasons)[:300] or "blocked")
            self.service.dispatch.release_unclaimed(mission_id, version, "assignment_withdrawn")
            self.store.event(f"task:{task['task_id']}", "assignment.withdrawn", self.worker,
                             {"epoch": epoch, "robot_id": live["robot_id"], "mission_id": mission_id,
                              "reasons": reasons, "next": other["robot_id"]})
            self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                   state=TaskState.QUEUED.value, robot_id=None, mission_id=None,
                                   reason="assignment_withdrawn")
        self.touched.add(mission_id)
        self.service.dirty.add(mission_id)

    # ── cancellation / 取消 ──

    def cancel(self, caller: Caller | None, project_id, task_id, request_id, reason) -> dict:
        """Persist the cancel first; no assignment follows, a claimed flight is reconciled (D059 §8).

        先持久化取消；之后不再分配，已领取的飞行照常对账（D059 §8）。
        """
        task = self._task(caller, project_id, task_id, MISSION_OPERATE)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        missions = self.cancel_task(task["task_id"], actor=caller.identity, request_id=request_id,
                                    reason=str(reason)[:300])
        for mission_id in missions:
            self.service.dirty.add(mission_id)
        if missions:
            self.service.dispatch.prepare()
        return self._task_view(self.store.task(task["task_id"]))

    def cancel_task(self, task_id: str, *, actor: str, request_id: str, reason: str) -> list[str]:
        """Inside one transaction: the task's cancel and the P1 cancel intent of its live mission.

        在一个事务内：任务的取消与其有效任务的 P1 取消意图。
        """
        with self.store.transaction():
            task = self.store.task(task_id)
            if TaskState(task["state"]) in TERMINAL_TASK:
                raise ServiceError("task.not_cancellable", f"the task is already {task['state']}")
            if task["cancel"] is not None:
                return []
            cancel = {"requested_by": actor, "request_id": request_id, "reason": reason,
                      "requested_at": self.clock().isoformat()}
            live = self.store.live(task_id)
            missions = []
            if live is not None and live["mission_id"]:
                mission = self.ledger.mission(live["mission_id"])
                if mission is not None and mission["status"] not in MISSION_TERMINAL:
                    self.ops.store.record_cancel(live["mission_id"], requested_by=actor, request_id=request_id,
                                                 reason=f"task {task_id} cancelled")
                missions.append(live["mission_id"])
            state = TaskState.CANCEL_REQUESTED if live is not None else TaskState.CANCELLED
            self.store.update_task(task_id, task["state_version"], actor=actor, state=state.value, cancel=cancel,
                                   outcome={"result": "cancelled"} if state is TaskState.CANCELLED else None)
        return missions

    def cancel_run(self, run_id: str, *, actor: str, request_id: str, reason: str) -> list[str]:
        """The P2 run cancel reaches the run's tasks in the same transaction (D059 §8). / P2 运行取消在同一事务到达其任务。"""
        missions = []
        for task in self.store.tasks(requested_by=f"workflow:{run_id}"):
            if TaskState(task["state"]) in TERMINAL_TASK or task["cancel"] is not None:
                continue
            missions += self.cancel_task(task["task_id"], actor=actor, request_id=request_id, reason=reason)
        return missions

    def finish_cancels(self) -> None:
        for task in self.store.tasks(states=tuple(s.value for s in CANCELLING_TASK)):
            try:
                self._finish_cancel(task)
            except StaleTask:
                continue

    def _finish_cancel(self, task: dict) -> None:
        live = self.store.live(task["task_id"])
        if live is None:
            self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                   state=TaskState.CANCELLED.value, outcome={"result": "cancelled"})
            return
        mission = self.ledger.mission(live["mission_id"])
        settled = mission is None or (mission["status"] in MISSION_TERMINAL and not self._holding(mission["mission_id"]))
        if settled:
            # A result that arrived after the cancel is recorded, never acted upon. / 取消后到达的结果只记录，不据此行动。
            with self.store.transaction():
                self.store.end_assignment(task["task_id"], live["epoch"], state=AssignmentState.ENDED.value,
                                          reason="cancelled")
                self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                       state=TaskState.CANCELLED.value,
                                       outcome={"result": "cancelled",
                                                "late_result": mission["status"] if mission else None})
            self.touched.add(live["mission_id"])
        elif task["state"] == TaskState.CANCEL_REQUESTED.value:
            self.store.update_task(task["task_id"], task["state_version"], actor=SCHEDULER,
                                   state=TaskState.CANCELLING.value)

    # ── the claim gate and views / 领取闸门与视图 ──

    def assignment_problem(self, mission_id: str) -> str | None:
        """A delivery flies only for its task's live assignment at the current epoch. / 投递只为任务当前代次的有效分配起飞。"""
        row = self.store.assignment_of(mission_id)
        if row is None:
            return None
        task = self.store.task(row["task_id"])
        if row["state"] != AssignmentState.ACTIVE.value or task is None or task["epoch"] != row["epoch"]:
            return "assignment_superseded"
        return None

    def mission_task(self, mission_id: str) -> dict | None:
        row = self.store.assignment_of(mission_id)
        if row is None:
            return None
        task = self.store.task(row["task_id"])
        return {"task_id": row["task_id"], "epoch": row["epoch"], "assignment": row["state"],
                "task_state": task["state"] if task else None, "current_epoch": task["epoch"] if task else None}

    def _summary(self, task: dict) -> dict:
        last = self.store.last_decision(task["task_id"])
        waiting = None
        if last is not None and task["state"] == TaskState.QUEUED.value:
            waiting = {c["robot_id"]: c["reasons"] for c in last["body"]["candidates"]}
        return {**{k: task[k] for k in ("task_id", "project_id", "asset_id", "volume_id", "candidates", "priority",
                                        "state", "epoch", "robot_id", "mission_id", "reason", "source",
                                        "created_at", "updated_at", "not_after")},
                "waiting": waiting, "decision": {k: last[k] for k in ("decision_id", "verdict", "robot_id", "epoch",
                                                                     "created_at")} if last else None}

    def _task_view(self, task: dict) -> dict:
        decisions = []
        for row in self.store.decisions(task["task_id"], 20):
            body = row["body"]
            decisions.append({"decision_id": row["decision_id"], "created_at": row["created_at"],
                              "epoch": row["epoch"], "verdict": body["verdict"], "robot_id": body["robot_id"],
                              "order": body["order"], "snapshot_sha256": body["snapshot_sha256"],
                              "ranking_version": body["ranking_version"], "policy_version": body["policy_version"],
                              "candidates": [{k: c[k] for k in ("robot_id", "verdict", "reasons", "permanent", "eta_s",
                                                                "usage", "held", "envelope")}
                                             for c in body["candidates"]]})
        assignments = []
        for row in self.store.assignments(task["task_id"]):
            mission = self.ledger.mission(row["mission_id"]) if row["mission_id"] else None
            assignments.append({**{k: row[k] for k in ("epoch", "robot_id", "mission_id", "state", "reason",
                                                       "blocked_since", "created_at", "updated_at")},
                                "mission_status": mission["status"] if mission else None})
        return {"task": {**self._summary(task), "excluded": task["excluded"], "cancel": task["cancel"],
                         "outcome": task["outcome"], "requested_by": task["requested_by"]},
                "assignments": assignments, "decisions": decisions,
                "events": self.store.events(f"task:{task['task_id']}", 60)}

    def task_view(self, caller: Caller | None, project_id, task_id) -> dict:
        return self._task_view(self._task(caller, project_id, task_id, MISSION_READ))

    def list_view(self, caller: Caller | None, project_id) -> dict:
        """The project's queue, each robot's assignment and preview verdict, and the airspace holds and envelopes.

        项目的队列、各机器人的分配与预览判定，以及空域持有与包络。
        """
        self._require(caller, project_id, MISSION_READ)
        tasks = self.store.tasks(project_id=project_id)
        live = [t for t in tasks if TaskState(t["state"]) not in TERMINAL_TASK]
        done = [t for t in tasks if TaskState(t["state"]) in TERMINAL_TASK][-30:]
        robots = []
        for robot_id in self.project_robots(project_id):
            eligibility = self.service.dispatch.judge(robot_id, stage=Stage.PREVIEW)
            active = self.store.assignments(robot_id=robot_id, states=(AssignmentState.ACTIVE.value,))
            robots.append({"robot_id": robot_id, "site_id": self.ops.catalog.robots[robot_id].site_id,
                           "dock_id": self.ops.catalog.robots[robot_id].dock_id,
                           "assignments": [{k: a[k] for k in ("task_id", "epoch", "mission_id")} for a in active],
                           "eligibility": {"verdict": eligibility.verdict.value, "reasons": list(eligibility.reasons)}})
        holds = []
        for reservation in self.ops.store.reservations(states=HOLDING):
            if reservation.project_id != project_id:
                continue
            cells = [r for r in reservation.resources if is_cell(r)]
            if cells:
                holds.append({"activity": reservation.activity_key, "mission_id": reservation.mission_id,
                              "robot_id": reservation.robot_id, "state": reservation.state.value, "cells": cells})
        # What the page may offer: each asset with the robots whose site map registers it and its volume.
        # 页面可提供的选项：每个资产及登记它的机器人与所属体积。
        assets: dict[str, dict] = {}
        for robot_id in self.project_robots(project_id):
            for asset_id, asset in sorted(self.ops.registry(robot_id).data.get("assets", {}).items()):
                entry = assets.setdefault(asset_id, {"volume_id": asset.get("volume"), "robots": []})
                entry["robots"].append(robot_id)
        own = {h["activity"] for h in holds}
        envelopes: dict[str, list[str]] = {}
        for cell, activity in self.airspace.envelopes().items():
            if activity in own:
                envelopes.setdefault(activity, []).append(cell)
        return {"project_id": project_id,
                "catalog": {"catalog_id": self.catalog.catalog_id, "sha256": self.catalog.sha256,
                            "ranking": self.catalog.ranking.version, "policy": self.catalog.policy.version},
                "roles": sorted(r.value for r in self.ops.directory.roles(caller, project_id)),
                "tasks": [self._summary(t) for t in live + done], "robots": robots, "assets": assets,
                "airspace": {"frame": self.catalog.airspace.frame, "cell_m": self.catalog.airspace.cell_m,
                             "holds": holds, "envelopes": [{"activity": a, "cells": sorted(c)}
                                                           for a, c in sorted(envelopes.items())]}}
