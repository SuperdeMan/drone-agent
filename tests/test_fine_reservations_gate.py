"""The D084 gate's S1 and decision criteria on hand-made receipts. / D084 门禁的 S1 与决策判据，用手工回执检验。"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_fine_reservations.py"))
SHA = "a" * 40
CASES = [("p3_s1_parallel", 7), ("p3_s1_parallel", 19), ("p3_s1_parallel", 41), ("p3_s1_contention", 7),
         ("p3_s1_relay", 7)]


def receipt(footprint: str = "box-v1", **row) -> dict:
    results = [{"scenario": scenario, "seed": seed, "passed": True, "replay_agrees": True, "judge_exit_code": 0,
                "counts": {"lost_task": 0}, "containment": {"violations": 0, "samples": 120, "min_margin_m": 1.7},
                **row} for scenario, seed in CASES]
    body = {"status": "passed", "layer": "S1", "source_sha": SHA, "results": results,
            "other_containers_before": ["x"], "other_containers_after": ["x"]}
    return {**body, "footprint": footprint} if footprint != "box-v1" else body


def test_both_s1_receipts_must_hold_every_airborne_sample_and_name_their_model():
    good = GATE["s1"](receipt(), receipt("corridor-v1"), SHA)
    assert good["status"] == "passed" and good["min_margin_m"] == {"box-v1": 1.7, "corridor-v1": 1.7}
    for box, corridor in ((receipt(containment={"violations": 1, "samples": 120}), receipt("corridor-v1")),
                          (receipt(), receipt("corridor-v1", containment={"violations": 0, "samples": 0})),
                          (receipt("corridor-v1"), receipt("corridor-v1")),    # the box receipt ran corridors
                          (receipt(), receipt()),                              # the corridor receipt ran the box
                          (receipt(), receipt("corridor-v1", passed=False))):
        assert GATE["s1"](box, corridor, SHA)["status"] == "failed"
    moved = receipt("corridor-v1")
    moved["other_containers_after"] = ["y"]
    assert GATE["s1"](receipt(), moved, SHA)["status"] == "failed"
    short = receipt("corridor-v1")
    short["results"].pop()
    assert GATE["s1"](receipt(), short, SHA)["status"] == "failed"
    assert GATE["s1"](None, receipt("corridor-v1"), SHA)["status"] == "missing"


def test_the_decision_criterion_passes_on_safety_and_reports_the_benefit_verdict(monkeypatch):
    verdict = {"adopt": False, "criteria": {"inputs": {"status": "passed"}, "safety": {"status": "passed"},
                                            "long_wait": {"status": "failed"}}}
    monkeypatch.setitem(GATE["decision"].__globals__, "decide", lambda **_: copy.deepcopy(verdict))
    runs = {name: {} for name in GATE["COMPARISONS"]}
    result = GATE["decision"](runs, {}, {})
    assert result["status"] == "passed" and result["adopt"] is False
    verdict["criteria"]["safety"]["status"] = "failed"
    assert GATE["decision"](runs, {}, {})["status"] == "failed"
    assert GATE["decision"]({**runs, "box_long": None}, {}, {})["status"] == "missing"


def test_s1_batches_of_one_model_combine_only_when_each_passed_on_an_unchanged_host():
    first, second = receipt("corridor-v1"), receipt("corridor-v1")
    first["results"], second["results"] = first["results"][:3], second["results"][3:]
    joined = GATE["combine"]([first, second])
    assert joined["status"] == "passed" and joined["footprint"] == "corridor-v1" and len(joined["results"]) == 5
    assert GATE["s1"](receipt(), joined, SHA)["status"] == "passed"
    second["other_containers_after"] = ["y"]
    assert GATE["combine"]([first, second])["status"] == "failed"
    assert GATE["combine"]([first, receipt()])["status"] == "failed"  # two models mixed / 混了两种模型
    assert GATE["combine"]([]) is None
