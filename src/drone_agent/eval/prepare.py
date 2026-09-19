"""Materialize a seeded test package in an owned run directory.

在独立运行目录生成带随机种子的测试任务包。
"""

import argparse
import json
import uuid
from pathlib import Path

import yaml

from drone_agent.eval.fixture import make_package
from drone_agent.mission.registry import Registry

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--root", type=Path, default=Path("/workspace"))
parser.add_argument("--output", type=Path)
parser.add_argument("--scenario", default="nominal")
parser.add_argument("--seed", type=int, default=7)
parser.add_argument("--sha", default="uncommitted")
parser.add_argument("--speed-factor", type=int, choices=[1, 2], default=1)
args = parser.parse_args()
suite = yaml.safe_load((args.root / "configs/scenarios/m1_suite.yaml").read_text())
if args.output:
    scenario = next(case for case in suite["scenarios"] if case["id"] == args.scenario)
    registry = Registry(args.root)
    package = make_package(registry, args.seed, f"m1-{args.scenario}-{args.seed}-{uuid.uuid4().hex}")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "package.json").write_text(package.model_dump_json(indent=2))
    (args.output / "scenario.json").write_text(
        json.dumps(
            {
                "scenario": scenario,
                "seed": args.seed,
                "source_sha": args.sha,
                "requested_speed_factor": args.speed_factor,
                "registry_hash": registry.sha256,
                "route_speed_mps": package.nodes[1].params["speed_mps"],
            }
        )
    )
else:
    print(json.dumps(suite))
