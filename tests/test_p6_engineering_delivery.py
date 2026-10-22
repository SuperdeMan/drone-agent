"""Exercise the engineering gate against business overclaim and incomplete CI evidence.

验证工程门禁拒绝冒称业务通过、缺失持续检查及来源不一致；仅替换 Git 读取边界。
"""
from __future__ import annotations

import copy
import runpy
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p6_engineering_delivery.py"))
CANDIDATE = "a" * 40


@pytest.fixture
def evidence(monkeypatch):
    """Use actual immutable materials and replace only Git access. / 使用实际不可变材料，只替换 Git 访问。"""
    monkeypatch.setitem(GATE["verify"].__globals__, "sources_match", lambda *args: True)
    scope = GATE["read"](ROOT / "eval/s2/road_business_v1/engineering_closeout_20261022/scope.json")
    ci = {"format": "drone.p6-github-checks/v1", "repository": "SuperdeMan/drone-agent", "head_sha": CANDIDATE,
          "tested_merge_sha": "b" * 40, "run_id": 1, "workflow": "checks", "status": "completed", "conclusion": "success",
          "steps": [{"name": name, "status": "completed", "conclusion": "success"} for name in GATE["STEPS"]]}
    return copy.deepcopy(scope), ci


def test_engineering_success_preserves_business_deferred(evidence):
    scope, ci = evidence
    result = GATE["verify"](ROOT, scope, ci, CANDIDATE)
    assert result["engineering_delivery_closed"] is True
    assert result["d089_business_accepted"] is False
    assert result["formal_independent_assets"] == result["formal_stability_pairs"] == result["formal_model_calls"] == 0


@pytest.mark.parametrize("change", ["unapproved", "business_claim", "wrong_candidate", "missing_pytest", "failed_pytest", "changed_contract"])
def test_incomplete_or_expanded_claim_cannot_close(evidence, change):
    scope, ci = evidence
    if change == "unapproved":
        scope["status"] = "proposed"
    elif change == "business_claim":
        scope["d089_business_accepted"] = True
    elif change == "wrong_candidate":
        ci["head_sha"] = "c" * 40
    elif change == "missing_pytest":
        ci["steps"] = [r for r in ci["steps"] if r["name"] != "Pytest"]
    elif change == "failed_pytest":
        next(r for r in ci["steps"] if r["name"] == "Pytest")["conclusion"] = "failure"
    else:
        scope["d089_protocol_sha256"] = "0" * 64
    result = GATE["verify"](ROOT, scope, ci, CANDIDATE)
    assert result["engineering_delivery_closed"] is False
    assert result["d089_business_accepted"] is False


def test_changed_sources_cannot_borrow_ci(evidence, monkeypatch):
    scope, ci = evidence
    monkeypatch.setitem(GATE["verify"].__globals__, "sources_match", lambda *args: False)
    result = GATE["verify"](ROOT, scope, ci, CANDIDATE)
    assert result["engineering_delivery_closed"] is False


def test_tampered_archive_leaf_cannot_close(evidence, tmp_path):
    scope, ci = evidence
    archive = GATE["ARCHIVE"]
    shutil.copytree(ROOT / archive, tmp_path / archive)
    for name in (scope["backlog_file"], "eval/s2/road_business_v1/protocol-draft.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    with (tmp_path / archive / "progress.json").open("ab") as file:
        file.write(b" ")
    result = GATE["verify"](tmp_path, scope, ci, CANDIDATE)
    assert result["engineering_delivery_closed"] is False
    assert next(c for c in result["checks"] if c["name"] == "all_archive_files_exact")["passed"] is False
