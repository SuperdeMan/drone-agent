"""Deterministic adversarial corpus: every case is blocked at the expected layer with its codes (M2 gate).

确定性对抗语料：每个用例都在期望层被拦下并带期望码（M2 门禁）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from drone_agent.eval.adversarial import CATEGORIES, run_case, run_corpus
from drone_agent.mission.registry import M2_SCENE, Registry

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "eval/adversarial/deterministic_v1.yaml"
CASES = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))["cases"]
REGISTRY = Registry(ROOT, scene=ROOT / M2_SCENE)


def test_corpus_covers_six_categories_with_at_least_five_cases_each():
    report = run_corpus(CORPUS, ROOT)
    assert set(report["per_category"]) == set(CATEGORIES)
    assert all(count >= 5 for count in report["per_category"].values()), report["per_category"]
    assert len({case["id"] for case in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_case_is_blocked_at_the_expected_layer_with_its_codes(case):
    result = run_case(case, REGISTRY)
    assert result["passed"], (result["blocked_at"], result["codes"], result["reasons"])
    assert not result["authorized_package"]


def test_the_unpatched_base_is_admitted_so_each_case_isolates_its_attack():
    # Without this, a broken base spec would make every case "pass" for the wrong reason.
    # 若基础规格本身不可准入，每个用例都会因错误原因而「通过」。
    result = run_case({"id": "control", "category": "order", "expected": {"blocked_at": "admission", "codes": []}},
                      REGISTRY)
    assert result["blocked_at"] is None and result["authorized_package"]
