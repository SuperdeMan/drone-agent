"""S0 world for P6 (D078): the P4 world with target-region quality, recapture templates and a control template.

The world is the P4 S0 world (formal mission service, logical docks and aircraft running the real onboard uplink,
guardian and executive, the business loop and the labelled scripted vision double) loaded with the P6 workflow and
business catalogs. The appearance world additionally puts glare or a shadow over a marker, blurs whole captures, and
drifts an aircraft's capture position (the logical aircraft reports the drifted pose; its flight path is unchanged).
People act only through the API: operators approve each mission, the recapture included, or decline it by cancelling
it; reviewers decide findings. Each scenario of the P6 matrix (P6-F01–F10) records what the independent judge
(eval/judge_p6.py) needs; the scenario code decides nothing.

P6 的 S0 世界（D078）：P4 世界加上目标区域质量、补拍模板与对照模板。

世界即 P4 S0 世界（正式任务服务、运行真实机载 uplink、guardian 与 executive 的逻辑机场与飞行器、业务闭环与带标注的脚本
视觉替身），加载 P6 工作流与业务目录。外观世界另能在标记上加反光或阴影、模糊整幅采集，并使飞行器的拍摄位置偏差（逻辑
飞行器上报偏差后的位姿，飞行路径不变）。人只经 API 行动：操作者逐个审批任务（包括补拍），或以取消任务的方式拒绝；
复核人决定发现。P6 矩阵（P6-F01–F10）的每个场景记录独立裁判（eval/judge_p6.py）需要的内容；场景代码不做任何判定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from drone_agent.eval.p1_world import ScenarioTimeout
from drone_agent.eval.p2_world import arm_crash
from drone_agent.eval.p4_world import (
    ADMIN,
    ATTEMPTS,
    HARBOR,
    OPERATOR,
    VIEWER,
    P4World,
    always,
    confirm_all,
)
from drone_agent.eval.p6_vision import P6Appearance

SUITE = "configs/scenarios/p6_suite.yaml"
WORKFLOWS = "configs/workflows/p6_campus_v1.yaml"
BUSINESS = "configs/analysis/p6_campus_v1.yaml"
REPORTS = "event:p6-reports"
REFERENCE_WORKFLOW = {"uav_c": "recapture_vlm_watch", "uav_a": "plain_vlm_watch"}


class P6World(P4World):
    """One P6 S0 case: the P4 world with capture effects and the P6 catalogs. / 一个 P6 S0 用例。"""

    def __init__(self, case: Path, repo: Path, *, seed: int):
        super().__init__(case, repo, seed=seed, business=repo / BUSINESS, workflows=repo / WORKFLOWS)
        self.appearance = P6Appearance({"asset_red": "red", "asset_blue": "blue"})
        for robot_id, uav in self.uavs.items():
            uav.camera = self.appearance.camera(robot_id)

    def effect(self, robot_id: str, asset_id: str = "asset_red", *, overlay: str | None = None,
               blur: bool = False) -> None:
        """What the next captures of the asset show; no arguments clears it. / 资产后续采集所呈现的效果；无参数即清除。"""
        self.appearance.effect(robot_id, asset_id, overlay=overlay, blur=blur)
        self.inject("capture_effect", robot_id=robot_id, asset_id=asset_id, overlay=overlay, blur=blur)

    def drift(self, robot_id: str, offset_m: tuple[float, float] | None) -> None:
        """Drift the aircraft's capture position (both the pose it reports and what its camera sees).

        使飞行器的拍摄位置偏差（同时作用于其上报的位姿与相机所见）。
        """
        if offset_m is None:
            self.appearance.drift.pop(robot_id, None)
        else:
            self.appearance.drift[robot_id] = offset_m
        self.uavs[robot_id].capture_offset = offset_m
        self.inject("capture_drift", robot_id=robot_id, offset_m=list(offset_m) if offset_m else None)

    def mission_of_node(self, run_id: str, node_id: str) -> str | None:
        key = f"wf:{run_id}:{node_id}:1"
        return next((m["mission_id"] for m in self.missions_of(run_id) if m["idempotency_key"] == key), None)

    async def approve_node(self, run_id: str, node_id: str, *, actor: str = OPERATOR) -> bool:
        mission = self.mission_of_node(run_id, node_id)
        if mission is None or self.status(mission) != "awaiting_approval":
            return False
        return bool((await self.approve(mission, actor=actor, expect=False)).get("ok"))

    async def until_recapture_waits(self, run_id: str, *, timeout_s: float = 150.0) -> str:
        """Approve only the first inspection, until the run's recapture mission awaits its own approval.

        只审批首次巡检，直到运行的补拍任务等待其自己的审批。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await self.step()
            await self.approve_node(run_id, "inspect")
            mission = self.mission_of_node(run_id, "recapture")
            if mission is not None and self.status(mission) == "awaiting_approval":
                return mission
            if self.final(run_id):
                raise ScenarioTimeout(f"{run_id} ended without a recapture")
        raise ScenarioTimeout("timed out waiting for the recapture approval")


