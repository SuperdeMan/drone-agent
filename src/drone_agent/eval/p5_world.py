"""S0 and S3 world for P5 (D071, D072): the second business template and the vendor protocol contract.

The world is the P4 S0 world (the formal mission service with its operations, workflow and business catalogs, logical
docks, and logical aircraft running the real onboard uplink, guardian and executive) loaded with the P5 catalogs, plus
one vendor-managed dock: the S3 simulator (`eval/vendor_sim.py`) behind the real vendor gateway, which runs inside the
same service and claims the vendor robot's deliveries through the same gate. People act only through the API under
their own identities; obstacles and damage are changes of the simulated world; vendor faults are switches of the
simulator. Each case (P5-R01–R08: the road obstacle template; P5-V01–V12: the vendor contract) records what the
independent judge (`eval/judge_p5.py`) needs; the scenario code decides nothing.

P5 的 S0 与 S3 世界（D071、D072）：第二业务模板与厂商协议合同。

世界即 P4 的 S0 世界（带运营、工作流与业务目录的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与 executive 的
逻辑飞行器），加载 P5 目录，另加一个厂商托管机场：真实厂商网关背后的 S3 模拟器（`eval/vendor_sim.py`）；网关运行在同一服务
内，经同一闸门领取厂商机器人的投递。人只以各自身份经 API 行动；障碍与损伤是模拟世界的变化；厂商故障是模拟器的开关。每个
用例（P5-R01–R08：道路障碍模板；P5-V01–V12：厂商合同）记录独立裁判（`eval/judge_p5.py`）需要的内容；场景代码不对通过与否
做任何决定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from drone_agent.eval.p1_world import TERMINAL, ScenarioTimeout
from drone_agent.eval.p4_world import EXPORT_TABLES as P4_TABLES
from drone_agent.eval.p4_world import P4World, always, confirm_all
from drone_agent.eval.vendor_sim import MemoryLink, VendorDockSim
from drone_agent.fleet.vendor_gateway import JOURNAL, VendorGateway

SUITE = "configs/scenarios/p5_suite.yaml"
CATALOG = "configs/sites/p5_campus_v1.yaml"
WORKFLOWS = "configs/workflows/p5_campus_v1.yaml"
BUSINESS = "configs/analysis/p5_campus_v1.yaml"
MEMBERS = "configs/sites/p5_members_s0.yaml"
OPERATOR, REVIEWER, VIEWER, ADMIN, HARBOR, JUDGE = (
    "harness:p5-operator", "harness:p5-reviewer", "harness:p5-viewer", "harness:p5-admin", "harness:p5-harbor",
    "harness:p5-judge")
ROADS = "event:road-reports"
VENDOR_SPEED = 8.0
SIGNATURES = {"asset_red": "red", "asset_blue": "blue", "road_north": "green"}
EXPORT_TABLES = (*P4_TABLES, "issues")
ATTEMPTS = 3


class P5World(P4World):
    """One P5 case: the P4 world with the P5 catalogs and one vendor dock simulator. / 一个 P5 用例。"""

    extra_backends = ("vendor_protocol_sim",)
    # The catalogs a subclass may replace (the desk platform, D069). / 子类可替换的目录（任务台平台，D069）。
    catalog_file, workflows_file, business_file, members_file = CATALOG, WORKFLOWS, BUSINESS, MEMBERS
    signatures = SIGNATURES

    def __init__(self, case: Path, repo: Path, *, seed: int):
        self.vendor_sim = VendorDockSim("dock_vd", "uav_v1", signatures=SIGNATURES, speed=VENDOR_SPEED, seed=seed)
        self.vendor_last = time.monotonic()
        self.vendor_paused = False
        super().__init__(case, repo, seed=seed, business=repo / self.business_file,
                         workflows=repo / self.workflows_file, members=repo / self.members_file,
                         catalog=repo / self.catalog_file, signatures=self.signatures)

    def _service(self):
        service = super()._service()
        # A restarted service dials the vendor again: a new session, a new hello. / 重启的服务重新连接厂商：新会话、新 hello。
        service.vendor = VendorGateway(service, {"dock_vd": MemoryLink(self.vendor_sim)}, clock=self.clock)
        return service

    async def step(self, dt: float = 0.1) -> None:
        await super().step(dt)
        now = time.monotonic()
        self.vendor_sim.step(now - self.vendor_last)
        self.vendor_last = now
        if not self.vendor_paused:
            await self.service.tick_vendor()
        for mission_id in sorted(self.service.dirty):
            self.service.dirty.discard(mission_id)
            self.service.refresh(mission_id)

    async def settle(self) -> None:
        await super().settle()
        store = self.service.ops.store
        await self.until(lambda: store.dock_status("dock_vd") is not None and
                         store.dock_status("dock_vd").session.value == "active", "vendor dock session active", 15)

    # ── P5 identities on the P2 / P4 helpers / P2 / P4 辅助方法换成 P5 身份 ──

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

    async def feedback(self, order_id: str, *, request_id: str, actor: str = OPERATOR, probe: bool = False,
                       project: str = "campus_ops") -> dict:
        return await super().feedback(order_id, request_id=request_id, actor=actor, probe=probe, project=project)

    async def decide_finding(self, finding_id: str, decision: str, *, actor: str = REVIEWER,
                             request_id: str | None = None, probe: bool = False, project: str = "campus_ops") -> dict:
        return await super().decide_finding(finding_id, decision, actor=actor, request_id=request_id, probe=probe,
                                            project=project)

    async def road_report(self, event_id: str, segment: str = "road_north", *, principal: str = ROADS,
                          event_type: str = "road.report", probe: bool = False) -> dict:
        return await self.raise_event(event_id, segment, principal=principal, project="campus_ops",
                                      workflow_id="road_watch", trigger_id="road_report", event_type=event_type,
                                      payload={"segment": segment}, probe=probe)

    def obstacle(self, present: bool, robot_id: str = "uav_fa", asset_id: str = "road_north") -> None:
        """An obstacle appears on, or is cleared from, the road segment in the simulated world.

        模拟世界中道路段上出现或清除一个障碍。
        """
        self.appearance.set(robot_id, asset_id, "damaged" if present else "normal")
        self.inject("obstacle", robot_id=robot_id, asset_id=asset_id, present=present)

    def vendor_damage(self, asset: str, present: bool) -> None:
        (self.vendor_sim.damaged.add if present else self.vendor_sim.damaged.discard)(asset)
        self.inject("vendor_appearance", asset_id=asset, damaged=present)

    def vendor_fault(self, **faults) -> None:
        for name, value in faults.items():
            setattr(self.vendor_sim.faults, name, value)
        self.inject("vendor_fault", **{k: v for k, v in faults.items()})

    def vendor_missions(self) -> list[dict]:
        return [m for m in self.ledger.missions(500)
                if (self.service.ops.store.binding(m["mission_id"]) or {}).get("robot_id") == "uav_v1"]

    def export(self, scenario: dict) -> None:
        super().export(scenario)
        tables = {}
        with sqlite3.connect(str(self.case / "service/ledger.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            for table in EXPORT_TABLES:
                tables[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
            tables["vendor_journal"] = [dict(row) for row in db.execute(
                "SELECT robot_id, mission_id, mission_version, seq, kind, sha256, previous, timestamp, body "
                "FROM events WHERE journal=? ORDER BY mission_id, mission_version, seq", (JOURNAL,))]
        (self.case / "service-export/business.json").write_text(
            json.dumps(tables, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        (self.case / "service-export/appearance.json").write_text(
            json.dumps({"history": self.appearance.history, "members": self.members_file}, indent=2), encoding="utf-8")
        (self.case / "world/vendor.json").write_text(json.dumps(self.vendor_sim.export(), indent=2, default=str),
                                                     encoding="utf-8")


# ── P5-R: the road obstacle template / 道路障碍模板 ──


async def road_order(w: P5World, *, event_id: str = "report-1") -> tuple[str, str, dict]:
    """An obstacle, a road report, the inspection and a confirmed finding: (run, finding, order).

    出现障碍、道路报告、巡检与确认的发现：（运行，发现，工单）。
    """
    w.obstacle(True)
    response = await w.road_report(event_id)
    if not response.get("ok"):
        raise RuntimeError(f"road report refused: {response.get('issue')}")
    run = response["result"]["run_id"]
    await w.run_business([run], until=lambda: w.final(run), what="road inspection run end", decisions=confirm_all)
    finding = w.finding_of(run)
    order = w.order_of(finding)
    if order is None:
        raise ScenarioTimeout("no clearance order was opened")
    return run, finding, order


async def r01_clear(w: P5World) -> None:
    run, _, order = await road_order(w)
    w.obstacle(False)
    await w.feedback(order["order_id"], request_id="clearance-1")
    runs = [run]
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "closed" and all(
        w.final(r) for r in runs), what="clearance order closed", decisions=confirm_all)


async def r02_event_dedup(w: P5World) -> None:
    runs = set()
    for _ in range(20):
        response = await w.road_report("report-dup")
        if response.get("ok"):
            runs.add(response["result"]["run_id"])
    w.inject("events_sent", count=20, runs=len(runs))
    for run in runs:
        await w.run_business([run], until=lambda r=run: w.final(r), what="deduplicated run end")


async def r03_already_cleared(w: P5World) -> None:
    w.obstacle(True)
    response = await w.road_report("report-cleared")
    run = response["result"]["run_id"]
    w.obstacle(False)  # someone moved it before the aircraft arrived / 飞行器到达前已有人移走
    await w.run_business([run], until=lambda: w.final(run), what="normal inspection")


async def r04_dismissed(w: P5World) -> None:
    w.obstacle(True)
    response = await w.road_report("report-dismissed")
    run = response["result"]["run_id"]
    await w.run_business([run], until=lambda: w.final(run), what="dismissed run", decisions=always("dismissed"))


async def r05_not_cleared(w: P5World) -> None:
    run, _, order = await road_order(w)
    await w.feedback(order["order_id"], request_id="clearance-1")
    runs = [run]
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "reinspection_failed" and all(
        w.final(r) for r in runs), what="round 1 failed", decisions=confirm_all)
    w.obstacle(False)
    await w.feedback(order["order_id"], request_id="clearance-2")
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "closed" and all(
        w.final(r) for r in runs), what="round 2 closed", decisions=confirm_all)


async def r06_event_authority(w: P5World) -> None:
    await w.road_report("intruder-1", principal="event:intruder", probe=True)
    await w.road_report("bad-segment", segment="road_south", probe=True)
    await w.road_report("bad-type", event_type="asset.alarm", probe=True)
    await w.road_report("operator-as-event", principal=OPERATOR, probe=True)
    await w.api("a2a:probe-client", "workflows.event", trust="third_party", probe=True, project_id="campus_ops",
                workflow_id="road_watch", trigger_id="road_report", event_type="road.report", event_id="a2a-1",
                payload={"segment": "road_north"})
    response = await w.road_report("report-valid")
    run = response["result"]["run_id"]
    await w.run_business([run], until=lambda: w.final(run), what="valid report run")


async def r07_cancel_reinspection(w: P5World) -> None:
    run, _, order = await road_order(w)
    w.obstacle(False)
    await w.feedback(order["order_id"], request_id="clearance-1")
    child = w.round_runs(order["order_id"])[0]
    await w.run_business([child], until=lambda: bool(w.missions_of(child)), what="reinspection mission",
                         approve=False)
    await w.cancel_run(child)
    await w.run_business([run, child], until=lambda: w.final(child), what="cancelled reinspection", approve=False)


async def r08_isolation(w: P5World) -> None:
    run, finding, order = await road_order(w)
    for method, params in (("findings.get", {"finding_id": finding}), ("orders.get", {"order_id": order["order_id"]}),
                           ("workflows.get", {"run_id": run}), ("business.summary", {}),
                           ("findings.review", {"finding_id": finding, "decision": "dismissed",
                                                "request_id": "harbor-review", "note": "probe"}),
                           ("orders.repair", {"order_id": order["order_id"], "request_id": "harbor-repair",
                                              "note": "probe"})):
        await w.api(HARBOR, method, probe=True, project_id="campus_ops", **params)
    await w.api(VIEWER, "orders.repair", probe=True, project_id="campus_ops", order_id=order["order_id"],
                request_id="viewer-repair", note="probe")
    await w.api(VIEWER, "workflows.start", probe=True, project_id="campus_ops", workflow_id="road_watch",
                request_id="viewer-start", inputs={"segment": "road_north"})


# ── P5-V: the vendor protocol contract / 厂商协议合同 ──


async def vendor_run(w: P5World, asset: str = "asset_red", *, request_id: str | None = None,
                     until: Callable[[str], bool] | None = None, timeout_s: float = 120.0) -> str:
    """Start `vendor_watch`, approve its mission like a person would and step until `until(run)`.

    启动 `vendor_watch`，像人一样审批其任务，并步进到 `until(run)`。
    """
    run = await w.start("vendor_watch", {"asset": asset}, project="vendor_ops",
                        request_id=request_id or f"vendor-{asset}-{len(w.transcript)}")
    check = until or (lambda r: w.final(r))
    await w.run_business([run], until=lambda: check(run), what="vendor run", decisions=confirm_all,
                         timeout_s=timeout_s)
    return run


def vendor_terminal(w: P5World) -> bool:
    return all(m["status"] in TERMINAL for m in w.vendor_missions()) and bool(w.vendor_missions())


def vendor_progressed(w: P5World, step: int) -> bool:
    active = w.vendor_sim.active
    return active is not None and active["index"] >= step


async def v01_nominal(w: P5World) -> None:
    await vendor_run(w)


async def v02_duplicates(w: P5World) -> None:
    w.vendor_fault(duplicate=True)
    await vendor_run(w)


async def v03_reorder(w: P5World) -> None:
    w.vendor_fault(reorder=True)
    await vendor_run(w)


async def v04_lost_acks(w: P5World) -> None:
    # The lid opens slowly, so no progress tells the gateway that the lost execute arrived. / 舱盖开得慢，没有进度表明丢失答复的执行已到达。
    w.vendor_fault(drop_replies={"flighttask_prepare": 1, "flighttask_execute": 1}, slow_open_s=3.5)
    await vendor_run(w)


async def v05_reconnect_replay(w: P5World) -> None:
    # The link drops on the way back, after the photo, and the reconnect replays the last progress.
    # 回程中、拍照之后链路断开，重连时重放最近进度。
    w.vendor_fault(disconnect_at_step=5, down_for_s=2.0, replay_on_reconnect=True, drop_terminal=False)
    await vendor_run(w)


async def v06_terminal_lost(w: P5World) -> None:
    w.vendor_fault(disconnect_at_step=5, down_for_s=2.0, replay_on_reconnect=False, drop_terminal=True)
    run = await w.start("vendor_watch", {"asset": "asset_red"}, project="vendor_ops", request_id="vendor-lost")
    await w.run_business([run], until=lambda: bool(w.vendor_sim.truth["terminals"]), what="vendor flight ends",
                         timeout_s=60)
    await w.hold(8.0)  # nothing more arrives; the outcome must stay open / 不再有任何报文；结果必须保持未决
    w.inject("observed_open", missions=[m["mission_id"] for m in w.vendor_missions()])


async def v07_cancel_before_takeoff(w: P5World) -> None:
    w.vendor_fault(drop_replies={"flighttask_prepare": 1})
    run = await w.start("vendor_watch", {"asset": "asset_red"}, project="vendor_ops", request_id="vendor-undo")
    await w.run_business([run], until=lambda: any(c["method"] == "flighttask_prepare"
                                                  for c in w.vendor_sim.truth["commands"]),
                         what="prepare sent", timeout_s=60)
    await w.cancel_run(run)
    await w.run_business([run], until=lambda: w.final(run) and vendor_terminal(w), what="cancelled before takeoff",
                         approve=False, timeout_s=60)


async def v08_cancel_in_flight(w: P5World) -> None:
    w.vendor_fault(drop_replies={"return_home": 1}, slow_return_s=4.0)
    run = await w.start("vendor_watch", {"asset": "asset_red"}, project="vendor_ops", request_id="vendor-rth")
    await w.run_business([run], until=lambda: vendor_progressed(w, 2), what="vendor airborne", timeout_s=60)
    await w.cancel_run(run)
    await w.run_business([run], until=lambda: w.final(run) and vendor_terminal(w), what="returned home",
                         approve=False, timeout_s=90)
    await w.hold(2.0)


async def v09_media(w: P5World) -> None:
    w.vendor_fault(**({"missing_media": True} if w.seed % 2 else {"corrupt_media": True}))
    await vendor_run(w, timeout_s=150)


async def v10_prepare_rejected(w: P5World) -> None:
    w.vendor_fault(reject_prepare=314003)
    await vendor_run(w)


async def v11_restart(w: P5World) -> None:
    run = await w.start("vendor_watch", {"asset": "asset_red"}, project="vendor_ops", request_id="vendor-restart")
    await w.run_business([run], until=lambda: vendor_progressed(w, 3), what="vendor mid-flight", timeout_s=60)
    w.vendor_paused = True
    w.restart_service()
    await w.hold(0.5)
    w.vendor_paused = False
    await w.run_business([run], until=lambda: w.final(run), what="run end after restart", decisions=confirm_all)


async def v12_no_low_level(w: P5World) -> None:
    from drone_agent.fleet.vendor import VendorError, command, load_profile

    profile = yaml.safe_load((w.repo / "configs/vendors/dock_task_v1.yaml").read_text(encoding="utf-8"))
    attempts = {"low_level_control": {**profile, "low_level_control": "stick"},
                "extra_command": {**profile, "commands": {**profile["commands"],
                                                          "fly": {"method": "drc_stick_control"}}},
                "firmware_claim": {**profile, "protocol": {**profile["protocol"], "verified_against_firmware": True}}}
    for name, data in attempts.items():
        path = w.case / "input" / f"profile-{name}.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        try:
            load_profile(w.case, str(path.relative_to(w.case)))
            refused = False
        except VendorError:
            refused = True
        w.inject("profile_load", variant=name, refused=refused)
    for method in ("drc_stick_control", "velocity_setpoint", "attitude"):
        try:
            command(method, flight="f-probe", gateway="dock_vd", data={}, ts="2026-01-01T00:00:00+00:00")
            refused = False
        except VendorError:
            refused = True
        w.inject("command_build", method=method, refused=refused)
    await vendor_run(w)


SCENARIOS: dict[str, Callable] = {
    "p5_r01_clear": r01_clear, "p5_r02_event_dedup": r02_event_dedup, "p5_r03_already_cleared": r03_already_cleared,
    "p5_r04_dismissed": r04_dismissed, "p5_r05_not_cleared": r05_not_cleared,
    "p5_r06_event_authority": r06_event_authority, "p5_r07_cancel_reinspection": r07_cancel_reinspection,
    "p5_r08_isolation": r08_isolation,
    "p5_v01_nominal": v01_nominal, "p5_v02_duplicates": v02_duplicates, "p5_v03_reorder": v03_reorder,
    "p5_v04_lost_acks": v04_lost_acks, "p5_v05_reconnect_replay": v05_reconnect_replay,
    "p5_v06_terminal_lost": v06_terminal_lost, "p5_v07_cancel_before_takeoff": v07_cancel_before_takeoff,
    "p5_v08_cancel_in_flight": v08_cancel_in_flight, "p5_v09_media": v09_media,
    "p5_v10_prepare_rejected": v10_prepare_rejected, "p5_v11_restart": v11_restart,
    "p5_v12_no_low_level": v12_no_low_level,
}


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str, layer: str) -> dict:
    """Run one S0 or S3 case and judge it online and from the recordings. / 运行一个 S0 或 S3 用例并在线与按录制各裁判一次。"""
    from drone_agent.eval.judge_p5 import judge_case

    if case.exists():
        shutil.rmtree(case)
    world = P5World(case, repo, seed=seed)
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
        world.service.business.jobs.stop()
        pending = {uav.task for uav in world.uavs.values() if uav.task is not None and not uav.task.done()}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=30)
    world.export({"scenario": scenario["id"], "seed": seed, "source_sha": sha, "layer": layer,
                  "description": scenario.get("description", ""), "expected": scenario.get("expected", {}),
                  "harness_error": error, "duration_s": round(time.monotonic() - started, 2)})
    world.ledger.close()
    online = judge_case(case, repo)
    replay = judge_case(case, repo, use_replay=True)
    keys = ("classification", "problems", "false_success_reports", "counts")
    agrees = all(online.get(k) == replay.get(k) for k in keys)
    result = {**{k: online.get(k) for k in ("scenario", "seed", "layer", "classification", "problems", "counts",
                                             "false_success_reports", "expected")},
              "passed": bool(online.get("passed")) and agrees, "replay_agrees": agrees, "harness_error": error}
    (case / "judge").mkdir(exist_ok=True)
    (case / "judge/result.json").write_text(json.dumps(online, indent=2, default=str), encoding="utf-8")
    (case / "judge/replay.json").write_text(json.dumps(replay, indent=2, default=str), encoding="utf-8")
    return result


async def run_suite(output: Path, repo: Path, *, sha: str, part: str = "all", only: list[str] | None = None,
                    seeds: list[int] | None = None) -> dict:
    """Run the road template matrix (`s0`), the vendor contract matrix (`s3`) or both. / 运行道路模板矩阵、厂商合同矩阵或两者。"""
    from drone_agent.eval.judge_p5 import COUNTS

    definition = suite(repo)
    layers = {"s0": ("S0",), "s3": ("S3",), "all": ("S0", "S3")}[part]
    results, voided = [], []
    for layer in layers:
        for scenario in definition[layer.lower()]:
            if only and scenario["id"] not in only:
                continue
            for seed in seeds or definition["seeds"]:
                for attempt in range(1, ATTEMPTS + 1):
                    case = output / f"{scenario['id']}-{seed}"
                    result = await run_case(case, repo, scenario, seed, sha, layer)
                    result["attempt"] = attempt
                    print(json.dumps({k: result[k] for k in ("scenario", "seed", "attempt", "passed",
                                                              "classification")}), flush=True)
                    if result["classification"] != "void":
                        break
                    archive = output / "voided" / f"{scenario['id']}-{seed}-attempt{attempt}"
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(case), str(archive))
                    voided.append({k: result[k] for k in ("scenario", "seed", "attempt", "problems")})
                results.append(result)
    counts = {key: sum((r.get("counts") or {}).get(key, 0) for r in results) for key in COUNTS}
    summary = {"schema_version": "0.1.0", "layers": list(layers), "source_sha": sha, "suite": definition["format"],
               "cases": len(results), "passed": sum(1 for r in results if r["passed"]),
               "by_layer": {layer: {"cases": sum(1 for r in results if r["layer"] == layer),
                                    "passed": sum(1 for r in results if r["layer"] == layer and r["passed"])}
                            for layer in layers},
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
    parser.add_argument("--part", choices=["all", "s0", "s3"], default="all")
    parser.add_argument("--scenario", default="all", help="all or comma-separated scenario ids")
    parser.add_argument("--seeds", default=None, help="comma-separated seeds; defaults to the suite's")
    args = parser.parse_args()
    only = None if args.scenario == "all" else args.scenario.split(",")
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else None
    summary = asyncio.run(run_suite(args.output, args.root, sha=args.sha, part=args.part, only=only, seeds=seeds))
    print(json.dumps({k: summary[k] for k in ("status", "cases", "passed", "by_layer", "counts")}))
    raise SystemExit(0 if summary["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
