"""The D083 gate's desk criterion on hand-made receipts. / D083 门禁任务台判据，用手工回执检验。"""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_explain.py"))


def probe(status="validated", citations=("issue:1",), summary="", prompt="explain-v1@abc", issues=("scope.foreign_reference",)):
    return {"hello": {"identity_scheme": "tailnet"},
            "mission": {"mission_id": "m-1", "status": "rejected", "issues": list(issues)},
            "explanation": {"status": status, "explanation": {"answer": "x", "citations": list(citations)}
                            if status == "validated" else None, "summary": summary, "prompt_version": prompt,
                            "fact_ids": ["mission", "issue:1", "missing:version"], "explain_s": 4.2}}


def test_a_checked_answer_or_the_summary_both_show_the_reason(monkeypatch):
    monkeypatch.setitem(GATE["desk"].__globals__, "P6R", {"desk": lambda *args: {"status": "passed"}})
    assert GATE["desk"]({}, {}, {}, probe(), "a" * 40)["status"] == "passed"
    summary = probe(status="rejected", summary="Mission status: rejected. Reason codes: scope.foreign_reference.")
    assert GATE["desk"]({}, {}, {}, summary, "a" * 40)["shown"] == "summary"
    for broken in (probe(citations=()), probe(citations=("issue:9",)), probe(status="rejected", summary="nothing"),
                   probe(prompt="planner-v3@x"), probe(issues=())):
        assert GATE["desk"]({}, {}, {}, broken, "a" * 40)["status"] == "failed", broken
    assert GATE["desk"]({}, {}, {}, None, "a" * 40)["status"] == "missing"
