"""The pre-registered D084 rule on synthetic summaries shaped like the committed suites.

预登记的 D084 规则在按已提交场景集构造的合成汇总上的表现。
"""

from __future__ import annotations

import copy

import pytest

from drone_agent.eval.airspace_choice import LONG_RUNG, decide, expected_cases, rungs, thresholds

SHA = "a" * 40


def summary(kind: str, footprint: str, *, wait: float = 0.0, drain: float = 0.0, sha: str = SHA, **row) -> dict:
    """Every case of the `kind` suite with the same numbers; `row` overrides fields of each result.

    `kind` 场景集的每个用例取相同数值；`row` 覆盖每个结果的字段。
    """
    results = [{"scenario": scenario, "seed": seed, "passed": True, "counts": {"lost_task": 0},
                "containment": {"violations": 0},
                "metrics": {"airspace_wait_s": {"total": wait}, "ladder": {"duration_s": drain}}, **row}
               for scenario, seed in sorted(expected_cases()[kind])]
    body = {"footprint": footprint, "status": "passed", "source_sha": sha, "results": results}
    return {**body, "layer": "S1"} if kind == "s1" else {**body, "part": "s0" if kind == "matrix" else "ladder"}


def frozen(noise_wait: float = 6.0) -> dict:
    return {"source_sha": SHA, "noise_wait_s": noise_wait, "noise_drain_s": 3.0, "wait_drop": 0.3,
            "noise_factor": 3.0, "long_drain": 1.05, "ladder_drain": 1.10}


def runs(*, long_wait: float = 10.0, long_drain: float = 100.0, ladder_drain: float = 50.0) -> dict:
    return {"box_long": summary("long", "box-v1", wait=40.0, drain=100.0),
            "corridor_long": summary("long", "corridor-v1", wait=long_wait, drain=long_drain),
            "box_ladder": summary("ladder", "box-v1", wait=1.0, drain=50.0),
            "corridor_ladder": summary("ladder", "corridor-v1", wait=1.0, drain=ladder_drain),
            "box_matrix": summary("matrix", "box-v1"), "corridor_matrix": summary("matrix", "corridor-v1"),
            "corridor_s1": summary("s1", "corridor-v1")}


def test_the_expected_cases_come_from_the_committed_suites():
    cases = expected_cases()
    assert cases["long"] == {(LONG_RUNG, seed) for seed in (7, 19, 41)}
    assert {scenario for scenario, _ in cases["ladder"]} == {"p3_l10", "p3_l30", "p3_l100"}
    assert len(cases["matrix"]) == 45 and len(cases["s1"]) == 5


def test_rungs_sum_over_seeds_and_a_missing_containment_is_a_violation():
    numbers = rungs(summary("long", "box-v1", wait=2.0, drain=20.0))[LONG_RUNG]
    assert numbers["wait_s"] == 6.0 and numbers["drain_s"] == 60.0 and numbers["cases"] == 3
    row = summary("long", "box-v1")
    del row["results"][0]["containment"]
    assert rungs(row)[LONG_RUNG]["violations"] == 1


def test_thresholds_come_from_two_box_runs_of_the_long_rung_on_one_commit():
    found = thresholds(summary("long", "box-v1", wait=10.0, drain=100.0),
                       summary("long", "box-v1", wait=12.0, drain=101.0))
    assert found["noise_wait_s"] == 6.0 and found["noise_drain_s"] == 3.0 and found["source_sha"] == SHA
    with pytest.raises(ValueError):
        thresholds(summary("long", "box-v1"), summary("long", "corridor-v1"))
    with pytest.raises(ValueError):
        thresholds(summary("long", "box-v1"), summary("long", "box-v1", sha="b" * 40))
    with pytest.raises(ValueError):
        thresholds(summary("long", "box-v1"), summary("ladder", "box-v1"))


def test_a_clear_drop_with_safety_everywhere_is_adopted():
    result = decide(**runs(), frozen=frozen())
    assert result["adopt"], result["criteria"]
    assert result["criteria"]["long_wait"]["at_least_s"] == 36.0 and result["criteria"]["long_wait"]["drop_s"] == 90.0


@pytest.mark.parametrize("change, criterion", [
    ({"long_wait": 30.0}, "long_wait"),        # 120 -> 90 s: below 30 %
    ({"long_drain": 110.0}, "long_drain"),     # 10 % slower on the long rung
    ({"ladder_drain": 60.0}, "ladder_drain"),  # 20 % slower on an existing rung
])
def test_each_benefit_criterion_can_reject(change, criterion):
    result = decide(**runs(**change), frozen=frozen())
    assert not result["adopt"] and result["criteria"][criterion]["status"] == "failed"


@pytest.mark.parametrize("name", ["corridor_long", "box_matrix", "corridor_s1"])
@pytest.mark.parametrize("row", [{"passed": False}, {"counts": {"lost_task": 1}}, {"containment": {"violations": 1}}])
def test_any_unsafe_case_rejects(name, row):
    inputs = runs()
    inputs[name]["results"][0].update(copy.deepcopy(row))
    result = decide(**inputs, frozen=frozen())
    assert not result["adopt"] and result["criteria"]["safety"]["status"] == "failed"


def test_noise_must_be_exceeded_three_times_not_merely_matched():
    assert not decide(**runs(), frozen=frozen(noise_wait=30.0))["adopt"]  # 3 x 30 = 90 s, dropped 90 s
    assert decide(**runs(), frozen=frozen(noise_wait=29.9))["adopt"]


@pytest.mark.parametrize("tamper", [
    lambda r: r.update(box_ladder=r["box_matrix"], corridor_ladder=r["corridor_matrix"]),  # matrices as ladders
    lambda r: r["corridor_ladder"]["results"].pop(),                                     # a rung left out
    lambda r: r["corridor_long"]["results"].pop(),                                       # a seed left out
    lambda r: r.update(box_long=r["corridor_long"], corridor_long=r["box_long"]),        # models swapped
    lambda r: r["corridor_s1"].update(footprint="box-v1"),                              # the box S1 receipt
    lambda r: r["corridor_matrix"].update(source_sha="b" * 40),                          # another commit
])
def test_inputs_that_are_not_what_they_claim_are_refused(tamper):
    inputs = runs()
    tamper(inputs)
    result = decide(**inputs, frozen=frozen())
    assert not result["adopt"] and result["criteria"]["inputs"]["status"] == "failed"


def test_noise_runs_on_another_commit_are_refused():
    result = decide(**runs(), frozen={**frozen(), "source_sha": "c" * 40})
    assert not result["adopt"] and result["criteria"]["inputs"]["status"] == "failed"
