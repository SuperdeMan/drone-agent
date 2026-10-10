"""Materialize one P6 S1 case and export the ledger's tables for its judge (D078).

Without `--output` it prints the S1 part of the suite. A case says whether the harness puts the glare disc over the
red marker, whether it removes the disc while the recapture waits for approval, and whether it places the P4 damage
patch; the runner reads nothing else. `--export` is the P4 read-only export of a stopped service's tables.

生成一个 P6 S1 用例，并为其裁判导出账本表（D078）。不带 `--output` 时打印场景集的 S1 部分。用例给出编排是否在红色标记上放置
反光圆片、是否在补拍等待审批时移除它，以及是否放置 P4 损伤贴片；执行器不读取其他内容。`--export` 即 P4 对已停止服务各表的
只读导出。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from drone_agent.eval.p1_prepare import materialize as p1_materialize
from drone_agent.eval.p4_prepare import PATCH_SDF, export_tables

SUITE = "configs/scenarios/p6_suite.yaml"
# The glare disc: an emissive white disc of 0.95 m radius just above the red marker's centre, without collision; it
# leaves about 29% of the marker visible, so the capture still verifies. / 反光圆片：红色标记中心正上方半径 0.95 m 的自发光
# 白色圆片，无碰撞体；标记约 29% 仍可见，因此采集仍能证实。
GLARE_SDF = ('<?xml version="1.0"?><sdf version="1.9"><model name="glare_disc"><static>true</static>'
             '<pose>4 4 0.08 0 0 0</pose><link name="glare"><visual name="glare_visual"><geometry><cylinder>'
             '<radius>0.95</radius><length>0.02</length></cylinder></geometry><material><ambient>1 1 1 1</ambient>'
             '<diffuse>1 1 1 1</diffuse><emissive>1 1 1 1</emissive></material></visual></link></model></sdf>')


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "p6.suite/v1":
        raise ValueError("not a p6.suite/v1 file")
    return suite


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    scenario = next(case for case in load_suite(root)["s1"] if case["id"] == scenario_id)
    if seed not in scenario["seeds"]:
        raise ValueError("seed outside the case's seeds")
    output.mkdir(parents=True, exist_ok=True)
    # The P1 fixture writer gives the service its labelled scripted planner file. / 由 P1 夹具写出带标注的脚本规划文件。
    p1_materialize(root, output, "p1_s1_nominal", 7, sha)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "layer": "S1", "project_id": "campus_s1",
                "robot_id": "uav_01", "workflow_id": "recapture_watch", "inputs": {"asset": "asset_red"},
                "world": scenario["world"], "glare_sdf": GLARE_SDF, "patch_sdf": PATCH_SDF,
                "expected": scenario["expected"], "description": scenario["description"]}
    (output / "scenario.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


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
