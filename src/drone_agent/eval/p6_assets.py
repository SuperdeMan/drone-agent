"""S0 world for the asset archive (WP-P6-04, D087): the P6 world, scenarios E1–E6.

The world is the P6 S0 world (formal mission service with the P6 campus catalogs, logical aircraft running the real
onboard stack, the business loop and its quality-v2 target region) with the asset archive opened the way the service
process opens it. People act only through the API; the scenarios read the archive through `assets.list`,
`assets.timeline` and `assets.compare` and record every answer. The world itself only damages and repairs markers,
drifts an aircraft's capture position, restarts the service on a site map whose `asset_red` is larger (E4), rewrites
one analysis job's model judgement in the ledger (E5), and copies the business ledger to backfill it (E6). The
independent judge (eval/judge_assets.py) checks the answers against that truth and the ledger; the scenario code
decides nothing.

资产档案的 S0 世界（WP-P6-04，D087）：P6 世界，场景 E1–E6。

世界即 P6 S0 世界（带 P6 园区目录的正式任务服务、运行真实机载栈的逻辑飞行器、业务闭环及其 quality-v2 目标区域），并像服务
进程那样打开资产档案。人只经 API 行动；场景经 `assets.list`、`assets.timeline` 与 `assets.compare` 读取档案并记录每个回答。
世界本身只做这些事：损坏与修复标记、使飞行器拍摄位置偏差、用 `asset_red` 更大的站点地图重启服务（E4）、在账本中改写一个
分析作业的模型判断（E5），以及复制业务账本做回填（E6）。独立裁判（eval/judge_assets.py）对照这些真值与账本核对回答；场景
代码不做任何判定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import yaml

from drone_agent.eval.p4_world import ATTEMPTS, OPERATOR, VIEWER, always, confirm_all
from drone_agent.eval.p6_world import P6World

SUITE = "configs/scenarios/p6_assets_suite.yaml"
ASSET = "campus_ops/site_b/asset_red"
E4_MAP = "configs/scenarios/p6_site_b_e4_v1.yaml"
# Fixed migration instant of the E6 copies: the backfill must be a function of the ledger and this instant alone.
# E6 副本固定的迁移时刻：回填只能依赖账本与这个时刻。
E6_INSTANT = "2026-10-13T00:00:00+00:00"


class AssetWorld(P6World):
    """One archive case: the P6 world whose answers are recorded. / 一个档案用例：记录回答的 P6 世界。"""

    def __init__(self, case: Path, repo: Path, *, seed: int):
        self.answers: list[dict] = []
        super().__init__(case, repo, seed=seed)

    async def ask(self, method: str, *, label: str, actor: str = OPERATOR, **params) -> dict:
        response = await self.bz(method, actor=actor, project_id="campus_ops", **params)
        self.answers.append({"label": label, "method": method, "actor": actor, "params": params,
                             "response": response})
        return response

    def capture(self, run_id: str, node_id: str = "await_inspection") -> str:
        """The evidence id of a run's inspection, from its node output. / 运行巡检节点输出中的证据 id。"""
        return self.node(run_id, node_id)["result"]["evidence_id"]

    async def inspect(self, request_id: str, *, decisions=None) -> str:
        """One recapture_watch run on asset_red that ends without a recapture; returns its capture.

        在 asset_red 上运行一次不发生补拍而结束的 recapture_watch；返回其采集。
        """
        run = await self.start("recapture_watch", {"asset": "asset_red"}, request_id=request_id)
        await self.run_business([run], until=lambda: self.final(run), what=f"{request_id} run end", decisions=decisions)
        return self.capture(run)

    async def inspect_declined(self, request_id: str) -> str:
        """A run whose first capture is refused and whose recapture the operator declines; returns that capture.

        首次采集被拒、操作者拒绝其补拍的运行；返回那次采集。
        """
        run = await self.start("recapture_watch", {"asset": "asset_red"}, request_id=request_id)
        mission = await self.until_recapture_waits(run)
        self.inject("recapture_declined", mission_id=mission)
        await self.cancel(mission, actor=OPERATOR)
        await self.run_business([run], until=lambda: self.final(run), what=f"{request_id} run end", approve=False)
        return self.capture(run)

    def switch_map(self) -> None:
        """Restart the service with site_b on the E4 map (only asset_red's size differs). / 用 E4 地图重启服务。"""
        data = yaml.safe_load(self.catalog_path.read_text(encoding="utf-8"))
        data["sites"]["site_b"]["scene"] = E4_MAP
        path = self.case / "input" / "catalog-e4.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.catalog_path = path
        self.restart_service()
        self.inject("map_changed", site_id="site_b", scene=E4_MAP)

    def rewrite_judgement(self, job_id: str) -> None:
        """Rewrite one job's model judgement in the ledger, as a corrupted or re-run analyzer might.

        在账本中改写一个作业的模型判断，如同损坏或重跑的分析器所为。
        """
        with self.ledger.transaction() as db:
            db.execute("UPDATE bz_jobs SET verdict='normal', result=json_set(result, '$.verdict', 'normal', "
                       "'$.score', 0.01, '$.defect_type', NULL, '$.family', NULL) WHERE job_id=?", (job_id,))
        self.inject("judgement_rewritten", job_id=job_id, verdict="normal", score=0.01)

    def backfill_copies(self) -> dict:
        """Copy the ledger, strip the archive from the copies as a pre-D087 ledger, and migrate two of them at the same
        instant and one of them twice; the files stay for the judge.

        复制账本，从副本中去掉档案（相当于 D087 之前的账本），在同一时刻迁移其中两份、其中一份迁移两次；文件留给裁判。
        """
        from datetime import datetime

        from drone_agent.fleet.asset_store import ArchiveContext, migrate
        from drone_agent.fleet.ledger import BusinessLedger

        folder = self.case / "e6"
        folder.mkdir(exist_ok=True)
        live = folder / "live.sqlite3"
        with self.ledger._lock:
            target = sqlite3.connect(str(live))
            try:
                self.ledger._db.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
        for name in ("stripped", "a", "b"):
            shutil.copyfile(live, folder / f"{name}.sqlite3")
            with sqlite3.connect(str(folder / f"{name}.sqlite3")) as db:
                for table in ("ar_revisions", "ar_observations", "ar_events"):
                    db.execute(f"DROP TABLE {table}")
                db.execute("DROP INDEX ar_jobs_evidence")
                db.execute("DELETE FROM meta WHERE key LIKE 'asset_%'")
        ops = self.service.ops
        context = ArchiveContext(ops.catalog, None, ops.registries)
        reports = {}
        for name, runs in (("a", 2), ("b", 1)):
            ledger = BusinessLedger(folder / f"{name}.sqlite3")
            try:
                reports[name] = [migrate(ledger, context, backups=folder / "backups",
                                         clock=lambda: datetime.fromisoformat(E6_INSTANT))["status"]
                                 for _ in range(runs)]
            finally:
                ledger.close()
        shutil.rmtree(folder / "backups", ignore_errors=True)
        self.inject("backfill_copies", instant=E6_INSTANT, migrations=reports)
        return reports

    def export(self, scenario: dict) -> None:
        super().export(scenario)
        (self.case / "service-export/assets.json").write_text(
            json.dumps({"answers": self.answers, "asset": ASSET, "e4_map": E4_MAP}, indent=2, ensure_ascii=False,
                       default=str), encoding="utf-8")


