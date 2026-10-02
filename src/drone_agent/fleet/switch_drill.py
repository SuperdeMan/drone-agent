"""Catalog switch drill (P5, D069): open a copy of the live ledger with the new catalogs, offline, and read it back.

The resident desk moves from the P4 catalogs to the p5_desk_v1 ones without a schema change. Before anything is
switched, desk activation runs this on a copy of the live ledger with the new image and no network: the four catalogs
load against the copied tables (recording their digests the way the service would), a service with throwaway keys
renders the latest missions, workflow runs and orders and every project's audit page through the API as the desk's
read-only supervisor identity, and only the catalog tables may gain rows. Only counts, digests and the names of
failures are reported; the copy is deleted by the caller.

目录切换演练（P5，D069）：离线地用新目录打开当前账本的副本并读回。

常驻任务台从 P4 目录切换到 p5_desk_v1 目录，不改变 schema。在切换任何组件之前，任务台激活用新镜像、无网络地对当前账本的
副本运行本演练：四个目录在复制的表上加载（像服务那样记录其摘要），一个使用一次性密钥的服务以任务台只读监管者身份经 API 渲染
最近的任务、工作流运行与工单以及每个项目的审计页，且只有目录表可以增加行。只报告计数、摘要与失败名称；副本由调用方删除。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

CATALOG_TABLES = ("op_catalogs", "wf_catalogs", "sc_catalogs", "bz_catalogs")
READER = "harness:desk-supervisor"


def table_counts(path: Path) -> dict[str, int]:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}
    finally:
        db.close()


async def drill(root: Path, ledger_path: Path, *, scene: Path, catalog: Path, members: Path, workflows: Path,
                scheduling: Path, business: Path, backends: tuple[str, ...], sample: int = 20) -> dict:
    from drone_agent.fleet.api import dispatch
    from drone_agent.fleet.business import BusinessEngine, build_business
    from drone_agent.fleet.dispatch import build_operations
    from drone_agent.fleet.ledger import BusinessLedger
    from drone_agent.fleet.provenance import source_context
    from drone_agent.fleet.scheduler import Scheduler, build_scheduling
    from drone_agent.fleet.service import MissionService
    from drone_agent.fleet.transport import FleetHub
    from drone_agent.fleet.workflow import WorkflowEngine, build_workflows
    from drone_agent.mission.registry import Registry
    from drone_agent.planner.replan import ApprovalPolicy
    from drone_agent.runtime.signing import SigningKey

    before = table_counts(ledger_path)
    failures: list[str] = []
    seen = {"missions": 0, "runs": 0, "orders": 0, "audit": {}}
    ledger = BusinessLedger(ledger_path)
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        try:
            ops = build_operations(root, ledger, catalog, members, backups=scratch / "backups")
            flows = build_workflows(root, ledger, workflows, ops, backups=scratch / "backups")
            plan = build_scheduling(ledger, scheduling, ops, backups=scratch / "backups")
            trade = build_business(root, ledger, business, ops, flows, backups=scratch / "backups")
            registry = Registry(root, scene=scene)
            service = MissionService(root=root, scene=scene, ledger=ledger, hub=FleetHub(ledger, scratch / "media"),
                                     signing_key=SigningKey.generate(),
                                     approval_policy=ApprovalPolicy.from_yaml(root / "configs/approval_policy.yaml"),
                                     provenance_context=source_context(root, scene, registry.sha256,
                                                                       backend=backends[0]),
                                     operations=ops, backends=backends)
            service.workflows = WorkflowEngine(service, flows, root=root)
            service.scheduler = Scheduler(service, plan)
            service.business = BusinessEngine(service, trade, root=root, vision=None, recordings=scratch / "rec")

            async def read(method: str, **params) -> dict | None:
                response = await dispatch(service, {"method": method, "actor": READER, "trust": "first_party",
                                                    "params": params})
                if not response.get("ok"):
                    failures.append(f"{method}:{(response.get('issue') or {}).get('code')}")
                    return None
                return response["result"]

            for mission in ledger.missions(sample):
                if await read("view", mission_id=mission["mission_id"]) is not None:
                    seen["missions"] += 1
            for project in sorted(ops.catalog.projects):
                runs = (await read("workflows.list", project_id=project) or {}).get("runs", [])
                for run in runs[:sample]:
                    if await read("workflows.get", project_id=project, run_id=run["run_id"]) is not None:
                        seen["runs"] += 1
                for order in (await read("orders.list", project_id=project) or [])[:sample]:
                    if await read("orders.get", project_id=project, order_id=order["order_id"]) is not None:
                        seen["orders"] += 1
                page = await read("audit.list", project_id=project, before=None, limit=50)
                seen["audit"][project] = len((page or {}).get("entries", []))
            service.business.jobs.stop()
            identities = {"operations": [ops.catalog.catalog_id, ops.catalog.sha256],
                          "workflows": [flows.catalog.catalog_id, flows.catalog.sha256],
                          "scheduling": [plan.catalog.catalog_id, plan.catalog.sha256],
                          "business": [trade.catalog.catalog_id, trade.catalog.sha256],
                          "migrations": {"operations": ops.migration.get("status"),
                                         "workflows": (flows.migration or {}).get("status"),
                                         "scheduling": (plan.migration or {}).get("status"),
                                         "business": (trade.migration or {}).get("status")}}
        except Exception as error:  # the drill reports, never raises / 演练只报告，从不抛出
            failures.append(f"{type(error).__name__}: {error}"[:300])
            identities = {}
        finally:
            ledger.close()
    after = table_counts(ledger_path)
    grew = sorted(name for name in after if after[name] != before.get(name) and name not in CATALOG_TABLES)
    if grew:
        failures.append("non-catalog tables changed: " + ", ".join(grew))
    return {"status": "passed" if not failures and identities else "failed", "catalogs": identities,
            "rows_before": sum(before.values()), "rows_after": sum(after.values()), "read": seen,
            "failures": failures[:20]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--ledger", type=Path, required=True, help="a copy of the live ledger; it is modified")
    parser.add_argument("--scene", type=Path, default=Path("/workspace/configs/scenarios/m2_campus_v2.yaml"))
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--members", type=Path, required=True)
    parser.add_argument("--workflows", type=Path, required=True)
    parser.add_argument("--scheduling", type=Path, required=True)
    parser.add_argument("--business", type=Path, required=True)
    parser.add_argument("--execution-backend", default="px4_sitl,logical_sim,vendor_protocol_sim")
    args = parser.parse_args()
    result = asyncio.run(drill(args.root, args.ledger, scene=args.scene, catalog=args.catalog, members=args.members,
                               workflows=args.workflows, scheduling=args.scheduling, business=args.business,
                               backends=tuple(args.execution_backend.split(","))))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
