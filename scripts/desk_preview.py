"""Local preview of the flight desk with every workspace (D068, P5), for frontend work on this machine only.

It serves the desk page on loopback over the S0 logical world: the formal mission service with the resident desk's
platform catalogs (p5_desk_v1: the campus site, the logical fleet and the vendor-managed dock with its protocol
simulator), logical docks, and logical aircraft that run the real onboard uplink, guardian and executive; the vision
model is the labelled scripted double and the planner the scripted fixture. The PX4 aircraft is relabelled as a logical
simulation in a case-local copy of the catalog, so every source label on the page stays true. There is no simulation
supervisor and no independent judge here, and the fixed M1 page shows an idle stub. `--seed` drives a few objects into
being through the formal API (a flown mission, a workflow run up to its candidate finding, a queued fleet task, a
vendor-managed inspection and a mission awaiting approval). Nothing here is evidence for any gate.

本机预览全部工作区的飞行台（D068、P5），只用于本机的前端开发。在回环地址上以 S0 逻辑世界提供任务台页面：带常驻任务台平台目录
（p5_desk_v1：园区站点、逻辑机队与带协议模拟器的厂商托管机场）的正式任务服务、逻辑机场，以及运行真实机载 uplink、guardian 与
executive 的逻辑飞行器；视觉模型为带标注的脚本替身，规划器为脚本夹具。PX4 飞行器在用例本地的目录副本中改标为逻辑模拟，页面上的
每个来源标注都如实。这里没有仿真监管者与独立裁判，固定 M1 页面显示空闲替身。`--seed` 经正式 API 造出若干对象（一次飞完的任务、
一个运行到候选发现的工作流、一个排队的机队任务单、一次厂商托管巡检和一个待审批的任务）。这里的一切都不能作为任何门禁的证据。

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
from drone_agent.eval.p5_desk_world import P5DeskWorld

ROOT = Path(__file__).resolve().parents[1]
CATALOG = "configs/sites/p5_desk_v1.yaml"
PROJECT = "campus_s1"
PROJECTS = ("campus_s1", "fleet_s0", "vendor_s3")


class PreviewWorld(P5DeskWorld):
    """The desk platform catalogs over the S0 world, the PX4 aircraft relabelled logical. / S0 世界上的任务台平台目录。"""

    def __init__(self, case: Path, repo: Path, *, principal: str):
        (case / "input").mkdir(parents=True, exist_ok=True)
        catalog = yaml.safe_load((repo / CATALOG).read_text(encoding="utf-8"))
        catalog["projects"][PROJECT]["name"] = "Campus preview, logical flights / 园区预览（逻辑飞行）"
        for robot in catalog["robots"].values():
            if robot["execution_backend"] == "px4_sitl":
                robot["execution_backend"] = "logical_sim"  # true for these flights / 对这些飞行如实
        (case / "input/catalog.yaml").write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")
        (case / "input/members.yaml").write_text(yaml.safe_dump({
            "format": "drone.project-members/v1", "schema_version": "0.1.0",
            "members": [{"principal": principal, "project_id": project,
                         "roles": ["operator", "approver", "reviewer", "admin"]} for project in PROJECTS]}),
            encoding="utf-8")
        self.catalog_file, self.members_file = str(case / "input/catalog.yaml"), str(case / "input/members.yaml")
        super().__init__(case, repo, seed=7)


async def seed(world: PreviewWorld, principal: str) -> None:
    """A few objects through the formal API, as a person would create them. / 像人一样经正式 API 造出若干对象。"""
    robot = "uav_01"
    flown = await world.submit(robot, "red", actor=principal, project=PROJECT)
    await world.approve(flown, actor=principal)
    await world.until(lambda: world.status(flown) in ("completed", "incomplete"), "the seeded flight", 120)
    run = await world.start("desk_watch", {"asset": "asset_red"}, actor=principal, project=PROJECT)
    world.damage(robot)
    await approve_until_finding(world, run, principal)
    vendor = await world.start("vendor_watch", {"asset": "asset_red"}, actor=principal, project="vendor_s3")
    for _ in range(1200):
        await world.step()
        await world.approve_waiting(vendor, actor=principal)
        if world.final(vendor):
            break
    await world.api(principal, "tasks.submit", project_id="fleet_s0", asset_id="asset_blue",
                    volume_id="campus_training", candidates=[], priority=1, idempotency_key="preview-task-1")
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
