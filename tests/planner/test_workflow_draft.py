"""WP-P2-07: model drafts stay narrow, inactive and inside the caller's project; injections cannot widen them.

WP-P2-07：模型草案保持窄范围、未生效且限于调用方项目；注入无法放宽它们。
"""

from __future__ import annotations

from pathlib import Path

from drone_agent.eval.workflow_adversarial import run_corpus
from drone_agent.fleet.workflow_models import WorkflowSpec
from drone_agent.planner.workflow_draft import compile_intent, intent_schema, validate_intent
from tests.fleet.test_workflow_engine import OPERATOR, REVIEWER, VIEWER, Clock, build, call

ROOT = Path(__file__).resolve().parents[2]
INTENT = {"decision": "plan", "decline_reason": "", "title": "Two markers / 两处标记", "assets": ["asset_red", "asset_blue"],
          "schedule": {"at": "09:00", "timezone": "Asia/Shanghai"}, "work_order_on_confirmed": True, "notes": ""}


async def test_the_adversarial_corpus_never_produces_an_active_or_escaping_draft():
    report = await run_corpus(ROOT)
    assert report["status"] == "passed" and report["escapes"] == 0 and report["activatable_drafts"] == 0
    assert {r["status"] for r in report["results"]} == {"failed", "refused", "planned"}
    assert all(r["source"] == "scripted" for r in report["results"])


def test_the_compiled_draft_keeps_review_before_orders_and_the_project_robot():
    spec = compile_intent(INTENT, project_id="campus_ops", robot_id="uav_a", volume_id="campus_training",
                          analyzer="scripted_fixture_v1")
    assert isinstance(spec, WorkflowSpec) and spec.workflow_id.startswith("draft_") and spec.budget.max_missions == 2
    kinds = [t.kind for t in spec.triggers]
    assert kinds == ["manual", "schedule"] and spec.trigger("daily").schedule.at == "09:00"
    assert {n.params.robot_id for n in spec.nodes if n.activity == "submit_mission"} == {"uav_a"}
    assert spec.dependencies("inspect_2") == {"await_1"} and spec.node("inspect_2").requires == "done"
    orders = [n for n in spec.nodes if n.activity == "create_work_order"]
    assert [o.when[0].node for o in orders] == ["review_1", "review_2"]
    manual = compile_intent({**INTENT, "schedule": None, "work_order_on_confirmed": False}, project_id="campus_ops",
                            robot_id="uav_a", volume_id="campus_training", analyzer="scripted_fixture_v1")
    assert [t.kind for t in manual.triggers] == ["manual"]
    assert not [n for n in manual.nodes if n.activity == "create_work_order"]


def test_the_intent_schema_is_closed_and_enumerated():
    schema = intent_schema(["asset_red", "asset_blue"])
    assert validate_intent(INTENT, schema) == []
    for change in ({"approve": True}, {"assets": ["asset_green"]}, {"assets": []},
                   {"schedule": {"at": "9:00", "timezone": "Asia/Shanghai"}},
                   {"schedule": {"at": "09:00", "timezone": "Europe/Paris"}}, {"decision": "decline"}):
        assert validate_intent({**INTENT, **change}, schema), change


async def test_the_draft_api_needs_the_draft_scope_and_its_result_cannot_start(tmp_path):
    from drone_agent.eval.p2_world import DRAFT_TEXTS, draft_planner

    service = build(tmp_path, Clock())
    service.workflow_planner = draft_planner(ROOT)
    drafted = (await call(service, OPERATOR, "workflows.draft", project_id="campus_ops",
                          text=DRAFT_TEXTS["benign"]))["result"]
    assert drafted["status"] == "planned" and drafted["active"] is False and drafted["use"]["source"] == "scripted"
    started = await call(service, OPERATOR, "workflows.start", project_id="campus_ops",
                         workflow_id=drafted["spec"]["workflow_id"], request_id="d-1", inputs={})
    assert started["issue"]["code"] == "service.not_found"
    escalated = (await call(service, OPERATOR, "workflows.draft", project_id="campus_ops",
                            text=DRAFT_TEXTS["escalate"]))["result"]
    assert escalated["status"] == "failed" and escalated["spec"] is None and escalated["active"] is False
    for actor in (VIEWER, REVIEWER):
        refused = await call(service, actor, "workflows.draft", project_id="campus_ops", text=DRAFT_TEXTS["benign"])
        assert refused["issue"]["code"] == "auth.project_denied"
    agent = await call(service, "a2a:client", "workflows.draft", trust="third_party", project_id="campus_ops",
                       text=DRAFT_TEXTS["benign"])
    assert agent["issue"]["code"] in ("auth.scope_missing", "auth.method_not_allowed")
    audit = service.workflows.store.events("drafts:campus_ops")
    assert [e["body"]["status"] for e in audit] == ["planned", "failed"]
    assert len(service.ledger._rows("SELECT * FROM wf_catalogs")) == 1, "a draft is never stored as a catalog"
