"""Materialize one seeded M3 case in an owned run directory, or print the suite (WP-M3-20).

在独立运行目录中生成一个带种子的 M3 用例，或打印场景集（WP-M3-20）。
"""

import argparse
import json
import uuid
from pathlib import Path

import yaml

from drone_agent.eval.m3_fixture import make_m3_package
from drone_agent.mission.registry import M3_SCENE, Registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenario", default="ext_inspect")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--sha", default="uncommitted")
    args = parser.parse_args()
    suite = yaml.safe_load((args.root / "configs/scenarios/m3_suite.yaml").read_text(encoding="utf-8"))
    if not args.output:
        print(json.dumps(suite))
        return
    scenario = next(case for case in suite["scenarios"] if case["id"] == args.scenario)
    registry = Registry(args.root, scene=args.root / M3_SCENE)
    package = make_m3_package(registry, args.seed, f"m3-{args.scenario}-{args.seed}-{uuid.uuid4().hex}",
                              scenario["shape"])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "package.json").write_text(package.model_dump_json(indent=2))
    speed = next(node.params["speed_mps"] for node in package.nodes if "speed_mps" in node.params)
    (args.output / "scenario.json").write_text(json.dumps({
        "scenario": scenario,
        "seed": args.seed,
        "source_sha": args.sha,
        "requested_speed_factor": suite["speed_factor"],
        "registry_hash": registry.sha256,
        "cruise_speed_mps": speed,
        "milestone": "M3",
    }))


if __name__ == "__main__":
    main()