async def reference_for(w: P6World, robot: str, asset: str = "asset_red") -> None:
    """Register a clean capture of the asset as its reference appearance (admin). / 登记资产的干净采集为参考外观。"""
    baseline = await w.start(REFERENCE_WORKFLOW[robot], {"asset": asset}, request_id=f"baseline-{robot}")
    await w.run_business([baseline], until=lambda: w.final(baseline), what="baseline run")
    response = await w.register_reference(baseline)
    if not response.get("ok"):
        raise RuntimeError(f"reference refused: {response.get('issue')}")


async def recapture_after(w: P6World, run: str, clear: Callable[[], None], *, decisions=None) -> None:
    """The first capture is refused; the world changes before the recapture is approved, then the run ends.

    首次采集被拒；补拍获批之前世界发生变化，随后运行结束。
    """
    await w.until_recapture_waits(run)
    clear()
    await w.run_business([run], until=lambda: w.final(run), what="run end after the recapture", decisions=decisions)


# ── scenarios P6-F01–F10 / 场景 ──


async def f01_glare_finding(w: P6World) -> None:
    w.damage("uav_b")
    w.effect("uav_b", overlay="glare")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-glare")
    await recapture_after(w, run, lambda: w.effect("uav_b"), decisions=confirm_all)


async def f02_blur_event(w: P6World) -> None:
    w.effect("uav_b", blur=True)
    first = await w.raise_event("report-1", "asset_red", principal=REPORTS, workflow_id="recapture_watch",
                                trigger_id="report", event_type="marker.report")
    again = await w.raise_event("report-1", "asset_red", principal=REPORTS, workflow_id="recapture_watch",
                                trigger_id="report", event_type="marker.report")
    w.inject("duplicate_event", first=first.get("ok"), again=again.get("ok"),
             same_run=(first.get("result") or {}).get("run_id") == (again.get("result") or {}).get("run_id"))
    run = first["result"]["run_id"]
    await recapture_after(w, run, lambda: w.effect("uav_b"))


async def f03_drift(w: P6World) -> None:
    w.drift("uav_b", (1.2, 0.0))
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-drift")
    await recapture_after(w, run, lambda: w.drift("uav_b", None))


async def f04_both_refused(w: P6World) -> None:
    w.effect("uav_b", overlay="shadow")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-shadow")
    await w.run_business([run], until=lambda: w.final(run), what="run end with two refusals")


async def f05_not_recapturable(w: P6World) -> None:
    await reference_for(w, "uav_c")
    for mode in ("timeout", "error", "mismatch"):
        w.vision_provider.mode = mode
        w.inject("vision_mode", mode=mode)
        run = await w.start("recapture_vlm_watch", {"asset": "asset_red"}, request_id=f"watch-{mode}")
        await w.run_business([run], until=lambda r=run: w.final(r), what=f"{mode} run end", timeout_s=200)
    w.vision_provider.mode = "occluded"
    w.inject("vision_mode", mode="occluded")
    run = await w.start("recapture_vlm_watch", {"asset": "asset_red"}, request_id="watch-occluded")

    def clear() -> None:
        w.vision_provider.mode = None
        w.inject("vision_mode", mode=None)

    await recapture_after(w, run, clear)


async def f06_declined(w: P6World) -> None:
    w.effect("uav_b", overlay="glare")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-declined")
    mission = await w.until_recapture_waits(run)
    w.inject("recapture_declined", mission_id=mission)
    await w.cancel(mission, actor=OPERATOR)
    await w.run_business([run], until=lambda: w.final(run), what="run end after the declined recapture",
                         approve=False)


async def f07_cancel_run(w: P6World) -> None:
    w.effect("uav_b", overlay="glare")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-cancel")
    await w.until_recapture_waits(run)
    await w.cancel_run(run)
    await w.run_business([run], until=lambda: w.final(run), what="cancelled run", approve=False)
    await w.hold(1.0)


async def f08_crash(w: P6World) -> None:
    w.effect("uav_b", overlay="glare")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-crash")
    deadline = time.monotonic() + 150
    while (w.job_of(run) or {}).get("state") != "refused":
        if time.monotonic() > deadline:
            raise ScenarioTimeout("the first analysis was not refused")
        await w.step()
        await w.approve_node(run, "inspect")
    # The mission service accepts the recapture but its response is lost; the process dies before the node moves.
    # 任务服务接受了补拍但响应丢失；进程在节点推进之前死亡。
    arm_crash(w, "response_lost")
    await w.hold(0.6)
    w.inject("crash", window="recapture_response_lost", run_id=run, missions=len(w.missions_of(run)))
    w.restart_service()
    await recapture_after(w, run, lambda: w.effect("uav_b"))


