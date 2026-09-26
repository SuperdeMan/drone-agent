"""The P2 S1 judge on a case in the S1 layout: the per-mission M2 flight judge plus the P1 and P2 checks.

The case is produced locally with the S1 catalogs and logical flights (a judge smoke test, never flight evidence),
then arranged the way scripts/remote_p2.py arranges a PX4 SITL case.

以 S1 布局的用例验证 P2 S1 裁判：逐任务的 M2 飞行裁判加上 P1 与 P2 检查。用例在本机用 S1 目录与逻辑飞行生成（裁判
冒烟测试，从不作为飞行证据），再按 scripts/remote_p2.py 整理 PX4 SITL 用例的方式排布。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from drone_agent.eval.judge_p2 import judge_s1_case
from drone_agent.eval.p2_prepare import CASES, load_suite
from drone_agent.eval.p2_world import OPERATOR, REVIEWER, P2World

ROOT = Path(__file__).resolve().parents[2]


class S1World(P2World):
    backend = "px4_sitl"


def s1_layout(world: P2World, target: Path, scenario_id: str, seed: int) -> Path:
    """Arrange an exported case like remote_p2 does. / 按 remote_p2 的方式整理导出的用例。"""
    source = world.case
    for folder in ("input", "service", "service-export", "world", "dock", "truth"):
        (target / folder).mkdir(parents=True)
    scenario = next(case for case in load_suite(ROOT)["s1"] if case["id"] == scenario_id)
    (target / "input/scenario.json").write_text(json.dumps({
        "scenario": scenario_id, "seed": seed, "source_sha": "test", "layer": "S1", **CASES[scenario_id],
        "expected": scenario["expected"]}), encoding="utf-8")
    (target / "service/ready.json").write_text(json.dumps({"signer_key_id": world.key.key_id}), encoding="utf-8")
    views = json.loads((source / "service-export/views.json").read_text(encoding="utf-8"))
    (target / "service-export/views.json").write_text(json.dumps(views), encoding="utf-8")
    for name in ("workflows.json", "resources.json"):
        shutil.copyfile(source / "service-export" / name, target / "service-export" / name)
    for name in ("api.jsonl", "injections.jsonl", "uav_01-flights.json"):
        shutil.copyfile(source / "world" / name, target / "world" / name)
    shutil.copyfile(source / "world/docks.jsonl", target / "dock/dock.jsonl")
    shutil.copyfile(source / "world/uav_01-truth.jsonl", target / "truth/truth.jsonl")
    shutil.copytree(source / "robots/uav_01/aircraft", target / "aircraft")
    shutil.copytree(source / "robots/uav_01/inbox/history", target / "inbox/history")
    return target


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    base = tmp_path_factory.mktemp("p2-s1-layout")

    async def run() -> P2World:
        world = S1World(base / "world", ROOT, seed=7, catalog=ROOT / "configs/sites/p1_s1_v1.yaml",
                        members=ROOT / "configs/sites/p2_members_s1.yaml",
                        workflows=ROOT / "configs/workflows/p2_s1_v1.yaml")
        await world.settle()
        run = await world.start("asset_check", {"asset": "asset_red"}, project="campus_s1")
        runs = [run]
        await world.drive(runs, until=lambda: len(runs) > 1 and all(world.final(r) for r in runs),
                          what="chain end", reviews={"review": "confirmed"}, reviewer=REVIEWER, approver=OPERATOR)
        for uav in world.uavs.values():
            await uav.settle()
        await world.hold(1.0)
        return world

    world = asyncio.run(run())
    world.export({"scenario": "p2_s1_chain", "seed": 7, "source_sha": "test", "layer": "S0", "expected": {}})
    world.ledger.close()
    return world, base


def test_a_chain_in_the_s1_layout_passes_every_flight_and_workflow_check(chain):
    world, base = chain
    case = s1_layout(world, base / "s1", "p2_s1_chain", 7)
    result = judge_s1_case(case, ROOT)
    assert result["passed"], json.dumps({k: result[k] for k in ("problems", "counts", "flight_judges")}, indent=1,
                                        default=str)
    assert len(result["flight_judges"]) == 2 and all(j["passed"] for j in result["flight_judges"].values())
    replay = judge_s1_case(case, ROOT, use_replay=True)
    assert replay["counts"] == result["counts"] and replay["problems"] == result["problems"]


def test_a_tampered_s1_case_is_caught(chain, tmp_path):
    world, base = chain
    case = s1_layout(world, tmp_path / "s1", "p2_s1_chain", 7)
    tables = json.loads((case / "service-export/workflows.json").read_text(encoding="utf-8"))
    tables["wf_reviews"][0]["reviewer"] = "harness:p2-operator"
    (case / "service-export/workflows.json").write_text(json.dumps(tables), encoding="utf-8")
    result = judge_s1_case(case, ROOT)
    assert not result["passed"] and result["counts"]["false_order"] >= 1
