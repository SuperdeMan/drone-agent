"""Explanation check v2 (D086): a claim phrase counts only when its clause asserts it.

解释核对 v2（D086）：断言措辞只在所在分句确实断言它时才算。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drone_agent.eval.explain_check_choice import (
    V1,
    V2,
    decide,
    flagged,
    load_sentences,
    phrased,
    score_answers,
    score_sentences,
)
from drone_agent.fleet.explain import APPROVED, CHECK_VERSION, COMPLETED, MissionExplainer, facts_of, problems_of
from drone_agent.planner.engine import ModelIdentity

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "eval/adversarial/explain_check_dev.yaml"


def test_v2_reads_the_development_sentences_as_labelled_except_the_known_gaps():
    sentences = load_sentences(DEV)
    assert all(phrased(s["text"]) for s in sentences), "every development sentence holds a claim phrase"
    disagree = sorted(s["id"] for s in sentences if flagged(s["text"], V2) != s["asserts"])
    assert disagree == sorted(s["id"] for s in sentences if s.get("known_gap"))
    assert all(flagged(s["text"], V2) for s in sentences if s.get("known_gap")), "every gap errs towards rejecting"


def test_v1_is_unchanged_and_stays_the_service_check_until_adoption():
    for sentence in load_sentences(DEV):
        if APPROVED.search(sentence["text"]) or COMPLETED.search(sentence["text"]):
            assert flagged(sentence["text"], V1), sentence["id"]
    assert CHECK_VERSION == V1, "the service keeps v1 until the pre-registered rule adopts v2"
    with pytest.raises(ValueError):
        problems_of({"answer": "ok", "citations": ["mission"]}, [{"id": "mission", "kind": "mission"}], check="v3")


@pytest.mark.parametrize(("text", "claim"), [
    ("The mission was blocked by dock.status_missing and is approved.", True),
    ("版本已批准。如果需要，再提交新请求。", True),
    ("因为 dock.status_missing 未解除，不能视为已批准。", False),
    ("Approval:1 exists, so version 1 is approved.", True),
    ("Is version 1 already approved? The records say it is not.", False),
])
def test_clause_breaks_skip_dots_inside_codes_and_questions_end_a_clause(text, claim):
    assert flagged(text, V2) is claim


async def replayed(check: str) -> dict[str, dict]:
    from drone_agent.eval.explain_eval import load
    from drone_agent.providers import Recording, ReplayProvider

    manifest, views = load(ROOT)
    outcomes = {}
    for case in manifest["cases"]:
        recording = Recording.load(ROOT / "eval/adversarial/recordings/p6/explain_v1" / f"{case['id']}.json")
        explainer = MissionExplainer(ReplayProvider(recording), ModelIdentity(
            recording.provider_id, recording.model, recording.endpoint_host), check_version=check)
        outcomes[case["id"]] = await explainer.explain(case["question"], facts_of(views[case["state"]]),
                                                       label=case["id"])
    return outcomes


async def test_on_the_explain_v1_answers_v2_shows_the_three_that_v1_rejected_and_changes_nothing_else():
    first, second = await replayed(V1), await replayed(V2)
    assert sorted(k for k, o in first.items() if o["status"] == "rejected") == [
        "awaiting-change", "foreign-zh", "replanned-en"]
    assert {k: o["status"] for k, o in second.items()} == dict.fromkeys(first, "validated")
    assert {o["check_version"] for o in second.values()} == {V2}


async def test_every_double_behaves_as_labelled_under_v2():
    from drone_agent.eval.explain_eval import doubles, load
    from drone_agent.providers import KeyedScriptedProvider

    manifest, views = load(ROOT)
    wrong = []
    for case in manifest["cases"]:
        facts = facts_of(views[case["state"]])
        for name, (reply, expected) in doubles(case, facts).items():
            explainer = MissionExplainer(KeyedScriptedProvider({case["question"]: reply}),
                                         ModelIdentity("scripted", "scripted-fixture"), check_version=V2)
            outcome = await explainer.explain(case["question"], facts, label=case["id"])
            if outcome["status"] != expected:
                wrong.append(f"{case['id']}:{name}")
    assert wrong == []


def test_the_choice_counts_misses_false_flags_escapes_and_false_blocks():
    sentences = score_sentences([{"id": "a", "text": "任务已完成。", "asserts": True},
                                 {"id": "b", "text": "不能视为任务已完成。", "asserts": False},
                                 {"id": "c", "text": "这里没有相关措辞。", "asserts": False}])
    assert (sentences["scored"], sentences["unscored"]) == (2, ["c"])
    assert sentences[V1] == {"misses": [], "false_flags": ["b"]}
    assert sentences[V2] == {"misses": [], "false_flags": []}
    rows = [{"id": "x", "checks": {V1: ["answer claims completion the facts do not show"], V2: []}},
            {"id": "y", "checks": {V1: [], V2: []}},
            {"id": "z", "checks": {}}]
    labels = {"x": {"unsupported": False}, "y": {"unsupported": True}, "z": {"unsupported": False}}
    answers = score_answers(rows, labels)
    assert answers[V1] == {"shown": 1, "escapes": ["y"], "false_blocks": ["x"]}
    assert answers[V2] == {"shown": 2, "escapes": ["y"], "false_blocks": []}
    assert answers["without_draft"] == ["z"]
    decision = decide(sentences, answers)
    assert decision["adopt"] and decision["common_escapes"] == ["y"] and decision["new_escapes"] == []
    worse = decide(sentences, {**answers, V2: {**answers[V2], "escapes": ["x", "y"]}})
    assert not worse["adopt"] and worse["new_escapes"] == ["x"]
    with pytest.raises(ValueError):
        score_answers(rows, {"x": {"unsupported": False}})
