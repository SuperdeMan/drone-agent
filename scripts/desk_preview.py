"""Local preview of the flight desk with every workspace (D068), for frontend work on this machine only.

It serves the desk page on loopback over the S0 logical world: the formal mission service with the resident desk's
operations, workflow, scheduling and business catalogs, logical docks, and a logical aircraft that runs the real
onboard uplink, guardian and executive; the vision model is the labelled scripted double and the planner the scripted
fixture. The aircraft is relabelled as a logical simulation in a case-local copy of the catalog, so every source label
on the page stays true. There is no simulation supervisor and no independent judge here, and the fixed M1 page shows
an idle stub. `--seed` drives a few objects into being through the formal API (a flown mission, a workflow run up to
its candidate finding, a queued task and a mission awaiting approval). Nothing here is evidence for any gate.

本机预览全部工作区的飞行台（D068），只用于本机的前端开发。在回环地址上以 S0 逻辑世界提供任务台页面：带常驻任务台同款运营、
工作流、调度与业务目录的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与 executive 的逻辑飞行器；视觉模型为带标注
的脚本替身，规划器为脚本夹具。飞行器在用例本地的目录副本中改标为逻辑模拟，页面上的每个来源标注都如实。这里没有仿真监管者与
独立裁判，固定 M1 页面显示空闲替身。`--seed` 经正式 API 造出若干对象（一次飞完的任务、一个运行到候选发现的工作流、一个排队的
任务单和一个待审批的任务）。这里的一切都不能作为任何门禁的证据。

Run: `uv run python scripts/desk_preview.py [--seed]`, then open http://127.0.0.1:8770.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import uvicorn
import yaml

from drone_agent.console.mission import MAX_FRAME, LocalApi, MissionConsole, scene_scope
from drone_agent.console.unified import UnifiedConsole
from drone_agent.eval.p1_world import TEXTS
from drone_agent.eval.p2_world import P2World
from drone_agent.eval.p4_world import P4World
from drone_agent.eval.scripted_vision import AppearanceWorld, ScriptedVisionProvider
from drone_agent.fleet.scheduler import Scheduler, build_scheduling

ROOT = Path(__file__).resolve().parents[1]
CATALOG = "configs/sites/p1_s1_v1.yaml"
WORKFLOWS = "configs/workflows/p4_s1_v1.yaml"
SCHEDULING = "configs/scheduling/p3_desk_v1.yaml"
BUSINESS = "configs/analysis/p4_s1_v1.yaml"
PROJECT = "campus_s1"


class PreviewWorld(P4World):
    """The P4 S0 world on the resident desk's catalogs, plus the P3 scheduler. / 常驻任务台目录上的 P4 S0 世界，加 P3 调度器。"""

    def __init__(self, case: Path, repo: Path, *, principal: str):
        (case / "input").mkdir(parents=True, exist_ok=True)
        catalog = yaml.safe_load((repo / CATALOG).read_text(encoding="utf-8"))
        catalog["projects"][PROJECT]["name"] = "Campus preview, logical flights / 园区预览（逻辑飞行）"
        for robot in catalog["robots"].values():
            robot["execution_backend"] = "logical_sim"  # true for these flights / 对这些飞行如实
        (case / "input/catalog.yaml").write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")
        (case / "input/members.yaml").write_text(yaml.safe_dump({
            "format": "drone.project-members/v1", "schema_version": "0.1.0",
            "members": [{"principal": principal, "project_id": PROJECT,
                         "roles": ["operator", "approver", "reviewer", "admin"]}]}), encoding="utf-8")
        self.business_path, self.scheduling_path = repo / BUSINESS, repo / SCHEDULING
        self.appearance = AppearanceWorld({"asset_red": "red", "asset_blue": "blue"})
        self.vision_provider, self.vision_on, self.analysis_paused, self.runner_override = (
            ScriptedVisionProvider(), True, False, None)
        P2World.__init__(self, case, repo, seed=7, workflows=repo / WORKFLOWS, catalog=case / "input/catalog.yaml",
                         members=case / "input/members.yaml")
        for robot_id, uav in self.uavs.items():
            uav.camera = self.appearance.camera(robot_id)

    def _service(self):
        service = super()._service()
        scheduling = build_scheduling(self.ledger, self.scheduling_path, service.ops,
                                      backups=self.case / "service/backups", clock=self.clock)
        service.scheduler = Scheduler(service, scheduling, worker_id="sch-preview")
        return service

    async def step(self, dt: float = 0.1) -> None:
        await super().step(dt)
        self.service.tick_scheduler()