# ── scenarios E1–E6 / 场景 ──


async def e1_repair(w: AssetWorld) -> None:
    w.damage("uav_b")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-damaged")
    await w.run_business([run], until=lambda: w.final(run), what="inspection run end", decisions=confirm_all)
    order = w.order_of(w.finding_of(run))
    if order is None:
        raise RuntimeError("no order was opened")
    w.repair_world("uav_b")
    repair = {"action": "replaced", "part": "marker cover", "performed_at": (w.clock() - timedelta(minutes=5)).isoformat()}
    w.inject("repair_record", order_id=order["order_id"], repair=repair)
    await w.bz("orders.repair", project_id="campus_ops", order_id=order["order_id"], request_id="repair-1",
               note="replaced the cover", repair=repair)
    await w.run_business([run], until=lambda: w.order_state(order["order_id"]) == "closed" and all(
        w.final(r) for r in [run, *w.round_runs(order["order_id"])]), what="order closed", decisions=confirm_all)
    await w.ask("assets.list", label="list")
    await w.ask("assets.compare", label="compare", asset=ASSET, evidence=None, against=None)
    await w.ask("assets.timeline", label="timeline", asset=ASSET, before=None, limit=200)
    # A viewer reads but may not record maintenance. / viewer 可读，但不能登记维护。
    await w.ask("assets.timeline", label="viewer_timeline", actor=VIEWER, asset=ASSET, before=None, limit=5)
    await w.bz("assets.maintenance", actor=VIEWER, probe=True, project_id="campus_ops", asset=ASSET,
               request_id="viewer-maintenance", action="cleaned", part="", performed_at=w.clock().isoformat(),
               note="viewer")


