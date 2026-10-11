"""The planner choice gate's criteria (D080, D081) on hand-made receipts; Git reads are injected (the cloud checks image
has no .git).

规划提示选择门禁判据（D080、D081），用手工回执检验；Git 读取经注入（云端检查镜像没有 .git）。
"""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
D080, D081 = GATE["COMPARISONS"]["D080"], GATE["COMPARISONS"]["D081"]
CANDIDATE, REGISTERED = "a" * 40, "c" * 40


def held_out(prompt: str, label: str, wrong: int, started: str, corpus: str) -> dict:
    intents = ["caught"] * wrong + ["accepted"] * (2 - wrong)
    return {"corpus": corpus, "mode": "live", "planner_prompt": prompt, "label": label, "status": "passed",
            "skipped": [], "cases": 2, "escapes": 0, "software_revision": CANDIDATE, "corpus_sha256": "digest",
            "recordings": {"one.json": "x", "two.json": "y"}, "duration_s": 1.0, "started_at": started,
            "report": {"results": [{"id": f"c{i}", "intent": x, "planned": None, "outcome": "refused",
                                   "authorized_package": False} for i, x in enumerate(intents)]}}


def found(cmp, current=(2, 2), candidate=(0, 1)) -> dict:
    runs = {}
    for i, (corpus, prompt, label) in enumerate(cmp.runs):
        rounds = {cmp.current: current, cmp.candidate: candidate}[prompt]
        wrong = rounds[0 if label == "r1" else 1] if corpus == cmp.held_out else 0
        runs[(corpus, prompt, label)] = held_out(prompt, label, wrong, f"2026-10-11T0{i}:00:00Z", corpus)
    return runs


def inject(monkeypatch, function, default="planner-v1"):
    scope = GATE[function].__globals__
    order = {(REGISTERED, CANDIDATE), (CANDIDATE, CANDIDATE), (REGISTERED, REGISTERED)}
    monkeypatch.setitem(scope, "between", lambda revision, start, end: (start, revision) in order and
                        (revision, end) in order)
    monkeypatch.setitem(scope, "expected_cases", lambda cmp, sha, corpus: 2)
    monkeypatch.setitem(scope, "digest", lambda data: "digest")
    monkeypatch.setitem(scope, "show", lambda sha, path: f'DEFAULT_PROMPT = "{default}"\n'.encode())


def test_each_comparison_runs_its_own_prompts_and_corpora_in_the_pre_registered_order():
    assert D080.runs == (("plan_ops_v2", "planner-v1", "r1"), ("plan_ops_v2", "planner-v2", "r1"),
                         ("plan_ops_v2", "planner-v1", "r2"), ("plan_ops_v2", "planner-v2", "r2"),
                         ("nl_v1", "planner-v2", "r1"), ("plan_ops_v1", "planner-v2", "r1"))
    assert D081.runs[:2] == (("plan_ops_v3", "planner-v2", "r1"), ("plan_ops_v3", "planner-v3", "r1"))
    assert D081.development_run == ("plan_ops_v2", "planner-v3", "r1")
    assert set(D081.corpora) == {"plan_ops_v3", "plan_ops_v2", "nl_v1"}
    assert D081.regressions and not D080.regressions
    assert "src/drone_agent/fleet/service.py" in D081.paths and "src/drone_agent/fleet/service.py" not in D080.paths


def test_live_runs_need_the_pre_registered_prompt_round_and_order(monkeypatch):
    inject(monkeypatch, "live_runs")
    runs = found(D080)
    assert GATE["live_runs"](D080, runs, CANDIDATE, REGISTERED)["status"] == "passed"
    swapped = dict(runs)
    first, second = D080.runs[0], D080.runs[1]
    swapped[first] = {**runs[first], "started_at": runs[second]["started_at"]}
    swapped[second] = {**runs[second], "started_at": runs[first]["started_at"]}
    assert "the runs did not start in the pre-registered order" in GATE["live_runs"](D080, swapped, CANDIDATE,
                                                                                     REGISTERED)["problems"]
    relabelled = {**runs, first: {**runs[first], "label": "r2"}}
    assert GATE["live_runs"](D080, relabelled, CANDIDATE, REGISTERED)["status"] == "failed"
    other_prompt = {**runs, first: {**runs[first], "planner_prompt": "planner-v2"}}
    assert GATE["live_runs"](D080, other_prompt, CANDIDATE, REGISTERED)["status"] == "failed"
    assert GATE["live_runs"](D080, {**runs, first: None}, CANDIDATE, REGISTERED)["status"] == "failed"
    assert GATE["live_runs"](D080, runs, CANDIDATE, None)["status"] == "failed", "no pre-registration"
    assert GATE["live_runs"](D081, found(D081), CANDIDATE, REGISTERED)["status"] == "passed"
    assert GATE["live_runs"](D081, runs, CANDIDATE, REGISTERED)["status"] == "failed", "D080's receipts"


