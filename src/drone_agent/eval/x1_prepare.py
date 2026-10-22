"""Materialize one X1 S1 case and export the ledger's tables for its judge (D088).

Without `--output` it prints the S1 part of the suite. A case names the workflow input, where the harness places the
anomaly block in the shared frame and the case's actions (remove the block once the offer is out, cut the ground
robot's link while it drives, cancel the ground mission while it drives and carry the stopped robot home, as a person
would); the runner reads nothing else. The block's
Gazebo model is built here: a static cube with an emissive-only material whose channels are the declared colour raised
to the power 2.2, because Gazebo encodes emissive colours as sRGB (measured 2026-10-14: the declared orange and magenta
then render exactly as declared, while the plain values render about 55 levels too bright in green). The scripted
planner file only exists because the mission service's scripted mode needs one: the workflow's missions are compiled
deterministically. `--export` copies the scheduling, workflow, handoff, business, operations and mission tables of a
stopped service's ledger into JSON, read-only, so the judge never opens a live database or reuses service code.

生成一个 X1 S1 用例，并为其裁判导出账本表（D088）。不带 `--output` 时打印场景集的 S1 部分。用例给出工作流输入、编排在共享坐标
中放置异常物方块的位置，以及用例的动作（提议发出后移走方块、地面车行驶中切断其链路、地面车行驶中取消地面任务并像人一样
把停住的车送回）；执行器不读取其他内容。方块的 Gazebo 模型在此生成：静态立方体，只有自发光材质，各通道取声明颜色的 2.2 次幂，因为 Gazebo 按 sRGB 编码自发光
颜色（2026-10-14 实测：这样声明的橙色与品红按声明值成像，而直接取值时绿色通道约亮 55 级）。脚本规划文件只因任务服务的脚本模式
需要而存在：工作流的任务是确定性编译的。`--export` 以只读方式把已停止服务账本中的调度、工作流、交接、业务、运营与任务表导出为
JSON，使裁判从不打开运行中的数据库，也不复用服务代码。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from drone_agent.eval.p1_prepare import materialize as p1_materialize
from drone_agent.eval.p3_prepare import TABLES as P3_TABLES
from drone_agent.eval.x1_world import MID_OFFSETS
from drone_agent.fleet.locator import COLOURS

SUITE = "configs/scenarios/x1_suite.yaml"
TABLES = (*P3_TABLES, "ho_handoffs", "ho_events", "bz_rechecks", "bz_findings", "bz_jobs")
LAYOUT = {"catalog": "configs/sites/x1_s1_v1.yaml", "members": "configs/sites/x1_members_s1.yaml",
          "scheduling": "configs/scheduling/x1_s1_v1.yaml", "workflows": "configs/workflows/x1_s1_v1.yaml",
          "business": "configs/analysis/x1_s1_v1.yaml"}
BLOCK_MODEL = "anomaly_block"
BLOCK_SIZE_M = 0.8
# The block sits near asset_mid (5, 4) at S0's per-seed offsets; asset_west's case puts it in the middle of the
# fenced compound. / 方块按 S0 各种子的偏移放在 asset_mid (5, 4) 附近；asset_west 的用例把它放在围栏院子中部。
COMPOUND_BLOCK = (-4.0, 6.0)
# What the runner does per case; the suite holds descriptions and expectations. / 执行器按用例的动作；场景集存描述与期望。
CASES = {
    "x1_nominal": {"asset": "asset_mid", "block": "mid", "actions": []},
    "x1_unreachable": {"asset": "asset_west", "block": "compound", "actions": []},
    "x1_not_found": {"asset": "asset_mid", "block": "mid", "actions": ["remove_block_after_offer"]},
    "x1_link_loss_driving": {"asset": "asset_mid", "block": "mid", "actions": ["cut_link_while_driving"]},
    "x1_cancel_during_recheck": {"asset": "asset_mid", "block": "mid",
                                 "actions": ["cancel_while_driving", "carry_home_after_stop"]},
}
# Where a person sets the rover down: its parking spot in the world, facing north as it was spawned.
# 人把地面车放下的位置：其在世界中的停车位，与生成时一样朝北。
PARKING_POSE = 'name: "rover_differential_1", position: {x: 2, y: -4, z: 0.02}, orientation: {z: 0.7071068, w: 0.7071068}'


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "drone.x1-suite/v1":
        raise ValueError("not a drone.x1-suite/v1 file")
    return suite


def block_sdf(x: float, y: float, colour: str, size_m: float = BLOCK_SIZE_M) -> str:
    """The anomaly block's Gazebo model: the declared colour through Gazebo's sRGB encoding of emissive light.

    异常物方块的 Gazebo 模型：声明颜色经 Gazebo 对自发光的 sRGB 编码后成像。
    """
    if colour not in COLOURS:
        raise ValueError(f"unknown anomaly colour {colour!r}")
    emissive = " ".join(f"{(channel / 255) ** 2.2:.4f}" for channel in COLOURS[colour])
    box = f"<geometry><box><size>{size_m} {size_m} {size_m}</size></box></geometry>"
    return (f'<?xml version="1.0"?><sdf version="1.9"><model name="{BLOCK_MODEL}"><static>true</static>'
            f'<pose>{x} {y} {size_m / 2} 0 0 0</pose><link name="block"><visual name="block_visual">{box}'
            '<material><ambient>0 0 0 1</ambient><diffuse>0 0 0 1</diffuse><specular>0 0 0 1</specular>'
            f'<emissive>{emissive} 1</emissive></material></visual><collision name="block_collision">{box}'
            '</collision></link></model></sdf>')


def block_position(case: dict, seed: int) -> tuple[float, float]:
    if case["block"] == "compound":
        return COMPOUND_BLOCK
    dx, dy = MID_OFFSETS[seed]
    return 5.0 + dx, 4.0 + dy


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    suite = load_suite(root)
    scenario = next(case for case in suite["s1"] if case["id"] == scenario_id)
    if seed not in scenario.get("seeds", suite["seeds"]):
        raise ValueError("seed outside the case's seeds")
    output.mkdir(parents=True, exist_ok=True)
    # The P1 fixture writer gives the service its labelled scripted planner file. / 由 P1 夹具写出带标注的脚本规划文件。
    p1_materialize(root, output, "p1_s1_nominal", 7, sha)
    case = CASES[scenario_id]
    x, y = block_position(case, seed)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "layer": "S1", "project_id": "campus_x1",
                "robots": {"uav_01": "a", "ugv_01": "g"}, "workflow_id": "anomaly_recheck",
                "inputs": {"asset": case["asset"]}, "actions": case["actions"],
                "block": {"x": x, "y": y, "size_m": BLOCK_SIZE_M, "colour": "orange", "model": BLOCK_MODEL,
                          "sdf": block_sdf(x, y, "orange")}, "parking_pose": PARKING_POSE,
                "expected": scenario["expected"], "description": scenario["description"]}
    (output / "scenario.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "layout.json").write_text(json.dumps(LAYOUT, indent=2), encoding="utf-8")
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