async def e2_angle(w: AssetWorld) -> None:
    nadir = await w.inspect("watch-nadir")
    captures = {"nadir": nadir}
    for label, offset in (("east", (1.2, 0.0)), ("north", (0.0, 1.2))):
        w.drift("uav_b", offset)
        captures[label] = await w.inspect_declined(f"watch-{label}")
    w.drift("uav_b", None)
    w.inject("captures", **captures)
    await w.ask("assets.compare", label="north_vs_east", asset=ASSET, evidence=captures["north"],
                against=captures["east"])
    await w.ask("assets.compare", label="east_vs_nadir", asset=ASSET, evidence=captures["east"],
                against=captures["nadir"])


async def e3_new_anomaly(w: AssetWorld) -> None:
    normal = await w.inspect("watch-normal")
    w.damage("uav_b")
    damaged = await w.inspect("watch-damaged", decisions=always("dismissed"))
    w.inject("captures", normal=normal, damaged=damaged)
    await w.ask("assets.compare", label="compare", asset=ASSET, evidence=None, against=None)


async def e4_definition(w: AssetWorld) -> None:
    before = await w.inspect("watch-before")
    w.switch_map()
    after = await w.inspect("watch-after")
    w.inject("captures", before=before, after=after)
    await w.ask("assets.compare", label="compare", asset=ASSET, evidence=after, against=before)
    await w.ask("assets.list", label="list")


async def e5_isolation(w: AssetWorld) -> None:
    normal = await w.inspect("watch-normal")
    w.damage("uav_b")
    damaged = await w.inspect("watch-damaged", decisions=always("dismissed"))
    w.inject("captures", normal=normal, damaged=damaged)
    common = {"asset": ASSET, "evidence": damaged, "against": normal}
    await w.ask("assets.compare", label="compare_before", **common)
    await w.ask("assets.timeline", label="timeline_before", asset=ASSET, before=None, limit=200)
    job = next(j for j in w.business.store.jobs(asset=ASSET) if j["evidence_id"] == damaged)
    w.rewrite_judgement(job["job_id"])
    await w.ask("assets.compare", label="compare_after", **common)
    await w.ask("assets.timeline", label="timeline_after", asset=ASSET, before=None, limit=200)


async def e6_backfill(w: AssetWorld) -> None:
    from drone_agent.eval.p6_world import reference_for

    await reference_for(w, "uav_c")
    w.damage("uav_b")
    run = await w.start("recapture_watch", {"asset": "asset_red"}, request_id="watch-damaged")
    await w.run_business([run], until=lambda: w.final(run), what="inspection run end", decisions=confirm_all)
    order = w.order_of(w.finding_of(run))
    if order is None:
        raise RuntimeError("no order was opened")
    w.repair_world("uav_b")
    await w.bz("orders.repair", project_id="campus_ops", order_id=order["order_id"], request_id="repair-1",
               note="repaired", repair={"action": "repaired", "part": "", "performed_at": w.clock().isoformat()})
    await w.run_business([run], until=lambda: w.order_state(order["order_id"]) == "closed" and all(
        w.final(r) for r in [run, *w.round_runs(order["order_id"])]), what="order closed", decisions=confirm_all)
    await w.bz("assets.maintenance", project_id="campus_ops", asset=ASSET, request_id="clean-1", action="cleaned",
               part="lens of the marker light", performed_at=w.clock().isoformat(), note="routine")
    await w.hold(0.5)
    w.backfill_copies()


SCENARIOS: dict[str, Callable] = {
    "p6_e1_repair": e1_repair, "p6_e2_angle": e2_angle, "p6_e3_new_anomaly": e3_new_anomaly,
    "p6_e4_definition": e4_definition, "p6_e5_isolation": e5_isolation, "p6_e6_backfill": e6_backfill,
}


def suite(repo: Path) -> dict:
    return yaml.safe_load((repo / SUITE).read_text(encoding="utf-8"))


async def run_case(case: Path, repo: Path, scenario: dict, seed: int, sha: str) -> dict:
    """Run one archive case and judge it. / 运行一个档案用例并裁判。"""
    from drone_agent.eval.judge_assets import judge_case

    if case.exists():
        shutil.rmtree(case)
    world = AssetWorld(case, repo, seed=seed)
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
    result = {**{k: online.get(k) for k in ("scenario", "seed", "classification", "problems", "counts",
                                             "false_success_reports", "expected", "archive")},
              "passed": bool(online.get("passed")), "harness_error": error}
    (case / "judge").mkdir(exist_ok=True)
    (case / "judge/result.json").write_text(json.dumps(online, indent=2, default=str), encoding="utf-8")
    return result


async def run_suite(output: Path, repo: Path, *, sha: str, only: list[str] | None = None,
                    seeds: list[int] | None = None) -> dict:
    from drone_agent.eval.judge_assets import COUNTS

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
