"""Require the complete same-revision seeded matrix before reporting M1 closed.

只有同一版本的完整多种子矩阵通过，才允许报告 M1 关闭。
"""

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def verify(receipts, sha, suite, expectations):
    expected = {(case["id"], seed) for case in suite["scenarios"] for seed in suite["seeds"]}
    wanted = {case["id"]: case["expected"] for case in suite["scenarios"]}
    results = {}
    for receipt in receipts:
        if receipt.get("source_sha") != sha or receipt.get("status") != "passed":
            raise ValueError("receipt is failed or belongs to a different revision")
        if receipt.get("other_containers_before") != receipt.get("other_containers_after"):
            raise ValueError("shared-server isolation was not verified")
        for row in receipt["results"]:
            key = row["scenario"], row["seed"]
            if key not in expected or key in results:
                raise ValueError("unexpected or duplicate scenario/seed result")
            if (
                row.get("source_sha") != sha
                or row.get("requested_speed_factor") != suite["speed_factor"]
                or row.get("passed") is not True
                or row.get("judge_exit_code") != 0
                or row.get("replay_agrees") is not True
                or row.get("false_success_reports") != 0
                or row.get("problems") != []
                or row.get("classification") != wanted[row["scenario"]]
            ):
                raise ValueError("scenario did not meet the independent acceptance criteria")
            edge = expectations[row["scenario"]].get("edge")
            if row.get("validated_edge") != edge:
                raise ValueError("required recovery edge was not observed")
            artifacts = row.get("artifacts", {})
            required = {
                "aircraft/guardian.jsonl",
                "aircraft/guardian.mcap",
                "aircraft/executive.jsonl",
                "aircraft/executive.mcap",
                "truth/truth.jsonl",
                "input/package.json",
            }
            if not required <= set(artifacts) or not any(
                name.startswith("ulog/") and name.endswith(".ulg") for name in artifacts
            ):
                raise ValueError("required raw evidence is missing")
            if any(not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in artifacts.values()):
                raise ValueError("invalid evidence digest")
            results[key] = {
                name: row.get(name)
                for name in (
                    "scenario",
                    "seed",
                    "classification",
                    "false_success_reports",
                    "replay_agrees",
                    "validated_edge",
                    "truth_samples",
                )
            }
    if set(results) != expected:
        raise ValueError(f"incomplete matrix: {len(results)}/{len(expected)} cases")
    return {
        "schema_version": "0.1.0",
        "milestone": "M1",
        "status": "passed",
        "source_sha": sha,
        "scenarios": len(suite["scenarios"]),
        "seeds": suite["seeds"],
        "cases": len(results),
        "classifications": dict(Counter(row["classification"] for row in results.values())),
        "false_success_reports": 0,
        "replay_agreements": len(results),
        "recovery_edges": len({value["edge"] for value in expectations.values() if "edge" in value}),
        "results": [results[key] for key in sorted(results)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--receipts", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")

    def config(path):
        raw = subprocess.check_output(["git", "show", f"{args.sha}:{path}"], cwd=ROOT)
        return yaml.safe_load(raw.decode("utf-8"))

    receipts = [json.loads(path.read_text(encoding="utf-8-sig")) for path in args.receipts]
    report = verify(
        receipts, args.sha, config("configs/scenarios/m1_suite.yaml"), config("configs/scenarios/m1_expectations.yaml")
    )
    report["receipts"] = [
        {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "artifact_directory": receipt["artifact_directory"],
            "deployment_id": receipt["deployment_id"],
            "images": receipt["images"],
        }
        for path, receipt in zip(args.receipts, receipts, strict=True)
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"results", "receipts"}}))


if __name__ == "__main__":
    main()
