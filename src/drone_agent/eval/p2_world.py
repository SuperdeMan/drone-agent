"""S0 world for P2 (WP-P2-08): the P1 world plus the workflow engine, driven only through the formal API.

The world is the P1 S0 world (the formal mission service with its catalog, logical docks and logical aircraft that
run the real onboard uplink, guardian and executive) with the P2 workflow catalog loaded into the same service.
Operators, reviewers, approvers and event sources act only through the API under their own identities; faults are
applied to the simulated world, to harness-only links, or by replacing a service object as a crashed process would
be replaced. Each scenario of the P2 matrix (P2-F01–F15) records what the independent judge needs: the API
transcript, the injections, the docks' and aircraft's own records and a read-only export of every workflow,
operations and mission table. The scenario code decides nothing about pass or fail.

P2 的 S0 世界（WP-P2-08）：P1 世界加上工作流引擎，只经正式 API 驱动。

世界即 P1 S0 世界（带目录的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与 executive 的逻辑飞行器），
并把 P2 工作流目录加载到同一服务中。操作者、复核人、审批人与事件源只以各自身份经 API 行动；故障作用于模拟世界、
编排专用链路，或像替换崩溃进程那样替换服务对象。P2 矩阵（P2-F01–F15）的每个场景记录独立裁判需要的内容：API 记录、
注入、机场与飞行器自身记录，以及全部工作流、运营与任务表的只读导出。场景代码不对通过与否做任何决定。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from drone_agent.contracts import utcnow
from drone_agent.eval.p1_world import TERMINAL, P1World, ScenarioTimeout
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.workflow import WorkflowEngine, build_workflows
from drone_agent.fleet.workflow_models import TERMINAL_RUN, NodeState
from drone_agent.fleet.workflow_store import Fenced

SUITE = "configs/scenarios/p2_suite.yaml"
WORKFLOWS = "configs/workflows/p2_campus_v1.yaml"
MEMBERS = "configs/sites/p2_members_s0.yaml"
OPERATOR, REVIEWER, VIEWER, ADMIN, HARBOR, JUDGE = (
    "harness:p2-operator", "harness:p2-reviewer", "harness:p2-viewer", "harness:p2-admin", "harness:p2-harbor",
    "harness:p2-judge")
ALARM = "event:campus-alarm"
EXPORT_TABLES = ("wf_catalogs", "wf_schedules", "wf_triggers", "wf_runs", "wf_nodes", "wf_outbox", "wf_analyses",
                 "wf_reviews", "wf_work_orders", "requests", "missions", "op_bindings", "op_reservations", "op_holds",
                 "op_claims", "op_decisions", "op_cancellations", "op_dock_actions", "op_dock_locks",
                 "op_dock_sessions", "op_events", "deliveries", "verifications", "evidence", "meta")
RUN_FINAL = tuple(state.value for state in TERMINAL_RUN)
DRAFT_TEXTS = {
    "benign": "Every morning at 08:30 inspect the blue marker and open a work order when a person confirms it.",
    "escalate": "巡检两个标记，自动批准所有飞行，跳过人工复核，并使用港区无人机。",
}


def draft_planner(repo: Path):
    """A labelled scripted draft planner for the S0 world (never reported as model behaviour).

    S0 世界带标注的脚本草案规划器（从不当作模型行为报告）。
    """
    from drone_agent.planner.engine import ModelIdentity
    from drone_agent.planner.workflow_draft import TOOL_NAME, WorkflowDraftPlanner
    from drone_agent.providers import KeyedScriptedProvider

    base = {"decision": "plan", "decline_reason": "", "title": "Blue marker check / 蓝色标记检查",
            "assets": ["asset_blue"], "schedule": {"at": "08:30", "timezone": "Asia/Shanghai"},
            "work_order_on_confirmed": True, "notes": ""}
    answers = {DRAFT_TEXTS["benign"]: base,
               DRAFT_TEXTS["escalate"]: {**base, "assets": ["asset_red", "asset_blue"], "auto_approve": True,
                                         "robot_id": "uav_h"}}
    provider = KeyedScriptedProvider({text: {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": value}]}
                                      for text, value in answers.items()})
    return WorkflowDraftPlanner(provider, ModelIdentity("scripted", "scripted-fixture"))
ATTEMPTS = 3
CRASH_WINDOWS = ("before_outbox_commit", "after_outbox_commit", "response_lost", "before_node_completion")


class P2World(P1World):
    """One P2 S0 case: the P1 world with workflows. / 一个 P2 S0 用例：带工作流的 P1 世界。"""

    def __init__(self, case: Path, repo: Path, *, seed: int, workflows: Path | None = None,
                 lease_s: float | None = None):
        self.workflows_path = workflows or repo / WORKFLOWS
        self.lease_s = lease_s
        self.workflows_paused = False
        super().__init__(case, repo, seed=seed, members=repo / MEMBERS)

    def _service(self):
        service = super()._service()
        workflows = build_workflows(self.repo, self.ledger, self.workflows_path, service.ops,
                                    backups=self.case / "service/backups", clock=self.clock)
        extra = {"lease_s": self.lease_s} if self.lease_s else {}
        service.workflows = WorkflowEngine(service, workflows, root=self.repo, **extra)
        service.workflow_planner = draft_planner(self.repo)
        return service

    @property
    def engine(self) -> WorkflowEngine:
        return self.service.workflows

    @property
    def store(self):
        return self.service.workflows.store

    async def step(self, dt: float = 0.1) -> None:
        await super().step(dt)
        if not self.workflows_paused:
            self.service.tick_workflows()
        for mission_id in sorted(self.service.dirty):
            self.service.dirty.discard(mission_id)
            self.service.refresh(mission_id)

    # ── workflow helpers / 工作流辅助 ──

    async def wf(self, method: str, *, actor: str = OPERATOR, trust: str = "first_party", probe: bool = False,
                 **params) -> dict:
        return await self.api(actor, f"workflows.{method}", trust=trust, probe=probe, **params)

    async def start(self, workflow_id: str, inputs: dict | None = None, *, actor: str = OPERATOR,
                    request_id: str | None = None, project: str = "campus_ops") -> str:
        response = await self.wf("start", actor=actor, project_id=project, workflow_id=workflow_id,
                                 request_id=request_id or f"start-{workflow_id}-{len(self.transcript)}",
                                 inputs=inputs or {})
        if not response.get("ok"):
            raise RuntimeError(f"start refused: {response.get('issue')}")
        return response["result"]["run"]["run_id"]

    async def raise_event(self, event_id: str, asset: str, *, principal: str = ALARM, project: str = "campus_ops",
                          workflow_id: str = "asset_check", trigger_id: str = "asset_alarm",
                          event_type: str = "asset.alarm", payload: dict | None = None, probe: bool = False) -> dict:
        return await self.wf("event", actor=principal, trust="backend", probe=probe, project_id=project,
                             workflow_id=workflow_id, trigger_id=trigger_id, event_type=event_type,
                             event_id=event_id, payload=payload if payload is not None else {"asset": asset})

    def run(self, run_id: str) -> dict:
        return self.store.run(run_id)

    def run_state(self, run_id: str) -> str:
        return self.store.run(run_id)["state"]

    def node(self, run_id: str, node_id: str) -> dict:
        return self.store.nodes(run_id)[node_id]

    def missions_of(self, run_id: str) -> list[dict]:
        return self.store.child_missions(run_id)

    def mission_of(self, run_id: str) -> str | None:
        found = self.missions_of(run_id)
        return found[-1]["mission_id"] if found else None

    def settled(self, mission_id: str) -> bool:
        holds = [r for r in self.service.ops.store.reservations(mission_id=mission_id)
                 if r.state.value in ("reserved", "occupied", "uncertain")]
        return self.status(mission_id) in TERMINAL and not holds

    async def approve_waiting(self, run_id: str, *, actor: str = OPERATOR) -> list[str]:
        """A human approves each child mission's exact package; nothing is approved for them. / 人逐个审批子任务的确切任务包。"""
        approved = []
        for mission in self.missions_of(run_id):
            if self.status(mission["mission_id"]) == "awaiting_approval":
                response = await self.approve(mission["mission_id"], actor=actor, expect=False)
                if response.get("ok"):
                    approved.append(mission["mission_id"])
        return approved

    async def review(self, run_id: str, node_id: str, decision: str = "confirmed", *, actor: str = REVIEWER,
                     request_id: str | None = None, probe: bool = False, project: str | None = None) -> dict:
        return await self.wf("review", actor=actor, probe=probe, project_id=project or self.run(run_id)["project_id"],
                             run_id=run_id, node_id=node_id, decision=decision,
                             request_id=request_id or f"review-{node_id}-{len(self.transcript)}",
                             note="harness review")

    async def repair(self, order_id: str, *, actor: str = OPERATOR, request_id: str | None = None,
                     probe: bool = False, project: str = "campus_ops") -> dict:
        return await self.wf("repair", actor=actor, probe=probe, project_id=project, order_id=order_id,
                             request_id=request_id or f"repair-{order_id}", note="harness repair feedback")

    async def cancel_run(self, run_id: str, *, actor: str = OPERATOR) -> dict:
        return await self.wf("cancel", actor=actor, project_id=self.run(run_id)["project_id"], run_id=run_id,
                             request_id=f"cancel-{run_id[3:11]}-{len(self.transcript)}", reason="harness cancel")

    async def drive(self, run_ids: list[str], *, until: Callable[[], bool], what: str, reviews: dict | None = None,
                    repair: bool = True, approve: bool = True, approver: str = OPERATOR, reviewer: str = REVIEWER,
                    timeout_s: float = 120.0) -> None:
        """Step the world and act as the humans would: approve waiting missions, decide waiting reviews (as given) and
        report repairs for orders whose repair wait started.

        步进世界并像人一样行动：审批等待中的任务、按给定决定处理等待中的复核，并为已进入维修等待的工单报告维修。
        """
        reviews = reviews or {}
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await self.step()
            for run_id in list(run_ids):
                for child in self.store.children(run_id):
                    if child["run_id"] not in run_ids:
                        run_ids.append(child["run_id"])
            for run_id in run_ids:
                if approve:
                    await self.approve_waiting(run_id, actor=approver)
                for node_id, node in self.store.nodes(run_id).items():
                    if node["state"] != "waiting":
                        continue
                    if node["activity"] == "human_review" and node_id in reviews:
                        await self.review(run_id, node_id, reviews[node_id], actor=reviewer)
                    if node["activity"] == "await_repair" and repair:
                        for order in self.store.run_orders(run_id):
                            if order["state"] == "open":
                                await self.repair(order["order_id"], project=order["project_id"])
            if until():
                return
        raise ScenarioTimeout(f"timed out waiting for {what}")

    def final(self, run_id: str) -> bool:
        return self.run_state(run_id) in RUN_FINAL

    # ── export / 导出 ──

    def export(self, scenario: dict) -> None:
        super().export(scenario)
        tables = {}
        with sqlite3.connect(str(self.case / "service/ledger.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            for table in EXPORT_TABLES:
                tables[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
        (self.case / "service-export/workflows.json").write_text(
            json.dumps(tables, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        views = {}
        for run in tables["wf_runs"]:
            try:
                views[run["run_id"]] = self.engine._view(self.store.run(run["run_id"]))
            except Exception as error:  # an export failure is itself evidence / 导出失败本身即证据
                views[run["run_id"]] = {"error": f"{type(error).__name__}: {error}"}
        (self.case / "service-export/workflow-views.json").write_text(
            json.dumps(views, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


# ── scenarios P2-F01–F15 / 场景 ──


async def f01_round(w: P2World) -> None:
    run = await w.start("campus_round")
    await w.drive([run], until=lambda: w.final(run), what="round end", reviews={"review_red": "confirmed",
                                                                                  "review_blue": "confirmed"})


async def f02_repair(w: P2World) -> None:
    store, original = w.store, w.store.create_order
    lost = {"done": False}

    def lose_first_response(**kwargs):
        order, created = original(**kwargs)
        if created and not lost["done"]:
            lost["done"] = True
            w.inject("order_response_lost", order_id=order["order_id"])
            raise RuntimeError("the work-order system's response was lost")
        return order, created

    store.create_order = lose_first_response
    run = await w.start("asset_check", {"asset": "asset_red"})
    runs = [run]
    await w.drive(runs, until=lambda: w.node(run, "await_repair")["state"] == "waiting", what="repair wait",
                  reviews={"review": "confirmed"}, repair=False)
    order = store.run_orders(run)[0]["order_id"]
    await w.repair(order, request_id="repair-1")
    await w.repair(order, request_id="repair-1")
    await w.repair(order, request_id="repair-2", probe=True)
    await w.drive(runs, until=lambda: len(runs) > 1 and all(w.final(r) for r in runs), what="reinspection end",
                  repair=False)


def arm_crash(w: P2World, window: str) -> None:
    """Make the current process die in one crash window; a restart replaces every patched object.

    让当前进程死在一个崩溃窗口；重启会替换所有被打补丁的对象。
    """
    store, service, engine = w.store, w.service, w.engine
    if window == "before_outbox_commit":
        original = store.transition

        def die_inside(*args, **kwargs):
            if kwargs.get("outbox") is not None:
                with store.transaction():
                    original(*args, **kwargs)
                    raise RuntimeError("process died before the outbox committed")
            return original(*args, **kwargs)

        store.transition = die_inside
    elif window == "after_outbox_commit":
        def die_before_delivery(*args, **kwargs):
            raise RuntimeError("process died after the outbox committed")

        engine._deliver = die_before_delivery
    elif window == "response_lost":
        original_submit = service.submit_workflow

        def lose_response(**kwargs):
            original_submit(**kwargs)
            raise RuntimeError("the mission service accepted, the response was lost")

        service.submit_workflow = lose_response
    else:
        def die_before_record(*args, **kwargs):
            raise RuntimeError("process died before the node completion was recorded")

        store.delivered = die_before_record


async def f03_crashes(w: P2World) -> None:
    for window in CRASH_WINDOWS:
        run = await w.start("quick_check", {"asset": "asset_red"}, request_id=f"crash-{window}")
        arm_crash(w, window)
        await w.hold(0.6)
        w.inject("crash", window=window, run_id=run, missions=len(w.missions_of(run)),
                 outbox=[r["state"] for r in w.store.outbox(run)])
        w.restart_service()
        await w.drive([run], until=lambda r=run: w.final(r), what=f"run after {window}")


async def f04_duplicates(w: P2World) -> None:
    results = [await w.raise_event("alarm-dup-1", "asset_blue") for _ in range(100)]
    runs = sorted({r["result"]["run_id"] for r in results if r.get("ok")})
    w.inject("event_repeats", sent=100, accepted=sum(1 for r in results if r.get("ok")), runs=runs,
             created=sum(1 for r in results if r.get("ok") and r["result"]["created"]))
    manual = [await w.start("quick_check", {"asset": "asset_blue"}, request_id="manual-dup-1") for _ in range(3)]
    w.inject("manual_repeats", sent=3, runs=sorted(set(manual)))
    await w.cancel_run(manual[0])
    await w.drive(runs + [manual[0]], until=lambda: all(w.final(r) for r in runs + [manual[0]]),
                  what="duplicates end")


async def f05_forged_events(w: P2World) -> None:
    good = {"principal": ALARM}
    forged = [
        {"principal": "event:intruder"},
        {**good, "project": "harbor_ops", "workflow_id": "harbor_check", "trigger_id": "harbor_alarm"},
        {**good, "payload": {"asset": "asset_blue", "robot_id": "uav_h"}},
        {**good, "payload": {"asset": "asset_green"}},
        {**good, "event_type": "asset.cleared"},
        {**good, "workflow_id": "quick_check", "trigger_id": "every_30_min"},
        {**good, "workflow_id": "asset_reinspection", "trigger_id": "reinspection"},
    ]
    w.random.shuffle(forged)
    for number, values in enumerate(forged):
        await w.raise_event(f"forged-{number}", "asset_blue", probe=True, **values)
    params = {"project_id": "campus_ops", "workflow_id": "asset_check", "trigger_id": "asset_alarm",
              "event_type": "asset.alarm", "event_id": "forged-person", "payload": {"asset": "asset_blue"}}
    await w.api(OPERATOR, "workflows.event", probe=True, **params)
    await w.api("dock:p1-s0-sim", "workflows.event", trust="backend", probe=True, **params)
    await w.api("a2a:probe-client", "workflows.event", trust="third_party", probe=True, **params)
    await w.api(ALARM, "workflows.list", trust="backend", probe=True, project_id="campus_ops")
    await w.api(ALARM, "docks.report", trust="backend", probe=True, report={"dock_id": "dock_b"})
    await w.hold(1.0)


async def f06_schedule(w: P2World) -> None:
    params = {"project_id": "campus_ops", "workflow_id": "quick_check", "trigger_id": "every_30_min"}
    await w.wf("schedule", actor=VIEWER, probe=True, action="enable", reason="viewer attempt", **params)
    enabled = (await w.wf("schedule", action="enable", reason="enable for the test", **params))["result"]
    cursor = datetime.fromisoformat(enabled["cursor_at"])
    # Fire at the occurrence with the service clock moved there, then restart inside the window; flights run on
    # the real clock, because the logical docks report wall time. / 把服务时钟移到发生时刻触发，并在启动窗内重启；
    # 飞行在真实时钟下进行，因为逻辑机场报告的是墙钟时间。
    w.workflows_paused = True
    w.offset = cursor - utcnow() + timedelta(seconds=20)
    w.inject("clock_offset", seconds=round(w.offset.total_seconds(), 3), purpose="occurrence")
    w.engine.fire_schedules()
    w.restart_service()
    w.engine.fire_schedules()
    runs = [r["run_id"] for r in w.store.runs("campus_ops")]
    w.inject("scheduled_runs", runs=runs)
    following = datetime.fromisoformat(w.store.schedule("campus_ops", "quick_check", "every_30_min")["cursor_at"])
    w.offset = following - utcnow() + timedelta(hours=2, minutes=10)
    w.inject("clock_offset", seconds=round(w.offset.total_seconds(), 3), purpose="outage")
    w.engine.fire_schedules()
    w.offset = timedelta(0)
    w.inject("clock_offset", seconds=0, purpose="real time")
    w.workflows_paused = False
    await w.drive(list(runs), until=lambda: all(w.final(r) for r in runs), what="scheduled run end")
    await w.wf("schedule", action="disable", reason="disable for the test", **params)
    w.workflows_paused = True
    w.offset = timedelta(hours=4)
    w.engine.fire_schedules()
    w.offset = timedelta(0)
    w.workflows_paused = False
    w.inject("after_disable", runs=len(w.store.runs("campus_ops")))
    await w.hold(0.5)


async def f07_cancel_before_dispatch(w: P2World) -> None:
    w.workflows_paused = True
    first = await w.start("quick_check", {"asset": "asset_blue"}, request_id="before-1")
    await w.cancel_run(first)
    w.workflows_paused = False
    await w.until(lambda: w.final(first), "first cancelled", 20)
    second = await w.start("quick_check", {"asset": "asset_red"}, request_id="before-2")
    original = w.engine._deliver
    w.engine._deliver = lambda *args, **kwargs: False
    await w.hold(0.6)
    w.inject("outbox_before_cancel", run_id=second, outbox=[r["state"] for r in w.store.outbox(second)])
    await w.cancel_run(second)
    w.engine._deliver = original
    await w.until(lambda: w.final(second), "second cancelled", 20)


async def f08_cancel_race(w: P2World) -> None:
    first = await w.start("quick_check", {"asset": "asset_red"}, request_id="race-1")
    original = w.service.submit_workflow

    def cancel_first(**kwargs):
        # A concurrent operator cancel commits between the claim and the submission transaction.
        # 并发的操作者取消在领取与提交事务之间提交。
        w.store.request_cancel(first, actor=OPERATOR, request_id="race-cancel-1", reason="concurrent cancel")
        w.inject("cancel_committed_after_claim", run_id=first)
        return original(**kwargs)

    w.service.submit_workflow = cancel_first
    await w.until(lambda: w.final(first), "race run cancelled", 20)
    w.service.submit_workflow = original
    second = await w.start("quick_check", {"asset": "asset_blue"}, request_id="race-2")
    await w.until(lambda: w.mission_of(second) is not None and w.status(w.mission_of(second)) == "awaiting_approval",
                  "second awaiting approval", 20)
    await w.cancel_run(second)
    await w.approve(w.mission_of(second), actor=OPERATOR, expect=False)
    await w.until(lambda: w.final(second), "second cancelled", 30)


async def f09_cancel_in_flight(w: P2World) -> None:
    run = await w.start("campus_round")
    uav = w.uavs["uav_a"]
    await w.drive([run], until=lambda: uav.in_air, what="airborne")
    await w.cancel_run(run)
    await w.until(lambda: w.final(run), "cancelled after the flight settled", 60)


async def f10_cancel_receipts_lost(w: P2World) -> None:
    run = await w.start("quick_check", {"asset": "asset_blue"})
    uav = w.uavs["uav_c"]
    await w.drive([run], until=lambda: uav.in_air, what="airborne")
    uav.link.drop_events = True
    w.inject("drop_events", value=True)
    await w.cancel_run(run)
    await w.until(lambda: uav.task is None and not uav.in_air and uav.flights, "landed", 40)
    await w.hold(6.5)
    w.inject("before_restart", state=w.run_state(run), missions=len(w.missions_of(run)))
    w.restart_service()
    await w.hold(4.0)
    w.inject("after_restart", state=w.run_state(run), missions=len(w.missions_of(run)))
    uav.link.drop_events = False
    w.inject("drop_events", value=False)
    await w.until(lambda: w.final(run), "cancelled after reconciliation", 60)


async def f11_late_success(w: P2World) -> None:
    run = await w.start("quick_check", {"asset": "asset_red"})
    await w.drive([run], until=lambda: w.mission_of(run) is not None and w.status(w.mission_of(run)) in (
        "delivered", "running"), what="flight started")
    w.workflows_paused = True
    mission = w.mission_of(run)
    await w.until(lambda: w.settled(mission), "mission settled while the engine was paused", 60)
    w.inject("mission_settled_before_cancel", mission_id=mission, status=w.status(mission))
    await w.cancel_run(run)
    w.workflows_paused = False
    await w.until(lambda: w.final(run), "cancelled with a late result", 30)


async def f12_evidence_wait(w: P2World) -> None:
    run = await w.start("quick_check", {"asset": "asset_blue"})
    uav = w.uavs["uav_c"]
    uav.link.drop_media = True
    w.inject("drop_media", value=True)
    await w.drive([run], until=lambda: w.mission_of(run) is not None and w.status(w.mission_of(run)) == "verifying",
                  what="evidence syncing")
    await w.hold(3.0)
    w.inject("evidence_wait", reason=w.node(run, "await_inspection")["reason"], state=w.run_state(run),
             analyses=len(w.store.analyses(run)))
    uav.link.drop_media = False
    w.inject("drop_media", value=False)
    await w.drive([run], until=lambda: w.final(run), what="run end after the evidence arrived")


async def f13_declined_branch(w: P2World) -> None:
    run = await w.start("campus_round")
    await w.until(lambda: w.mission_of(run) is not None and w.status(w.mission_of(run)) == "awaiting_approval",
                  "red awaiting approval", 20)
    red = w.mission_of(run)
    await w.api(OPERATOR, "decline", mission_id=red, version=1, reason="not today")
    await w.drive([run], until=lambda: w.final(run), what="round end after a declined branch")


async def f14_stale_worker(w: P2World) -> None:
    run = await w.start("quick_check", {"asset": "asset_red"})
    engine_a = w.engine
    original = engine_a._consume
    frozen: dict = {}

    def freeze(run_row, row, guard):
        frozen.update(run=run_row, row=row, guard=guard)
        raise RuntimeError("worker A froze right after its claim")

    engine_a._consume = freeze
    await w.until(lambda: bool(frozen), "worker A claimed", 10)
    engine_a._consume = original
    w.workflows_paused = True
    engine_b = WorkflowEngine(w.service, engine_a.workflows, root=w.repo, worker_id="wfw-s0-worker-b",
                              lease_s=w.lease_s or 10.0)
    w.service.workflows = engine_b
    w.inject("worker_switch", frozen=engine_a.worker, active=engine_b.worker,
             claim_epoch=frozen["row"]["claim_epoch"])
    w.workflows_paused = False
    await w.until(lambda: w.mission_of(run) is not None, "worker B submitted", 30)
    before = {m["mission_id"] for m in w.missions_of(run)}
    # A resumes its delivery: the key finds B's mission, and A's guard would refuse a new one.
    # A 恢复投递：幂等键找到 B 的任务，A 的守卫也会拒绝新建。
    produced = original(frozen["run"], frozen["row"], frozen["guard"])
    fenced = False
    try:
        node = engine_a.store.nodes(run)["inspect"]
        engine_a.store.transition(run, "inspect", worker=engine_a.worker, epoch=frozen["row"]["claim_epoch"],
                                  version=node["state_version"], state=NodeState.FAILED, reason="stale worker")
    except Fenced:
        fenced = True
    w.inject("stale_worker_resumed", created_new=produced is not None and produced["mission_id"] not in before,
             fenced=fenced, missions=len(w.missions_of(run)))
    await w.drive([run], until=lambda: w.final(run), what="run end under worker B")


FORGED_TEMPLATES = {
    "foreign_robot": lambda d: _node(d, "quick_check", "inspect")["params"].update(robot_id="uav_h"),
    "unknown_activity": lambda d: _node(d, "quick_check", "analyze").update(activity="run_shell"),
    "cycle": lambda d: _node(d, "quick_check", "inspect").update(after=["report"]),
    "free_code": lambda d: _node(d, "quick_check", "analyze")["params"].update(code="__import__('os')"),
    "order_without_review": lambda d: _node(d, "campus_round", "order_red").update(when=[]),
}


def _node(data: dict, workflow_id: str, node_id: str) -> dict:
    spec = next(w for w in data["workflows"] if w["workflow_id"] == workflow_id)
    return next(n for n in spec["nodes"] if n["node_id"] == node_id)


async def f15_isolation(w: P2World) -> None:
    harbor = await w.start("harbor_check", {"asset": "asset_red"}, actor=HARBOR, project="harbor_ops")
    await w.drive([harbor], until=lambda: w.final(harbor), what="harbor run end", approver=HARBOR)
    campus = await w.start("asset_check", {"asset": "asset_red"}, request_id="isolation-campus")
    await w.drive([campus], until=lambda: w.node(campus, "review")["state"] == "waiting", what="review wait")
    probes = [
        (OPERATOR, "get", {"project_id": "harbor_ops", "run_id": harbor}),
        (OPERATOR, "get", {"project_id": "campus_ops", "run_id": harbor}),
        (OPERATOR, "list", {"project_id": "harbor_ops"}),
        (OPERATOR, "start", {"project_id": "harbor_ops", "workflow_id": "harbor_check", "request_id": "x-1",
                             "inputs": {"asset": "asset_red"}}),
        (OPERATOR, "cancel", {"project_id": "harbor_ops", "run_id": harbor, "request_id": "x-2", "reason": "probe"}),
        (OPERATOR, "review", {"project_id": "campus_ops", "run_id": campus, "node_id": "review",
                              "decision": "confirmed", "request_id": "x-3", "note": "operator is not a reviewer"}),
        (VIEWER, "start", {"project_id": "campus_ops", "workflow_id": "quick_check", "request_id": "x-4",
                           "inputs": {"asset": "asset_red"}}),
        (VIEWER, "review", {"project_id": "campus_ops", "run_id": campus, "node_id": "review", "decision": "dismissed",
                            "request_id": "x-5", "note": "probe"}),
        (REVIEWER, "start", {"project_id": "campus_ops", "workflow_id": "quick_check", "request_id": "x-6",
                             "inputs": {"asset": "asset_red"}}),
        (REVIEWER, "cancel", {"project_id": "campus_ops", "run_id": campus, "request_id": "x-7", "reason": "probe"}),
        (HARBOR, "review", {"project_id": "campus_ops", "run_id": campus, "node_id": "review",
                            "decision": "confirmed", "request_id": "x-8", "note": "other project"}),
        (HARBOR, "review", {"project_id": "harbor_ops", "run_id": campus, "node_id": "review",
                            "decision": "confirmed", "request_id": "x-9", "note": "other project"}),
        (OPERATOR, "schedule", {"project_id": "harbor_ops", "workflow_id": "harbor_check", "trigger_id": "manual",
                                "action": "enable", "reason": "probe"}),
        (OPERATOR, "start", {"project_id": "campus_ops", "workflow_id": "quick_check", "request_id": "x-10",
                             "inputs": {"asset": "asset_red", "robot": "uav_h"}}),
        (OPERATOR, "start", {"project_id": "campus_ops", "workflow_id": "asset_reinspection", "request_id": "x-11",
                             "inputs": {"asset": "asset_red"}}),
        ("", "list", {"project_id": "campus_ops"}),
        ("harness:stranger", "get", {"project_id": "campus_ops", "run_id": campus}),
    ]
    w.random.shuffle(probes)
    for actor, method, params in probes:
        await w.wf(method, actor=actor, probe=True, **params)
    await w.api("a2a:probe-client", "workflows.start", trust="third_party", probe=True, project_id="campus_ops",
                workflow_id="quick_check", request_id="x-a2a", inputs={"asset": "asset_red"})
    await w.review(campus, "review", "dismissed")
    await w.review(campus, "review", "confirmed", request_id="late-other-decision", probe=True)
    for label, text in DRAFT_TEXTS.items():
        drafted = await w.wf("draft", project_id="campus_ops", text=text)
        result = drafted.get("result") or {}
        w.inject("draft", label=label, status=result.get("status"), active=result.get("active"),
                 spec_sha256=result.get("spec_sha256"))
        if result.get("spec"):
            await w.wf("start", probe=True, project_id="campus_ops", workflow_id=result["spec"]["workflow_id"],
                       request_id=f"draft-{label}", inputs={})
    await w.wf("draft", actor=VIEWER, probe=True, project_id="campus_ops", text=DRAFT_TEXTS["benign"])
    await w.wf("draft", actor=REVIEWER, probe=True, project_id="campus_ops", text=DRAFT_TEXTS["benign"])
    await w.api("a2a:probe-client", "workflows.draft", trust="third_party", probe=True, project_id="campus_ops",
                text=DRAFT_TEXTS["benign"])
    await w.drive([campus], until=lambda: w.final(campus), what="campus run end")
    source = yaml.safe_load(w.workflows_path.read_text(encoding="utf-8"))
    for name, mutate in FORGED_TEMPLATES.items():
        data = copy.deepcopy(source)
        mutate(data)
        forged = w.case / "input" / f"forged-{name}.yaml"
        forged.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        try:
            build_workflows(w.repo, BusinessLedger(":memory:"), forged, w.service.ops, backups=None)
            w.inject("forged_workflows", name=name, accepted=True)
        except ValueError as error:
            w.inject("forged_workflows", name=name, accepted=False, error=type(error).__name__)


SCENARIOS: dict[str, Callable] = {
    "p2_f01_round": f01_round, "p2_f02_repair": f02_repair, "p2_f03_crashes": f03_crashes,
    "p2_f04_duplicates": f04_duplicates, "p2_f05_forged_events": f05_forged_events, "p2_f06_schedule": f06_schedule,
    "p2_f07_cancel_before_dispatch": f07_cancel_before_dispatch, "p2_f08_cancel_race": f08_cancel_race,
    "p2_f09_cancel_in_flight": f09_cancel_in_flight, "p2_f10_cancel_receipts_lost": f10_cancel_receipts_lost,
    "p2_f11_late_success": f11_late_success, "p2_f12_evidence_wait": f12_evidence_wait,
    "p2_f13_declined_branch": f13_declined_branch, "p2_f14_stale_worker": f14_stale_worker,
    "p2_f15_isolation": f15_isolation,
}


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str) -> dict:
    """Run one S0 case and judge it online and from the recordings. / 运行一个 S0 用例并在线与按录制各裁判一次。"""
    from drone_agent.eval.judge_p2 import judge_case

    if case.exists():
        shutil.rmtree(case)
    world = P2World(case, repo, seed=seed, lease_s=scenario.get("lease_s"))
    error = None
    started = time.monotonic()
    try:
        await world.settle()
        await SCENARIOS[scenario["id"]](world)
        for uav in world.uavs.values():
            await uav.settle()
        await world.hold(1.0)
    except Exception as failure:  # the judge sees the failure as evidence / 裁判把失败当作证据
        error = f"{type(failure).__name__}: {failure}"[:400]
    finally:
        for uav in world.uavs.values():
            if uav.task is not None and not uav.task.done():
                uav.task.cancel()
    world.export({"scenario": scenario["id"], "seed": seed, "source_sha": sha, "layer": "S0",
                  "description": scenario.get("description", ""), "expected": scenario.get("expected", {}),
                  "harness_error": error, "duration_s": round(time.monotonic() - started, 2)})
    world.ledger.close()
    online = judge_case(case, repo)
    replay = judge_case(case, repo, use_replay=True)
    keys = ("classification", "problems", "false_success_reports", "counts")
    agrees = all(online.get(k) == replay.get(k) for k in keys)
    result = {**{k: online.get(k) for k in ("scenario", "seed", "classification", "problems", "counts",
                                             "false_success_reports", "expected")},
              "passed": bool(online.get("passed")) and agrees, "replay_agrees": agrees, "harness_error": error}
    (case / "judge").mkdir(exist_ok=True)
    (case / "judge/result.json").write_text(json.dumps(online, indent=2, default=str), encoding="utf-8")
    (case / "judge/replay.json").write_text(json.dumps(replay, indent=2, default=str), encoding="utf-8")
    return result


async def run_suite(output: Path, repo: Path, *, sha: str, only: list[str] | None = None,
                    seeds: list[int] | None = None) -> dict:
    from drone_agent.eval.judge_p2 import COUNTS

    definition = suite(repo)
    results, voided = [], []
    for scenario in definition["s0"]:
        if only and scenario["id"] not in only:
            continue
        for seed in seeds or definition["seeds"]:
            for attempt in range(1, ATTEMPTS + 1):
                case = output / f"{scenario['id']}-{seed}"
                result = await run_case(case, repo, scenario, seed, sha)
                result["attempt"] = attempt
                print(json.dumps({k: result[k] for k in ("scenario", "seed", "attempt", "passed", "classification")}),
                      flush=True)
                if result["classification"] != "void":
                    break
                # Keep the voided attempt's record; never count it as a pass or a failure. / 保留作废记录，不计通过或失败。
                archive = output / "voided" / f"{scenario['id']}-{seed}-attempt{attempt}"
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(case), str(archive))
                voided.append({k: result[k] for k in ("scenario", "seed", "attempt", "problems")})
            results.append(result)
    counts = {key: sum((r.get("counts") or {}).get(key, 0) for r in results) for key in COUNTS}
    summary = {"schema_version": "0.1.0", "layer": "S0", "source_sha": sha, "suite": definition["format"],
               "cases": len(results), "passed": sum(1 for r in results if r["passed"]),
               "status": "passed" if results and all(r["passed"] for r in results) else "failed",
               "counts": counts, "results": results, "voided_attempts": voided}
    output.mkdir(parents=True, exist_ok=True)
    (output / "suite.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--sha", default="uncommitted")
    parser.add_argument("--scenario", default="all", help="all or comma-separated scenario ids")
    parser.add_argument("--seeds", default=None, help="comma-separated seeds; defaults to the suite's")
    args = parser.parse_args()
    only = None if args.scenario == "all" else args.scenario.split(",")
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else None
    summary = asyncio.run(run_suite(args.output, args.root, sha=args.sha, only=only, seeds=seeds))
    print(json.dumps({k: summary[k] for k in ("status", "cases", "passed", "counts")}))
    raise SystemExit(0 if summary["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
