"""The P3 S1 judge on a two-aircraft case in the S1 layout, and its reverse checks on tampered copies.

The case is the pair_check workflow produced locally with the S1 catalogs and logical flights (a judge smoke test,
never flight evidence), then arranged the way scripts/remote_p3.py arranges a PX4 SITL case: one directory set per
aircraft and Gazebo truth in the world frame. Each tampered copy breaks one invariant the judge must catch.

以 S1 布局的双机用例验证 P3 S1 裁判，并在篡改副本上做反向检查。用例是在本机用 S1 目录与逻辑飞行生成的 pair_check
工作流（裁判冒烟测试，从不作为飞行证据），再按 scripts/remote_p3.py 整理 PX4 SITL 用例的方式排布：每架飞行器一套目录，
Gazebo 真值为世界坐标。每个篡改副本破坏一条裁判必须抓到的不变量。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from drone_agent.eval.judge_p3 import judge_s1_case
from drone_agent.eval.p3_prepare import CASES, export_tables, load_suite
from drone_agent.eval.p3_world import P3World

ROOT = Path(__file__).resolve().parents[2]
LETTERS = {"uav_01": "a", "uav_02": "b"}
DOCKS = {"dock_s1a": "a", "dock_s1b": "b"}
ORIGINS = {"uav_01": (0.0, 0.0), "uav_02": (12.0, 0.0)}


class S1World(P3World):
    backend = "px4_sitl"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def s1_layout(world: P3World, target: Path) -> Path:
    """Arrange an exported case like remote_p3 does. / 按 remote_p3 的方式整理导出的用例。"""
    source = world.case
    for folder in ("input", "service", "service-export", "world", "dock"):
        (target / folder).mkdir(parents=True)
    scenario = next(case for case in load_suite(ROOT)["s1"] if case["id"] == "p3_s1_parallel")
    (target / "input/scenario.json").write_text(json.dumps({
        "scenario": "p3_s1_parallel", "seed": 7, "source_sha": "test", "layer": "S1", "project_id": "campus_p3",
        "robots": LETTERS, **CASES["p3_s1_parallel"], "expected": scenario["expected"]}), encoding="utf-8")
    shutil.copyfile(source / "input/layout.json", target / "input/layout.json")
    (target / "service/ready.json").write_text(json.dumps({"signer_key_id": world.key.key_id}), encoding="utf-8")
    for name in ("views.json", "task-views.json", "resources.json"):
        shutil.copyfile(source / "service-export" / name, target / "service-export" / name)
    export_tables(source / "service/ledger.sqlite3", target / "service-export/scheduling.json")
    for name in ("api.jsonl", "injections.jsonl", "uav_01-flights.json", "uav_02-flights.json"):
        shutil.copyfile(source / "world" / name, target / "world" / name)
    docks = _rows(source / "world/docks.jsonl")
    for dock, letter in DOCKS.items():
        _write_rows(target / "dock" / f"dock_{letter}.jsonl", [row for row in docks if row["dock_id"] == dock])
    for robot, letter in LETTERS.items():
        ox, oy = ORIGINS[robot]
        # The Gazebo collector records the world frame. / Gazebo 采集器记录世界坐标。
        _write_rows(target / f"truth_{letter}/truth.jsonl", [
            {"timestamp": row["timestamp"], "sim_time": row["sim_time"],
             "position": [row["position"][0] + ox, row["position"][1] + oy, row["position"][2]]}
            for row in _rows(source / "world" / f"{robot}-truth.jsonl")])
        shutil.copytree(source / "robots" / robot / "aircraft", target / f"aircraft_{letter}")
        shutil.copytree(source / "robots" / robot / "inbox/history", target / f"inbox_{letter}/history")
    return target


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    base = tmp_path_factory.mktemp("p3-s1-layout")

    async def run() -> P3World:
        world = S1World(base / "world", ROOT, seed=7, catalog=ROOT / "configs/sites/p3_s1_v1.yaml",
                        members=ROOT / "configs/sites/p3_members_s1.yaml",
                        scheduling=ROOT / "configs/scheduling/p3_s1_v1.yaml",
                        workflows=ROOT / "configs/workflows/p3_s1_v1.yaml")
        await world.settle()
        run = await world.start("pair_check", {}, project="campus_p3")
        await world.drive_tasks(lambda: world.final(run), "the pair final", runs=[run], timeout_s=240)
        for uav in world.uavs.values():
            await uav.settle()
        await world.hold(1.0)
        return world

    world = asyncio.run(run())
    world.export({"scenario": "p3_s1_parallel", "seed": 7, "source_sha": "test", "layer": "S0", "expected": {}})
    world.ledger.close()
    return s1_layout(world, base / "s1")


def tampered(pair: Path, target: Path, change) -> Path:
    case = shutil.copytree(pair, target / "case")
    path = case / "service-export/scheduling.json"
    tables = json.loads(path.read_text(encoding="utf-8"))
    change(tables, case)
    path.write_text(json.dumps(tables), encoding="utf-8")
    return case


def test_the_pair_in_the_s1_layout_passes_online_and_from_the_recordings(pair):
    result = judge_s1_case(pair, ROOT)
    assert result["passed"], json.dumps({k: result[k] for k in ("problems", "counts")}, indent=1, default=str)
    assert result["classification"] == "as_expected" and result["flights"] == 2
    missions = result["missions"]
    assert sorted(m["robot_id"] for m in missions.values()) == ["uav_01", "uav_02"]
    assert all(m["classification"] == "completed" and m["false_success_reports"] == 0 for m in missions.values())
    replay = judge_s1_case(pair, ROOT, use_replay=True)
    for key in ("classification", "false_success_reports", "problems", "counts"):
        assert replay[key] == result[key]


def test_truth_left_in_a_robot_frame_is_caught(pair, tmp_path):
    case = shutil.copytree(pair, tmp_path / "case")
    truth = case / "truth_b/truth.jsonl"
    # A collector that forgot the pad offset: uav_02 seems to fly uav_01's airspace. / 忘记机位偏移的采集器。
    _write_rows(truth, [{**row, "position": [row["position"][0] - 12.0, *row["position"][1:]]} for row in _rows(truth)])
    result = judge_s1_case(case, ROOT)
    assert not result["passed"] and result["classification"] == "unsafe_or_incorrect"
    assert result["counts"]["false_success"] >= 1


def test_a_second_active_assignment_is_caught(pair, tmp_path):
    def change(tables, _case):
        row = dict(tables["sc_assignments"][0])
        tables["sc_assignments"][0]["state"] = "active"
        tables["sc_assignments"].append({**row, "state": "active", "epoch": row["epoch"] + 1, "mission_id": None})

    result = judge_s1_case(tampered(pair, tmp_path, change), ROOT)
    assert not result["passed"] and result["counts"]["double_ownership"] >= 1


def test_a_withdrawn_assignment_that_flew_is_caught(pair, tmp_path):
    def change(tables, _case):
        tables["sc_assignments"][0]["state"] = "withdrawn"

    result = judge_s1_case(tampered(pair, tmp_path, change), ROOT)
    assert not result["passed"] and result["counts"]["duplicate_execution"] >= 1


def test_a_decision_that_does_not_replay_is_caught(pair, tmp_path):
    def change(tables, _case):
        decision = next(row for row in tables["sc_decisions"] if json.loads(row["body"])["verdict"] == "assign")
        body = json.loads(decision["body"])
        body["robot_id"] = "uav_02" if body["robot_id"] == "uav_01" else "uav_01"
        decision["body"] = json.dumps(body)

    result = judge_s1_case(tampered(pair, tmp_path, change), ROOT)
    assert not result["passed"] and result["counts"]["decision_mismatch"] >= 1


def test_a_completion_without_a_verified_inspection_is_caught(pair, tmp_path):
    def change(tables, _case):
        for row in tables["verifications"]:
            body = json.loads(row["body"])
            body["final_verdict"] = "unverified"
            row["body"] = json.dumps(body)

    result = judge_s1_case(tampered(pair, tmp_path, change), ROOT)
    assert not result["passed"] and result["counts"]["false_success"] >= 1


def test_a_cell_held_by_both_flights_is_caught(pair, tmp_path):
    def change(tables, _case):
        reserved = [e for e in tables["op_events"] if e["kind"] == "reservation.reserved"]
        first = json.loads(reserved[0]["body"])
        other = next(e for e in reserved if e["subject"] != reserved[0]["subject"])
        body = json.loads(other["body"])
        body["resources"] = sorted({*body["resources"], next(r for r in first["resources"] if r.startswith("air."))})
        other["body"] = json.dumps(body)

    result = judge_s1_case(tampered(pair, tmp_path, change), ROOT)
    assert not result["passed"] and result["counts"]["conflicting_reservation"] >= 1