def test_the_default_prompt_must_follow_the_rule(monkeypatch):
    inject(monkeypatch, "decision", default="planner-v2")
    adopted = GATE["decision"](D080, found(D080, current=(2, 2), candidate=(0, 1)), CANDIDATE)
    assert adopted["decision"] == "adopt" and adopted["status"] == "passed"
    kept = GATE["decision"](D080, found(D080, current=(2, 2), candidate=(2, 1)), CANDIDATE)
    assert kept["decision"] == "keep" and kept["status"] == "failed", "the candidate switched although v1 is kept"
    assert GATE["decision"](D081, found(D081, current=(2, 2), candidate=(2, 1)), CANDIDATE)["status"] == "passed", \
        "D081 keeps planner-v2"
    inject(monkeypatch, "decision", default="planner-v1")
    assert GATE["decision"](D080, found(D080, current=(2, 2), candidate=(2, 1)), CANDIDATE)["status"] == "passed"
    assert GATE["decision"](D080, found(D080, current=(2, 2), candidate=(0, 1)), CANDIDATE)["status"] == "failed", \
        "adopted but not switched"
    development = found(D080, current=(2, 2), candidate=(2, 1))
    development[D080.development_run] = None
    assert GATE["decision"](D080, development, CANDIDATE)["status"] == "passed", "the development run does not decide"
    deciding = found(D080)
    deciding[("nl_v1", "planner-v2", "r1")] = None
    assert GATE["decision"](D080, deciding, CANDIDATE)["status"] == "missing"
    inject(monkeypatch, "decision", default="planner-v3")
    assert GATE["decision"](D081, found(D081, current=(2, 2), candidate=(0, 1)), CANDIDATE)["status"] == "passed"


def test_the_desk_is_required_only_after_adoption(monkeypatch):
    assert GATE["desk"](D080, {"decision": "keep"}, None, None, None, [], CANDIDATE)["status"] == "passed"
    assert GATE["desk"](D080, {"decision": "no_decision"}, None, None, None, [], CANDIDATE)["status"] == "missing"
    assert GATE["desk"](D080, {"decision": "adopt"}, None, None, None, [], CANDIDATE)["status"] == "missing"
    # Activation and the two plans are the WP-P6-03 checks; here only the prompt the desk planned with is new.
    # 激活与两次规划沿用 WP-P6-03 的核对；这里新增的只是任务台规划所用的提示。
    scope = GATE["desk"].__globals__
    monkeypatch.setitem(scope, "P6R", {"desk": lambda *args: {"status": "passed"}})
    monkeypatch.setitem(scope, "P6A", {"desk_planning": lambda probes, sha: {"status": "passed"}})

    def probe(version):
        return {"planned": {"planner": {"prompt_version": version}}}

    adopted = {"decision": "adopt"}
    assert GATE["desk"](D080, adopted, {}, {}, {}, [probe("planner-v2"), probe("planner-v2@0123")], CANDIDATE)[
        "status"] == "passed"
    assert GATE["desk"](D080, adopted, {}, {}, {}, [probe("planner-v2"), probe("planner-v1")], CANDIDATE)[
        "status"] == "failed"
    assert GATE["desk"](D081, adopted, {}, {}, {}, [probe("planner-v3"), probe("planner-v3")], CANDIDATE)[
        "status"] == "passed"


def test_d080_may_change_only_the_default_prompt_line_of_the_engine(monkeypatch):
    scope = GATE["engine_change"].__globals__
    diff = ['--- a/x', '+++ b/x', '@@ -50 +50 @@', '-DEFAULT_PROMPT = "planner-v1"', '+DEFAULT_PROMPT = "planner-v2"']
    monkeypatch.setitem(scope, "git", lambda *args: "\n".join(diff))
    assert GATE["engine_change"](D080, CANDIDATE) == []
    monkeypatch.setitem(scope, "git", lambda *args: "\n".join([*diff, "+    temperature = 0.7"]))
    assert GATE["engine_change"](D080, CANDIDATE) == ["+    temperature = 0.7"]


def test_d081_reruns_the_p1_matrix_and_the_authorization_matrix(monkeypatch, tmp_path):
    scope = GATE["regression"].__globals__
    calls = []
    monkeypatch.setitem(scope, "P2", {"p1_regression": lambda sha, work: calls.append("p1") or {"status": "passed"}})
    monkeypatch.setitem(scope, "P6R", {"authz": lambda work: calls.append("authz") or {"status": "failed"}})
    assert GATE["regression"](D080, CANDIDATE, tmp_path) == {"status": "passed", "required": False,
                                                             "reason": "no planning code changed"}
    assert calls == []
    assert GATE["regression"](D081, CANDIDATE, tmp_path)["status"] == "failed" and calls == ["p1", "authz"]
