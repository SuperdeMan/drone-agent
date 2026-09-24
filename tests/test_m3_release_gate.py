"""The M3-SITL gate never lets a rerun hide a finding, counts void cases, and gates the D040 tiers (WP-M3-20).

M3-SITL 门禁绝不让重跑掩盖发现、对作废用例计数，并对 D040 档位把关（WP-M3-20）。
"""

import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_m3_release.py"))
SHA = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
SUITE = GATE["suite_at"](SHA, "configs/scenarios/m3_suite.yaml")


def row(scenario, seed, **overrides):
    value = {"scenario": scenario, "seed": seed, "source_sha": SHA, "passed": True, "replay_agrees": True,
             "judge_exit_code": 0, "false_success_reports": 0, "problems": [], "void": False,
             "shadow_report": {"exit_code": 0}, "metrics": {"resources": {"guardian": {"cpu_mean_cores": 0.1}}},
             "measure": {"edge": True, "edge_period_s": 1.0, "isolation": "separate"}}
    value.update(overrides)
    return value


def receipt(rows):
    return {"source_sha": SHA, "other_containers_before": {"n": 1}, "other_containers_after": {"n": 1},
            "results": rows}


def full():
    return [row(case["id"], seed) for case in SUITE["scenarios"] for seed in SUITE["seeds"]]


def test_every_case_passing_passes_and_void_cases_are_counted():
    rows = full()
    first = rows[0]
    rows.insert(0, row(first["scenario"], first["seed"], void=True, passed=False,
                       classification="void_simulator_stall", problems=["simulator_stall_before_recovery"]))
    report = GATE["m3_suite"]([receipt(rows)], SHA)
    assert report["status"] == "passed" and report["void"] == [(first["scenario"], first["seed"])]
    assert report["passed"] == report["expected"] == len(SUITE["scenarios"]) * len(SUITE["seeds"])


def test_a_non_void_failure_is_never_hidden_by_a_later_pass():
    rows = full()
    rows.insert(0, row(rows[0]["scenario"], rows[0]["seed"], passed=False, classification="safe_abort",
                       problems=["expected_recovery_edge_not_observed"]))
    report = GATE["m3_suite"]([receipt(rows)], SHA)
    assert report["status"] == "failed" and any("failed" in p for p in report["problems"])


def test_missing_cases_shadow_reports_or_resource_samples_fail():
    rows = full()[1:]
    assert GATE["m3_suite"]([receipt(rows)], SHA)["status"] == "failed"
    rows = full()
    rows[0]["shadow_report"] = {"exit_code": 1}
    assert GATE["m3_suite"]([receipt(rows)], SHA)["status"] == "failed"
    assert GATE["m3_suite"]([], SHA)["status"] == "missing"


def tier(scenario, seed, *, edge=True, period=1.0, isolation="separate", p99=0.101, max_s=0.11, edge_p99=400.0):
    return row(scenario, seed, measure={"edge": edge, "edge_period_s": period, "isolation": isolation},
               metrics={"supervision": {"p99_s": p99, "max_s": max_s}, "intent_latency_p99_ms": 180.0,
                        "edge_inference": {"inference_ms": {"p99": edge_p99}},
                        "resources": {"guardian": {"cpu_mean_cores": 0.1, "throttled_fraction": 0.0}}})


@pytest.mark.parametrize("shared_p99", [0.101, 0.4])
def test_measurement_gates_the_separate_tiers_and_only_reports_the_shared_core(shared_p99):
    rows = []
    for seed in (7, 19, 41):
        rows += [tier("route_fallback", seed), tier("ext_inspect", seed, edge=False),
                 tier("ext_inspect", seed, period=0.0),
                 tier("ext_inspect", seed, period=0.0, isolation="shared", p99=shared_p99)]
    report = GATE["measurement"]([receipt(rows)], SHA)
    assert report["status"] == "passed"
    assert report["cells"]["inference/shared"]["within_budget"] is (shared_p99 <= 0.12)


def test_measurement_needs_three_seeds_per_gated_tier_and_the_budget():
    rows = [tier("route_fallback", s) for s in (7, 19, 41)] + [tier("ext_inspect", s, edge=False) for s in (7, 19)]
    rows += [tier("ext_inspect", s, period=0.0) for s in (7, 19, 41)]
    assert GATE["measurement"]([receipt(rows)], SHA)["status"] == "missing"
    rows += [tier("ext_inspect", 41, edge=False, max_s=0.35)]
    assert GATE["measurement"]([receipt(rows)], SHA)["status"] == "failed"
