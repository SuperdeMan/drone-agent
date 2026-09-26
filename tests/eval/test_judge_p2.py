"""The P2 judge passes real S0 cases and catches each violation it claims to count (reverse verification).

P2 裁判通过真实的 S0 用例，并能抓到它声称计数的每一类违规（反向验证）。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from drone_agent.eval.judge_p2 import judge_case
from drone_agent.eval.p2_world import run_case, suite

ROOT = Path(__file__).resolve().parents[2]


def scenario(scenario_id: str) -> dict:
    return next(s for s in suite(ROOT)["s0"] if s["id"] == scenario_id)


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    import asyncio

    base = tmp_path_factory.mktemp("p2-cases")
    found = {}
    for scenario_id in ("p2_f01_round", "p2_f07_cancel_before_dispatch"):
        result = asyncio.run(run_case(base / scenario_id, ROOT, scenario(scenario_id), 7, "test"))
        assert result["passed"], json.dumps(result, indent=1, default=str)
        found[scenario_id] = base / scenario_id
    return found


def tampered(source: Path, target: Path, change) -> Path:
    shutil.copytree(source, target)
    path = target / "service-export/workflows.json"
    tables = json.loads(path.read_text(encoding="utf-8"))
    change(tables)
    path.write_text(json.dumps(tables), encoding="utf-8")
    return target


def later(value: str, seconds: float = 5) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def test_real_cases_pass_online_and_from_the_recordings(cases):
    for case in cases.values():
        online, replay = judge_case(case, ROOT), judge_case(case, ROOT, use_replay=True)
        assert online["passed"] and replay["passed"] and not any(online["counts"].values())
        assert online["counts"] == replay["counts"]


def test_a_second_mission_for_one_activity_is_a_duplicate_dispatch(cases, tmp_path):
    def change(tables):
        first = next(r for r in tables["requests"] if r["requested_by"].startswith("workflow:"))
        tables["requests"].append({**first, "request_id": "req-forged", "mission_id": "m-forged000001",
                                   "idempotency_key": first["idempotency_key"][:-1] + "2"})
    result = judge_case(tampered(cases["p2_f01_round"], tmp_path / "case", change), ROOT)
    assert result["counts"]["duplicate_dispatch"] >= 1 and not result["passed"]


def test_work_after_a_cancel_is_counted(cases, tmp_path):
    def mission_after(tables):
        run = tables["wf_runs"][0]
        at = json.loads(run["cancel"])["requested_at"]
        tables["requests"].append({"request_id": "req-late", "requested_by": f"workflow:{run['run_id']}",
                                   "idempotency_key": f"wf:{run['run_id']}:inspect:1", "mission_id": "m-late00000001",
                                   "body": "{}", "received_at": later(at)})
        tables["missions"].append({"mission_id": "m-late00000001", "status": "awaiting_approval"})
    result = judge_case(tampered(cases["p2_f07_cancel_before_dispatch"], tmp_path / "a", mission_after), ROOT)
    assert result["counts"]["post_cancel_dispatch"] >= 1

    def node_after(tables):
        run = tables["wf_runs"][0]
        at = json.loads(run["cancel"])["requested_at"]
        node = next(n for n in tables["wf_nodes"] if n["run_id"] == run["run_id"] and n["node_id"] == "analyze")
        node["started_at"] = later(at)
    result = judge_case(tampered(cases["p2_f07_cancel_before_dispatch"], tmp_path / "b", node_after), ROOT)
    assert result["counts"]["post_cancel_successor"] >= 1

    def reported_completed(tables):
        tables["wf_runs"][0]["state"] = "completed"
    result = judge_case(tampered(cases["p2_f07_cancel_before_dispatch"], tmp_path / "c", reported_completed), ROOT)
    assert result["counts"]["post_cancel_dispatch"] >= 1


def test_a_completed_run_with_an_unverified_inspection_is_a_false_success(cases, tmp_path):
    def unknown(tables):
        node = next(n for n in tables["wf_nodes"] if n["node_id"] == "await_blue")
        node["state"], node["reason"] = "outcome_unknown", "inspection.uncertain"
    result = judge_case(tampered(cases["p2_f01_round"], tmp_path / "a", unknown), ROOT)
    assert result["counts"]["false_success"] >= 1

    def unverified(tables):
        for row in tables["verifications"]:
            body = json.loads(row["body"])
            body["final_verdict"] = "unknown"
            row["body"] = json.dumps(body)
    result = judge_case(tampered(cases["p2_f01_round"], tmp_path / "b", unverified), ROOT)
    assert result["counts"]["false_success"] >= 1


def test_orders_need_a_reviewer_confirmation_and_are_never_closed(cases, tmp_path):
    def dismissed(tables):
        tables["wf_reviews"][0]["decision"] = "dismissed"
    assert judge_case(tampered(cases["p2_f01_round"], tmp_path / "a", dismissed), ROOT)["counts"]["false_order"] >= 1

    def by_operator(tables):
        tables["wf_reviews"][0]["reviewer"] = "harness:p2-operator"
    result = judge_case(tampered(cases["p2_f01_round"], tmp_path / "b", by_operator), ROOT)
    assert result["counts"]["false_order"] >= 1 and result["counts"]["project_escape"] >= 1

    def closed(tables):
        tables["wf_work_orders"][0]["state"] = "closed"
    assert judge_case(tampered(cases["p2_f01_round"], tmp_path / "c", closed), ROOT)["counts"]["false_order"] >= 1


def test_a_started_trigger_without_its_run_is_a_lost_run(cases, tmp_path):
    def lost(tables):
        tables["wf_triggers"].append({**tables["wf_triggers"][0], "event_id": "ghost", "run_id": "wr-ghost"})
    assert judge_case(tampered(cases["p2_f01_round"], tmp_path / "a", lost), ROOT)["counts"]["lost_run"] >= 1


def test_an_accepted_probe_is_a_project_escape(cases, tmp_path):
    target = tmp_path / "case"
    shutil.copytree(cases["p2_f01_round"], target)
    transcript = target / "world/api.jsonl"
    rows = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    rows.append({**rows[0], "probe": True, "ok": True, "code": None, "method": "workflows.get"})
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert judge_case(target, ROOT)["counts"]["project_escape"] >= 1
