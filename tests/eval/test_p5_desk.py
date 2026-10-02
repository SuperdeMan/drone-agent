"""The desk platform catalogs in S0, the soak harness against them, and the soak judge on its records (D069, D073).

任务台平台目录的 S0 形态、长稳编排在其上的运行，以及长稳裁判对其记录的判定（D069、D073）。
"""

from __future__ import annotations

import asyncio
import json
import runpy
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drone_agent.eval.judge_p5_soak import judge
from drone_agent.eval.p4_world import confirm_all
from drone_agent.eval.p5_desk_world import P5DeskWorld
from drone_agent.fleet.api import dispatch

ROOT = Path(__file__).resolve().parents[2]
SOAK = runpy.run_path(str(ROOT / "scripts/desk_soak.py"))
SUP = runpy.run_path(str(ROOT / "scripts/desk_supervisor.py"))


async def test_the_scheduler_flies_each_fleet_asset_with_its_site_robot_and_the_vendor_loop_closes(tmp_path):
    world = P5DeskWorld(tmp_path / "desk", ROOT, seed=7)
    try:
        await world.settle()
        fleet = await world.start("fleet_watch", {"asset": "asset_red_b"}, project="fleet_s0")
        await world.run_business([fleet], until=lambda: world.final(fleet), what="fleet watch")
        missions = world.missions_of(fleet)
        assert world.run_state(fleet) == "completed" and len(missions) == 1
        assert world.service.ops.store.binding(missions[0]["mission_id"])["robot_id"] == "uav_fb"
        world.vendor_damage("asset_blue", True)
        vendor = await world.start("vendor_watch", {"asset": "asset_blue"}, project="vendor_s3")
        await world.run_business([vendor], until=lambda: world.final(vendor), what="vendor watch",
                                 decisions=confirm_all)
        order = world.order_of(world.finding_of(vendor))
        assert order is not None
        world.vendor_damage("asset_blue", False)
        await world.feedback(order["order_id"], request_id="vendor-repair-1", project="vendor_s3")
        await world.run_business([vendor], until=lambda: world.order_state(order["order_id"]) == "closed",
                                 what="vendor order closed", decisions=confirm_all)
    finally:
        world.service.business.jobs.stop()
        world.ledger.close()


class BridgeApi:
    """The harness's API calls, run on the world's event loop from the harness thread. / 在世界事件循环上执行编排的调用。"""

    def __init__(self, world: P5DeskWorld, loop):
        self.world, self.loop = world, loop

    def call(self, actor: str, method: str, trust: str = "first_party", timeout: float = 130.0, **params) -> dict:
        started = time.monotonic()
        future = asyncio.run_coroutine_threadsafe(dispatch(self.world.service, {
            "method": method, "actor": actor, "trust": trust, "params": params}), self.loop)
        response = future.result(timeout=60)
        response["latency_s"] = round(time.monotonic() - started, 4)
        return response


