"""Materialize one P4 S1 case and export the ledger's tables for its judge (D063 §12).

Without `--output` it prints the S1 part of the suite. A case names the workflow the harness starts, how many
inspections run before any repair, and at which round of repair feedback the harness removes the damage patch from
the Gazebo world; the runner reads nothing else. `--export` copies the workflow, business, operations and mission
tables of a stopped service's ledger into JSON, read-only, so the judge never opens a live database or reuses service
code.

生成一个 P4 S1 用例，并为其裁判导出账本表（D063 §12）。不带 `--output` 时打印场景集的 S1 部分。用例给出编排启动的
工作流、维修前运行几次巡检，以及在第几轮维修反馈时编排从 Gazebo 世界中移除损伤贴片；执行器不读取其他内容。`--export` 以
只读方式把已停止服务账本中的工作流、业务、运营与任务表导出为 JSON，使裁判从不打开运行中的数据库，也不复用服务代码。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from drone_agent.eval.p1_prepare import materialize as p1_materialize
from drone_agent.eval.p2_prepare import TABLES as P2_TABLES

SUITE = "configs/scenarios/p4_suite.yaml"
TABLES = (*P2_TABLES, "bz_catalogs", "bz_jobs", "bz_references", "bz_findings", "bz_reviews", "bz_orders",
          "bz_rounds", "reports", "versions")
# The damage patch: a thin dark box over 40% of the red marker at (4, 4), without collision.
# 损伤贴片：覆盖 (4, 4) 处红色标记 40% 的薄暗色方块，无碰撞体。
PATCH_SDF = ('<?xml version="1.0"?><sdf version="1.9"><model name="damage_patch"><static>true</static>'
             '<pose>4 4 0.065 0 0 0</pose><link name="patch"><visual name="patch_visual"><geometry><box>'
             '<size>2.2 0.8 0.02</size></box></geometry><material><ambient>0.08 0.08 0.08 1</ambient>'
             '<diffuse>0.08 0.08 0.08 1</diffuse></material></visual></link></model></sdf>')
CASES = {
    "p4_s1_close": {"workflow_id": "appearance_watch", "inputs": {"asset": "asset_red"}, "inspections": 1,
                    "remove_at_round": 1, "rounds": 1},
    "p4_s1_not_repaired": {"workflow_id": "appearance_watch", "inputs": {"asset": "asset_red"}, "inspections": 1,
                           "remove_at_round": 2, "rounds": 2},
    "p4_s1_dedup": {"workflow_id": "appearance_watch", "inputs": {"asset": "asset_red"}, "inspections": 2,
                    "remove_at_round": 1, "rounds": 1},
}


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "p4.suite/v1":
        raise ValueError("not a p4.suite/v1 file")
    return suite


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    scenario = next(case for case in load_suite(root)["s1"] if case["id"] == scenario_id)
    if seed not in scenario["seeds"]:
        raise ValueError("seed outside the case's seeds")
    output.mkdir(parents=True, exist_ok=True)
    # The P1 fixture writer gives the service its labelled scripted planner file. / 由 P1 夹具写出带标注的脚本规划文件。
    p1_materialize(root, output, "p1_s1_nominal", 7, sha)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "layer": "S1", "project_id": "campus_s1",
                "robot_id": "uav_01", **CASES[scenario_id], "patch_sdf": PATCH_SDF,
                "expected": scenario["expected"], "description": scenario["description"]}
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
    parser.add_argument("--seed", type=int)
    parser.add_argument("--sha", default="uncommitted")
    parser.add_argument("--export", type=Path, help="a stopped service's ledger to export")
    args = parser.parse_args()
    if args.export is not None:
        print(json.dumps(export_tables(args.export, args.output)))
        return
    if args.output is None:
        print(json.dumps({"s1": load_suite(args.root)["s1"]}))
        return
    print(json.dumps(materialize(args.root, args.output, args.scenario, args.seed, args.sha)))


if __name__ == "__main__":
    main()
