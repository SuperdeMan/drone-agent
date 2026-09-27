"""S0 world for P4 (D063 §12): the P2 world plus the business loop, driven only through the formal API.

The world is the P2 S0 world (the formal mission service with its operations and workflow catalogs, logical docks
and logical aircraft running the real onboard uplink, guardian and executive) with the P4 workflow and business
catalogs loaded into the same service. The simulated world holds each registered asset's appearance: the harness
damages an asset, repairs it when the maintenance crew would, or makes a camera replay an old frame; the logical
camera renders what the world shows. The vision model is the labelled scripted double. Operators, reviewers,
approvers and the admin act only through the API under their own identities; faults are applied to the world, to
the scripted provider or by replacing a service object as a crashed process would be replaced. Each scenario of the
P4 matrix (P4-F01–F15) records what the independent judge needs; the scenario code decides nothing.

P4 的 S0 世界（D063 §12）：P2 世界加上业务闭环，只经正式 API 驱动。

世界即 P2 S0 世界（带运营与工作流目录的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与 executive 的逻辑
飞行器），并把 P4 工作流与业务目录加载到同一服务中。模拟世界持有每个登记资产的外观：编排损坏资产、在维修队会修复时修复
它，或让相机重放旧帧；逻辑相机渲染世界所呈现的内容。视觉模型为带标注的脚本替身。操作者、复核人、审批人与 admin 只以各自
身份经 API 行动；故障作用于世界、脚本 provider，或像替换崩溃进程那样替换服务对象。P4 矩阵（P4-F01–F15）的每个场景记录
独立裁判需要的内容；场景代码不对通过与否做任何决定。
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

from drone_agent.eval.p1_world import ScenarioTimeout
from drone_agent.eval.p2_world import EXPORT_TABLES as P2_TABLES
from drone_agent.eval.p2_world import P2World
from drone_agent.eval.scripted_vision import AppearanceWorld, ScriptedVisionProvider, VisionConfig
from drone_agent.fleet.business import BusinessEngine, build_business
from drone_agent.fleet.workflow_models import TERMINAL_RUN, activity_key

SUITE = "configs/scenarios/p4_suite.yaml"
WORKFLOWS = "configs/workflows/p4_campus_v1.yaml"
BUSINESS = "configs/analysis/p4_campus_v1.yaml"
MEMBERS = "configs/sites/p4_members_s0.yaml"
OPERATOR, REVIEWER, REVIEWER2, VIEWER, ADMIN, HARBOR, JUDGE = (
    "harness:p4-operator", "harness:p4-reviewer", "harness:p4-reviewer2", "harness:p4-viewer", "harness:p4-admin",
    "harness:p4-harbor", "harness:p4-judge")
BZ_TABLES = ("bz_catalogs", "bz_jobs", "bz_references", "bz_findings", "bz_reviews", "bz_orders", "bz_rounds")
EXPORT_TABLES = (*P2_TABLES, *BZ_TABLES, "reports", "versions")
RUN_FINAL = tuple(state.value for state in TERMINAL_RUN)
ATTEMPTS = 3


class P4World(P2World):
    """One P4 S0 case: the P2 world with the business loop and an appearance world. / 一个 P4 S0 用例。"""

    def __init__(self, case: Path, repo: Path, *, seed: int, business: Path | None = None):
        self.business_path = business or repo / BUSINESS
        self.appearance = AppearanceWorld({"asset_red": "red", "asset_blue": "blue"})
        self.vision_provider = ScriptedVisionProvider()
        self.vision_on = True
        self.analysis_paused = False
        self.runner_override: dict | None = None
        super().__init__(case, repo, seed=seed, workflows=repo / WORKFLOWS, members=repo / MEMBERS)
        for robot_id, uav in self.uavs.items():
            uav.camera = self.appearance.camera(robot_id)

    def _service(self):
        service = super()._service()
        path = self.business_path
        if self.runner_override:
            # A redeployment with a different runner policy (a new catalog digest, the old one stays pinned).
            # 以不同执行器策略重新部署（新的目录摘要，旧摘要仍被固定）。
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["runner"] = {**data["runner"], **self.runner_override}
            path = self.case / "input" / "business-override.yaml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        business = build_business(self.repo, self.ledger, path, service.ops, service.workflows.workflows,
                                  backups=self.case / "service/backups", clock=self.clock)
        vision = (self.vision_provider, VisionConfig()) if self.vision_on else None
        service.business = BusinessEngine(service, business, root=self.repo, vision=vision,
                                          recordings=self.case / "service/recordings/analysis")
        return service

    def restart_service(self) -> None:
        """A process restart: the old runner's tasks die with it, the jobs keep their leases. / 进程重启。"""
        old = getattr(self.service, "business", None)
        if old is not None:
            old.jobs.stop()
        super().restart_service()

    async def step(self, dt: float = 0.1) -> None:
        await super().step(dt)
        if not self.analysis_paused:
            await self.service.tick_analysis()

    # ── P4 identities on the P2 helpers / P2 辅助方法换成 P4 身份 ──

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

    # ── business helpers / 业务辅助 ──

    @property
    def business(self) -> BusinessEngine:
        return self.service.business

    async def bz(self, method: str, *, actor: str = OPERATOR, trust: str = "first_party", probe: bool = False,
                 **params) -> dict:
        return await self.api(actor, method, trust=trust, probe=probe, **params)

    def job_of(self, run_id: str, node_id: str = "analyze") -> dict | None:
        return self.business.store.job_by_key(activity_key(run_id, node_id))

    def finding_of(self, run_id: str, node_id: str = "analyze") -> str | None:
        job = self.job_of(run_id, node_id)
        return job["finding_id"] if job else None

    def order_of(self, finding_id: str | None) -> dict | None:
        return self.business.store.order_by_finding(finding_id) if finding_id else None

    def round_runs(self, order_id: str) -> list[str]:
        return [r["reinspection_run"] for r in self.business.store.rounds(order_id) if r["reinspection_run"]]

    def order_state(self, order_id: str) -> str:
        return self.business.store.order(order_id)["state"]

    def damage(self, robot_id: str, asset_id: str = "asset_red") -> None:
        self.appearance.set(robot_id, asset_id, "damaged")
        self.inject("appearance", robot_id=robot_id, asset_id=asset_id, state="damaged")

    def repair_world(self, robot_id: str, asset_id: str = "asset_red") -> None:
        self.appearance.set(robot_id, asset_id, "normal")
        self.inject("appearance", robot_id=robot_id, asset_id=asset_id, state="normal")

    async def feedback(self, order_id: str, *, request_id: str, actor: str = OPERATOR, probe: bool = False,
                       project: str = "campus_ops") -> dict:
        return await self.bz("orders.repair", actor=actor, probe=probe, project_id=project, order_id=order_id,
                             request_id=request_id, note="harness repair feedback")

    async def decide_finding(self, finding_id: str, decision: str, *, actor: str = REVIEWER,
                             request_id: str | None = None, probe: bool = False, project: str = "campus_ops") -> dict:
        return await self.bz("findings.review", actor=actor, probe=probe, project_id=project, finding_id=finding_id,
                             decision=decision, request_id=request_id or f"finding-{finding_id[3:11]}-{actor[-4:]}",
                             note="harness review")

    async def register_reference(self, run_id: str, *, actor: str = ADMIN, probe: bool = False,
                                 project: str = "campus_ops") -> dict:
        inspection = self.node(run_id, "await_inspection")["result"]
        return await self.bz("references.register", actor=actor, probe=probe, project_id=project,
                             mission_id=inspection["mission_id"], evidence_id=inspection["evidence_id"],
                             note="harness reference")

    async def run_business(self, runs: list[str], *, until: Callable[[], bool], what: str,
                           decisions: Callable[[str, str], str | None] | None = None, approve: bool = True,
                           timeout_s: float = 150.0) -> None:
        """Step the world and act as the humans would: approve every waiting mission of the runs and of their orders'
        reinspection runs, and decide waiting reviews as `decisions(run, node)` says (None leaves them waiting).

        步进世界并像人一样行动：审批这些运行及其工单复检运行中等待的任务，并按 `decisions(run, node)` 处理等待中的复核
        （None 表示继续等待）。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await self.step()
            for run_id in list(runs):
                finding = None
                for node_id, node in self.store.nodes(run_id).items():
                    if node["activity"] == "analyze_evidence" and (node["result"] or {}).get("finding_id"):
                        finding = node["result"]["finding_id"]
                order = self.order_of(finding)
                for child in self.round_runs(order["order_id"]) if order else []:
                    if child not in runs:
                        runs.append(child)
            for run_id in runs:
                if approve:
                    await self.approve_waiting(run_id)
                if decisions is None:
                    continue
                for node_id, node in self.store.nodes(run_id).items():
                    if node["activity"] == "human_review" and node["state"] == "waiting":
                        decision = decisions(run_id, node_id)
                        if decision is not None:
                            await self.review(run_id, node_id, decision, request_id=f"rv-{run_id[3:11]}-{node_id}")
            if until():
                return
        raise ScenarioTimeout(f"timed out waiting for {what}")

    def export(self, scenario: dict) -> None:
        super().export(scenario)
        tables = {}
        with sqlite3.connect(str(self.case / "service/ledger.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            for table in EXPORT_TABLES:
                tables[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
        (self.case / "service-export/business.json").write_text(
            json.dumps(tables, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        (self.case / "service-export/appearance.json").write_text(
            json.dumps({"history": self.appearance.history, "members": str(MEMBERS)}, indent=2), encoding="utf-8")


def always(decision: str):
    return lambda run_id, node_id: decision


def confirm_all(run_id: str, node_id: str) -> str:
    return "confirmed"


# ── scenarios P4-F01–F15 / 场景 ──


async def open_order(w: P4World, *, robot: str = "uav_b", workflow: str = "appearance_watch",
                     request_id: str | None = None) -> tuple[str, str, dict]:
    """Damage the asset, inspect it, confirm the finding: (run, finding, order). / 损坏资产、巡检并确认发现。"""
    w.damage(robot)
    run = await w.start(workflow, {"asset": "asset_red"}, request_id=request_id)
    await w.run_business([run], until=lambda: w.final(run), what="inspection run end", decisions=confirm_all)
    finding = w.finding_of(run)
    order = w.order_of(finding)
    if order is None:
        raise ScenarioTimeout("no order was opened")
    return run, finding, order


async def f01_close(w: P4World) -> None:
    run, _, order = await open_order(w)
    w.repair_world("uav_b")
    await w.feedback(order["order_id"], request_id="repair-1")
    runs = [run]
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "closed" and all(
        w.final(r) for r in runs), what="order closed", decisions=confirm_all)


async def f02_dedup(w: P4World) -> None:
    w.damage("uav_b")
    first = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="watch-1")
    await w.run_business([first], until=lambda: w.node(first, "review")["state"] == "waiting",
                         what="first review waiting")
    second = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="watch-2")
    await w.run_business([second], until=lambda: w.node(second, "analyze")["state"] == "completed",
                         what="second analysis")
    runs = [first, second]
    await w.run_business(runs, until=lambda: all(w.final(r) for r in (first, second)), what="both runs end",
                         decisions=lambda run_id, node_id: "confirmed" if run_id == first else None)
    order = w.order_of(w.finding_of(first))
    w.repair_world("uav_b")
    await w.feedback(order["order_id"], request_id="repair-1")
    await w.feedback(order["order_id"], request_id="repair-1")
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "closed" and all(
        w.final(r) for r in runs), what="order closed", decisions=confirm_all)


async def f03_dismissed(w: P4World) -> None:
    w.damage("uav_b")
    first = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="watch-1")
    await w.run_business([first], until=lambda: w.final(first), what="dismissed run", decisions=always("dismissed"))
    second = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="watch-2")
    await w.run_business([second], until=lambda: w.final(second), what="second dismissed run",
                         decisions=always("dismissed"))


async def f04_authority(w: P4World) -> None:
    w.damage("uav_c")
    await reference_for(w, "uav_c")
    w.vision_provider.mode = "inject"
    smuggled = await w.start("vlm_watch", {"asset": "asset_red"}, request_id="watch-inject")
    await w.run_business([smuggled], until=lambda: w.final(smuggled), what="smuggled answer refused")
    w.vision_provider.mode = None
    run = await w.start("vlm_watch", {"asset": "asset_red"}, request_id="watch-2")
    await w.run_business([run], until=lambda: w.node(run, "review")["state"] == "waiting", what="review waiting")
    finding = w.finding_of(run)
    for actor in (VIEWER, OPERATOR, ADMIN, HARBOR):
        await w.decide_finding(finding, "confirmed", actor=actor, probe=True, request_id=f"probe-{actor[-6:]}")
    await w.decide_finding(finding, "confirmed", actor=HARBOR, probe=True, project="harbor_ops",
                           request_id="probe-harbor-project")
    await w.review(run, "review", "confirmed", actor=OPERATOR, probe=True, request_id="probe-node-operator")
    for method, params in (("findings.list", {"project_id": "campus_ops"}),
                           ("findings.review", {"project_id": "campus_ops", "finding_id": finding,
                                                "decision": "confirmed", "request_id": "a2a", "note": "agent"}),
                           ("business.summary", {"project_id": "campus_ops"})):
        await w.api("a2a:probe-client", method, trust="third_party", probe=True, **params)
    await w.api("dock:p1-s0-sim", "findings.list", trust="backend", probe=True, project_id="campus_ops")
    await w.api("", "findings.list", probe=True, project_id="campus_ops")
    await w.run_business([run], until=lambda: w.final(run), what="confirmed run end", decisions=confirm_all)


async def reference_for(w: P4World, robot: str, asset: str = "asset_red") -> None:
    """Register a normal capture of the asset as its reference appearance (admin). / 登记资产的正常采集为参考外观。"""
    damaged = (robot, asset) in w.appearance.damaged
    w.appearance.set(robot, asset, "normal")
    workflow = {"uav_c": "vlm_watch", "uav_a": "dual_watch"}[robot]
    baseline = await w.start(workflow, {"asset": asset}, request_id=f"baseline-{robot}")
    await w.run_business([baseline], until=lambda: w.final(baseline), what="baseline run")
    response = await w.register_reference(baseline)
    if not response.get("ok"):
        raise RuntimeError(f"reference refused: {response.get('issue')}")
    await w.register_reference(baseline, actor=VIEWER, probe=True)
    if damaged:
        w.appearance.set(robot, asset, "damaged")


async def f05_not_repaired(w: P4World) -> None:
    run, _, order = await open_order(w)
    await w.feedback(order["order_id"], request_id="repair-1")
    runs = [run]
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "reinspection_failed" and all(
        w.final(r) for r in runs), what="round 1 failed", decisions=confirm_all)
    w.repair_world("uav_b")
    await w.feedback(order["order_id"], request_id="repair-2")
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "closed" and all(
        w.final(r) for r in runs), what="round 2 closed", decisions=confirm_all)


async def f06_evidence_missing(w: P4World) -> None:
    run, _, order = await open_order(w)
    w.repair_world("uav_b")
    # Flat frames for the reinspection and its bounded retries: the capture is never verified.
    # 复检及其有界重试都拍到平帧：采集永远不会被证实。
    w.uavs["uav_b"].frames = [None, None, None]
    w.inject("flat_frames", robot_id="uav_b", count=3)
    await w.feedback(order["order_id"], request_id="repair-1")
    runs = [run]
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) == "reinspection_unknown" and all(
        w.final(r) for r in runs), what="round unknown", decisions=confirm_all, timeout_s=200)
    w.uavs["uav_b"].frames = []


async def f07_replay(w: P4World) -> None:
    baseline = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="baseline")
    await w.run_business([baseline], until=lambda: w.final(baseline), what="normal baseline")
    inspection = w.node(baseline, "await_inspection")["result"]
    row = next(r for r in w.ledger.evidence(inspection["mission_id"]) if r["evidence_id"] == inspection["evidence_id"])
    old = w.hub.media(row["media_path"])
    run, _, order = await open_order(w, request_id="watch-damaged")
    w.appearance.replays["uav_b"] = old
    w.inject("camera_replay", robot_id="uav_b", media_sha256=inspection["evidence_sha256"])
    await w.feedback(order["order_id"], request_id="repair-1")
    runs = [run]
    await w.run_business(runs, until=lambda: w.order_state(order["order_id"]) != "reinspection_requested" and all(
        w.final(r) for r in runs), what="replayed round settled", decisions=confirm_all, timeout_s=200)


async def f08_cancel_reinspection(w: P4World) -> None:
    run, _, order = await open_order(w)
    w.repair_world("uav_b")
    await w.feedback(order["order_id"], request_id="repair-1")
    child = w.round_runs(order["order_id"])[0]
    await w.run_business([child], until=lambda: bool(w.missions_of(child)), what="reinspection mission",
                         approve=False)
    await w.cancel_run(child)
    await w.run_business([run, child], until=lambda: w.final(child), what="cancelled reinspection", approve=False)


async def f09_reuse(w: P4World) -> None:
    await reference_for(w, "uav_a")
    w.damage("uav_a")
    run = await w.start("dual_watch", {"asset": "asset_red"}, request_id="dual-1")
    await w.run_business([run], until=lambda: w.final(run), what="dual run end", decisions=always("confirmed"))
    inspection = w.node(run, "await_inspection")["result"]
    common = {"project_id": "campus_ops", "mission_id": inspection["mission_id"],
              "evidence_id": inspection["evidence_id"]}
    await w.bz("analysis.submit", **common, analyzer="vlm_s0_v1", request_id="reuse-1")
    await w.bz("analysis.submit", **common, analyzer="vlm_s0_v1", request_id="reuse-1")
    await w.bz("analysis.submit", **common, analyzer="thermal_s0_v1", request_id="reuse-thermal", probe=True)
    await w.bz("analysis.submit", **common, analyzer="vlm_s0_v1", request_id="reuse-viewer", actor=VIEWER,
               probe=True)
    await w.bz("analysis.submit", **{**common, "project_id": "harbor_ops"}, analyzer="vlm_s0_v1",
               request_id="reuse-harbor", actor=HARBOR, probe=True)
    await w.bz("analysis.submit", **common, analyzer="unknown_analyzer", request_id="reuse-unknown", probe=True)
    await w.hold(1.5)


async def f10_model_faults(w: P4World) -> None:
    await reference_for(w, "uav_c")
    w.damage("uav_c")
    run = await w.start("vlm_watch", {"asset": "asset_red"}, request_id="watch-1")
    await w.run_business([run], until=lambda: w.final(run), what="first run", decisions=always("dismissed"))
    inspection = w.node(run, "await_inspection")["result"]
    common = {"project_id": "campus_ops", "mission_id": inspection["mission_id"],
              "evidence_id": inspection["evidence_id"], "analyzer": "vlm_s0_v1"}
    for mode in ("timeout", "malformed", "refusal", "error"):
        w.vision_provider.mode = mode
        w.inject("vision_mode", mode=mode)
        await w.bz("analysis.submit", **common, request_id=f"fault-{mode}")
        await w.until(lambda: all(j["state"] in ("completed", "refused")
                                  for j in w.business.store.jobs(project_id="campus_ops")),
                      f"{mode} job settled", timeout_s=40)
    w.vision_provider.mode = None
    w.vision_on = False
    w.restart_service()
    await w.bz("analysis.submit", **common, request_id="fault-unavailable")
    await w.hold(1.0)
    w.vision_on = True
    w.runner_override = {"daily_tokens": 1}
    w.restart_service()
    await w.bz("analysis.submit", **common, request_id="fault-budget")
    await w.hold(1.0)
    w.runner_override = None
    w.restart_service()
    w.vision_provider.mode = "malformed"
    failed = await w.start("vlm_watch", {"asset": "asset_red"}, request_id="watch-malformed")
    await w.run_business([failed], until=lambda: w.final(failed), what="malformed run end")
    w.vision_provider.mode = None


async def f11_cancel(w: P4World) -> None:
    await reference_for(w, "uav_c")
    w.damage("uav_c")
    w.vision_provider.delay_s = 3.0
    calls = len(w.vision_provider.calls)
    run = await w.start("vlm_watch", {"asset": "asset_red"}, request_id="watch-cancel")
    # Cancel while the model is being asked, not before the job starts. / 在询问模型期间取消，而不是作业开始之前。
    await w.run_business([run], until=lambda: len(w.vision_provider.calls) > calls, what="model called")
    await w.cancel_run(run)
    await w.run_business([run], until=lambda: w.final(run) and (w.job_of(run) or {}).get("state") in (
        "completed", "refused"), what="cancelled run and its job", approve=False)
    w.vision_provider.delay_s = 0.0
    await w.hold(1.0)


async def f12_crash(w: P4World) -> None:
    await reference_for(w, "uav_c")
    w.damage("uav_c")
    w.vision_provider.delay_s = 3.0
    run = await w.start("vlm_watch", {"asset": "asset_red"}, request_id="watch-crash")
    await w.run_business([run], until=lambda: (w.job_of(run) or {}).get("state") == "running", what="job running")
    w.inject("crash", window="job_running", job_id=w.job_of(run)["job_id"])
    w.restart_service()
    w.vision_provider.delay_s = 0.0
    await w.run_business([run], until=lambda: w.node(run, "review")["state"] == "waiting", what="job rerun")
    second = await w.start("vlm_watch", {"asset": "asset_blue"}, request_id="watch-crash-2")
    await w.run_business([second], until=lambda: w.job_of(second) is not None, what="second job queued")
    # The result is recorded while no workflow pass runs; the process then dies before the node moves.
    # 结果入账时没有工作流处理运行；随后进程在节点推进之前死亡。
    w.workflows_paused = True
    await w.run_business([second], until=lambda: (w.job_of(second) or {}).get("state") in ("completed", "refused"),
                         what="second job result")
    w.inject("crash", window="result_recorded", job_id=w.job_of(second)["job_id"])
    w.restart_service()
    w.workflows_paused = False
    await w.run_business([run, second], until=lambda: w.final(run) and w.final(second), what="runs end",
                         decisions=confirm_all)


async def f13_concurrent_reviews(w: P4World) -> None:
    w.damage("uav_b")
    run = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="watch-1")
    await w.run_business([run], until=lambda: w.node(run, "review")["state"] == "waiting", what="review waiting")
    finding = w.finding_of(run)
    first, second = await asyncio.gather(
        w.decide_finding(finding, "confirmed", actor=REVIEWER, request_id="concurrent-a"),
        w.decide_finding(finding, "dismissed", actor=REVIEWER2, request_id="concurrent-b"))
    w.inject("concurrent_reviews", first=first.get("ok"), second=second.get("ok"))
    await w.decide_finding(finding, "confirmed", actor=REVIEWER, request_id="concurrent-a")
    await w.run_business([run], until=lambda: w.final(run), what="run end")


async def f14_isolation(w: P4World) -> None:
    w.damage("uav_b")
    w.damage("uav_h")
    campus = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="campus-1")
    harbor_response = await w.bz("workflows.start", actor=HARBOR, project_id="harbor_ops",
                                 workflow_id="harbor_watch", request_id="harbor-1", inputs={"asset": "asset_red"})
    harbor = harbor_response["result"]["run"]["run_id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        await w.step()
        await w.approve_waiting(campus)
        await w.approve_waiting(harbor, actor=HARBOR)
        if w.node(campus, "review")["state"] == "waiting" and w.node(harbor, "review")["state"] == "waiting":
            break
    campus_finding, harbor_finding = w.finding_of(campus), w.finding_of(harbor)
    inspection = w.node(campus, "await_inspection")["result"]
    probes = [
        (HARBOR, "findings.get", {"project_id": "campus_ops", "finding_id": campus_finding}),
        (HARBOR, "findings.get", {"project_id": "harbor_ops", "finding_id": campus_finding}),
        (HARBOR, "findings.review", {"project_id": "harbor_ops", "finding_id": campus_finding,
                                     "decision": "confirmed", "request_id": "x-1", "note": "other project"}),
        (REVIEWER, "findings.review", {"project_id": "campus_ops", "finding_id": harbor_finding,
                                       "decision": "dismissed", "request_id": "x-2", "note": "other project"}),
        (VIEWER, "findings.review", {"project_id": "campus_ops", "finding_id": campus_finding,
                                     "decision": "dismissed", "request_id": "x-3", "note": "viewer"}),
        (HARBOR, "analysis.submit", {"project_id": "harbor_ops", "mission_id": inspection["mission_id"],
                                     "evidence_id": inspection["evidence_id"], "analyzer": "signature_s0_v1",
                                     "request_id": "x-4"}),
        (HARBOR, "references.register", {"project_id": "harbor_ops", "mission_id": inspection["mission_id"],
                                         "evidence_id": inspection["evidence_id"], "note": "x-5"}),
        (HARBOR, "analysis.media", {"project_id": "harbor_ops", "asset": "campus_ops/site_b/asset_red"}),
        (VIEWER, "orders.repair", {"project_id": "campus_ops", "order_id": "ord-0000", "request_id": "x-6",
                                   "note": "viewer"}),
        (HARBOR, "business.summary", {"project_id": "campus_ops"}),
        ("harness:stranger", "findings.list", {"project_id": "campus_ops"}),
    ]
    w.random.shuffle(probes)
    for actor, method, params in probes:
        await w.bz(method, actor=actor, probe=True, **params)
    await w.decide_finding(harbor_finding, "confirmed", actor=HARBOR, project="harbor_ops", request_id="harbor-own")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        await w.step()
        for run_id in (campus, harbor):
            await w.approve_waiting(run_id, actor=HARBOR if run_id == harbor else OPERATOR)
        if w.node(campus, "review")["state"] == "waiting":
            await w.review(campus, "review", "dismissed", request_id="campus-own")
        if w.final(campus) and w.final(harbor):
            return
    raise ScenarioTimeout("isolation runs did not end")


async def f15_verdict_boundary(w: P4World) -> None:
    w.uavs["uav_b"].frames = [None, None, None]
    w.inject("flat_frames", robot_id="uav_b", count=3)
    flat = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="watch-flat")
    await w.run_business([flat], until=lambda: w.final(flat), what="flat run end", timeout_s=200)
    w.uavs["uav_b"].frames = []
    w.damage("uav_b")
    run = await w.start("appearance_watch", {"asset": "asset_red"}, request_id="watch-damaged")
    await w.run_business([run], until=lambda: w.node(run, "await_inspection")["state"] == "completed",
                         what="verified inspection")
    mission = w.node(run, "await_inspection")["result"]["mission_id"]
    w.inject("report_before_analysis", mission_id=mission, report=w.ledger.report(mission))
    await w.run_business([run], until=lambda: w.final(run), what="run end", decisions=confirm_all)
    w.inject("report_after_analysis", mission_id=mission, report=w.ledger.report(mission))


SCENARIOS: dict[str, Callable] = {
    "p4_f01_close": f01_close, "p4_f02_dedup": f02_dedup, "p4_f03_dismissed": f03_dismissed,
    "p4_f04_authority": f04_authority, "p4_f05_not_repaired": f05_not_repaired,
    "p4_f06_evidence_missing": f06_evidence_missing, "p4_f07_replay": f07_replay,
    "p4_f08_cancel_reinspection": f08_cancel_reinspection, "p4_f09_reuse": f09_reuse,
    "p4_f10_model_faults": f10_model_faults, "p4_f11_cancel": f11_cancel, "p4_f12_crash": f12_crash,
    "p4_f13_concurrent_reviews": f13_concurrent_reviews, "p4_f14_isolation": f14_isolation,
    "p4_f15_verdict_boundary": f15_verdict_boundary,
}


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str) -> dict:
    """Run one S0 case and judge it online and from the recordings. / 运行一个 S0 用例并在线与按录制各裁判一次。"""
    from drone_agent.eval.judge_p4 import judge_case

    if case.exists():
        shutil.rmtree(case)
    world = P4World(case, repo, seed=seed)
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
    from drone_agent.eval.judge_p4 import COUNTS

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
