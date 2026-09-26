"""Materialize one P3 S1 case and export the ledger's tables for its judge (WP-P3-05).

Without `--output` it prints the S1 part of the suite. A case names what the harness submits (a workflow or tasks),
when it withholds approvals and the fault the runner applies (a dock put into maintenance after an assignment); the
runner reads nothing else. The scripted planner file only exists because the mission service's scripted mode needs one:
task missions are planned deterministically. `--export` copies the scheduling, workflow, operations and mission tables
of a stopped service's ledger into JSON, read-only, so the judge never opens a live database or reuses service code.

生成一个 P3 S1 用例，并为其裁判导出账本表（WP-P3-05）。不带 `--output` 时打印场景集的 S1 部分。用例给出编排提交什么
（工作流或任务单）、何时暂缓审批，以及执行器施加的故障（分配后让机场进入维护）；执行器不读取其他内容。脚本规划文件只因
任务服务的脚本模式需要而存在：任务单的任务是确定性规划的。`--export` 以只读方式把已停止服务账本中的调度、工作流、运营与
任务表导出为 JSON，使裁判从不打开运行中的数据库，也不复用服务代码。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from drone_agent.eval.p1_prepare import materialize as p1_materialize

SUITE = "configs/scenarios/p3_suite.yaml"
TABLES = ("sc_catalogs", "sc_tasks", "sc_assignments", "sc_decisions", "sc_footprints", "wf_runs", "wf_nodes",
          "wf_outbox", "wf_triggers", "requests", "missions", "versions", "op_bindings", "op_reservations", "op_holds",
          "op_claims", "op_decisions", "op_cancellations", "op_dock_actions", "op_dock_locks", "op_dock_sessions",
          "op_events", "deliveries", "verifications", "evidence", "reports", "meta")
# What the runner does per case; the suite holds descriptions and expectations. / 执行器按用例的动作；场景集存描述与期望。
CASES = {
    "p3_s1_parallel": {"workflow_id": "pair_check", "tasks": [], "fault": None, "hold_approval": None},
    "p3_s1_contention": {"workflow_id": None, "fault": None, "hold_approval": None,
                         "tasks": [{"asset": "asset_mid", "key": "mid-1", "after": None},
                                   {"asset": "asset_mid", "key": "mid-2", "after": "mid-1:assigned"}]},
    "p3_s1_relay": {"workflow_id": None, "fault": "maintenance_after_assignment", "hold_approval": "uav_01",
                    "tasks": [{"asset": "asset_mid", "key": "mid", "after": None}]},
}


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "p3.suite/v1":
        raise ValueError("not a p3.suite/v1 file")
    return suite


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    scenario = next(case for case in load_suite(root)["s1"] if case["id"] == scenario_id)
    if seed not in scenario["seeds"]:
        raise ValueError("seed outside the case's seeds")
    output.mkdir(parents=True, exist_ok=True)
    # The P1 fixture writer gives the service its labelled scripted planner file. / 由 P1 夹具写出带标注的脚本规划文件。
    p1_materialize(root, output, "p1_s1_nominal", 7, sha)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "layer": "S1", "project_id": "campus_p3",
                "robots": {"uav_01": "a", "uav_02": "b"}, **CASES[scenario_id], "expected": scenario["expected"],
                "fault_id": scenario["fault"], "description": scenario["description"]}
    (output / "scenario.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def export_tables(ledger: Path, output: Path) -> dict:
    """Read-only JSON copy of the tables of a stopped service. / 已停止服务各表的只读 JSON 副本。"""
    db = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {name: [dict(row) for row in db.execute(f"SELECT * FROM {name}")] for name in TABLES if name in names}
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
