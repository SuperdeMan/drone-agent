"""The X1 gate's own rules on hand-made receipts (D088): S1 coverage, counts, capacity and identities, and the scope.

X1 门禁自身规则在构造回执上的测试（D088）：S1 覆盖、计数、容量与身份，以及范围。
"""

from __future__ import annotations

import copy
import runpy
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_x1_release.py"))
HEAD = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def receipt(sha: str) -> dict:
    from drone_agent.eval.judge_x1 import COUNTS

    suite = yaml.safe_load((ROOT / "configs/scenarios/x1_suite.yaml").read_text(encoding="utf-8"))
    rows = [{"scenario": case["id"], "seed": seed, "passed": True, "replay_agrees": True, "judge_exit_code": 0,
             "problems": [], "source_sha": sha, "layer": "S1", "counts": dict.fromkeys(COUNTS, 0),
             "capacity": {"verdict": {"sufficient": True}},
             "flown": {"uav_01": ["m-a-v1"], "ugv_01": ["m-g-v1"] if case["id"] != "x1_unreachable" else []}}
            for case in suite["s1"] for seed in suite["seeds"]]
    return {"source_sha": sha, "status": "passed", "layer": "S1", "other_containers_before": {"a": 1},
            "other_containers_after": {"a": 1}, "signer_key_id": "k1",
            "certificate_fingerprints": dict.fromkeys(GATE["FINGERPRINTS"], "x"), "results": rows}


def test_s1_needs_every_case_and_seed_with_zero_counts_capacity_and_both_robots():
    good = receipt(HEAD)
    assert GATE["s1"]([good], HEAD)["status"] == "passed"
    assert GATE["s1"]([], HEAD)["status"] == "missing"
    short = copy.deepcopy(good)
    short["results"].pop()
    assert GATE["s1"]([short], HEAD)["missing"]
    counted = copy.deepcopy(good)
    counted["results"][0]["counts"]["false_success"] = 1
    assert GATE["s1"]([counted], HEAD)["status"] == "failed"
    starved = copy.deepcopy(good)
    starved["results"][3]["capacity"]["verdict"]["sufficient"] = False
    assert GATE["s1"]([starved], HEAD)["status"] == "failed"
    foreign = copy.deepcopy(good)
    foreign["certificate_fingerprints"] = {"ca": "x", "service": "x", "robot": "x", "robot-tls-uav_02": "x"}
    assert GATE["s1"]([foreign], HEAD)["status"] == "failed"
    other = receipt("0" * 40)
    assert GATE["s1"]([other], HEAD)["status"] == "failed"


def test_the_scope_leaves_the_desk_alone_and_requires_the_onboard_regressions():
    result = GATE["scope"](HEAD)
    assert result["status"] == "passed" and result["desk_changed"] == []
    assert result["onboard_changed"] and result["regressions_required"] == ["m1", "m2_e2e", "m3_suite"]
