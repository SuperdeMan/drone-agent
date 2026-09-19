"""Reject incomplete, mixed-revision or misleading release evidence.

拒绝不完整、混合版本或误导性的发布证据。
"""

import copy
import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VERIFY = runpy.run_path(str(ROOT / "scripts/verify_m1_release.py"))["verify"]
SUITE = yaml.safe_load((ROOT / "configs/scenarios/m1_suite.yaml").read_text())
EXPECTATIONS = yaml.safe_load((ROOT / "configs/scenarios/m1_expectations.yaml").read_text())


def receipt():
    artifacts = {
        name: "c" * 64
        for name in [
            "aircraft/guardian.jsonl",
            "aircraft/guardian.mcap",
            "aircraft/executive.jsonl",
            "aircraft/executive.mcap",
            "truth/truth.jsonl",
            "input/package.json",
            "ulog/test.ulg",
        ]
    }
    rows = [
        {
            "scenario": case["id"],
            "seed": seed,
            "source_sha": "a" * 40,
            "requested_speed_factor": SUITE["speed_factor"],
            "passed": True,
            "classification": case["expected"],
            "judge_exit_code": 0,
            "replay_agrees": True,
            "false_success_reports": 0,
            "problems": [],
            "validated_edge": EXPECTATIONS[case["id"]].get("edge"),
            "artifacts": artifacts,
        }
        for case in SUITE["scenarios"]
        for seed in SUITE["seeds"]
    ]
    return {
        "status": "passed",
        "source_sha": "a" * 40,
        "results": rows,
        "other_containers_before": {"test_identity": 1},
        "other_containers_after": {"test_identity": 1},
    }


def test_full_matrix_is_required_for_release():
    result = VERIFY([receipt()], "a" * 40, SUITE, EXPECTATIONS)
    assert result["cases"] == len(SUITE["scenarios"]) * len(SUITE["seeds"])
    assert result["false_success_reports"] == 0


@pytest.mark.parametrize(
    "defect", ["missing", "revision", "false_success", "replay", "duplicate", "edge", "evidence", "isolation"]
)
def test_unqualified_results_cannot_close_the_milestone(defect):
    data = copy.deepcopy(receipt())
    if defect == "missing":
        data["results"].pop()
    elif defect == "revision":
        data["source_sha"] = "b" * 40
    elif defect == "false_success":
        data["results"][0]["false_success_reports"] = 1
    elif defect == "replay":
        data["results"][0]["replay_agrees"] = False
    elif defect == "duplicate":
        data["results"].append(data["results"][0])
    elif defect == "edge":
        data["results"][-1]["validated_edge"] = None
    elif defect == "evidence":
        data["results"][0]["artifacts"] = {}
    else:
        data["other_containers_after"] = {"test_identity": 2}
    with pytest.raises(ValueError):
        VERIFY([data], "a" * 40, SUITE, EXPECTATIONS)
