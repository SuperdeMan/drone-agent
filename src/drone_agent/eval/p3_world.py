"""S0 world for P3 (WP-P3-05): the P2 world plus the scheduler, several logical sites and the scale ladder.

The world is the P1 S0 world (the formal mission service with its catalog, logical docks and logical aircraft that run
the real onboard uplink, guardian and executive), with the P3 scheduling catalog and, where a case needs it, the P3
workflow catalog loaded into the same service. Operators, approvers and admins act only through the API under their
own identities; faults are applied to the simulated world, to harness-only links, or by replacing a service object or
running a second scheduler over the same ledger as a second worker would. Each case of the P3 matrix (P3-F01–F15) and
each rung of the ladder records what the independent judge needs: the API transcript, the injections, every aircraft's
flights and truth, the docks' own records and a read-only export of the scheduling, operations, workflow and mission
tables. The scenario code decides nothing about pass or fail.

P3 的 S0 世界（WP-P3-05）：P2 世界加上调度器、多个逻辑站点与规模阶梯。

世界即 P1 S0 世界（带目录的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与 executive 的逻辑飞行器），并把 P3
调度目录与用例需要的 P3 工作流目录加载到同一服务中。操作者、审批人与管理员只以各自身份经 API 行动；故障作用于模拟世界、
编排专用链路，或像替换进程、像第二个 worker 那样在同一账本上替换服务对象或运行第二个调度器。P3 矩阵（P3-F01–F15）的每个
用例与阶梯的每一档都记录独立裁判需要的内容：API 记录、注入、每架飞行器的飞行与真值、机场自身记录，以及调度、运营、工作流
与任务表的只读导出。场景代码不对通过与否做任何决定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from drone_agent.contracts import utcnow
from drone_agent.eval.p1_world import P1World, ScenarioTimeout
from drone_agent.eval.p2_world import P2World, draft_planner
from drone_agent.fleet.scheduler import Scheduler, build_scheduling
from drone_agent.fleet.scheduling_models import TERMINAL_TASK, TaskState
from drone_agent.fleet.workflow import WorkflowEngine, build_workflows

SUITE = "configs/scenarios/p3_suite.yaml"
CATALOG = "configs/sites/p3_campus_v1.yaml"
MEMBERS = "configs/sites/p3_members_s0.yaml"
SCHEDULING = "configs/scheduling/p3_campus_v1.yaml"
WORKFLOWS = "configs/workflows/p3_campus_v1.yaml"
OPERATOR, REVIEWER, VIEWER, ADMIN, HARBOR, JUDGE = (
    "harness:p3-operator", "harness:p3-reviewer", "harness:p3-viewer", "harness:p3-admin", "harness:p3-harbor",
    "harness:p3-judge")
EXPORT_TABLES = ("sc_catalogs", "sc_tasks", "sc_assignments", "sc_decisions", "sc_footprints", "requests", "missions",
                 "versions", "op_bindings", "op_reservations", "op_holds", "op_claims", "op_decisions",
                 "op_cancellations", "op_dock_actions", "op_dock_locks", "op_events", "deliveries", "verifications",
                 "evidence", "reports", "meta")
WORKFLOW_TABLES = ("wf_runs", "wf_nodes", "wf_outbox", "wf_triggers")
TASK_FINAL = tuple(state.value for state in TERMINAL_TASK)
ATTEMPTS = 3


class P3World(P2World):
    """One P3 S0 case: logical sites, docks and aircraft, the scheduler and optionally workflows.

    一个 P3 S0 用例：逻辑站点、机场与飞行器、调度器，以及可选的工作流。
    """

    def __init__(self, case: Path, repo: Path, *, seed: int, catalog: Path | None = None,
                 members: Path | None = None, scheduling: Path | None = None, workflows: Path | None = None,
                 with_workflows: bool = True, dock_period_s: float = 0.25):
        self.scheduling_path = scheduling or repo / SCHEDULING
        self.with_workflows = with_workflows
        self.scheduler_paused = False
        self.dock_period_s = dock_period_s
        self.extra_schedulers: list[Scheduler] = []
        super().__init__(case, repo, seed=seed, workflows=workflows or repo / WORKFLOWS,
                         catalog=catalog or repo / CATALOG, members=members or repo / MEMBERS)

    def _service(self):
        service = P1World._service(self)
        if self.with_workflows:
            workflows = build_workflows(self.repo, self.ledger, self.workflows_path, service.ops,
                                        backups=self.case / "service/backups", clock=self.clock)
            service.workflows = WorkflowEngine(service, workflows, root=self.repo)
            service.workflow_planner = draft_planner(self.repo)
        scheduling = build_scheduling(self.ledger, self.scheduling_path, service.ops,
                                      backups=self.case / "service/backups", clock=self.clock)
        service.scheduler = Scheduler(service, scheduling, worker_id=f"sch-main-{len(self.injections)}")
        return service

    @property
    def scheduler(self) -> Scheduler:
        return self.service.scheduler

    @property
    def tasks(self):
        return self.service.scheduler.store

    def second_scheduler(self) -> Scheduler:
        """Another worker over the same ledger, as a second service process would run. / 同一账本上的另一个 worker。"""
        scheduling = build_scheduling(self.ledger, self.scheduling_path, self.service.ops,
                                      backups=self.case / "service/backups", clock=self.clock)
        worker = Scheduler(self.service, scheduling, worker_id=f"sch-second-{len(self.extra_schedulers)}")
        # Keep the service's own airspace hooks pointing at the main scheduler. / 服务自身的空域钩子仍指向主调度器。
        self.service.dispatch.airspace = self.service.scheduler.airspace
        self.service.dispatch.assignment_problem = self.service.scheduler.assignment_problem
        self.extra_schedulers.append(worker)
        self.inject("second_scheduler", worker=worker.worker)
        return worker

    async def step(self, dt: float = 0.1) -> None:
        await P1World.step(self, dt)
        if self.service.workflows is not None and not self.workflows_paused:
            self.service.tick_workflows()
        if not self.scheduler_paused:
            self.service.tick_scheduler()
        for mission_id in sorted(self.service.dirty):
            self.service.dirty.discard(mission_id)
            self.service.refresh(mission_id)

    # ── the P2 and P1 helpers with this world's identities / 以本世界身份使用的 P2 与 P1 辅助 ──

    async def start(self, workflow_id: str, inputs: dict | None = None, *, actor: str = OPERATOR,
                    request_id: str | None = None, project: str = "campus_ops") -> str:
        return await super().start(workflow_id, inputs, actor=actor, request_id=request_id, project=project)

    async def approve(self, mission_id: str, *, actor: str = OPERATOR, expect: bool = True) -> dict:
        return await super().approve(mission_id, actor=actor, expect=expect)

    async def approve_waiting(self, run_id: str, *, actor: str = OPERATOR) -> list[str]:
        return await super().approve_waiting(run_id, actor=actor)

    async def cancel_run(self, run_id: str, *, actor: str = OPERATOR) -> dict:
        return await super().cancel_run(run_id, actor=actor)

    async def snapshot(self, label: str, project: str = "campus_ops") -> dict:
        response = await self.api(JUDGE, "resources.list", project_id=project)
        self.snapshots.append({"label": label, "at": utcnow().isoformat(), "project": project,
                               "resources": response.get("result")})
        return response.get("result") or {}

    # ── task helpers / 任务单辅助 ──

    async def task(self, asset: str, *, candidates: list[str] | None = None, priority: int = 0,
                   volume: str = "campus_training", key: str | None = None, actor: str = OPERATOR,
                   project: str = "campus_ops", expect: bool = True) -> str | None:
        response = await self.api(actor, "tasks.submit", project_id=project, asset_id=asset, volume_id=volume,
                                  candidates=candidates or [], priority=priority,
                                  idempotency_key=key or f"task-{asset}-{len(self.transcript)}")
        if not response.get("ok"):
            if expect:
                raise RuntimeError(f"task refused: {response.get('issue')}")
            return None
        return response["result"]["task"]["task_id"]

    def task_row(self, task_id: str) -> dict:
        return self.tasks.task(task_id)

    def task_state(self, task_id: str) -> str:
        return self.tasks.task(task_id)["state"]

    def task_final(self, task_id: str) -> bool:
        return self.task_state(task_id) in TASK_FINAL

    def task_mission(self, task_id: str) -> str | None:
        return self.tasks.task(task_id)["mission_id"]

    def task_robot(self, task_id: str) -> str | None:
        return self.tasks.task(task_id)["robot_id"]

    def assignments(self, task_id: str) -> list[dict]:
        return self.tasks.assignments(task_id)

    async def cancel_task(self, task_id: str, *, actor: str = OPERATOR, project: str = "campus_ops") -> dict:
        return await self.api(actor, "tasks.cancel", project_id=project, task_id=task_id,
                              request_id=f"cancel-{task_id[3:11]}-{len(self.transcript)}", reason="harness cancel")

    async def approve_tasks(self, *, actor: str = OPERATOR, only: set[str] | None = None) -> list[str]:
        """A human approves each assigned mission's exact package; nothing is approved for them.

        人逐个审批已分配任务的确切任务包；没有任何东西替他们审批。
        """
        approved = []
        for task in self.tasks.tasks(states=(TaskState.ASSIGNED.value,)):
            if only is not None and task["task_id"] not in only:
                continue
            mission_id = task["mission_id"]
            if mission_id and self.status(mission_id) == "awaiting_approval":
                approver = HARBOR if task["project_id"] == "harbor_ops" and actor == OPERATOR else actor
                response = await self.approve(mission_id, actor=approver, expect=False)
                if response.get("ok"):
                    approved.append(mission_id)
        return approved

    async def drive_tasks(self, until: Callable[[], bool], what: str, *, approve: bool = True,
                          only: set[str] | None = None, timeout_s: float = 120.0,
                          runs: list[str] | None = None) -> None:
        """Step and act as the approver would until `until()`; workflow runs' missions are approved too.

        步进并像审批人那样行动，直到 `until()`；工作流运行的任务同样被审批。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await self.step()
            if approve:
                await self.approve_tasks(only=only)
                for run_id in runs or []:
                    await self.approve_waiting(run_id)
            if until():
                return
        raise ScenarioTimeout(f"timed out waiting for {what}")

    def airborne(self, robot_id: str) -> bool:
        return self.uavs[robot_id].in_air

    def claimed(self, mission_id: str) -> bool:
        return any(c["state"] == "claimed" for c in self.service.ops.store.claims(mission_id))

    # ── export / 导出 ──

    def export(self, scenario: dict) -> None:
        P1World.export(self, scenario)
        tables = {}
        with sqlite3.connect(str(self.case / "service/ledger.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in EXPORT_TABLES + WORKFLOW_TABLES:
                if table in names:
                    tables[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
        (self.case / "service-export/scheduling.json").write_text(
            json.dumps(tables, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        views = {}
        for row in tables["sc_tasks"]:
            try:
                views[row["task_id"]] = self.scheduler._task_view(self.tasks.task(row["task_id"]))
            except Exception as error:  # an export failure is itself evidence / 导出失败本身即证据
                views[row["task_id"]] = {"error": f"{type(error).__name__}: {error}"}
        (self.case / "service-export/task-views.json").write_text(
            json.dumps(views, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        if self.service.workflows is not None and "wf_runs" in tables:
            runs = {}
            for run in tables["wf_runs"]:
                try:
                    runs[run["run_id"]] = self.engine._view(self.store.run(run["run_id"]))
                except Exception as error:  # an export failure is itself evidence / 导出失败本身即证据
                    runs[run["run_id"]] = {"error": f"{type(error).__name__}: {error}"}
            (self.case / "service-export/workflow-views.json").write_text(
                json.dumps(runs, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        paths = {"catalog": self.catalog_path, "members": self.members_path, "scheduling": self.scheduling_path,
                 "workflows": self.workflows_path if self.with_workflows else None}
        (self.case / "input/layout.json").write_text(json.dumps(
            {k: str(Path(v).resolve().relative_to(self.repo.resolve()).as_posix()) if v else None
             for k, v in paths.items()}, indent=2), encoding="utf-8")


def all_final(w: P3World, tasks: list[str]) -> Callable[[], bool]:
    return lambda: all(w.task_final(task) for task in tasks)


# ── scenarios P3-F01–F15 / 场景 ──


async def f01_assign(w: P3World) -> None:
    order = ["asset_mid", "asset_east"] if w.seed % 2 else ["asset_east", "asset_mid"]
    tasks = [await w.task(asset) for asset in order]
    await w.drive_tasks(all_final(w, tasks), "both tasks final", timeout_s=90)


async def f02_exclusion(w: P3World) -> None:
    dock, uav = w.docks["dock_a"], w.uavs["uav_a"]
    variant = {7: "maintenance", 19: "charging", 41: "offline"}.get(w.seed, "maintenance")
    if variant == "maintenance":
        dock.faults.upkeep = "maintenance"
    elif variant == "charging":
        uav.battery.fraction, dock.charge_rate, dock.charging, dock.cooling_left = 0.6, 0.0005, True, 0.0
    else:
        dock.faults.offline = True
    w.inject("dock_a", variant=variant)
    # Long enough for an offline dock's last report to go stale (3 s freshness). / 足以让离线机场的最后报告过期（3 s）。
    await w.hold(4.0)
    task = await w.task("asset_mid")
    await w.drive_tasks(all_final(w, [task]), "mid task final", timeout_s=60)
    dock.faults.upkeep, dock.faults.offline, dock.charge_rate, dock.charging = "normal", False, 0.5, False
    if variant == "maintenance":
        await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_a", action="release",
                    reason="cleared after the case")
    w.inject("dock_a", variant="restored")


async def f03_no_candidate(w: P3World) -> None:
    unregistered = await w.task("asset_west", candidates=["uav_b"])
    unapproved = await w.task("asset_mid", volume="campus_rooftop")
    w.docks["dock_a"].faults.upkeep = "maintenance"
    w.inject("upkeep", dock="dock_a", value="maintenance")
    await w.hold(1.5)
    waiting = await w.task("asset_west")
    await w.drive_tasks(lambda: w.task_final(unregistered) and w.task_final(unapproved), "rejections", timeout_s=20)
    await w.hold(3.0 + w.random.uniform(0, 1))
    await w.snapshot("waiting")
    w.inject("waiting_checked", task=waiting, state=w.task_state(waiting), mission=w.task_mission(waiting))
    w.docks["dock_a"].faults.upkeep = "normal"
    await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_a", action="release",
                reason="inspected and cleared")
    w.inject("admin_release", dock="dock_a")
    await w.drive_tasks(all_final(w, [waiting]), "the waiting task final", timeout_s=60)


async def f04_race(w: P3World) -> None:
    second = w.second_scheduler()
    w.scheduler_paused = True
    mid = await w.task("asset_mid", key=f"race-mid-{w.seed}")
    north = await w.task("asset_north", key=f"race-north-{w.seed}")
    await w.hold(1.0)
    main = w.scheduler
    # Both workers read the same queue and decide before either commits. / 两个 worker 在任何一方提交前读同一队列并作出判定。
    stale = {t: w.tasks.task(t) for t in (mid, north)}
    first = {t: main._decide(stale[t]) for t in (mid, north)}
    other = {t: second._decide(stale[t]) for t in (mid, north)}
    w.inject("decisions", main={t: d["robot_id"] for t, d in first.items()},
             second={t: d["robot_id"] for t, d in other.items()})
    for task_id in (mid, north):
        main._assign(stale[task_id], first[task_id], main._record(stale[task_id], first[task_id]))
        second._assign(stale[task_id], other[task_id], second._record(stale[task_id], other[task_id]))
    w.inject("race_done", mid=w.task_state(mid), north=w.task_state(north))
    w.scheduler_paused = False
    await w.drive_tasks(all_final(w, [mid, north]), "both raced tasks final", timeout_s=120)


async def f05_airspace(w: P3World) -> None:
    first = await w.task("asset_mid", key=f"mid-1-{w.seed}")
    await w.drive_tasks(lambda: w.task_state(first) == "assigned", "first mid assigned", approve=False, timeout_s=15)
    second = await w.task("asset_mid", key=f"mid-2-{w.seed}")
    far = [await w.task("asset_mid2"), await w.task("asset_east2")]
    await w.drive_tasks(all_final(w, [first, second, *far]), "every task final", timeout_s=150)


async def f06_lost_contact(w: P3World) -> None:
    uav = w.uavs["uav_a"]
    first = await w.task("asset_mid", key=f"mid-{w.seed}")
    await w.drive_tasks(lambda: w.airborne("uav_a"), "uav_a airborne", timeout_s=40)
    uav.link.drop_status = uav.link.drop_events = True
    w.inject("link_lost", robot_id="uav_a")
    await w.hold(6.0 + w.random.uniform(0, 1))
    east = await w.task("asset_east", key=f"east-{w.seed}")
    far = await w.task("asset_mid2", key=f"far-{w.seed}")
    await w.drive_tasks(lambda: w.task_final(far), "the far task final", timeout_s=90)
    await w.snapshot("envelope")
    w.inject("envelope_checked", east=w.task_state(east), east_mission=w.task_mission(east))
    uav.link.drop_status = uav.link.drop_events = False
    w.inject("link_restored", robot_id="uav_a")
    await w.drive_tasks(all_final(w, [first, east, far]), "every task final", timeout_s=120)


async def f07_uncertain(w: P3World) -> None:
    uav = w.uavs["uav_a"]
    first = await w.task("asset_mid", key=f"mid-{w.seed}")
    await w.drive_tasks(lambda: w.airborne("uav_a"), "uav_a airborne", timeout_s=40)
    uav.link.drop_events = True
    w.inject("drop_events", robot_id="uav_a", value=True)
    await w.until(lambda: uav.task is None and not uav.in_air and uav.flights, "landed", 40)
    mission = w.task_mission(first)
    await w.until(lambda: any(r.state.value == "uncertain"
                              for r in w.service.ops.store.reservations(mission_id=mission)), "uncertain hold", 30)
    second = await w.task("asset_mid", key=f"mid-2-{w.seed}")
    await w.hold(2.0)
    w.restart_service()
    await w.hold(2.0)
    await w.snapshot("uncertain")
    w.inject("uncertain_checked", first=w.task_state(first), epoch=w.task_row(first)["epoch"],
             second=w.task_state(second))
    uav.link.drop_events = False
    w.inject("drop_events", robot_id="uav_a", value=False)
    await w.drive_tasks(all_final(w, [first, second]), "both tasks final", timeout_s=120)


async def f08_withdraw(w: P3World) -> None:
    task = await w.task("asset_mid", key=f"mid-{w.seed}")
    await w.drive_tasks(lambda: w.task_state(task) == "assigned", "assigned", approve=False, timeout_s=15)
    old = w.task_mission(task)
    w.inject("assigned", task=task, robot=w.task_robot(task), mission=old)
    w.docks["dock_a"].faults.upkeep = "maintenance"
    w.inject("upkeep", dock="dock_a", value="maintenance")
    await w.drive_tasks(lambda: w.task_row(task)["epoch"] >= 2 and w.task_state(task) == "assigned",
                        "reassigned", approve=False, timeout_s=40)
    view = w.service._view(old)
    await w.api(OPERATOR, "approve", probe=True, mission_id=old, version=1,
                package_hash=view["versions"][0]["package_hash"])
    await w.drive_tasks(all_final(w, [task]), "task final", timeout_s=60)
    w.docks["dock_a"].faults.upkeep = "normal"
    await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_a", action="release",
                reason="cleared after the case")


async def f09_withdraw_race(w: P3World) -> None:
    uav = w.uavs["uav_a"]
    # (i) the claim commits first: the withdrawal must refuse. / 领取先提交：撤回必须放弃。
    first = await w.task("asset_mid", key=f"claim-first-{w.seed}")
    await w.drive_tasks(lambda: w.task_mission(first) and w.claimed(w.task_mission(first)), "claimed", timeout_s=40)
    task, live = w.task_row(first), w.tasks.live(first)
    other = w.scheduler._decide(task, skip={live["robot_id"]}, ignore={f"mission:{live['mission_id']}:v1"})
    w.scheduler._withdraw(task, live, 1, ["harness.forced"], other)
    w.inject("forced_withdrawal", task=first, state=w.task_state(first))
    await w.drive_tasks(all_final(w, [first]), "first final", timeout_s=60)
    # (ii) the withdrawal commits first: the delivery is voided before any pull. / 撤回先提交：任何拉取之前投递已作废。
    original = uav.cycle
    held = {"on": True}

    async def cycle_without_pull():
        if not held["on"]:
            await original()

    await w.until(lambda: w.docks["dock_a"].energy()[0] == "ready" and not w.service.ops.store.holders(
        w.service.ops.catalog.resources("uav_a")), "uav_a ready again", 30)
    uav.cycle = cycle_without_pull
    second = await w.task("asset_mid", key=f"withdraw-first-{w.seed}")
    await w.drive_tasks(lambda: w.task_state(second) == "assigned" and w.task_robot(second) == "uav_a"
                        and w.status(w.task_mission(second)) == "queued", "queued on uav_a", timeout_s=40)
    old = w.task_mission(second)
    w.docks["dock_a"].faults.upkeep = "maintenance"
    w.inject("upkeep", dock="dock_a", value="maintenance")
    await w.drive_tasks(lambda: w.task_row(second)["epoch"] >= 2, "withdrawn and reassigned", approve=False,
                        timeout_s=40)
    held["on"] = False
    w.inject("uplink_released", robot_id="uav_a", old_mission=old)
    await w.drive_tasks(all_final(w, [second]), "second final", timeout_s=60)
    uav.cycle = original
    w.docks["dock_a"].faults.upkeep = "normal"
    await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_a", action="release",
                reason="cleared after the case")


async def f10_stale_epoch(w: P3World) -> None:
    uav = w.uavs["uav_a"]
    original = uav.cycle

    async def cycle_without_pull():
        return None

    uav.cycle = cycle_without_pull
    task = await w.task("asset_mid", key=f"mid-{w.seed}")
    await w.drive_tasks(lambda: w.task_state(task) == "assigned" and w.status(w.task_mission(task)) == "queued",
                        "approved and queued", timeout_s=40)
    stale = w.task_row(task)
    stale_decision = w.scheduler._decide(stale)
    old = w.task_mission(task)
    w.docks["dock_a"].faults.upkeep = "maintenance"
    w.inject("upkeep", dock="dock_a", value="maintenance")
    await w.drive_tasks(lambda: w.task_row(task)["epoch"] >= 2, "reassigned", approve=False, timeout_s=40)
    # A worker that decided on the old epoch commits late; nothing may change. / 按旧代次判定的 worker 迟到提交，不得改变任何东西。
    w.scheduler._assign(stale, stale_decision, w.scheduler._record(stale, stale_decision))
    view = w.service._view(old)
    await w.api(OPERATOR, "approve", probe=True, mission_id=old, version=1,
                package_hash=view["versions"][0]["package_hash"])
    w.inject("stale_probes", task=task, epoch=w.task_row(task)["epoch"], old_mission=old,
             superseded=w.scheduler.assignment_problem(old))
    uav.cycle = original
    await w.drive_tasks(all_final(w, [task]), "task final", timeout_s=60)
    w.docks["dock_a"].faults.upkeep = "normal"
    await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_a", action="release",
                reason="cleared after the case")


async def f11_relay(w: P3World) -> None:
    run = await w.start("pair_round")
    faulted = {"on": False}

    def watch() -> bool:
        tasks = w.tasks.tasks(requested_by=f"workflow:{run}")
        mid = next((t for t in tasks if t["asset_id"] == "asset_mid"), None)
        if not faulted["on"] and mid is not None and mid["mission_id"] and \
                w.status(mid["mission_id"]) == "completed":
            w.docks["dock_a"].faults.upkeep = "fault"
            faulted["on"] = True
            w.inject("upkeep", dock="dock_a", value="fault", after=mid["task_id"])
        return w.final(run)

    await w.drive_tasks(watch, "the round final", runs=[run], timeout_s=180)


async def f12_flight_failure(w: P3World) -> None:
    # A motor fault: a never leaves its pad, so the inspection never runs, a definite failure (an uncertain one, such as
    # an inspection cut short in the air, is never relayed).
    # 电机故障：a 从未离开机位，巡检从未执行，这是明确的失败（不确定的失败，如空中被打断的巡检，从不接力）。
    w.uavs["uav_a"].climb_blocked = True
    w.inject("motor_fault", robot_id="uav_a")
    task = await w.task("asset_mid", key=f"mid-{w.seed}")
    await w.drive_tasks(all_final(w, [task]), "task final", timeout_s=240)
    w.uavs["uav_a"].climb_blocked = False


async def f13_cancel(w: P3World) -> None:
    # (i) queued / 排队中
    for dock in ("dock_a", "dock_b"):
        w.docks[dock].faults.upkeep = "maintenance"
    w.inject("upkeep", docks=["dock_a", "dock_b"], value="maintenance")
    await w.hold(1.5)
    queued = await w.task("asset_mid", key=f"queued-{w.seed}")
    await w.hold(1.5)
    await w.cancel_task(queued)
    for dock in ("dock_a", "dock_b"):
        w.docks[dock].faults.upkeep = "normal"
        await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id=dock, action="release",
                    reason="cleared")
    await w.drive_tasks(all_final(w, [queued]), "queued task cancelled", approve=False, timeout_s=20)
    # (ii) assigned, awaiting approval / 已分配、待审批
    waiting = await w.task("asset_mid", key=f"waiting-{w.seed}")
    await w.drive_tasks(lambda: w.task_state(waiting) == "assigned", "assigned", approve=False, timeout_s=20)
    await w.cancel_task(waiting)
    await w.drive_tasks(all_final(w, [waiting]), "waiting task cancelled", approve=False, timeout_s=30)
    # (iii) in flight / 飞行中
    flying = await w.task("asset_north", key=f"flying-{w.seed}")
    await w.drive_tasks(lambda: w.task_robot(flying) is not None and w.airborne(w.task_robot(flying)), "airborne",
                        timeout_s=60)
    await w.cancel_task(flying)
    await w.drive_tasks(all_final(w, [flying]), "flying task cancelled", approve=False, timeout_s=60)
    # (iv) a workflow cancel reaches its tasks / 工作流取消到达其任务单
    run = await w.start("pair_check")
    await w.drive_tasks(lambda: len(w.tasks.tasks(requested_by=f"workflow:{run}")) == 2, "run tasks", approve=False,
                        timeout_s=20)
    await w.cancel_run(run)
    await w.drive_tasks(lambda: w.final(run) and all(t["state"] in TASK_FINAL for t in
                                                     w.tasks.tasks(requested_by=f"workflow:{run}")),
                        "run and tasks cancelled", approve=False, timeout_s=40)


async def f14_isolation(w: P3World) -> None:
    harbor = await w.task("asset_mid", actor=HARBOR, project="harbor_ops", key=f"harbor-{w.seed}")
    own = await w.task("asset_mid", key=f"own-{w.seed}")
    probes = [
        (HARBOR, "tasks.submit", {"project_id": "campus_ops", "asset_id": "asset_mid", "volume_id": "campus_training",
                                  "candidates": [], "priority": 0, "idempotency_key": "probe-cross-submit"}),
        (OPERATOR, "tasks.get", {"project_id": "harbor_ops", "task_id": harbor}),
        (OPERATOR, "tasks.get", {"project_id": "campus_ops", "task_id": harbor}),
        (OPERATOR, "tasks.list", {"project_id": "harbor_ops"}),
        (OPERATOR, "tasks.cancel", {"project_id": "harbor_ops", "task_id": harbor, "request_id": "probe-cancel-1",
                                    "reason": "probe"}),
        (OPERATOR, "tasks.submit", {"project_id": "campus_ops", "asset_id": "asset_mid", "volume_id": "campus_training",
                                    "candidates": ["uav_h"], "priority": 0, "idempotency_key": "probe-outside"}),
        (VIEWER, "tasks.submit", {"project_id": "campus_ops", "asset_id": "asset_mid", "volume_id": "campus_training",
                                  "candidates": [], "priority": 0, "idempotency_key": "probe-viewer"}),
        (VIEWER, "tasks.cancel", {"project_id": "campus_ops", "task_id": own, "request_id": "probe-cancel-2",
                                  "reason": "probe"}),
        ("harness:stranger", "tasks.list", {"project_id": "campus_ops"}),
        ("", "tasks.list", {"project_id": "campus_ops"}),
    ]
    w.random.shuffle(probes)
    for actor, method, params in probes:
        await w.api(actor, method, probe=True, **params)
    await w.api("a2a:probe-client", "tasks.submit", trust="third_party", probe=True, project_id="campus_ops",
                asset_id="asset_mid", volume_id="campus_training", candidates=[], priority=0,
                idempotency_key="probe-a2a")
    await w.api("dock:p3-s0-sim", "tasks.list", trust="backend", probe=True, project_id="campus_ops")
    await w.drive_tasks(all_final(w, [harbor, own]), "both projects' tasks final", timeout_s=90)


async def f15_declined(w: P3World) -> None:
    task = await w.task("asset_mid", key=f"mid-{w.seed}")
    await w.drive_tasks(lambda: w.task_state(task) == "assigned", "assigned", approve=False, timeout_s=15)
    mission = w.task_mission(task)
    await w.api(OPERATOR, "decline", mission_id=mission, version=1, reason="harness declines the robot choice")
    await w.drive_tasks(all_final(w, [task]), "task final", approve=False, timeout_s=30)
    await w.hold(2.0)


SCENARIOS: dict[str, Callable] = {
    "p3_f01_assign": f01_assign, "p3_f02_exclusion": f02_exclusion, "p3_f03_no_candidate": f03_no_candidate,
    "p3_f04_race": f04_race, "p3_f05_airspace": f05_airspace, "p3_f06_lost_contact": f06_lost_contact,
    "p3_f07_uncertain": f07_uncertain, "p3_f08_withdraw": f08_withdraw, "p3_f09_withdraw_race": f09_withdraw_race,
    "p3_f10_stale_epoch": f10_stale_epoch, "p3_f11_relay": f11_relay, "p3_f12_flight_failure": f12_flight_failure,
    "p3_f13_cancel": f13_cancel, "p3_f14_isolation": f14_isolation, "p3_f15_declined": f15_declined,
}


# ── the scale ladder / 规模阶梯 ──


async def ladder_stream(w: P3World, nodes: int, budget: int, total: int) -> dict:
    """A closed-loop task stream: at most `budget` tasks in flight, `total` tasks overall, a fixed mix per seed.

    闭环任务流：在途任务至多 `budget` 个，总计 `total` 个，每个种子的组合固定。
    """
    pairs = nodes // 2
    rng = random.Random(w.seed)
    plan = []
    for index in range(total):
        pair = rng.randrange(pairs)
        roll = rng.random()
        if roll < 0.1:
            plan.append({"asset": f"asset_west_p{pair:02d}", "candidates": [f"uav_p{pair:02d}r"]})  # rejected
        elif roll < 0.6:
            plan.append({"asset": f"asset_{rng.choice(['mid', 'north'])}_p{pair:02d}", "candidates": []})
        else:
            side = rng.choice(["west", "east"])
            plan.append({"asset": f"asset_{side}_p{pair:02d}", "candidates": []})
    maintenance = rng.sample(range(pairs), max(1, pairs // 10))
    submitted: list[str] = []
    started = time.monotonic()
    samples = []
    injected = released = False
    while True:
        await w.step()
        await w.approve_tasks()
        live = [t for t in submitted if not w.task_final(t)]
        if not injected and len(submitted) >= total // 3:
            for pair in maintenance:
                w.docks[f"dock_p{pair:02d}l"].faults.upkeep = "maintenance"
            w.inject("ladder_maintenance", pairs=maintenance)
            injected = True
        if injected and not released and len(submitted) == total:
            # Maintenance ends once every task is in, so a task only a locked dock's aircraft can serve drains too;
            # telemetry never releases a lock, an admin does (D055).
            # 所有任务提交后维护结束，只有被锁机场的飞行器能执行的任务也能排空；遥测从不解锁，由 admin 解锁（D055）。
            for pair in maintenance:
                w.docks[f"dock_p{pair:02d}l"].faults.upkeep = "normal"
                await w.api(ADMIN, "resources.maintenance", project_id="fleet_ops", dock_id=f"dock_p{pair:02d}l",
                            action="release", reason="ladder maintenance over")
            w.inject("ladder_maintenance_released", pairs=maintenance)
            released = True
        while len(live) < budget and len(submitted) < total:
            item = plan[len(submitted)]
            task = await w.task(item["asset"], candidates=item["candidates"], project="fleet_ops",
                                key=f"ladder-{nodes}-{len(submitted)}")
            submitted.append(task)
            live.append(task)
        samples.append({"t": round(time.monotonic() - started, 2), "live": len(live),
                        "airborne": sum(1 for u in w.uavs.values() if u.in_air)})
        if len(submitted) == total and not live:
            break
        if time.monotonic() - started > 60 + total * 8:
            raise ScenarioTimeout(f"ladder {nodes} did not drain")
    return {"nodes": nodes, "budget": budget, "total": total, "duration_s": round(time.monotonic() - started, 2),
            "samples": samples, "maintenance_pairs": maintenance}


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


def host() -> dict:
    import os

    return {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
            "cpus": os.cpu_count(), "python": platform.python_version()}


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str) -> dict:
    """Run one S0 case or ladder rung and judge it online and from the recordings. / 运行一个用例或阶梯档并裁判两次。"""
    from drone_agent.eval.judge_p3 import judge_case
    from drone_agent.eval.p3_layout import ladder

    if case.exists():
        shutil.rmtree(case)
    nodes = scenario.get("nodes")
    layout = None
    if nodes:
        layout = ladder(repo, nodes, repo / "outputs" / "p3-ladder" / f"{case.name}")
        world = P3World(case, repo, seed=seed, catalog=Path(layout["files"]["catalog"]),
                        members=Path(layout["files"]["members"]), scheduling=Path(layout["files"]["scheduling"]),
                        with_workflows=False)
    else:
        world = P3World(case, repo, seed=seed)
    error, stream = None, None
    started = time.monotonic()
    try:
        await world.settle()
        if nodes:
            stream = await ladder_stream(world, nodes, scenario["budget"], scenario.get("tasks", nodes * 2))
        else:
            await SCENARIOS[scenario["id"]](world)
        for uav in world.uavs.values():
            await uav.settle()
        await world.hold(1.0)
    except Exception as failure:  # the judge sees the failure as evidence / 裁判把失败当作证据
        error = f"{type(failure).__name__}: {failure}"[:400]
    finally:
        pending = {uav.task for uav in world.uavs.values() if uav.task is not None and not uav.task.done()}
        for task in pending:
            task.cancel()
        if pending:
            # Let cancelled flights close their journals before the export reads them. / 导出前先让被取消的飞行关闭日志。
            await asyncio.wait(pending, timeout=30)
    world.export({"scenario": scenario["id"], "seed": seed, "source_sha": sha, "layer": "S0",
                  "description": scenario.get("description", ""), "expected": scenario.get("expected", {}),
                  "harness_error": error, "duration_s": round(time.monotonic() - started, 2),
                  "ladder": {**stream, "layout_sha256": layout["sha256"], "host": host()} if stream else None})
    world.ledger.close()
    online = judge_case(case, repo)
    replay = judge_case(case, repo, use_replay=True)
    keys = ("classification", "problems", "false_success_reports", "counts")
    agrees = all(online.get(k) == replay.get(k) for k in keys)
    result = {**{k: online.get(k) for k in ("scenario", "seed", "classification", "problems", "counts",
                                             "false_success_reports", "expected", "metrics")},
              "passed": bool(online.get("passed")) and agrees, "replay_agrees": agrees, "harness_error": error}
    (case / "judge").mkdir(exist_ok=True)
    (case / "judge/result.json").write_text(json.dumps(online, indent=2, default=str), encoding="utf-8")
    (case / "judge/replay.json").write_text(json.dumps(replay, indent=2, default=str), encoding="utf-8")
    return result


async def run_suite(output: Path, repo: Path, *, sha: str, only: list[str] | None = None,
                    seeds: list[int] | None = None, part: str = "s0") -> dict:
    """`part` is `s0` (the fault matrix) or `ladder` (the scale rungs). / `part` 为 `s0`（故障矩阵）或 `ladder`（规模阶梯）。"""
    from drone_agent.eval.judge_p3 import COUNTS

    definition = suite(repo)
    results, voided = [], []
    for scenario in definition[part]:
        if only and scenario["id"] not in only:
            continue
        for seed in seeds or scenario.get("seeds") or definition["seeds"]:
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
    summary = {"schema_version": "0.1.0", "layer": "S0", "part": part, "source_sha": sha, "suite": definition["format"],
               "cases": len(results), "passed": sum(1 for r in results if r["passed"]),
               "status": "passed" if results and all(r["passed"] for r in results) else "failed",
               "counts": counts, "results": results, "voided_attempts": voided, "host": host()}
    output.mkdir(parents=True, exist_ok=True)
    (output / f"suite-{part}.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--sha", default="uncommitted")
    parser.add_argument("--part", choices=["s0", "ladder"], default="s0")
    parser.add_argument("--scenario", default="all", help="all or comma-separated scenario ids")
    parser.add_argument("--seeds", default=None, help="comma-separated seeds; defaults to the suite's")
    args = parser.parse_args()
    only = None if args.scenario == "all" else args.scenario.split(",")
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else None
    summary = asyncio.run(run_suite(args.output, args.root, sha=args.sha, only=only, seeds=seeds, part=args.part))
    print(json.dumps({k: summary[k] for k in ("status", "cases", "passed", "counts")}))
    raise SystemExit(0 if summary["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
