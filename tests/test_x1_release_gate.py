"""The X1 gate's own rules on hand-made receipts (D088): S1 coverage, counts, capacity and identities, and the scope.
Only the Git reading boundary is replaced (the cloud checks image has no `.git`); the gate's decisions are not.

X1 门禁自身规则在构造回执上的测试（D088）：S1 覆盖、计数、容量与身份，以及范围。只替换 Git 读取边界（云端检查镜像没有
`.git`），不替换门禁判定本身。
"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_x1_release.py"))
SHA = "a" * 40


@pytest.fixture
def worktree(monkeypatch):
    """Read committed files from the working tree. / 从工作树读取已提交文件。"""
    monkeypatch.setitem(GATE["s1"].__globals__, "show", lambda sha, path: (ROOT / path).read_bytes())


def receipt(sha: str = SHA) -> dict:
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


def test_s1_needs_every_case_and_seed_with_zero_counts_capacity_and_both_robots(worktree):
    good = receipt()
    assert GATE["s1"]([good], SHA)["status"] == "passed"
    assert GATE["s1"]([], SHA)["status"] == "missing"
    short = copy.deepcopy(good)
    short["results"].pop()
    assert GATE["s1"]([short], SHA)["missing"]
    counted = copy.deepcopy(good)
    counted["results"][0]["counts"]["false_success"] = 1
    assert GATE["s1"]([counted], SHA)["status"] == "failed"
    starved = copy.deepcopy(good)
    starved["results"][3]["capacity"]["verdict"]["sufficient"] = False
    assert GATE["s1"]([starved], SHA)["status"] == "failed"
    foreign = copy.deepcopy(good)
    foreign["certificate_fingerprints"] = {"ca": "x", "service": "x", "robot": "x", "robot-tls-uav_02": "x"}
    assert GATE["s1"]([foreign], SHA)["status"] == "failed"
    assert GATE["s1"]([receipt("b" * 40)], SHA)["status"] == "failed"
    aircraft_only = copy.deepcopy(good)
    for row in aircraft_only["results"]:
        row["flown"]["ugv_01"] = []
    assert GATE["s1"]([aircraft_only], SHA)["status"] == "failed"


def test_the_scope_leaves_the_desk_alone_and_requires_the_onboard_regressions(monkeypatch):
    changed = ["src/drone_agent/guardian/core.py", "src/drone_agent/fleet/handoff.py", ".github/workflows/checks.yml"]

    def git(*args):
        return SHA if args[0] == "rev-parse" else "\n".join(changed)

    monkeypatch.setitem(GATE["scope"].__globals__, "git", git)
    monkeypatch.setitem(GATE["scope"].__globals__, "ancestor", lambda older, newer: True)
    result = GATE["scope"](SHA)
    assert result["status"] == "passed" and result["desk_changed"] == []
    assert result["onboard_changed"] == ["src/drone_agent/guardian/core.py"]
    assert result["regressions_required"] == ["m1", "m2_e2e", "m3_suite"]
    assert result["approved_outside_code"] == [".github/workflows/checks.yml"]
    changed.append("sim/compose.desk.yaml")
    assert GATE["scope"](SHA)["status"] == "failed"
    monkeypatch.setitem(GATE["scope"].__globals__, "ancestor", lambda older, newer: False)
    assert GATE["scope"](SHA)["status"] == "failed"
