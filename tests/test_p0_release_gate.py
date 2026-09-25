"""Negative P0 gate cases: absent evidence, mixed revisions and overwritten history cannot pass.

P0 门禁反例：缺证据、混版本与改写历史都不能通过。
"""

from __future__ import annotations

import copy
import hashlib
import json
import runpy
from pathlib import Path

from drone_agent.eval.provenance import audit_view
from drone_agent.fleet.provenance import ModelUse, SourceContext, digest, export, seal
from drone_agent.mission.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_p0_release.py"))
SHA = "a" * 40


def source_view(source: str = "scripted") -> dict:
    """A minimal refused request: no flight or fabricated media. / 最小拒绝请求，不飞行、不编造媒体。"""
    prompt = ROOT / "src/drone_agent/planner/prompts/planner-v1.md"
    context = SourceContext(
        execution_backend="px4_sitl", run_id="test-gate", software_sha=SHA,
        scenario_sha256=hashlib.sha256((ROOT / "configs/scenarios/m2_campus_v2.yaml").read_bytes()).hexdigest(),
        platform_sha256=hashlib.sha256((ROOT / "configs/platforms/px4_sitl_multirotor.yaml").read_bytes()).hexdigest(),
        registry_sha256=Registry(ROOT, scene=ROOT / "configs/scenarios/m2_campus_v2.yaml").sha256)
    header = context.header("m-source", 1, ModelUse(source=source, provider_id="test", model_id="test-model",
                                                  input_sha256="d" * 64, outcome="refused",
                                                  prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest()))
    record = {"mission_id": "m-source", "version": 1, "decision": {"run_provenance": {"run": seal(header)}}}
    return {"mission": {"mission_id": "m-source", "status": "refused"}, "versions": [
        {"version": 1, "origin": "harness", "decision": record["decision"], "provenance": export(record)}],
        "evidence": [], "report": None}


def test_audit_rejects_wrong_revision_unknown_source_and_projection_tampering():
    view = source_view()
    assert audit_view(view, SHA, planning_source="scripted", root=ROOT)["status"] == "passed"
    assert audit_view(view, "b" * 40, planning_source="scripted", root=ROOT)["status"] == "failed"
    assert audit_view(view, SHA, planning_source="live_model", root=ROOT)["status"] == "failed"
    edited = copy.deepcopy(view)
    edited["versions"][0]["provenance"]["execution_backend"] = "real_device"
    assert audit_view(edited, SHA, planning_source="scripted", root=ROOT)["status"] == "failed"
    edited["versions"][0]["decision"]["run_provenance"] = {}
    assert audit_view(edited, SHA, planning_source="scripted", root=ROOT)["status"] == "failed"


def test_source_coverage_requires_every_judge_bound_view(tmp_path):
    assert GATE["source_views"]([], None, SHA)["status"] == "missing"
    view = source_view()
    raw = json.dumps(view).encode()
    path = tmp_path / "nl_refused-7.json"
    path.write_bytes(raw)
    row = {"scenario": "nl_refused", "seed": 7,
           "artifacts": {"service-export/view.json": hashlib.sha256(raw).hexdigest()}}
    assert GATE["source_views"]([{"results": [row]}], tmp_path, SHA)["status"] == "passed"
    path.write_bytes(raw + b"\n")
    assert GATE["source_views"]([{"results": [row]}], tmp_path, SHA)["status"] == "failed"
    assert GATE["source_views"]([{"results": [row, {"scenario": "missing", "seed": 19}]}], tmp_path, SHA)["status"] == \
        "failed"


def test_live_label_without_a_completed_independent_flight_cannot_close_p0():
    assert GATE["live_sources"]([], SHA)["status"] == "missing"
    view = source_view("live_model")
    mission = {**view["mission"], "versions": view["versions"], "evidence": [], "report": None}
    assert GATE["live_sources"]([{"mission": mission}], SHA)["status"] == "failed"
    mission["status"] = "completed"
    mission["cloud"] = {"judge": {"passed": True, "replay_agrees": True, "false_success_reports": 1, "problems": []}}
    assert GATE["live_sources"]([{"mission": mission}], SHA)["status"] == "failed"


def test_historical_sitl_pass_never_closes_hardware_or_future_products():
    history = GATE["historical_boundaries"]()
    assert history["status"] == "passed" and history["m3_sitl"] == "passed"
    assert history["m3_combined"] == "not_passed" and history["jil"] == "missing"
    assert history["counts_as_candidate_validation"] is False
    inventory = GATE["capabilities"](SHA, {"new_evidence": {"status": "missing"}})
    stages = {r["milestone"]: r for r in inventory}
    assert stages["P0"]["software_validation"] == "not_passed"
    assert stages["M3"]["status"] == "not_passed" and stages["H1"]["status"] == "missing"
    assert stages["P1"]["status"] == "planned" and stages["P5"]["software_validation"] == "missing"


def test_projection_hash_does_not_include_its_own_digest():
    source = source_view()["versions"][0]["provenance"]
    assert digest({k: v for k, v in source.items() if k != "sha256"}) == source["sha256"]
