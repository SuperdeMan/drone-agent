"""Materialize one P1 S1 case and export the operations tables for its judge (WP-P1-08).

Without `--output` it prints the S1 part of the suite. The scripted planner file is the labelled test double the
mission service uses in these cases; it answers only the case's own request text. `--export-ops` copies the op_*
tables and the deliveries of a stopped service's ledger into JSON, read-only, so the judge never opens a live
database or reuses service code.

生成一个 P1 S1 用例，并为其裁判导出运营表（WP-P1-08）。不带 `--output` 时打印场景集的 S1 部分。脚本规划文件是
这些用例中任务服务使用的带标注测试替身，只回答本用例自己的请求文本。`--export-ops` 以只读方式把已停止服务账本
中的 op_* 表与投递导出为 JSON，使裁判从不打开运行中的数据库，也不复用服务代码。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from drone_agent.eval.adversarial import NOMINAL_DRAFT
from drone_agent.planner.draft import TOOL_NAME
from drone_agent.providers.replay import SCRIPTED_FORMAT

SUITE = "configs/scenarios/p1_suite.yaml"
TEXT = "Inspect the red equipment marker east of the pad and bring back a photo."
TABLES = ("op_bindings", "op_reservations", "op_holds", "op_claims", "op_decisions", "op_cancellations",
          "op_dock_actions", "op_dock_locks", "op_dock_sessions", "op_events", "deliveries", "meta")


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "p1.suite/v1":
        raise ValueError("not a p1.suite/v1 file")
    return suite


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    scenario = next(case for case in load_suite(root)["s1"] if case["id"] == scenario_id)
    if seed not in scenario["seeds"]:
        raise ValueError("seed outside the case's seeds")
    output.mkdir(parents=True, exist_ok=True)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "layer": "S1", "text": TEXT,
                "scope": {"volume_id": "campus_training"}, "project_id": "campus_s1", "robot_id": "uav_01",
                "expected": scenario["expected"], "fault": scenario["fault"],
                "description": scenario["description"]}
    (output / "scenario.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    answer = {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": {
        **NOMINAL_DRAFT, "tasks": [{"task_id": "inspect_asset_red", "skill_id": "skill.inspect.asset",
                                    "asset_id": "asset_red"}], "goal": "Inspect the red equipment marker"}}]}
    (output / "planner-fixtures.json").write_text(json.dumps(
        {"format": SCRIPTED_FORMAT, "source": "scripted", "note": "hand-written test double, not model output",
         "answers": {TEXT: answer}}, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def export_operations(ledger: Path, output: Path) -> dict:
    """Read-only JSON copy of the operations tables of a stopped service. / 已停止服务运营表的只读 JSON 副本。"""
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
    parser.add_argument("--export-ops", type=Path, help="stopped service ledger to export")
    args = parser.parse_args()
    if args.export_ops:
        print(json.dumps(export_operations(args.export_ops, args.output)))
    elif args.output:
        print(json.dumps(materialize(args.root, args.output, args.scenario, args.seed, args.sha), ensure_ascii=False))
    else:
        print(json.dumps(load_suite(args.root), ensure_ascii=False))


if __name__ == "__main__":
    main()
