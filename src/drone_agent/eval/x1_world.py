"""S0 world for X1 (D088): the P4 world with the P3 scheduler, the handoff engine and logical ground robots.

The world is the P4 S0 world (the formal mission service with its operations, workflow and business catalogs, logical
docks, and logical aircraft running the real onboard uplink, guardian and executive), with the X1 scheduling catalog
and the handoff engine loaded into the same service the way `--air-ground` loads them, and one logical ground robot per
dockless catalog robot: the real uplink, which answers offers from the robot's own map, and a fresh guardian and
executive per mission around one physical ground body. Operators and approvers act only through the API under their
own identities; faults are applied to the simulated world or to harness-only links. The scenario code decides nothing
about pass or fail.

X1 的 S0 世界（D088）：P4 世界加上 P3 调度器、交接引擎与逻辑地面车。

世界即 P4 S0 世界（带运营、工作流与业务目录的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与 executive 的
逻辑飞行器），并按 `--air-ground` 的方式把 X1 调度目录与交接引擎加载到同一服务中；目录中每台没有机场的机器人对应一台逻辑
地面车：真实的 uplink（依据地面车自己的地图回答提议），以及围绕同一个物理车体、每次任务新建的 guardian 与 executive。操作者
与审批人只以各自身份经 API 行动；故障作用于模拟世界或编排专用链路。场景代码不对通过与否做任何决定。
"""

from __future__ import annotations

from pathlib import Path

from drone_agent.contracts import FactSource, Frame, Position, WorldFact, utcnow
from drone_agent.eval.logical_flight import Battery
from drone_agent.eval.logical_ground import quick_ground
from drone_agent.eval.logical_rover import LogicalRover
from drone_agent.eval.p4_world import P4World
from drone_agent.eval.x1_scene import Anomaly, X1Scene
from drone_agent.fleet.air_ground import build_air_ground
from drone_agent.fleet.scheduler import Scheduler, build_scheduling
from drone_agent.mission.registry import load_registry

CATALOG = "configs/sites/x1_campus_v1.yaml"
MEMBERS = "configs/sites/x1_members_s0.yaml"
SCHEDULING = "configs/scheduling/x1_campus_v1.yaml"
WORKFLOWS = "configs/workflows/x1_campus_v1.yaml"
BUSINESS = "configs/analysis/x1_campus_v1.yaml"
ROVER_POLICY = "configs/recovery_policies/rover_campus_v1.yaml"
# RTK-grade aircraft positions for the point cases; the P1 value (1 m^2) gives search regions. / 点目标用例使用 RTK 级
# 的飞行器位置；P1 的取值（1 m^2）得到搜索区域。
RTK = (0.04, 0.0, 0.0, 0.0, 0.04, 0.0, 0.0, 0.0, 0.09)
OPERATOR, REVIEWER, VIEWER, ADMIN, HARBOR, JUDGE = (
    "harness:x1-operator", "harness:x1-reviewer", "harness:x1-viewer", "harness:x1-admin", "harness:x1-harbor",
    "harness:x1-judge")


class X1World(P4World):
    """One X1 S0 case: logical sites, docks, aircraft and ground robots, the scheduler and the handoff engine.

    一个 X1 S0 用例：逻辑站点、机场、飞行器与地面车，调度器与交接引擎。
    """

    def __init__(self, case: Path, repo: Path, *, seed: int):
        self.scheduling_path = repo / SCHEDULING
        self.scheduler_paused = False
        self.rovers: dict[str, LogicalRover] = {}
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
            await rover.cycle()
        if not self.scheduler_paused:
            self.service.tick_scheduler()
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

    # ── the scene / 场景 ──

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

    # ── handoff helpers / 交接辅助 ──

    def offer(self, *, robot_id: str, target: tuple[float, float], asset_id: str = "asset_mid",
              signature: str = "orange", kind: str = "point", radius_m: float | None = None, ttl_s: float = 60,
              key: str | None = None, map_version: str = "x1_ground_a_v1") -> tuple[dict, dict]:
        """A ground recheck task and its offer, as the coordinator creates them (the business loop's step).

        与协调器的做法相同，创建一个地面复核任务及其提议（业务闭环中的一步）。
        """
        task, _ = self.scheduler.submit_handoff(
            requested_by="coordinator:x1", key=key or f"recheck-{asset_id}-{len(self.injections)}",
            project_id="campus_ops", asset_id=asset_id, volume_id=map_version, robot_id=robot_id,
            detail={"asset_id": asset_id, "signature": signature})
        value = {"kind": kind, "description": f"{signature} block, about 0.5 m"}
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
