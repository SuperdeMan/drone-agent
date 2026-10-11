"""The D080 gate's criteria on hand-made receipts; Git reads are injected (the cloud checks image has no .git).

D080 门禁判据，用手工回执检验；Git 读取经注入（云端检查镜像没有 .git）。
"""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
CANDIDATE, REGISTERED = "a" * 40, "c" * 40


def held_out(prompt: str, label: str, wrong: int, started: str, corpus: str = "plan_ops_v2") -> dict:
    intents = ["caught"] * wrong + ["accepted"] * (2 - wrong)
    return {"corpus": corpus, "mode": "live", "planner_prompt": prompt, "label": label, "status": "passed",
            "skipped": [], "cases": 2, "escapes": 0, "software_revision": CANDIDATE, "corpus_sha256": "digest",
            "recordings": {"one.json": "x", "two.json": "y"}, "duration_s": 1.0, "started_at": started,
            "report": {"results": [{"id": f"c{i}", "intent": x, "planned": None, "outcome": "refused",
                                   "authorized_package": False} for i, x in enumerate(intents)]}}


def found(v1=(2, 2), v2=(0, 1)) -> dict:
    runs = {}
    for i, (corpus, prompt, label) in enumerate(GATE["RUNS"]):
        wrong = {"planner-v1": v1, "planner-v2": v2}[prompt][0 if label == "r1" else 1] if corpus == "plan_ops_v2" else 0
        runs[(corpus, prompt, label)] = held_out(prompt, label, wrong, f"2026-10-11T0{i}:00:00Z", corpus)
    return runs


def inject(monkeypatch, function, default="planner-v1"):
    scope = GATE[function].__globals__
    order = {(REGISTERED, CANDIDATE), (CANDIDATE, CANDIDATE), (REGISTERED, REGISTERED)}
    monkeypatch.setitem(scope, "between", lambda revision, start, end: (start, revision) in order and
                        (revision, end) in order)
    monkeypatch.setitem(scope, "expected_cases", lambda sha, corpus: 2)
    monkeypatch.setitem(scope, "digest", lambda data: "digest")
    monkeypatch.setitem(scope, "show", lambda sha, path: f'DEFAULT_PROMPT = "{default}"\n'.encode())


def test_live_runs_need_the_pre_registered_prompt_round_and_order(monkeypatch):
    inject(monkeypatch, "live_runs")
    runs = found()
    assert GATE["live_runs"](runs, CANDIDATE, REGISTERED)["status"] == "passed"
    swapped = dict(runs)
    first, second = GATE["RUNS"][0], GATE["RUNS"][1]
    swapped[first] = {**runs[first], "started_at": runs[second]["started_at"]}
    swapped[second] = {**runs[second], "started_at": runs[first]["started_at"]}
    assert "the runs did not start in the pre-registered order" in GATE["live_runs"](swapped, CANDIDATE,
                                                                                     REGISTERED)["problems"]
    relabelled = {**runs, first: {**runs[first], "label": "r2"}}
    assert GATE["live_runs"](relabelled, CANDIDATE, REGISTERED)["status"] == "failed"
    other_prompt = {**runs, first: {**runs[first], "planner_prompt": "planner-v2"}}
    assert GATE["live_runs"](other_prompt, CANDIDATE, REGISTERED)["status"] == "failed"
    assert GATE["live_runs"]({**runs, first: None}, CANDIDATE, REGISTERED)["status"] == "failed"
    assert GATE["live_runs"](runs, CANDIDATE, None)["status"] == "failed", "no pre-registration"


def test_the_default_prompt_must_follow_the_rule(monkeypatch):
    inject(monkeypatch, "decision", default="planner-v2")
    adopted = GATE["decision"](found(v1=(2, 2), v2=(0, 1)), CANDIDATE)
    assert adopted["decision"] == "adopt" and adopted["status"] == "passed"
    kept = GATE["decision"](found(v1=(2, 2), v2=(2, 1)), CANDIDATE)
    assert kept["decision"] == "keep" and kept["status"] == "failed", "the candidate switched although v1 is kept"
    inject(monkeypatch, "decision", default="planner-v1")
    assert GATE["decision"](found(v1=(2, 2), v2=(2, 1)), CANDIDATE)["status"] == "passed"
    assert GATE["decision"](found(v1=(2, 2), v2=(0, 1)), CANDIDATE)["status"] == "failed", "adopted but not switched"
    development = found(v1=(2, 2), v2=(2, 1))
    development[GATE["DEVELOPMENT"]] = None
    assert GATE["decision"](development, CANDIDATE)["status"] == "passed", "the development run does not decide"
    deciding = found()
    deciding[("nl_v1", "planner-v2", "r1")] = None
    assert GATE["decision"](deciding, CANDIDATE)["status"] == "missing"


def test_the_desk_is_required_only_after_adoption(monkeypatch):
    assert GATE["desk"]({"decision": "keep"}, None, None, None, [], CANDIDATE)["status"] == "passed"
    assert GATE["desk"]({"decision": "no_decision"}, None, None, None, [], CANDIDATE)["status"] == "missing"
    assert GATE["desk"]({"decision": "adopt"}, None, None, None, [], CANDIDATE)["status"] == "missing"
    # Activation and the two plans are the WP-P6-03 checks; here only the prompt the desk planned with is new.
    # 激活与两次规划沿用 WP-P6-03 的核对；这里新增的只是任务台规划所用的提示。
    scope = GATE["desk"].__globals__
    monkeypatch.setitem(scope, "P6R", {"desk": lambda *args: {"status": "passed"}})
    monkeypatch.setitem(scope, "P6A", {"desk_planning": lambda probes, sha: {"status": "passed"}})

    def probe(version):
        return {"planned": {"planner": {"prompt_version": version}}}

    adopted = {"decision": "adopt"}
    assert GATE["desk"](adopted, {}, {}, {}, [probe("planner-v2"), probe("planner-v2@0123")], CANDIDATE)[
        "status"] == "passed"
    assert GATE["desk"](adopted, {}, {}, {}, [probe("planner-v2"), probe("planner-v1")], CANDIDATE)["status"] == "failed"


def test_the_engine_may_change_only_its_default_prompt(monkeypatch):
    scope = GATE["engine_change"].__globals__
    diff = ['--- a/x', '+++ b/x', '@@ -50 +50 @@', '-DEFAULT_PROMPT = "planner-v1"', '+DEFAULT_PROMPT = "planner-v2"']
    monkeypatch.setitem(scope, "git", lambda *args: "\n".join(diff))
    assert GATE["engine_change"](CANDIDATE) == []
    monkeypatch.setitem(scope, "git", lambda *args: "\n".join([*diff, "+    temperature = 0.7"]))
    assert GATE["engine_change"](CANDIDATE) == ["+    temperature = 0.7"]
