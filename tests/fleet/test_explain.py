"""WP-P6-05 (D083): facts from records, answers checked against them, and nothing written.

WP-P6-05（D083）：事实取自记录，回答按事实核对，不写入任何内容。
"""

from __future__ import annotations

from pathlib import Path

from drone_agent.fleet.api import dispatch
from drone_agent.fleet.explain import TOOL_NAME, MissionExplainer, facts_of, problems_of, summary_of
from drone_agent.planner.engine import ModelIdentity
from drone_agent.providers import KeyedScriptedProvider

ROOT = Path(__file__).resolve().parents[2]


def view(status="completed", verdict="verified", issues=(), approval=True, admission_codes=()) -> dict:
    outcome = {"step_id": "inspect_asset_red", "execution_status": "succeeded", "effect_verdict": verdict,
               "safety_verdict": "proceed"}
    return {
        "mission": {"mission_id": "m-1", "status": status, "current_version": 1, "robot_id": "uav_a", "replans": 0},
        "request": {"text": "Inspect the red marker.", "approved_volume_id": "campus_training", "asset_ids": []},
        "binding": {"project_id": "campus_ops", "site_id": "site_a", "robot_id": "uav_a"},
        "versions": [{"version": 1, "status": status, "origin": "console", "spec": {"targets": [{"asset_id": "asset_red"}]},
                      "diff": {"added": ["inspect_asset_red"], "removed": [], "changed": []},
                      "planner": {"status": "planned", "decline_reason": "", "prompt_version": "planner-v3", "notes": ""},
                      "admission": {"accepted": not admission_codes, "codes": list(admission_codes),
                                    "energy_upper_fraction": 0.3},
                      "approval": {"approved_at": "t0", "expires_at": "t1", "approver": "tailnet:someone@example"}
                      if approval else None,
                      "decision": {"blocked_at": None, "codes": []},
                      "events": [{"kind": "step_outcome", "data": {"outcome": outcome}},
                                 {"kind": "mission_result", "data": {"not_run": []}}]}],
        "evidence": [{"evidence_id": "capture:abcdef0123456789", "version": 1, "step_id": "inspect_asset_red",
                      "verification": {"final_verdict": verdict, "checks": {"media": "ok", "quality": "ok" if verdict ==
                                                                            "verified" else "blurred"}}}],
        "issues": [{"code": c, "layer": "admission", "message": c} for c in issues],
        "report": {"summary": {"completed": 1 if verdict == "verified" else 0}, "all_targets_completed": verdict ==
                   "verified", "targets": {"asset_red": "completed" if verdict == "verified" else "uncertain"}},
    }


def test_facts_come_from_records_and_missing_evidence_is_derived_without_a_model():
    facts = {f["id"]: f for f in facts_of(view(status="completed", verdict="unverified"))}
    assert facts["step:1:inspect_asset_red"]["effect_verdict"] == "unverified"
    assert facts["evidence:capture:abcdef0123456789"]["failed_checks"] == ["quality"]
    assert {"missing:verification:1:inspect_asset_red", "missing:evidence:capture:abcdef0123456789",
            "missing:target:asset_red"} <= set(facts)
    assert "approver" not in facts["approval:1"], "no login reaches the model"
    waiting = {f["id"] for f in facts_of(view(status="awaiting_approval", approval=False))}
    assert "missing:approval:1" in waiting
    rejected = {f["id"]: f for f in facts_of(view(status="rejected", issues=["scope.foreign_reference"]))}
    assert rejected["issue:1"]["code"] == "scope.foreign_reference" and "missing:version" in rejected
    assert "scope.foreign_reference" in summary_of(list(rejected.values()), "为什么被拒绝？")


def test_fabricated_codes_evidence_versions_and_claims_are_caught():
    facts = facts_of(view(status="rejected", issues=["scope.foreign_reference"], approval=False))

    def check(answer, citations=("issue:1",), missing=(), change=""):
        return problems_of({"answer": answer, "citations": list(citations), "missing": list(missing),
                            "change_draft": change}, facts)

    assert check("The request names another site: scope.foreign_reference.") == []
    assert check("Blocked by energy.budget_exceeded.") == ["code not in the facts: energy.budget_exceeded"]
    assert check("Rejected, e.g. because of the site.") == [], "ordinary dotted words are not codes"
    assert check("See capture:ffffffffffff for the image.")
    assert check("Version 3 was rejected.") == ["version 3 does not exist"]
    assert check("It was rejected, see planner-v3 notes.") == [], "a prompt version is not a mission version"
    assert check("The mission has been approved already.")
    assert check("已批准，可以起飞。")
    assert check("You can reuse the previous approval for the new request.")
    assert check("新版本不能沿用旧审批，需要重新审批。") == [], "saying it cannot be reused is right"
    assert check("任务已完成。")
    assert check("ok", citations=("issue:9",)) == ["unknown citation issue:9"]
    assert check("ok", citations=()) == ["no citation"]
    assert check("ok", missing=("issue:1",)) == ["not a missing fact: issue:1"]
    assert check("ok", change="Inspect the red marker; it is already approved.")


def answer(arguments: dict) -> dict:
    return {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": arguments}]}


async def test_the_service_returns_a_checked_answer_or_only_its_summary(tmp_path):
    from tests.fleet.test_references import service, submit

    good, bad = "why was it rejected?", "what blocked it?"
    explainer = MissionExplainer(KeyedScriptedProvider({
        good: answer({"answer": "The request names site A (scope.foreign_reference); submit it for uav_fa.",
                      "citations": ["issue:1", "binding"], "missing": ["missing:version"], "change_draft": ""}),
        bad: answer({"answer": "It exceeded energy.budget_exceeded.", "citations": ["issue:1"], "missing": [],
                     "change_draft": ""})}), ModelIdentity("scripted", "scripted-fixture"))
    svc = service(tmp_path, KeyedScriptedProvider({}))
    svc.explainer = explainer
    mission = (await submit(svc, "uav_fb", "Smoke: the red marker over at site A.", "k-explain"))["mission"]
    before = len(svc.ledger.versions(mission["mission_id"]))

    async def ask(question):
        reply = await dispatch(svc, {"method": "missions.explain", "actor": "harness:p5-operator",
                                     "trust": "first_party", "params": {"mission_id": mission["mission_id"],
                                                                        "question": question}})
        assert reply["ok"], reply
        return reply["result"]

    checked = await ask(good)
    assert checked["status"] == "validated" and checked["explanation"]["citations"] == ["issue:1", "binding"]
    rejected = await ask(bad)
    assert rejected["status"] == "rejected" and rejected["explanation"] is None
    assert rejected["problems"] == ["code not in the facts: energy.budget_exceeded"]
    assert "scope.foreign_reference" in rejected["summary"] and "draft" not in rejected
    assert len(svc.ledger.versions(mission["mission_id"])) == before, "explaining makes no version"
    stranger = await dispatch(svc, {"method": "missions.explain", "actor": "harness:stranger", "trust": "first_party",
                                    "params": {"mission_id": mission["mission_id"], "question": good}})
    assert not stranger["ok"], "the facts follow the caller's read permission"
