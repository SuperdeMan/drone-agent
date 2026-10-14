"""S0 world for X1 (D088): the P4 world with the P3 scheduler, the handoff engine and logical ground robots.

The world is the P4 S0 world (the formal mission service with its operations, workflow and business catalogs, logical
docks, and logical aircraft running the real onboard uplink, guardian and executive), with the X1 scheduling catalog
and the handoff engine loaded into the same service the way `--air-ground` loads them, and one logical ground robot per
dockless catalog robot: the real uplink, which answers offers from the robot's own map, and a fresh guardian and
executive per mission around one physical ground body. Operators and approvers act only through the API under their
own identities; faults are applied to the simulated world, to harness-only links, or by a harness acting as a robot
over the hub. Each case of the X1 matrix records what the independent judge needs: the API transcript, the
injections, every robot's missions and truth, the scene and a read-only export of the scheduling, operations,
workflow, business and handoff tables. The scenario code decides nothing about pass or fail.

X1 的 S0 世界（D088）：P4 世界加上 P3 调度器、交接引擎与逻辑地面车。

世界即 P4 S0 世界（带运营、工作流与业务目录的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与 executive 的
逻辑飞行器），并按 `--air-ground` 的方式把 X1 调度目录与交接引擎加载到同一服务中；目录中每台没有机场的机器人对应一台逻辑
地面车：真实的 uplink（依据地面车自己的地图回答提议），以及围绕同一个物理车体、每次任务新建的 guardian 与 executive。操作者
与审批人只以各自身份经 API 行动；故障作用于模拟世界、编排专用链路，或由编排以机器人身份经 hub 发出。X1 矩阵的每个用例都
记录独立裁判需要的内容：API 记录、注入、每台机器人的任务与真值、场景，以及调度、运营、工作流、业务与交接表的只读导出。
场景代码不对通过与否做任何决定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml

from drone_agent.contracts import FactSource, Frame, Position, WorldFact, utcnow
from drone_agent.eval.logical_flight import Battery
from drone_agent.eval.logical_ground import quick_ground
from drone_agent.eval.logical_rover import LogicalRover
from drone_agent.eval.p1_world import TERMINAL, ScenarioTimeout
from drone_agent.eval.p3_world import EXPORT_TABLES as SCHEDULING_TABLES
from drone_agent.eval.p3_world import WORKFLOW_TABLES, host
from drone_agent.eval.p4_world import P4World
from drone_agent.eval.x1_scene import Anomaly, X1Scene
from drone_agent.fleet.air_ground import build_air_ground
from drone_agent.fleet.handoff import HandoffRefused
from drone_agent.fleet.scheduler import Scheduler, build_scheduling
from drone_agent.fleet.transport import LocalFleetClient
from drone_agent.mission.registry import load_registry

SUITE = "configs/scenarios/x1_suite.yaml"
CATALOG = "configs/sites/x1_campus_v1.yaml"
MEMBERS = "configs/sites/x1_members_s0.yaml"
SCHEDULING = "configs/scheduling/x1_campus_v1.yaml"
WORKFLOWS = "configs/workflows/x1_campus_v1.yaml"
BUSINESS = "configs/analysis/x1_campus_v1.yaml"
ROVER_POLICY = "configs/recovery_policies/rover_campus_v1.yaml"
# RTK-grade aircraft positions for the point cases; the P1 value (1 m^2) gives search regions. / 点目标用例使用 RTK 级
# 的飞行器位置；P1 的取值（1 m^2）得到搜索区域。
RTK = (0.04, 0.0, 0.0, 0.0, 0.04, 0.0, 0.0, 0.0, 0.09)
P1_COVARIANCE = (1, 0, 0, 0, 1, 0, 0, 0, 1)
HANDOFF_TABLES = ("ho_handoffs", "ho_events", "bz_rechecks", "bz_findings", "bz_jobs")
OPERATOR, REVIEWER, VIEWER, ADMIN, HARBOR, JUDGE = (
    "harness:x1-operator", "harness:x1-reviewer", "harness:x1-viewer", "harness:x1-admin", "harness:x1-harbor",
    "harness:x1-judge")
RUN_FINAL = ("completed", "failed", "outcome_unknown", "cancelled")
ATTEMPTS = 3


class X1World(P4World):
    """One X1 S0 case: logical sites, docks, aircraft and ground robots, the scheduler and the handoff engine.

    一个 X1 S0 用例：逻辑站点、机场、飞行器与地面车，调度器与交接引擎。
    """

    def __init__(self, case: Path, repo: Path, *, seed: int):
        self.scheduling_path = repo / SCHEDULING
        self.scheduler_paused = False
        self.rovers: dict[str, LogicalRover] = {}
        self.paused_rovers: set[str] = set()
        # Ground missions the harness approves by itself; a case may hold them back. / 编排自行审批的地面任务；用例可暂缓。
        self.approve_ground = True
        super().__init__(case, repo, seed=seed, business=repo / BUSINESS, workflows=repo / WORKFLOWS,
                         members=repo / MEMBERS, catalog=repo / CATALOG,
                         signatures={"asset_mid": "green", "asset_west": "red"})
        ops, scheduling = self.service.ops, self.service.scheduler.catalog
        self.scene = X1Scene({site_id: scheduling.origin(site_id) for site_id in ops.catalog.sites})
        for robot_id, uav in self.uavs.items():
            site = ops.catalog.robots[robot_id].site_id
            uav.camera = self.scene.aerial_camera(uav, site, ops.registries[site].data)
            uav.covariance = RTK

    def _service(self):
        service = super()._service()
        backups = self.case / "service/backups"
        scheduling = build_scheduling(self.ledger, self.scheduling_path, service.ops, backups=backups,
                                      clock=self.clock)
        service.scheduler = Scheduler(service, scheduling, worker_id=f"sch-main-{len(self.injections)}")
        build_air_ground(service, backups=backups, clock=self.clock)
        return service

    def _add_robot(self, robot_id: str, entry) -> None:
        if entry.dock_id is not None:
            super()._add_robot(robot_id, entry)
            return
        ops, repo = self.service.ops, self.repo
        registry = quick_ground(load_registry(repo, repo / ops.catalog.sites[entry.site_id].scene))
        registry.capability = ops.capabilities[robot_id]
        self.rovers[robot_id] = LogicalRover(self.case / "robots" / robot_id, registry, self.hub, self.trust,
                                             repo / ROVER_POLICY, battery=Battery(1.0))

    @property
    def scheduler(self) -> Scheduler:
        return self.service.scheduler

    @property
    def handoffs(self):
        return self.service.handoffs

    async def step(self, dt: float = 0.1) -> None:
        await super().step(dt)
        catalog = self.service.ops.catalog
        for robot_id, rover in self.rovers.items():
            rover.body.blocks = self.scene.ground_blocks(catalog.robots[robot_id].site_id)
            if robot_id not in self.paused_rovers:
                await rover.cycle()
        if not self.scheduler_paused:
            self.service.tick_scheduler()
        if self.approve_ground:
            await self.approve_ground_missions()
        for mission_id in sorted(self.service.dirty):
            self.service.dirty.discard(mission_id)
            self.service.refresh(mission_id)

    # ── the P1, P2 and P4 helpers with this world's identities / 以本世界身份使用的 P1、P2 与 P4 辅助 ──

    async def approve(self, mission_id: str, *, actor: str = OPERATOR, expect: bool = True) -> dict:
        return await super().approve(mission_id, actor=actor, expect=expect)

    async def cancel(self, mission_id: str, *, actor: str = OPERATOR) -> dict:
        return await super().cancel(mission_id, actor=actor)

    async def start(self, workflow_id: str, inputs: dict | None = None, *, actor: str = OPERATOR,
                    request_id: str | None = None, project: str = "campus_ops") -> str:
        return await super().start(workflow_id, inputs, actor=actor, request_id=request_id, project=project)

    async def review(self, run_id: str, node_id: str, decision: str = "confirmed", *, actor: str = REVIEWER,
                     request_id: str | None = None, probe: bool = False, project: str | None = None) -> dict:
        return await super().review(run_id, node_id, decision, actor=actor, request_id=request_id, probe=probe,
                                    project=project)

    async def approve_waiting(self, run_id: str, *, actor: str = OPERATOR) -> list[str]:
        return await super().approve_waiting(run_id, actor=actor)

    async def cancel_run(self, run_id: str, *, actor: str = OPERATOR) -> dict:
        return await super().cancel_run(run_id, actor=actor)

    async def bz(self, method: str, *, actor: str = OPERATOR, trust: str = "first_party", probe: bool = False,
                 **params) -> dict:
        return await self.api(actor, method, trust=trust, probe=probe, **params)

    async def snapshot(self, label: str, project: str = "campus_ops") -> dict:
        response = await self.api(JUDGE, "resources.list", project_id=project)
        self.snapshots.append({"label": label, "at": utcnow().isoformat(), "project": project,
                               "resources": response.get("result")})
        return response.get("result") or {}

    # ── the scene and the robots / 场景与机器人 ──

    def anomaly(self, site_id: str, x: float, y: float, *, size_m: float = 0.8, colour: str = "orange",
                from_air: bool = True, from_ground: bool = True) -> Anomaly:
        """Place a declared block in the world, recorded for the judge. / 在世界中放置一个声明方块，记录供裁判读取。"""
        placed = self.scene.place(site_id, x, y, size_m=size_m, colour=colour, from_air=from_air,
                                  from_ground=from_ground)
        self.inject("anomaly_placed", x=placed.x, y=placed.y, size_m=size_m, colour=colour, from_air=from_air,
                    from_ground=from_ground)
        return placed

    def remove_anomaly(self, anomaly: Anomaly) -> None:
        self.scene.remove(anomaly)
        self.inject("anomaly_removed", x=anomaly.x, y=anomaly.y, colour=anomaly.colour)

    def link(self, robot_id: str, *, up: bool) -> None:
        """A harness-only link loss of one ground robot: its uplink neither reports nor pulls, so a cancel cannot reach
        it, while the mission it runs carries on. / 只在编排中使用的地面车链路中断：其 uplink 既不上报也不拉取，取消请求
        到不了车上，而车上正在执行的任务继续进行。"""
        (self.paused_rovers.add if not up else self.paused_rovers.discard)(robot_id)
        self.inject("link_restored" if up else "link_lost", robot_id=robot_id)

    def pause(self, robot_id: str, *, paused: bool) -> None:
        """A ground robot whose uplink stops pulling (and reporting) altogether. / 上行完全停止拉取与上报的地面车。"""
        (self.paused_rovers.add if paused else self.paused_rovers.discard)(robot_id)
        self.inject("rover_paused" if paused else "rover_resumed", robot_id=robot_id)

    def carry_home(self, robot_id: str) -> None:
        """A person brings a ground robot that stopped on the way back to its parking spot (harness only); its hold is
        released only once it reports itself parked there. / 由人把停在途中的地面车送回停车位（只在编排中）；它在那里报告
        已停放之后，持有才会释放。"""
        body = self.rovers[robot_id].body
        body.position, body.velocity, body.path, body.coast = list(body.registry.parking()), (0.0, 0.0), [], None
        self.inject("carried_home", robot_id=robot_id)

    async def approve_ground_missions(self) -> list[str]:
        """A human approves each ground mission's exact package when it waits. / 人逐个审批等待中的地面任务的确切任务包。"""
        approved = []
        for task in self.scheduler.store.tasks(states=("assigned",)):
            mission = task["mission_id"]
            if task["source"] == "handoff" and mission and self.status(mission) == "awaiting_approval":
                response = await self.approve(mission, expect=False)
                if response.get("ok"):
                    approved.append(mission)
        return approved

    def rechecks(self) -> list[dict]:
        return self.handoffs.store.rechecks()

    def ground_mission(self, task_id: str) -> str | None:
        task = self.scheduler.store.task(task_id)
        return task["mission_id"] if task else None

    async def drive_run(self, run_id: str, *, decision: str = "confirmed", timeout_s: float = 150,
                        during: Callable[[], Awaitable[None]] | None = None) -> dict:
        """Drive one workflow run: approve its missions, confirm or dismiss its review, and run `during` each step.

        推进一次工作流运行：审批其任务，确认或驳回其复核，并在每一步运行 `during`。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await self.step()
            await self.approve_waiting(run_id)
            if during is not None:
                await during()
            nodes = self.store.nodes(run_id)
            if "review" in nodes and nodes["review"]["state"] == "waiting":
                await self.review(run_id, "review", decision)
            if self.run(run_id)["state"] in RUN_FINAL:
                return nodes
        raise ScenarioTimeout(f"run {run_id} did not end")

    # ── handoff helpers / 交接辅助 ──

    def offer(self, *, robot_id: str, target: tuple[float, float], asset_id: str = "asset_mid",
              signature: str = "orange", kind: str = "point", radius_m: float | None = None, ttl_s: float = 20,
              key: str | None = None, map_version: str = "x1_ground_a_v1") -> tuple[dict, dict]:
        """A ground recheck task and its offer, as the coordinator creates them (the business loop's step).

        与协调器的做法相同，创建一个地面复核任务及其提议（业务闭环中的一步）。
        """
        task, _ = self.scheduler.submit_handoff(
            requested_by="coordinator:x1", key=key or f"recheck-{asset_id}-{len(self.injections)}",
            project_id="campus_ops", asset_id=asset_id, volume_id=map_version, robot_id=robot_id,
            detail={"asset_id": asset_id, "signature": signature})
        value = {"kind": kind, "description": f"{signature} block, about 0.8 m", "semi_major_95_m": 0.5}
        if radius_m is not None:
            value["radius_m"] = radius_m
        fact = WorldFact(fact_id=f"target-{task['task_id']}", subject=asset_id,
                         predicate="x1.target_point" if kind == "point" else "x1.search_region", value=value,
                         frame=Frame(frame_id="map_enu", map_version=map_version),
                         position=Position(x=target[0], y=target[1], z=0.0,
                                           covariance=(0.05, 0.0, 0.0, 0.0, 0.05, 0.0, 0.0, 0.0, 0.0)),
                         timestamp=utcnow(), source=FactSource.SENSOR)
        handoff = self.handoffs.offer(task_id=task["task_id"], project_id="campus_ops", robot_id=robot_id,
                                      target=fact, evidence_refs=[], ttl_s=ttl_s, actor="coordinator:x1")
        self.inject("handoff_offered", task_id=task["task_id"], handoff_id=handoff["handoff_id"], robot_id=robot_id)
        return task, handoff

    def handoff(self, handoff_id: str) -> dict:
        return self.handoffs.store.handoff(handoff_id)

    async def reply_as(self, robot_id: str, reply: dict) -> dict:
        """A reply sent by the harness as `robot_id` over the hub, as a faulty or late robot would. / 编排以
        `robot_id` 身份经 hub 发出的回复，如同出故障或迟到的机器人。"""
        receipt = await LocalFleetClient(self.hub, robot_id).reply_handoff(reply)
        self.inject("reply_sent", robot_id=robot_id, handoff_id=reply.get("handoff_id"),
                    accepted=reply.get("accepted"), receipt=receipt.accepted, reason=receipt.reason)
        return {"accepted": receipt.accepted, "reason": receipt.reason}

    # ── export / 导出 ──

    def export(self, scenario: dict) -> None:
        super().export(scenario)
        world = self.case / "world"
        for rover in self.rovers.values():
            rover.export(world)
        tables = {}
        with sqlite3.connect(str(self.case / "service/ledger.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in SCHEDULING_TABLES + WORKFLOW_TABLES + HANDOFF_TABLES:
                if table in names:
                    tables[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
        (self.case / "service-export/scheduling.json").write_text(
            json.dumps(tables, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        runs = {}
        for run in tables.get("wf_runs", []):
            try:
                runs[run["run_id"]] = self.engine._view(self.store.run(run["run_id"]))
            except Exception as error:  # an export failure is itself evidence / 导出失败本身即证据
                runs[run["run_id"]] = {"error": f"{type(error).__name__}: {error}"}
        (self.case / "service-export/workflow-views.json").write_text(
            json.dumps(runs, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        scene = {"origins": {k: list(v) for k, v in self.scene.origins.items()},
                 "anomalies": [a.__dict__ for a in self.scene.anomalies]}
        (world / "scene.json").write_text(json.dumps(scene, indent=2), encoding="utf-8")
        paths = {"catalog": self.catalog_path, "members": self.members_path, "scheduling": self.scheduling_path,
                 "workflows": self.workflows_path, "business": self.business_path}
        (self.case / "input/layout.json").write_text(json.dumps(
            {k: str(Path(v).resolve().relative_to(self.repo.resolve()).as_posix()) for k, v in paths.items()},
            indent=2), encoding="utf-8")


# ── scenarios / 场景 ──

# Where the block sits near asset_mid (5, 4) per seed, on the marker so the frame shows it. / 各种子下方块相对
# asset_mid (5, 4) 的位置，压在标记上，使画面能拍到它。
MID_OFFSETS = {7: (0.3, 0.2), 19: (-0.4, 0.3), 41: (0.2, -0.5)}


def mid_block(w: X1World) -> tuple[float, float]:
    dx, dy = MID_OFFSETS.get(w.seed, (0.3, 0.2))
    return 5.0 + dx, 4.0 + dy


async def recheck_of(w: X1World, timeout_s: float = 60) -> dict:
    await w.until(lambda: bool(w.rechecks()), "a recheck", timeout_s)
    return w.rechecks()[0]


async def x1_nominal(w: X1World) -> None:
    w.anomaly("site_a", *mid_block(w))
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})
    await w.drive_run(run)


async def x1_unreachable(w: X1World) -> None:
    # Seen from asset_north's frame, inside the fenced compound of the ground map. / 从 asset_north 的画面中可见，
    # 位于地面地图的围栏院子之内。
    w.anomaly("site_a", 7.2, 11.6)
    run = await w.start("anomaly_recheck", {"asset": "asset_north"})
    await w.drive_run(run)
    await w.hold(1.0)


async def x1_search_region(w: X1World) -> None:
    w.uavs["uav_a"].covariance = P1_COVARIANCE
    w.inject("aircraft_covariance", robot_id="uav_a", variance_m2=1.0)
    w.anomaly("site_a", *mid_block(w))
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})
    await w.drive_run(run)


async def x1_not_found(w: X1World) -> None:
    block = w.anomaly("site_a", *mid_block(w))
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})
    removed = {"done": False}

    async def remove_once() -> None:
        if not removed["done"] and w.rechecks():
            w.remove_anomaly(block)
            removed["done"] = True

    await w.drive_run(run, during=remove_once)


async def x1_false_positive(w: X1World) -> None:
    # Seen from the air only: an artefact of the aerial view, never on the ground. / 只从空中可见：空中画面的伪影。
    w.anomaly("site_a", *mid_block(w), from_ground=False)
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})
    await w.drive_run(run)


async def x1_offer_timeout(w: X1World) -> None:
    w.pause("ugv_01", paused=True)
    w.anomaly("site_a", *mid_block(w))
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})

    async def resume_after_expiry() -> None:
        recheck = w.rechecks()[0] if w.rechecks() else None
        if recheck and recheck["handoff_id"] and "ugv_01" in w.paused_rovers:
            if w.handoff(recheck["handoff_id"])["state"] == "timed_out":
                w.pause("ugv_01", paused=False)

    await w.drive_run(run, during=resume_after_expiry)
    if "ugv_01" in w.paused_rovers:
        w.pause("ugv_01", paused=False)
    await w.hold(2.0)


async def x1_duplicate_offer(w: X1World) -> None:
    w.pause("ugv_01", paused=True)
    task, handoff = w.offer(robot_id="ugv_01", target=(3.3, 8.2))
    try:
        w.handoffs.offer(task_id=task["task_id"], project_id="campus_ops", robot_id="ugv_01",
                         target=WorldFact.model_validate(handoff["target"]), evidence_refs=[], ttl_s=20,
                         actor="coordinator:x1")
        w.inject("second_offer", task_id=task["task_id"], refused=None)
    except HandoffRefused as refusal:
        w.inject("second_offer", task_id=task["task_id"], refused=refusal.code)
    # A foreign robot answers first, then the right robot answers twice. / 先由其他机器人作答，再由本机作答两次。
    await w.reply_as("ugv_02", {"handoff_id": handoff["handoff_id"], "accepted": True})
    w.pause("ugv_01", paused=False)
    await w.until(lambda: w.handoff(handoff["handoff_id"])["state"] != "offered", "the robot's answer", 15)
    answer = w.rovers["ugv_01"].uplink.state["handoffs"][handoff["handoff_id"]]
    await w.reply_as("ugv_01", answer)
    await w.until(lambda: w.ground_mission(task["task_id"]) is not None, "the ground mission", 20)
    mission = w.ground_mission(task["task_id"])
    await w.until(lambda: w.status(mission) in TERMINAL, "the ground mission's end", 90)
    await w.hold(2.0)


async def x1_late_accept(w: X1World) -> None:
    w.pause("ugv_01", paused=True)
    task, handoff = w.offer(robot_id="ugv_01", target=(3.3, 8.2))
    await w.hold(0.5)
    await w.api(OPERATOR, "tasks.cancel", project_id="campus_ops", task_id=task["task_id"],
                request_id=f"cancel-{task['task_id']}", reason="no longer needed")
    await w.until(lambda: w.handoff(handoff["handoff_id"])["state"] == "withdrawn", "the offer withdrawn", 15)
    await w.reply_as("ugv_01", {"handoff_id": handoff["handoff_id"], "accepted": True,
                                "map_version": "x1_ground_a_v1", "path_length_m": 6.18, "eta_s": 6.2})
    w.pause("ugv_01", paused=False)
    await w.hold(3.0)


async def x1_accept_after_timeout(w: X1World) -> None:
    w.pause("ugv_01", paused=True)
    task, handoff = w.offer(robot_id="ugv_01", target=(3.3, 8.2), ttl_s=10)
    await w.until(lambda: w.handoff(handoff["handoff_id"])["state"] == "timed_out", "the offer timed out", 25)
    await w.reply_as("ugv_01", {"handoff_id": handoff["handoff_id"], "accepted": True,
                                "map_version": "x1_ground_a_v1", "path_length_m": 6.18, "eta_s": 6.2})
    w.pause("ugv_01", paused=False)
    await w.hold(3.0)


async def x1_link_loss_driving(w: X1World) -> None:
    w.anomaly("site_a", *mid_block(w))
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})
    state = {"lost_at": None, "restored": False, "second": None}

    async def link_events() -> None:
        body = w.rovers["ugv_01"].body
        if state["lost_at"] is None and not w.rovers["ugv_01"].idle and body.mode == "MISSION":
            w.link("ugv_01", up=False)
            state["lost_at"] = time.monotonic()
            # Another ground robot is offered a target in the same area while the first is silent.
            # 第一台车失联期间，向另一台地面车提议同一片区域的目标。
            x, y = mid_block(w)
            state["second"] = w.offer(robot_id="ugv_02", target=(x + 6.0, y + 4.0), map_version="x1_ground_b_v1",
                                      key="recheck-second")
        if state["lost_at"] is not None and not state["restored"] and time.monotonic() - state["lost_at"] > 12:
            w.link("ugv_01", up=True)
            state["restored"] = True

    await w.drive_run(run, during=link_events)
    if state["second"] is not None:
        task = state["second"][0]
        await w.until(lambda: w.scheduler.store.task(task["task_id"])["state"] not in ("queued", "assigned")
                      or w.ground_mission(task["task_id"]) is not None and
                      w.status(w.ground_mission(task["task_id"])) in TERMINAL, "the second task", 90)
    await w.hold(2.0)


async def x1_old_owner_continues(w: X1World) -> None:
    w.anomaly("site_a", *mid_block(w))
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})
    state = {"lost": False, "cancelled": False, "second": None, "restored": False, "at": None}

    async def events() -> None:
        rover = w.rovers["ugv_01"]
        recheck = w.rechecks()[0] if w.rechecks() else None
        mission = w.ground_mission(recheck["ground_task_id"]) if recheck and recheck["ground_task_id"] else None
        if not state["lost"] and mission and not rover.idle and rover.body.mode == "MISSION":
            w.link("ugv_01", up=False)
            state.update(lost=True, at=time.monotonic())
        if state["lost"] and not state["cancelled"] and time.monotonic() - state["at"] > 2:
            # The coordinator gives up on the silent robot and offers the finding to another one; the cancel cannot
            # reach the first robot, which keeps driving. / 协调器放弃失联的车并把发现提议给另一台；取消到不了第一台
            # 车，它继续行驶。
            await w.cancel(mission)
            x, y = mid_block(w)
            state["second"] = w.offer(robot_id="ugv_02", target=(x + 6.0, y + 4.0), map_version="x1_ground_b_v1",
                                      key="recheck-old-owner")
            state["cancelled"] = True
        if state["cancelled"] and not state["restored"] and time.monotonic() - state["at"] > 14:
            w.link("ugv_01", up=True)
            state["restored"] = True

    await w.drive_run(run, during=events)
    if state["second"] is not None:
        task = state["second"][0]
        await w.until(lambda: w.scheduler.store.task(task["task_id"])["state"] not in ("queued", "assigned")
                      or w.ground_mission(task["task_id"]) is not None and
                      w.status(w.ground_mission(task["task_id"])) in TERMINAL, "the second task", 90)
    await w.hold(2.0)


async def x1_shared_pad(w: X1World) -> None:
    # The aircraft flies another inspection from its pad while the ground robot is offered a target past the apron.
    # 飞行器从机位起飞执行另一次巡检，同时向地面车提议一个需要经过起降区的目标。
    w.anomaly("site_a", *mid_block(w))
    flight = await w.start("mid_watch", {"asset": "asset_west"})
    await w.until(lambda: any(w.status(m["mission_id"]) == "awaiting_approval" for m in w.missions_of(flight)),
                  "the aircraft's mission", 20)
    await w.approve_waiting(flight)
    x, y = mid_block(w)
    task, _ = w.offer(robot_id="ugv_01", target=(x - 2.0, y + 4.0), ttl_s=60)
    await w.until(lambda: w.ground_mission(task["task_id"]) is not None, "the ground mission", 60)
    mission = w.ground_mission(task["task_id"])
    await w.until(lambda: w.status(mission) in TERMINAL, "the ground mission's end", 90)
    await w.drive_run(flight)
    await w.hold(2.0)


async def x1_cancel_during_recheck(w: X1World) -> None:
    w.anomaly("site_a", *mid_block(w))
    run = await w.start("anomaly_recheck", {"asset": "asset_mid"})
    state = {"cancelled": False, "carried": False}

    async def cancel_while_driving() -> None:
        rover = w.rovers["ugv_01"]
        recheck = w.rechecks()[0] if w.rechecks() else None
        mission = w.ground_mission(recheck["ground_task_id"]) if recheck and recheck["ground_task_id"] else None
        if not state["cancelled"] and mission and not rover.idle and rover.body.mode == "MISSION" \
                and rover.body.position[1] > 2.0:
            await w.cancel(mission)
            state["cancelled"] = True
        if state["cancelled"] and not state["carried"] and rover.idle and rover.body.armed is False \
                and mission and w.status(mission) in TERMINAL:
            # Stopped and disarmed on the way: its hold stays until a person brings it back. / 在途中停稳上锁：持有保留，
            # 直到有人把它送回。
            await w.hold(2.0)
            w.carry_home("ugv_01")
            state["carried"] = True

    await w.drive_run(run, during=cancel_while_driving)
    await w.hold(3.0)


SCENARIOS: dict[str, Callable[[X1World], Awaitable[None]]] = {
    "x1_nominal": x1_nominal, "x1_unreachable": x1_unreachable, "x1_search_region": x1_search_region,
    "x1_not_found": x1_not_found, "x1_false_positive": x1_false_positive, "x1_offer_timeout": x1_offer_timeout,
    "x1_duplicate_offer": x1_duplicate_offer, "x1_late_accept": x1_late_accept,
    "x1_accept_after_timeout": x1_accept_after_timeout, "x1_link_loss_driving": x1_link_loss_driving,
    "x1_old_owner_continues": x1_old_owner_continues, "x1_shared_pad": x1_shared_pad,
    "x1_cancel_during_recheck": x1_cancel_during_recheck,
}


# ── the matrix / 矩阵 ──


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str) -> dict:
    """Run one S0 case and judge it online and from the recordings. / 运行一个用例并裁判两次。"""
    from drone_agent.eval.judge_x1 import judge_case

    if case.exists():
        shutil.rmtree(case)
    world = X1World(case, repo, seed=seed)
    error = None
    started = time.monotonic()
    try:
        await world.settle()
        await world.hold(0.5)
        await SCENARIOS[scenario["id"]](world)
        for robot in [*world.uavs.values(), *world.rovers.values()]:
            await robot.settle()
        await world.hold(1.0)
    except Exception as failure:  # the judge sees the failure as evidence / 裁判把失败当作证据
        error = f"{type(failure).__name__}: {failure}"[:400]
    finally:
        pending = {r.task for r in [*world.uavs.values(), *world.rovers.values()]
                   if r.task is not None and not r.task.done()}
        for task in pending:
            task.cancel()
        if pending:
            # Let cancelled missions close their journals before the export reads them. / 导出前先让被取消的任务关闭日志。
            await asyncio.wait(pending, timeout=30)
    world.export({"scenario": scenario["id"], "seed": seed, "source_sha": sha, "layer": "S0",
                  "description": scenario.get("description", ""), "expected": scenario.get("expected", {}),
                  "harness_error": error, "duration_s": round(time.monotonic() - started, 2)})
    world.ledger.close()
    online = judge_case(case, repo)
    replay = judge_case(case, repo, use_replay=True)
    keys = ("classification", "problems", "counts")
    agrees = all(online.get(k) == replay.get(k) for k in keys)
    result = {**{k: online.get(k) for k in ("scenario", "seed", "classification", "problems", "counts", "expected",
                                             "metrics")},
              "passed": bool(online.get("passed")) and agrees, "replay_agrees": agrees, "harness_error": error}
    (case / "judge").mkdir(exist_ok=True)
    (case / "judge/result.json").write_text(json.dumps(online, indent=2, default=str), encoding="utf-8")
    (case / "judge/replay.json").write_text(json.dumps(replay, indent=2, default=str), encoding="utf-8")
    return result


async def run_suite(output: Path, repo: Path, *, sha: str, only: list[str] | None = None,
                    seeds: list[int] | None = None) -> dict:
    from drone_agent.eval.judge_x1 import COUNTS, aggregate

    definition = suite(repo)
    started_at = utcnow().isoformat()
    results, voided = [], []
    for scenario in definition["s0"]:
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
    summary = {"schema_version": "0.1.0", "layer": "S0", "source_sha": sha, "suite": definition["format"],
               "started_at": started_at, "finished_at": utcnow().isoformat(), "cases": len(results),
               "passed": sum(1 for r in results if r["passed"]),
               "status": "passed" if results and all(r["passed"] for r in results) else "failed",
               "counts": counts, "metrics": aggregate([r.get("metrics") or {} for r in results]),
               "results": results, "voided_attempts": voided, "host": host()}
    output.mkdir(parents=True, exist_ok=True)
    (output / "suite-s0.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
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