async def seed(world: PreviewWorld, principal: str) -> None:
    """A few objects through the formal API, as a person would create them. / 像人一样经正式 API 造出若干对象。"""
    robot = sorted(world.uavs)[0]
    flown = await world.submit(robot, "red", actor=principal, project=PROJECT)
    await world.approve(flown, actor=principal)
    await world.until(lambda: world.status(flown) in ("completed", "incomplete"), "the seeded flight", 120)
    run = await world.start("desk_watch", {"asset": "asset_red"}, actor=principal, project=PROJECT)
    world.damage(robot)
    await approve_until_finding(world, run, principal)
    await world.api(principal, "tasks.submit", project_id=PROJECT, asset_id="asset_blue", volume_id="campus_training",
                    candidates=[], priority=1, idempotency_key="preview-task-1")
    await world.api(principal, "missions.submit", project_id=PROJECT, robot_id=robot, text=TEXTS["blue"],
                    volume_id="campus_training", asset_ids=[], idempotency_key="preview-pending-1")


async def approve_until_finding(world: PreviewWorld, run: str, principal: str) -> None:
    """Approve the run's missions one by one until its analysis opens a finding. / 逐个审批运行的任务，直到分析开出发现。"""
    for _ in range(1200):
        await world.step()
        await world.approve_waiting(run, actor=principal)
        if world.finding_of(run):
            return
    raise TimeoutError("no candidate finding from the seeded run")


def idle_fixed_page(console: MissionConsole) -> UnifiedConsole:
    """The unified entry with an idle M1 stub: the fixed page renders, no run exists. / 带空闲 M1 替身的统一入口。"""
    state = {"source_sha": "0" * 40, "job": None, "fresh": False, "allowed_actions": [], "events": []}
    backend = SimpleNamespace(request=lambda _value: dict(state), fetch=lambda _job: {}, restore_pages=lambda _b: None,
                              health=lambda: {"runtime_source_sha": "0" * 40, "status": "ready"})
    return UnifiedConsole(console, backend)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--case", type=Path, default=Path(tempfile.gettempdir()) / "drone-agent-desk-preview",
                        help="a scratch directory, replaced on every start")
    parser.add_argument("--seed", action="store_true", help="create a few objects through the formal API")
    args = parser.parse_args()
    if args.case.exists():
        shutil.rmtree(args.case)
    user = getpass.getuser()
    principal = f"local:{user}"
    world = PreviewWorld(args.case, ROOT, principal=principal)
    await world.settle()
    origin = f"http://127.0.0.1:{args.port}"
    scope = {**scene_scope(ROOT, ROOT / "configs/scenarios/m2_campus_v2.yaml"), "planner": "scripted"}
    console = MissionConsole(LocalApi(world.service), origin, tailnet=False, scope=scope, local_user=user)
    server = uvicorn.Server(uvicorn.Config(idle_fixed_page(console), host="127.0.0.1", port=args.port, lifespan="off",
                                           ws="wsproto", ws_max_size=MAX_FRAME, http="h11", log_level="warning"))
    serving = asyncio.create_task(server.serve())
    print(json.dumps({"console_url": origin, "mode": "local preview", "flights": "logical", "case": str(args.case)}),
          flush=True)
    if args.seed:
        await seed(world, principal)
        print(json.dumps({"seeded": True}), flush=True)
    while not serving.done():
        await world.step()


if __name__ == "__main__":
    asyncio.run(main())