class NoContainers:
    """No docker here: every container operation is a recorded no-op. / 这里没有 docker：每个容器操作都是记录下的空操作。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            return {} if name in ("inspect", "stats") else True
        return call


def compressed_plan() -> dict:
    return {"format": "drone.soak-plan/v1", "plan_id": "test", "duration_h": 0.045, "seed": 7,
            "identities": {"operator": "harness:soak-operator", "approver": "harness:soak-approver",
                           "reviewer": "harness:soak-reviewer", "viewer": "harness:soak-viewer",
                           "other": "harness:soak-other", "outsider": "harness:soak-outsider",
                           "roads": "event:road-reports"},
            "schedules": [],
            "workload": {
                "s0_damage": {"layer": "S0", "every_h": 1, "offset_min": 0.05, "project_id": "fleet_s0",
                              "workflow_id": "fleet_watch", "assets": ["asset_blue"], "state": "damaged"},
                "s3_damage": {"layer": "S3", "every_h": 1, "offset_min": 0.1, "project_id": "vendor_s3",
                              "workflow_id": "vendor_watch", "asset": "asset_blue", "state": "damaged"},
                "burst": {"layer": "S0", "every_h": 1, "offset_min": 0.15,
                          "runs": [["fleet_s0", "fleet_watch", "asset_red"], ["vendor_s3", "vendor_watch", "road_north"]]},
                "authz": {"every_min": 60, "offset_min": 0.02}},
            "cancel": {"layer": "S0", "permille": 0, "stages": ["before_approval"]},
            "faults": {}, "sampling": {"every_s": 30},
            "criteria": {"min_duration_h": 72, "service_ready_s": 120, "dock_session_s": 60, "backlog_drain_h": 6,
                         "s1_flights_completed": 12, "s3_tasks": 100, "s2_probes": 10, "cycle_timeout_h": 3}}


async def test_the_soak_harness_drives_the_platform_and_the_judge_reads_its_records(tmp_path, monkeypatch):
    root = tmp_path / "root"
    desk = SUP["Desk"](root)
    for folder in desk.folders():
        folder.mkdir(parents=True, exist_ok=True)
    plan = compressed_plan()
    t0 = datetime.now(timezone.utc)
    base = desk.base / "soak" / "soak-test-00000001"
    base.mkdir(parents=True)
    (base / "manifest.json").write_text(json.dumps({
        "soak_id": "soak-test-00000001", "t0": t0.isoformat(), "end": (t0 + timedelta(hours=plan["duration_h"]))
        .isoformat(), "source_sha": "test", "deployment_id": "test", "plan_sha256": "0" * 64, "plan": plan,
        "full_length": False, "images": {}}))
    (base / "occurrences.json").write_text(json.dumps(SOAK["expand"](plan, t0)))
    monkeypatch.setitem(SOAK["HELPERS"], "capacity", lambda: {"load": [0, 0, 0], "memory": {}})
    world = P5DeskWorld(desk.base, ROOT, seed=7)
    try:
        await world.settle()
        soak = SOAK["Soak"](desk, base, api=BridgeApi(world, asyncio.get_running_loop()), containers=NoContainers())
        finished, failure = threading.Event(), []

        def harness():
            try:
                soak.begin()
                while soak.step():
                    time.sleep(0.3)
            except Exception as error:  # surfaced below / 在下方抛出
                failure.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=harness, daemon=True)
        thread.start()
        deadline = time.monotonic() + 400
        while not finished.is_set() and time.monotonic() < deadline:
            world.sync_world(desk.world)
            await world.step()
        thread.join(timeout=30)
        assert not failure and finished.is_set()
        state = json.loads((base / "state.json").read_text())
        statuses = {k: v["status"] for k, v in state["occurrences"].items()}
        assert statuses["s0_damage-000"] == "closed" and statuses["s3_damage-000"] == "closed", statuses
        assert statuses["burst-000"] == "done" and statuses["authz-000"] == "done"
        assert state["occurrences"]["authz-000"]["escapes"] == 0
        world.service.business.jobs.stop()
        copy = tmp_path / "ledger-copy.sqlite3"
        source = sqlite3.connect(str(desk.service / "ledger.sqlite3"))
        target = sqlite3.connect(str(copy))
        source.backup(target)
        target.close()
        source.close()
        world.export_truth(desk.fleet, desk.vendor / "log")
        result = judge(base, copy, world=desk.world_dir, fleet=desk.fleet, vendor=desk.vendor / "log")
        assert not any(result["counts"].values()), result["counts"]
        assert result["criteria"]["occurrences"]["status"] == "passed"
        assert result["criteria"]["safety"]["status"] == "passed", result["criteria"]["safety"]
        assert result["criteria"]["duration"]["status"] == "failed"  # a rehearsal is never the full soak / 演练不是完整长稳
        assert result["criteria"]["layers"]["by_layer"]["S0"]["missions"] >= 3
    finally:
        world.service.business.jobs.stop()
        world.ledger.close()


def test_the_plan_expands_deterministically_and_cancels_about_five_percent():
    plan = SOAK["load_plan"](ROOT)[0]
    t0 = datetime(2026, 10, 3, 0, 7, tzinfo=timezone.utc)
    items = SOAK["expand"](plan, t0)
    assert items == SOAK["expand"](plan, t0)
    kinds = {}
    for item in items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    assert kinds["s1_watch"] == 24 and kinds["authz"] == 144 and kinds["s2_probe"] == 12 and kinds["burst"] == 3
    assert kinds["service_kill"] == 3 and kinds["service_restart"] == 9 and kinds["model_throttle"] == 6
    assert all(t0 <= datetime.fromisoformat(i["at"]) < t0 + timedelta(hours=72) for i in items)
    chosen = [SOAK["cancel_choice"](plan, f"run-{n:05d}") for n in range(4000)]
    share = sum(1 for c in chosen if c) / len(chosen)
    assert 0.035 < share < 0.065 and {c for c in chosen if c} == set(plan["cancel"]["stages"])
    windows = [(datetime.fromisoformat(i["at"]), datetime.fromisoformat(i["at"]) + timedelta(minutes=15))
               for i in items if i["kind"] in ("model_unreachable", "model_throttle")]
    probes = [datetime.fromisoformat(i["at"]) for i in items if i["kind"] == "s2_probe"]
    planning = [datetime.fromisoformat(i["at"]) for i in items if i["kind"] == "planning"]
    assert not any(a <= p <= b for p in probes for a, b in windows)
    assert sum(1 for p in planning if any(a <= p <= b for a, b in windows)) == 12
