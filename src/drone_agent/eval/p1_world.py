"""S0 world for P1 (WP-P1-04/07/11): the formal mission service, logical docks and logical aircraft, stepped together.

Every actor reaches the service only through its API with a named identity: operators and admins as `harness:*`,
dock backends as their bound `dock:*` identity, aircraft through their uplink and the fleet hub. Faults are applied
to the simulated world or to the harness-only link, never to the service. Each scenario of the P1 fault matrix
(P1-F01–F14) drives the world into the state it tests and records everything the independent judge needs: the API
transcript, the docks' own truth and sent reports, the aircraft's flights and positions, the service export and the
onboard journals. The scenario code decides nothing about pass or fail.

P1 的 S0 世界（WP-P1-04/07/11）：正式任务服务、逻辑机场与逻辑飞行器一起步进。

每个参与者都只以具名身份经 API 访问服务：操作者与管理员为 `harness:*`，机场后端为其绑定的 `dock:*` 身份，飞行器
经其 uplink 与车队 hub。故障只作用于模拟世界或编排专用链路，从不作用于服务。P1 故障矩阵（P1-F01–F14）的每个场景
把世界推进到其测试的状态，并记录独立裁判需要的全部内容：API 记录、机场自身真值与发送的报告、飞行器的飞行与位置、
服务导出与机载账本。场景代码不对通过与否做任何决定。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import shutil
import sqlite3
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import yaml

from drone_agent.contracts import EnergyState, LocalizationHealth, RobotStatus, utcnow
from drone_agent.contracts.capability import CommsState
from drone_agent.eval.dock_simulator import DockBackend, DockSimulator
from drone_agent.eval.logical_flight import Battery
from drone_agent.eval.logical_uav import LogicalUav, quick_registry
from drone_agent.fleet.api import dispatch
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import source_context
from drone_agent.fleet.resources import MemberList, ProjectDirectory, load_members
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.transport import FleetHub
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.planner.draft import TOOL_NAME
from drone_agent.planner.engine import ModelIdentity, PlannerEngine
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.planner.tools.catalog import ToolCatalog
from drone_agent.planner.tools.client import InProcessSession
from drone_agent.providers import KeyedScriptedProvider
from drone_agent.runtime.signing import SigningKey, TrustStore

SUITE = "configs/scenarios/p1_suite.yaml"
CATALOG = "configs/sites/p1_campus_v1.yaml"
MEMBERS = "configs/sites/p1_members_s0.yaml"
POLICY = "configs/recovery_policies/multirotor_m1_v1.yaml"
OPERATOR, ADMIN, VIEWER, HARBOR, JUDGE = ("harness:p1-operator", "harness:p1-admin", "harness:p1-viewer",
                                          "harness:p1-harbor", "harness:p1-judge")
TEXTS = {"red": "Inspect the red equipment marker east of the pad and bring back a photo.",
         "blue": "Please photograph the blue marker north-west of the landing pad."}
TERMINAL = ("completed", "incomplete", "declined", "rejected", "refused", "planning_failed", "delivery_rejected",
            "cancelled", "dispatch_expired")
# A step this much longer than planned means the host stalled (e.g. slept); timing expectations are void (D045).
# 单步比计划长出这么多即主机停顿（如休眠）；时间相关期望作废（D045）。
HOST_STALL_S = 15.0
ATTEMPTS = 3


class ScenarioTimeout(RuntimeError):
    """The world did not reach the state a scenario waits for. / 世界没有达到场景等待的状态。"""


def draft(asset: str) -> dict:
    return {"decision": "plan", "decline_reason": "", "goal": f"Inspect {asset}", "goal_type": "inspect",
            "approved_volume_id": "campus_training",
            "tasks": [{"task_id": f"inspect_{asset}", "skill_id": "skill.inspect.asset", "asset_id": asset}],
            "notes": ""}


def scripted_planner(repo: Path) -> PlannerEngine:
    """A labelled scripted planner (never reported as model behaviour). / 带标注的脚本规划器（从不当作模型行为）。"""
    answers = {TEXTS["red"]: draft("asset_red"), TEXTS["blue"]: draft("asset_blue")}
    provider = KeyedScriptedProvider({text: {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": value}]}
                                      for text, value in answers.items()})
    registry = Registry(repo, scene=repo / M2_SCENE)
    tools = InProcessSession(ToolCatalog(repo, repo / M2_SCENE)).initialize()
    return PlannerEngine(provider, ModelIdentity("scripted", "scripted-fixture"), registry, tools)


class P1World:
    """One S0 case: service, docks, aircraft and the recordings the judge reads. / 一个 S0 用例。"""

    # The service's execution label; a judge smoke test of the S1 layout may use the S1 catalog's label.
    # 服务的执行标签；S1 布局的裁判冒烟测试可以使用 S1 目录的标签。
    backend = "logical_sim"

    def __init__(self, case: Path, repo: Path, *, seed: int, catalog: Path | None = None,
                 members: Path | None = None):
        self.case, self.repo, self.seed = case, repo, seed
        self.random = random.Random(seed)
        self.offset = timedelta(0)
        self.transcript: list[dict] = []
        self.injections: list[dict] = []
        self.snapshots: list[dict] = []
        self.catalog_path, self.members_path = catalog or repo / CATALOG, members or repo / MEMBERS
        (case / "input").mkdir(parents=True, exist_ok=True)
        self.key = SigningKey.generate()
        self.trust = TrustStore.from_entries([self.key.trust_entry()])
        self.ledger = BusinessLedger(case / "service/ledger.sqlite3")
        self.hub = FleetHub(self.ledger, case / "service/media")
        self.service = self._service()
        ops = self.service.ops
        self.uavs: dict[str, LogicalUav] = {}
        self.docks: dict[str, DockSimulator] = {}
        for robot_id, entry in ops.catalog.robots.items():
            battery = Battery(1.0)
            registry = quick_registry(repo, repo / ops.catalog.sites[entry.site_id].scene, ops.capabilities[robot_id])
            uav = LogicalUav(case / "robots" / robot_id, registry, self.hub, self.trust, repo / POLICY, battery=battery)
            self.uavs[robot_id] = uav
            self.docks[entry.dock_id] = DockSimulator(entry.dock_id, battery=battery, presence=uav.presence, seed=seed)
        self.backends: list[DockBackend] = []
        for principal in sorted({d.backend.principal for d in ops.catalog.docks.values()}):
            docks = {dock_id: self.docks[dock_id] for dock_id in ops.catalog.docks_of(principal)}
            self.backends.append(DockBackend(principal, docks, self._backend_call(principal)))
        self.last_step = time.monotonic()
        self.last_report: dict[str, float] = {}

    # ── the service / 服务 ──

    def clock(self):
        return utcnow() + self.offset

    def _service(self) -> MissionService:
        repo = self.repo
        operations = build_operations(repo, self.ledger, self.catalog_path, self.members_path,
                                      backups=self.case / "service/backups", clock=self.clock)
        registry = Registry(repo, scene=repo / M2_SCENE)
        return MissionService(root=repo, scene=repo / M2_SCENE, ledger=self.ledger, hub=self.hub,
                              signing_key=self.key,
                              approval_policy=ApprovalPolicy.from_yaml(repo / "configs/approval_policy.yaml"),
                              planner=scripted_planner(repo), clock=self.clock,
                              provenance_context=source_context(repo, repo / M2_SCENE, registry.sha256,
                                                                backend=self.backend),
                              operations=operations)

    def restart_service(self) -> None:
        """A service process restart: a new service object over the same ledger and hub. / 服务进程重启。"""
        self.hub.listeners.clear()
        self.service = self._service()
        self.inject("service_restart")

    def reload_members(self, members: MemberList) -> None:
        """A changed member file taking effect after a restart. / 成员文件变更并在重启后生效。"""
        self.service.ops.directory = ProjectDirectory(self.service.ops.catalog, members)
        self.inject("members_reloaded", members=len(members.members))

    def inject(self, kind: str, **fields) -> None:
        self.injections.append({"at": utcnow().isoformat(), "kind": kind, **fields})

    async def api(self, actor: str, method: str, *, trust: str = "first_party", probe: bool = False,
                  **params) -> dict:
        response = await dispatch(self.service, {"method": method, "actor": actor, "trust": trust, "params": params})
        result = response.get("result")
        self.transcript.append({
            "at": utcnow().isoformat(), "actor": actor, "trust": trust, "method": method, "probe": probe,
            "params": {k: v for k, v in params.items() if k != "report"}, "ok": bool(response.get("ok")),
            "code": (response.get("issue") or {}).get("code"),
            "result_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
            if result is not None else None,
            "mission_id": (result or {}).get("mission", {}).get("mission_id") if isinstance(result, dict) else None,
            "result": result if method.startswith("docks.") else None})
        return response

    def _backend_call(self, principal: str) -> Callable:
        async def call(method: str, **params) -> dict:
            return await dispatch(self.service, {"method": method, "actor": principal, "trust": "backend",
                                                 "params": params})
        return call

    # ── stepping / 步进 ──

    async def step(self, dt: float = 0.1) -> None:
        await asyncio.sleep(dt)
        now = time.monotonic()
        elapsed, self.last_step = now - self.last_step, now
        if elapsed > HOST_STALL_S:
            self.inject("host_stall", seconds=round(elapsed, 3))
        for dock in self.docks.values():
            dock.advance(elapsed)
        wall = utcnow()
        for backend in self.backends:
            due = {d: s for d, s in backend.docks.items() if now - self.last_report.get(d, 0.0) >= 0.25}
            if due:
                for dock_id in due:
                    self.last_report[dock_id] = now
                keep, backend.docks = backend.docks, due
                try:
                    await backend.cycle(wall)
                finally:
                    backend.docks = keep
        for uav in self.uavs.values():
            await uav.cycle()
        self.service.tick_operations()
        for mission_id in sorted(self.service.dirty | {m["mission_id"] for m in self.ledger.missions(200)
                                                        if m["status"] not in TERMINAL}):
            self.service.dirty.discard(mission_id)
            self.service.refresh(mission_id)

    async def until(self, predicate: Callable[[], bool], what: str, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await self.step()
            if predicate():
                return
        raise ScenarioTimeout(f"timed out waiting for {what}")

    async def hold(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await self.step()

    async def settle(self) -> None:
        """Let docks report and reconcile after start-up. / 启动后让机场报告并完成对账。"""
        await self.until(lambda: all(self.service.ops.store.dock_status(d) is not None and
                                     self.service.ops.store.dock_status(d).session.value == "active"
                                     for d in self.docks), "dock sessions active", 10)

    # ── mission helpers / 任务辅助 ──

    def mission(self, mission_id: str) -> dict:
        return self.ledger.mission(mission_id)

    def status(self, mission_id: str) -> str:
        return self.ledger.mission(mission_id)["status"]

    def delivered(self, robot_id: str, mission_id: str) -> bool:
        history = self.uavs[robot_id].dir / "inbox/history"
        return any(history.glob(f"{mission_id}-v*.json")) if history.is_dir() else False

    async def submit(self, robot_id: str, asset: str = "red", *, actor: str = OPERATOR, key: str | None = None,
                     project: str | None = None) -> str:
        project = project or self.service.ops.catalog.project_of(robot_id)
        response = await self.api(actor, "missions.submit", project_id=project, robot_id=robot_id, text=TEXTS[asset],
                                  volume_id="campus_training", asset_ids=[],
                                  idempotency_key=key or f"{robot_id}-{asset}-{len(self.transcript)}")
        if not response.get("ok"):
            raise RuntimeError(f"submit refused: {response.get('issue')}")
        return response["result"]["mission"]["mission_id"]

    async def approve(self, mission_id: str, *, actor: str = OPERATOR, expect: bool = True) -> dict:
        view = self.service._view(mission_id)
        version = view["mission"]["current_version"]
        record = next(v for v in view["versions"] if v["version"] == version)
        response = await self.api(actor, "approve", mission_id=mission_id, version=version,
                                  package_hash=record["package_hash"])
        if expect and not response.get("ok"):
            raise RuntimeError(f"approve refused: {response.get('issue')}")
        return response

    async def cancel(self, mission_id: str, *, actor: str = OPERATOR) -> dict:
        return await self.api(actor, "operate", mission_id=mission_id, action="cancel",
                              request_id=f"cancel-{mission_id[2:]}-{len(self.transcript)}")

    async def snapshot(self, label: str, project: str = "campus_ops") -> dict:
        response = await self.api(JUDGE, "resources.list", project_id=project)
        self.snapshots.append({"label": label, "at": utcnow().isoformat(), "project": project,
                               "resources": response.get("result")})
        return response.get("result") or {}

    def robot_status(self, robot_id: str, phase: str) -> None:
        """The aircraft itself reports a flight phase over its authenticated link. / 飞行器经其认证链路自报飞行阶段。"""
        status = RobotStatus(robot_id=robot_id, timestamp=utcnow(), flight_phase=phase,
                             energy=EnergyState(remaining_fraction=self.uavs[robot_id].battery.fraction),
                             localization=LocalizationHealth(fix_type="estimated", healthy=True),
                             comms=CommsState(uplink_ok=True, last_seen=utcnow()))
        self.hub.publish_status(robot_id, status)
        self.inject("robot_status", robot_id=robot_id, phase=phase)

    # ── export / 导出 ──

    def export(self, scenario: dict) -> None:
        case = self.case
        (case / "input/scenario.json").write_text(json.dumps(scenario, indent=2, ensure_ascii=False), encoding="utf-8")
        world = case / "world"
        world.mkdir(parents=True, exist_ok=True)

        def lines(name: str, rows) -> None:
            with (world / name).open("w", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        lines("api.jsonl", self.transcript)
        lines("injections.jsonl", self.injections)
        lines("docks.jsonl", [row for dock in self.docks.values() for row in dock.truth])
        lines("backends.jsonl", [{**row, "principal": b.principal} for b in self.backends for row in b.transcript])
        for uav in self.uavs.values():
            uav.export(world)
        export = case / "service-export"
        export.mkdir(parents=True, exist_ok=True)
        views = {}
        for mission in self.ledger.missions(500):
            try:
                views[mission["mission_id"]] = self.service._view(mission["mission_id"])
            except Exception as error:  # an export failure is itself evidence / 导出失败本身即证据
                views[mission["mission_id"]] = {"error": f"{type(error).__name__}: {error}"}
        (export / "views.json").write_text(json.dumps(views, indent=2, ensure_ascii=False, default=str),
                                           encoding="utf-8")
        (export / "resources.json").write_text(json.dumps(self.snapshots, indent=2, ensure_ascii=False, default=str),
                                               encoding="utf-8")
        tables = {}
        with sqlite3.connect(str(case / "service/ledger.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            for table in ("op_bindings", "op_reservations", "op_holds", "op_claims", "op_decisions",
                          "op_cancellations", "op_dock_actions", "op_dock_locks", "op_dock_sessions", "op_events",
                          "deliveries", "meta"):
                tables[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
        (export / "operations.json").write_text(json.dumps(tables, indent=2, ensure_ascii=False, default=str),
                                                encoding="utf-8")
        (export / "ready.json").write_text(json.dumps({"signer_key_id": self.key.key_id,
                                                        "catalog_sha256": self.service.ops.catalog.sha256},
                                                       indent=2), encoding="utf-8")


# ── scenarios P1-F01–F14 / 场景 ──


async def f01_nominal(w: P1World) -> None:
    asset = "red" if w.seed % 2 else "blue"
    await w.snapshot("before")
    mission = await w.submit("uav_a", asset)
    await w.approve(mission)
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 40)
    await w.until(lambda: not w.service.ops.store.holders(w.service.ops.catalog.resources("uav_a")),
                  "reservation released", 20)
    await w.snapshot("after")


async def f02_energy(w: P1World) -> None:
    dock, uav = w.docks["dock_b"], w.uavs["uav_b"]
    variants = ["charging", "cooling", "insufficient"]
    w.random.shuffle(variants)
    for variant in variants:
        level = round(w.random.uniform(0.55, 0.85), 3)
        uav.battery.fraction = level
        if variant == "charging":
            dock.charge_rate, dock.charging, dock.cooling_left = 0.0005, True, 0.0
        elif variant == "cooling":
            uav.battery.fraction, dock.charging, dock.cooling_left = 1.0, False, 3600.0
        else:
            dock.faults.ready_threshold, dock.charging, dock.cooling_left = 0.5, False, 0.0
        w.inject("energy_state", variant=variant, battery=uav.battery.fraction)
        mission = await w.submit("uav_b", "red")
        await w.approve(mission)
        await w.hold(2.0 + w.random.uniform(0, 1))
        await w.snapshot(f"blocked-{variant}")
        await w.cancel(mission)
        await w.until(lambda m=mission: w.status(m) in TERMINAL, "cancelled", 10)
        dock.charge_rate, dock.charging, dock.cooling_left, dock.faults.ready_threshold = 0.5, False, 0.0, 0.98


async def f03_maintenance(w: P1World) -> None:
    dock = w.docks["dock_c"]
    dock.faults.upkeep = "maintenance"
    w.inject("upkeep", value="maintenance")
    await w.hold(1.0)
    dock.faults.upkeep = "normal"
    w.inject("upkeep", value="normal")
    mission = await w.submit("uav_c", "red")
    await w.approve(mission)
    await w.hold(2.0 + w.random.uniform(0, 1))
    await w.snapshot("locked-after-normal-telemetry")
    await w.api(OPERATOR, "resources.maintenance", probe=True, project_id="campus_ops", dock_id="dock_c",
                action="release", reason="operator without admin role")
    await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_c", action="release",
                reason="inspected and cleared")
    w.inject("admin_release")
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 40)


async def f04_offline(w: P1World) -> None:
    dock = w.docks["dock_c"]
    dock.faults.offline = True
    w.inject("offline", value=True)
    mission = await w.submit("uav_c", "blue" if w.seed % 2 else "red")
    await w.approve(mission)
    await w.hold(4.0 + w.random.uniform(0, 1))
    dock.faults.offline = False
    forged = dock.report(utcnow())
    dock.faults.offline = True
    forged["seq"] = 10_000
    await w.api("dock:intruder", "docks.report", trust="backend", probe=True, report=forged)
    await w.snapshot("offline")
    dock.faults.offline = False
    w.inject("offline", value=False)
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 40)


async def f05_timestamps(w: P1World) -> None:
    dock = w.docks["dock_c"]
    dock.faults.future_by_s = 5.0
    w.inject("future_reports", seconds=5.0)
    mission = await w.submit("uav_c", "red")
    await w.approve(mission)
    await w.hold(4.0)
    # The mission stays blocked (environment deferred) through the rest, so it can only be claimed from the
    # rebooted dock's reconciled session. / 其余阶段任务保持阻断（环境推迟），只能从重启后已对账的会话领取。
    dock.faults.environment = "deferred"
    dock.faults.future_by_s = 0.0
    w.inject("future_reports", seconds=0.0)
    # Accepted reports resume first, so the reordered one is older than the last accepted.
    # 先恢复被接受的报告，使倒序报告早于最近一份已接受报告。
    await w.hold(0.6)
    dock.faults.reorder = True
    w.inject("reorder")
    await w.hold(0.6)
    dock.reboot()
    w.inject("reboot", boot_id=dock.boot_id)
    await w.hold(0.3)
    dock.faults.replay_boot = dock.boots[-1]
    w.inject("replay_boot", boot_id=dock.boots[-1])
    await w.hold(0.6)
    dock.faults.environment = "permitted"
    w.inject("environment", value="permitted")
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 40)


async def f06_lid_jam(w: P1World) -> None:
    dock = w.docks["dock_c"]
    dock.faults.lid_jam = True
    w.inject("lid_jam")
    mission = await w.submit("uav_c", "red")
    await w.approve(mission)
    await w.until(lambda: dock.lid == "jammed", "lid jammed", 15)
    await w.hold(2.0 + w.random.uniform(0, 1))
    await w.snapshot("jammed")
    await w.cancel(mission)
    await w.until(lambda: w.status(mission) in TERMINAL, "cancelled", 10)


async def f07_charge_stall(w: P1World) -> None:
    dock, uav = w.docks["dock_b"], w.uavs["uav_b"]
    uav.drain = 0.004
    dock.faults.charge_stall = True
    w.inject("charge_stall")
    first = await w.submit("uav_b", "red")
    await w.approve(first)
    await w.until(lambda: w.status(first) in TERMINAL, "first mission end", 40)
    await w.until(lambda: dock.charging, "charging started", 20)
    second = await w.submit("uav_b", "blue")
    await w.approve(second)
    await w.hold(4.0 + w.random.uniform(0, 1))
    await w.snapshot("stalled")
    await w.cancel(second)
    await w.until(lambda: w.status(second) in TERMINAL, "second cancelled", 10)


async def f08_contention(w: P1World) -> None:
    assets = ("red", "blue") if w.seed % 2 else ("blue", "red")
    first = await w.submit("uav_a", assets[0], key=f"contend-1-{w.seed}")
    again = await w.submit("uav_a", assets[0], key=f"contend-1-{w.seed}")
    w.inject("resubmitted", same=first == again)
    second = await w.submit("uav_a", assets[1], key=f"contend-2-{w.seed}")
    # The first submission holds the soft reservation, so approving the second must be refused.
    # 第一次提交持有软预约，因此先审批第二个任务必须被拒。
    await w.approve(second, expect=False)
    await w.approve(first)
    await w.until(lambda: w.status(first) in TERMINAL, "first mission end", 40)
    await w.until(lambda: not w.service.ops.store.holders(w.service.ops.catalog.resources("uav_a")),
                  "first reservation released", 20)
    await w.until(lambda: w.docks["dock_a"].energy()[0] == "ready", "dock ready again", 20)
    await w.approve(second)
    await w.until(lambda: w.status(second) in TERMINAL, "second mission end", 40)


async def f09_receipt_lost(w: P1World) -> None:
    uav = w.uavs["uav_a"]
    mission = await w.submit("uav_a", "red")
    await w.approve(mission)
    await w.until(lambda: uav.in_air, "airborne", 20)
    uav.link.drop_events = True
    w.inject("drop_events", value=True)
    await w.until(lambda: uav.task is None and not uav.in_air and uav.flights, "landed", 30)
    await w.until(lambda: any(r.state.value == "uncertain"
                              for r in w.service.ops.store.reservations(mission_id=mission)), "uncertain hold", 20)
    await w.snapshot("uncertain")
    other = await w.submit("uav_a", "blue")
    await w.approve(other, expect=False)
    w.restart_service()
    await w.hold(1.5)
    await w.snapshot("after-restart")
    uav.link.drop_events = False
    w.inject("drop_events", value=False)
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 30)
    await w.until(lambda: not w.service.ops.store.holders(w.service.ops.catalog.resources("uav_a")),
                  "released after reconciliation", 20)
    await w.cancel(other)


async def f10_presence_conflict(w: P1World) -> None:
    dock = w.docks["dock_a"]
    dock.faults.false_presence = True
    w.robot_status("uav_a", "airborne")
    mission = await w.submit("uav_a", "red")
    await w.approve(mission)
    await w.hold(2.0 + w.random.uniform(0, 1))
    await w.snapshot("conflict")
    w.robot_status("uav_a", "grounded")
    dock.faults.false_presence = False
    w.inject("conflict_cleared")
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 40)


async def f11_before_claim(w: P1World) -> None:
    dock, uav = w.docks["dock_c"], w.uavs["uav_c"]
    # (a) maintenance between approval and claim / 审批与领取之间进入维护
    first = await w.submit("uav_c", "red")
    await w.approve(first)
    dock.faults.upkeep = "maintenance"
    w.inject("upkeep", value="maintenance")
    await w.hold(2.0)
    await w.cancel(first)
    await w.until(lambda: w.status(first) in TERMINAL, "first cancelled", 10)
    dock.faults.upkeep = "normal"
    await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_c", action="release",
                reason="cleared")
    # (b) the approval lapses before the claim / 领取前审批过期
    dock.faults.environment = "deferred"
    w.inject("environment", value="deferred")
    second = await w.submit("uav_c", "blue")
    await w.approve(second)
    await w.hold(1.0)
    w.offset = timedelta(minutes=31)
    w.inject("clock_offset", minutes=31)
    await w.until(lambda: w.status(second) in TERMINAL, "approval lapsed", 10)
    w.offset = timedelta(0)
    w.inject("clock_offset", minutes=0)
    await w.hold(1.5)
    # (c) cancel before claim / 领取前取消
    third = await w.submit("uav_c", "red")
    await w.approve(third)
    await w.hold(0.5)
    await w.cancel(third)
    await w.until(lambda: w.status(third) in TERMINAL, "third cancelled", 10)
    dock.faults.environment = "permitted"
    w.inject("environment", value="permitted")
    # (d) claimed, cancel before the flight starts: relayed at the first running step / 已领取、起飞前取消：首个运行步骤时转发
    uav.cycle_original = uav.cycle
    held = {"on": True}

    async def cycle_without_takeoff():
        if held["on"]:
            uav.uplink.last_status = 0
            await uav.uplink.cycle()
            return
        await uav.cycle_original()

    uav.cycle = cycle_without_takeoff
    # A longer takeoff hold keeps the flight live when the relayed cancel arrives. / 更长的起飞保持使转发的取消到达时仍在飞。
    uav.registry.data["thresholds"]["takeoff"]["hold_duration_s"] = 3.0
    fourth = await w.submit("uav_c", "blue")
    await w.approve(fourth)
    await w.until(lambda: w.delivered("uav_c", fourth), "fourth delivered", 20)
    await w.cancel(fourth)
    w.inject("takeoff_released")
    held["on"] = False
    await w.until(lambda: w.status(fourth) in TERMINAL, "fourth ended", 40)
    await w.until(lambda: not w.service.ops.store.holders(w.service.ops.catalog.resources("uav_c")),
                  "fourth released", 20)


async def f12_restarts(w: P1World) -> None:
    dock, uav = w.docks["dock_b"], w.uavs["uav_b"]
    mission = await w.submit("uav_b", "red")
    await w.approve(mission)
    await w.until(lambda: w.service.ops.store.action_for(f"mission:{mission}:v1", "open_lid") is not None,
                  "open requested", 20)
    action = w.service.ops.store.action_for(f"mission:{mission}:v1", "open_lid")
    await w.until(lambda: action["action_id"] in dock.actions, "open acknowledged", 10)
    await w.api("dock:p1-s0-sim", "docks.ack", trust="backend", action_id=action["action_id"], accepted=True,
                reason="duplicate")
    w.inject("duplicate_ack", action_id=action["action_id"])
    await w.until(lambda: uav.in_air, "airborne", 20)
    w.restart_service()
    dock.reboot()
    w.inject("dock_reboot", boot_id=dock.boot_id)
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 40)
    await w.until(lambda: not w.service.ops.store.holders(w.service.ops.catalog.resources("uav_b")),
                  "released", 20)
    await w.api("dock:p1-s0-sim", "docks.ack", trust="backend", action_id=action["action_id"], accepted=False,
                reason="late")
    w.inject("late_ack", action_id=action["action_id"])
    await w.hold(1.0)


async def f13_projects(w: P1World) -> None:
    harbor = await w.submit("uav_h", "red", actor=HARBOR, project="harbor_ops")
    await w.approve(harbor, actor=HARBOR)
    await w.until(lambda: w.status(harbor) in TERMINAL, "harbor mission end", 40)
    own = await w.submit("uav_a", "blue")
    view = w.service._view(harbor)
    evidence = view["evidence"][0]["evidence_id"] if view["evidence"] else "image:none"
    version = view["versions"][0]
    probes = [
        (OPERATOR, "view", {"mission_id": harbor}), (OPERATOR, "summary", {"mission_id": harbor}),
        (OPERATOR, "media", {"mission_id": harbor, "evidence_id": evidence}),
        (OPERATOR, "approve", {"mission_id": harbor, "version": 1, "package_hash": version["package_hash"]}),
        (OPERATOR, "decline", {"mission_id": harbor, "version": 1, "reason": "probe"}),
        (OPERATOR, "operate", {"mission_id": harbor, "action": "cancel", "request_id": "probe-cancel-0001"}),
        (OPERATOR, "resources.list", {"project_id": "harbor_ops"}),
        (OPERATOR, "resources.get", {"project_id": "harbor_ops", "resource_id": "dock_h"}),
        (OPERATOR, "resources.get", {"project_id": "campus_ops", "resource_id": "dock_h"}),
        (OPERATOR, "resources.eligibility", {"project_id": "campus_ops", "robot_id": "uav_h"}),
        (OPERATOR, "missions.submit", {"project_id": "harbor_ops", "robot_id": "uav_h", "text": TEXTS["red"],
                                       "volume_id": "campus_training", "asset_ids": [], "idempotency_key": "probe-1"}),
        (OPERATOR, "missions.submit", {"project_id": "campus_ops", "robot_id": "uav_h", "text": TEXTS["red"],
                                       "volume_id": "campus_training", "asset_ids": [], "idempotency_key": "probe-2"}),
        (VIEWER, "approve", {"mission_id": own, "version": 1,
                             "package_hash": w.service._view(own)["versions"][0]["package_hash"]}),
        (VIEWER, "resources.maintenance", {"project_id": "campus_ops", "dock_id": "dock_a", "action": "set",
                                           "reason": "probe"}),
        ("", "view", {"mission_id": own}),
        ("harness:stranger", "view", {"mission_id": own}),
    ]
    w.random.shuffle(probes)
    for actor, method, params in probes:
        await w.api(actor, method, probe=True, **params)
    await w.api("a2a:probe-client", "missions.submit", trust="third_party", probe=True, project_id="campus_ops",
                robot_id="uav_a", text=TEXTS["red"], volume_id="campus_training", asset_ids=[],
                idempotency_key="probe-a2a")
    listed = await w.api(OPERATOR, "list")
    w.inject("operator_list", missions=[m["mission_id"] for m in listed.get("result") or []])
    members = load_members(w.members_path)
    trimmed = MemberList(format=members.format, members=tuple(
        m.model_copy(update={"roles": tuple(r for r in m.roles if r.value != "approver")})
        if m.principal == OPERATOR else m for m in members.members))
    w.reload_members(trimmed)
    await w.api(OPERATOR, "approve", probe=True, mission_id=own, version=1,
                package_hash=w.service._view(own)["versions"][0]["package_hash"])
    w.reload_members(members)
    await w.api(OPERATOR, "operate", mission_id=own, action="cancel", request_id="cancel-own-0001")


async def f14_sources(w: P1World) -> None:
    dock = w.docks["dock_a"]
    report = dock.report(utcnow())
    await w.api("dock:p1-s0-sim", "docks.report", trust="backend", probe=True,
                report={**report, "seq": report["seq"] + 50, "source": "real_device"})
    await w.api("dock:p1-s0-harbor", "docks.report", trust="backend", probe=True,
                report={**report, "seq": report["seq"] + 60})
    await w.api("dock:p1-s0-sim", "docks.report", trust="backend", probe=True,
                report={**report, "dock_id": "dock_real_01", "seq": report["seq"] + 70})
    await w.api("tailnet:operator@example.test", "docks.report", probe=True, report={**report, "seq": report["seq"] + 80})
    await w.api(OPERATOR, "missions.submit", probe=True, project_id="campus_ops", robot_id="uav_a",
                text=TEXTS["red"], volume_id="campus_training", asset_ids=[], idempotency_key="probe-backend",
                execution_backend="real_device")
    catalog = yaml.safe_load((w.repo / CATALOG).read_text(encoding="utf-8"))
    catalog["docks"]["dock_a"]["backend"]["kind"] = "real_device"
    forged = w.case / "input/forged-catalog.yaml"
    forged.write_text(yaml.safe_dump(catalog), encoding="utf-8")
    try:
        build_operations(w.repo, BusinessLedger(":memory:"), forged, w.members_path, backups=None)
        w.inject("forged_catalog", accepted=True)
    except ValueError as error:
        w.inject("forged_catalog", accepted=False, error=type(error).__name__)
    mission = await w.submit("uav_a", "red")
    await w.approve(mission)
    await w.until(lambda: w.status(mission) in TERMINAL, "mission end", 40)


SCENARIOS: dict[str, Callable] = {
    "p1_f01_nominal": f01_nominal, "p1_f02_energy": f02_energy, "p1_f03_maintenance": f03_maintenance,
    "p1_f04_offline": f04_offline, "p1_f05_timestamps": f05_timestamps, "p1_f06_lid_jam": f06_lid_jam,
    "p1_f07_charge_stall": f07_charge_stall, "p1_f08_contention": f08_contention,
    "p1_f09_receipt_lost": f09_receipt_lost, "p1_f10_presence_conflict": f10_presence_conflict,
    "p1_f11_before_claim": f11_before_claim, "p1_f12_restarts": f12_restarts, "p1_f13_projects": f13_projects,
    "p1_f14_sources": f14_sources,
}


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str) -> dict:
    """Run one S0 case and judge it online and from the recordings. / 运行一个 S0 用例并在线与按录制各裁判一次。"""
    from drone_agent.eval.judge_p1 import judge_case

    if case.exists():
        shutil.rmtree(case)
    world = P1World(case, repo, seed=seed)
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
    counts = {key: sum((r.get("counts") or {}).get(key, 0) for r in results)
              for key in ("wrong_dispatch", "duplicate_dispatch", "wrong_release", "project_escape", "false_success")}
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
