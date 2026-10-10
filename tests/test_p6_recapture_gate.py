"""Negative cases of the WP-P6-02 gate (D078) and the desk's glare model.

The gate never passes a criterion without evidence, a session whose first capture was not refused for its target
region, a recapture approved before the world was cleared, or a desk without the P6 catalogs; the desk's glare disc is
the S1 one at each marker.

WP-P6-02 门禁的反例（D078）与任务台的反光模型。门禁从不放过没有证据的判据、首次采集不是因目标区域被拒的会话、在世界
清除之前就审批的补拍，或没有 P6 目录的任务台；任务台的反光圆片就是各标记处的 S1 圆片。
"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p6_recapture.py"))
SHA = "a" * 40


def session() -> dict:
    nodes = [["inspect", "submit_mission", "completed", None],
             ["analyze", "analyze_evidence", "failed", "quality.target_exposure"],
             ["recapture", "submit_mission", "completed", None],
             ["analyze_recapture", "analyze_evidence", "completed", None],
             ["select", "select_analysis", "completed", None]]
    judge = {"classification": "completed", "passed": True, "replay_agrees": True, "false_success_reports": 0}
    return {"hello": {"identity_scheme": "tailnet"}, "root_run": "wr-1",
            "runs": {"wr-1": {"run": {"run_id": "wr-1", "workflow_id": "recapture_watch", "state": "completed"},
                              "nodes": nodes}},
            "cleared": {"t": 50.0, "asset": "asset_blue", "exit_code": 0},
            "sent": [{"t": 10.0, "action": "approve"}, {"t": 60.0, "action": "approve"}],
            "missions": {"m-1": {"status": "completed", "cloud": {"judge": judge}},
                         "m-2": {"status": "completed", "cloud": {"judge": judge}}}}


def test_a_complete_recapture_session_passes():
    assert GATE["desk_session"](session(), SHA)["status"] == "passed"


def test_a_session_without_a_target_refusal_or_with_an_early_approval_fails():
    other = session()
    other["runs"]["wr-1"]["nodes"][1][3] = "quality.blurry"
    assert "the_first_capture_was_not_refused_for_its_target_region" in GATE["desk_session"](other, SHA)["problems"]
    early = session()
    early["sent"] = [{"t": 10.0, "action": "approve"}, {"t": 20.0, "action": "approve"}]
    assert "the_recapture_was_approved_before_the_world_was_cleared" in GATE["desk_session"](early, SHA)["problems"]
    unjudged = session()
    unjudged["missions"]["m-2"]["cloud"]["judge"] = {**unjudged["missions"]["m-2"]["cloud"]["judge"],
                                                     "replay_agrees": False}
    assert GATE["desk_session"](unjudged, SHA)["status"] == "failed"
    assert GATE["desk_session"](None, SHA)["status"] == "missing"


def activation() -> dict:
    parts = {name: {"catalog_id": catalog, "migration": {"status": "current"}, "drill": {"status": "passed"}}
             for name, catalog in GATE["CATALOGS"].items()}
    return {"status": "verified", "source_sha": SHA, **parts, "switch_drill": {"status": "passed"},
            "execution_backends": GATE["BACKENDS"], "dock_sessions": {d: "active" for d in GATE["DOCKS"]},
            "containers": {"desk-fleet": {"docker_access": False}}}


def test_the_desk_needs_the_p6_catalogs_on_the_candidate():
    http = {"page": {"status": 200}, "script": {"matches_checkout": True},
            "health": {"ok": True, "console_source_sha": SHA, "service_source_sha": SHA}}
    spoof = {"forged_identity_used": False}
    assert GATE["desk"](activation(), http, spoof, SHA)["status"] == "passed"
    p5 = copy.deepcopy(activation())
    p5["workflows"]["catalog_id"] = "p5_desk_v1"
    assert GATE["desk"](p5, http, spoof, SHA)["status"] == "failed"
    assert GATE["desk"](activation(), http, {"forged_identity_used": True}, SHA)["status"] == "failed"
    assert GATE["desk"](None, http, spoof, SHA)["status"] == "missing"


def test_s1_receipts_of_another_revision_or_without_isolation_fail(monkeypatch):
    from drone_agent.eval.judge_p6 import COUNTS

    # Only the git read is injected (the cloud checks image has no .git). / 只注入 Git 读取（云端检查镜像没有 .git）。
    suite = (ROOT / "configs/scenarios/p6_suite.yaml").read_bytes()
    monkeypatch.setitem(GATE["s1"].__globals__, "show", lambda sha, path: suite)
    row = {"scenario": "p6_s1_glare_persists", "seed": 7, "passed": True, "replay_agrees": True,
           "judge_exit_code": 0, "problems": [], "counts": dict.fromkeys(COUNTS, 0),
           "flown_versions": ["m-1-v1", "m-2-v1"]}
    good = {"source_sha": SHA, "status": "passed", "layer": "S1", "results": [row], "signer_key_id": "k",
            "certificate_fingerprints": {"ca": 1, "service": 2, "robot": 3},
            "other_containers_before": {"sha256": "x"}, "other_containers_after": {"sha256": "x"}}
    assert GATE["s1"]([good], SHA)["problems"] == []
    assert GATE["s1"]([{**good, "source_sha": "b" * 40}], SHA)["status"] == "failed"
    assert GATE["s1"]([{**good, "other_containers_after": {"sha256": "y"}}], SHA)["status"] == "failed"
    unsafe = {**good, "results": [{**row, "counts": {**row["counts"], "excess_recapture": 1}}]}
    assert GATE["s1"]([unsafe], SHA)["status"] == "failed"
    assert GATE["s1"]([], SHA)["status"] == "missing"


def test_the_desk_glare_disc_is_the_s1_one_at_each_marker():
    from drone_agent.eval.p6_prepare import GLARE_SDF

    supervisor = runpy.run_path(str(ROOT / "scripts/desk_supervisor.py"))
    for asset, (x, y) in supervisor["MARKERS"].items():
        name, sdf = supervisor["world_model"](asset, "glare")
        assert name == f"glare_disc_{asset}"
        expected = GLARE_SDF.replace('name="glare_disc"', f'name="{name}"').replace("<pose>4 4 0.08", f"<pose>{x} {y} 0.08")
        assert sdf == expected