async def f09_control(w: P6World) -> None:
    await reference_for(w, "uav_a")
    await reference_for(w, "uav_c")
    for robot in ("uav_a", "uav_c"):
        w.effect(robot, overlay="shadow")
    control = await w.start("plain_vlm_watch", {"asset": "asset_red"}, request_id="control-shadow")
    run = await w.start("recapture_vlm_watch", {"asset": "asset_red"}, request_id="watch-shadow")
    await w.until_recapture_waits(run)
    w.effect("uav_c")
    # The control has no recapture: its false suspicion waits for a person, who dismisses it.
    # 对照没有补拍：其误报等待人处理，由复核人驳回。
    await w.run_business([control, run], until=lambda: w.final(control) and w.final(run), what="both runs end",
                         decisions=always("dismissed"))


async def f10_authority(w: P6World) -> None:
    w.effect("uav_b", overlay="glare")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-authority")
    mission = await w.until_recapture_waits(run)
    view = w.service._view(mission)
    version = view["mission"]["current_version"]
    package = next(v for v in view["versions"] if v["version"] == version)["package_hash"]
    approve = {"mission_id": mission, "version": version, "package_hash": package}
    for actor, trust in ((VIEWER, "first_party"), (HARBOR, "first_party"), (ADMIN, "first_party"),
                         ("a2a:probe-client", "third_party"), ("dock:p1-s0-sim", "backend"), ("", "first_party")):
        await w.api(actor, "approve", trust=trust, probe=True, **approve)
    await w.bz("workflows.start", actor=HARBOR, probe=True, project_id="campus_ops", workflow_id="recapture_watch",
               request_id="harbor-into-campus", inputs={"asset": "asset_red"})
    await w.bz("workflows.start", actor=VIEWER, probe=True, project_id="campus_ops", workflow_id="recapture_watch",
               request_id="viewer-start", inputs={"asset": "asset_red"})
    w.effect("uav_b")
    await w.run_business([run], until=lambda: w.final(run), what="authorised recapture")


SCENARIOS: dict[str, Callable] = {
    "p6_f01_glare_finding": f01_glare_finding, "p6_f02_blur_event": f02_blur_event, "p6_f03_drift": f03_drift,
    "p6_f04_both_refused": f04_both_refused, "p6_f05_not_recapturable": f05_not_recapturable,
    "p6_f06_declined": f06_declined, "p6_f07_cancel_run": f07_cancel_run, "p6_f08_crash": f08_crash,
    "p6_f09_control": f09_control, "p6_f10_authority": f10_authority,
}


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str) -> dict:
    """Run one S0 case and judge it online and from the recordings. / 运行一个 S0 用例并在线与按录制各裁判一次。"""
    from drone_agent.eval.judge_p6 import judge_case

    if case.exists():
        shutil.rmtree(case)
    world = P6World(case, repo, seed=seed)
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
    keys = ("classification", "problems", "false_success_reports", "counts", "metrics")
    agrees = all(online.get(k) == replay.get(k) for k in keys)
    result = {**{k: online.get(k) for k in ("scenario", "seed", "classification", "problems", "counts", "metrics",
                                             "false_success_reports", "expected")},
              "passed": bool(online.get("passed")) and agrees, "replay_agrees": agrees, "harness_error": error}
    (case / "judge").mkdir(exist_ok=True)
    (case / "judge/result.json").write_text(json.dumps(online, indent=2, default=str), encoding="utf-8")
    (case / "judge/replay.json").write_text(json.dumps(replay, indent=2, default=str), encoding="utf-8")
    return result


async def run_suite(output: Path, repo: Path, *, sha: str, only: list[str] | None = None,
                    seeds: list[int] | None = None) -> dict:
    from drone_agent.eval.judge_p6 import COUNTS, METRICS

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
    metrics = {key: sum((r.get("metrics") or {}).get(key, 0) for r in results) for key in METRICS}
    summary = {"schema_version": "0.1.0", "layer": "S0", "source_sha": sha, "suite": definition["format"],
               "cases": len(results), "passed": sum(1 for r in results if r["passed"]),
               "status": "passed" if results and all(r["passed"] for r in results) else "failed",
               "counts": counts, "metrics": metrics, "results": results, "voided_attempts": voided}
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
    print(json.dumps({k: summary[k] for k in ("status", "cases", "passed", "counts", "metrics")}))
    raise SystemExit(0 if summary["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
