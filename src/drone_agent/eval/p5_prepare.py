"""Materialize one P5 S1 case (D071): the road obstacle template on PX4 SITL in the P5 world.

Without `--output` it prints the S1 part of the suite. A case names the road report that starts it, how many rounds of
clearance feedback the harness may report, and at which round it removes the obstacle model from the Gazebo world;
the runner reads nothing else. The table export reuses the P4 one, so the judge never opens a live database.

生成一个 P5 S1 用例（D071）：P5 世界中 PX4 SITL 上的道路障碍模板。不带 `--output` 时打印场景集的 S1 部分。用例给出启动它的
道路报告、编排最多可报告几轮清障反馈，以及在第几轮把障碍物模型从 Gazebo 世界中移除；执行器不读取其他内容。表导出复用 P4
的实现，使裁判从不打开运行中的数据库。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from drone_agent.eval.p1_prepare import materialize as p1_materialize
from drone_agent.eval.p4_prepare import export_tables

SUITE = "configs/scenarios/p5_suite.yaml"
# The obstacle: a dark box over 40% of the 4 m x 2 m road marking at (2, 8), without collision.
# 障碍物：覆盖 (2, 8) 处 4 m × 2 m 道路标线 40% 的暗色箱体，无碰撞体。
OBSTACLE_SDF = ('<?xml version="1.0"?><sdf version="1.9"><model name="road_obstacle"><static>true</static>'
                '<pose>2 8 0.19 0 0 0</pose><link name="obstacle"><visual name="obstacle_visual"><geometry><box>'
                '<size>1.6 2.0 0.3</size></box></geometry><material><ambient>0.12 0.1 0.08 1</ambient>'
                '<diffuse>0.12 0.1 0.08 1</diffuse></material></visual></link></model></sdf>')
CASES = {
    "p5_s1_road": {"workflow_id": "road_watch", "trigger_id": "road_report", "event_type": "road.report",
                   "segment": "road_north", "remove_at_round": 1, "rounds": 1},
    "p5_s1_road_not_cleared": {"workflow_id": "road_watch", "trigger_id": "road_report", "event_type": "road.report",
                               "segment": "road_north", "remove_at_round": 2, "rounds": 2},
}


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "p5.suite/v1":
        raise ValueError("not a p5.suite/v1 file")
    return suite


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    scenario = next(case for case in load_suite(root)["s1"] if case["id"] == scenario_id)
    if seed not in scenario["seeds"]:
        raise ValueError("seed outside the case's seeds")
    output.mkdir(parents=True, exist_ok=True)
    # The P1 fixture writer gives the service its labelled scripted planner file. / 由 P1 夹具写出带标注的脚本规划文件。
    p1_materialize(root, output, "p1_s1_nominal", 7, sha)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "layer": "S1", "project_id": "campus_s1",
                "robot_id": "uav_01", **CASES[scenario_id], "inspections": 1, "obstacle_sdf": OBSTACLE_SDF,
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
