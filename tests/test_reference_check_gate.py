"""The D082 gate's criteria on hand-made receipts; Git reads are injected (the cloud checks image has no .git).

D082 门禁判据，用手工回执检验；Git 读取经注入（云端检查镜像没有 .git）。
"""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_reference_check.py"))


def foreign_probe(**changes) -> dict:
    probe = {"hello": {"identity_scheme": "tailnet"},
             "request": {"project_id": "fleet_s0", "robot_id": "uav_fb",
                         "text": "Inspect the red equipment marker at site A."},
             "planned": {"status": "rejected", "targets": [], "planner": None, "approval": None},
             "final_status": "rejected", "issues": ["scope.foreign_reference"]}
    return {**probe, **changes}


def test_the_foreign_request_must_be_rejected_before_planning():
    assert GATE["rejection"](None)["status"] == "missing"
    assert GATE["rejection"](foreign_probe())["status"] == "passed"
    planned = {"status": "awaiting_approval", "targets": ["asset_red_b"], "planner": {"prompt_version": "planner-v3"},
               "approval": None}
    for broken in (foreign_probe(planned=planned, final_status="declined"), foreign_probe(issues=[]),
                   foreign_probe(request={"project_id": "fleet_s0", "robot_id": "uav_fa",
                                          "text": "Inspect the red equipment marker at site A."}),
                   foreign_probe(hello={"identity_scheme": "local"})):
        assert GATE["rejection"](broken)["status"] == "failed", broken


def test_the_desk_needs_activation_both_plans_with_planner_v3_and_the_rejection(monkeypatch):
    scope = GATE["desk"].__globals__
    monkeypatch.setitem(scope, "P6R", {"desk": lambda *args: {"status": "passed"}})
    monkeypatch.setitem(scope, "P6A", {"desk_planning": lambda probes, sha: {"status": "passed"}})

    def plan(version):
        return {"planned": {"planner": {"prompt_version": version}}}

    good = [plan("planner-v3"), plan("planner-v3")]
    assert GATE["desk"]({}, {}, {}, good, foreign_probe(), "a" * 40)["status"] == "passed"
    assert GATE["desk"]({}, {}, {}, good, None, "a" * 40)["status"] == "missing"
    assert GATE["desk"]({}, {}, {}, [plan("planner-v2"), plan("planner-v3")], foreign_probe(), "a" * 40)[
        "status"] == "failed"


def test_scope_allows_the_check_and_refuses_onboard_changes(monkeypatch):
    scope = GATE["scope"].__globals__
    monkeypatch.setitem(scope, "ancestor", lambda older, newer: True)
    changed = ["src/drone_agent/fleet/references.py", "configs/sites/aliases/p5_desk_v1.yaml", "docs/reference-check.md"]
    monkeypatch.setitem(scope, "git", lambda *args: "\n".join(changed))
    assert GATE["scope"]("a" * 40)["status"] == "passed"
    monkeypatch.setitem(scope, "git", lambda *args: "\n".join([*changed, "src/drone_agent/guardian/supervisor.py"]))
    assert GATE["scope"]("a" * 40)["status"] == "failed"
    monkeypatch.setitem(scope, "git", lambda *args: "\n".join([*changed, "src/drone_agent/planner/engine.py"]))
    assert GATE["scope"]("a" * 40)["outside_scope"] == ["src/drone_agent/planner/engine.py"]
