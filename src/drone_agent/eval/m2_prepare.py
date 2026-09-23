"""Materialize one M2 end-to-end case: scenario metadata, scripted planner answers and fault files.

Without `--output` it prints the suite. The scripted planner file is the labelled test double the mission service
uses when no model key is configured; it answers only the case's own request text.

生成一个 M2 端到端用例：场景元数据、脚本规划回答与故障文件。不带 `--output` 时打印场景集。脚本规划文件是
未配置模型密钥时任务服务使用的带标注测试替身，只回答本用例自己的请求文本。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from drone_agent.eval.adversarial import NOMINAL_DRAFT
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.planner.draft import TOOL_NAME
from drone_agent.providers.replay import SCRIPTED_FORMAT

SUITE = "configs/scenarios/m2_suite.yaml"


def load_suite(root: Path) -> dict:
    suite = yaml.safe_load((root / SUITE).read_text(encoding="utf-8"))
    if suite.get("format") != "m2.suite/v1":
        raise ValueError("not an m2.suite/v1 file")
    return suite


def materialize(root: Path, output: Path, scenario_id: str, seed: int, sha: str) -> dict:
    suite = load_suite(root)
    scenario = next(case for case in suite["scenarios"] if case["id"] == scenario_id)
    if seed not in suite["seeds"]:
        raise ValueError("seed outside the suite")
    text = suite["texts"][scenario["texts"]][seed]
    registry = Registry(root, scene=root / M2_SCENE)
    output.mkdir(parents=True, exist_ok=True)
    metadata = {"scenario": scenario_id, "seed": seed, "source_sha": sha, "text": text, "scope": scenario["scope"],
                "expected": scenario["expected"], "outage": scenario.get("outage"),
                "faults": {str(k): v for k, v in (scenario.get("faults") or {}).items()},
                "registry_hash": registry.sha256, "description": scenario["description"]}
    (output / "scenario.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    answer = {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": {**NOMINAL_DRAFT, **scenario["planner"]}}]}
    (output / "planner-fixtures.json").write_text(json.dumps(
        {"format": SCRIPTED_FORMAT, "source": "scripted", "note": "hand-written test double, not model output",
         "answers": {text: answer}}, ensure_ascii=False, indent=2), encoding="utf-8")
    for version, fault in (scenario.get("faults") or {}).items():
        (output / f"fault-v{version}.json").write_text(json.dumps({"id": f"{scenario_id}-{seed}-v{version}", **fault}))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenario")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--sha", default="uncommitted")
    args = parser.parse_args()
    if args.output:
        print(json.dumps(materialize(args.root, args.output, args.scenario, args.seed, args.sha), ensure_ascii=False))
    else:
        print(json.dumps(load_suite(args.root), ensure_ascii=False))


if __name__ == "__main__":
    main()
