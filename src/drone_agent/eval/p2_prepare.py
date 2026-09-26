"""Materialize one P2 S1 case and export the ledger's tables for its judge (WP-P2-08).

Without `--output` it prints the S1 part of the suite. A case names the workflow the harness starts, its inputs, the
reviewer's decisions and the fault the runner applies (a cancel or a service restart while airborne); the runner
reads nothing else. The scripted planner file only exists because the mission service's scripted mode needs one:
workflow missions are planned deterministically from the template. `--export` copies the workflow, operations and
mission tables of a stopped service's ledger into JSON, read-only, so the judge never opens a live database or reuses
service code.

生成一个 P2 S1 用例，并为其裁判导出账本表（WP-P2-08）。不带 `--output` 时打印场景集的 S1 部分。用例给出编排启动的
工作流、输入、复核人的决定，以及执行器施加的故障（空中取消或空中重启服务）；执行器不读取其他内容。脚本规划文件只因
任务服务的脚本模式需要而存在：工作流任务由模板确定性规划。`--export` 以只读方式把已停止服务账本中的工作流、运营与任务
表导出为 JSON，使裁判从不打开运行中的数据库，也不复用服务代码。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from drone_agent.eval.p1_prepare import materialize as p1_materialize

SUITE = "configs/scenarios/p2_suite.yaml"
TABLES = ("wf_catalogs", "wf_schedules", "wf_triggers", "wf_runs", "wf_nodes", "wf_outbox", "wf_analyses",
          "wf_reviews", "wf_work_orders", "requests", "missions", "op_bindings", "op_reservations", "op_holds",
          "op_claims", "op_decisions", "op_cancellations", "op_dock_actions", "op_dock_locks", "op_dock_sessions",
          "op_events", "deliveries", "verifications", "evidence", "meta")
# What the runner does per case; the suite holds descriptions and expectations. / 执行器按用例的动作；场景集存描述与期望。
CASES = {
    "p2_s1_chain": {"workflow_id": "asset_check", "inputs": {"asset": "asset_red"}, "reviews": {"review": "confirmed"},
                    "repair": True, "fault": None},
    "p2_s1_cancel_in_flight": {"workflow_id": "campus_round", "inputs": {}, "reviews": {}, "repair": False,
                               "fault": "cancel_airborne"},
    "p2_s1_restart": {"workflow_id": "asset_check", "inputs": {"asset": "asset_blue"}, "reviews": {},
                      "repair": False, "fault": "restart_airborne"},
}


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "p2.suite/v1":
        raise ValueError("not a p2.suite/v1 file")
    return suite


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    scenario = next(case for case in load_suite(root)["s1"] if case["id"] == scenario_id)
    if seed not in scenario["seeds"]:
        raise ValueError("seed outside the case's seeds")
    output.mkdir(parents=True, exist_ok=True)
    # The P1 fixture writer gives the service its labelled scripted planner file. / 由 P1 夹具写出带标注的脚本规划文件。
    p1_materialize(root, output, "p1_s1_nominal", 7, sha)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "layer": "S1", "project_id": "campus_s1",
                "robot_id": "uav_01", **CASES[scenario_id], "expected": scenario["expected"],
                "fault_id": scenario["fault"], "description": scenario["description"]}
    (output / "scenario.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def export_tables(ledger: Path, output: Path) -> dict:
    """Read-only JSON copy of the tables of a stopped service. / 已停止服务各表的只读 JSON 副本。"""
    db = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {name: [dict(row) for row in db.execute(f"SELECT * FROM {name}")] for name in TABLES}
    finally:
        db.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(tables, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return {name: len(rows) for name, rows in tables.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenario")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--sha", default="uncommitted")
    parser.add_argument("--export", type=Path, help="stopped service ledger to export")
    args = parser.parse_args()
    if args.export:
        print(json.dumps(export_tables(args.export, args.output)))
    elif args.output:
        print(json.dumps(materialize(args.root, args.output, args.scenario, args.seed, args.sha), ensure_ascii=False))
    else:
        print(json.dumps(load_suite(args.root), ensure_ascii=False))


if __name__ == "__main__":
    main()
