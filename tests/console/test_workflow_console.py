"""The desk's P2 workflow entry: templates, starts, run views with reasons, role checks and inactive drafts.

任务台的 P2 工作流入口：模板、启动、带原因的运行视图、角色检查与未生效草案。
"""

from __future__ import annotations

from drone_agent.console.mission import LocalApi, MissionConsole, scene_scope
from drone_agent.eval.p2_world import DRAFT_TEXTS, OPERATOR, REVIEWER, VIEWER, P2World
from tests.console.test_mission_console import ORIGIN, Socket, headers
from tests.fleet.harness import ROOT, SCENE


def desk(world: P2World, login: str) -> tuple[MissionConsole, dict]:
    app = MissionConsole(LocalApi(world.service), ORIGIN, tailnet=True, scope=scene_scope(ROOT, SCENE), poll_s=0.05)
    app.identity = lambda _headers: login
    return app, headers(origin=ORIGIN, **{"tailscale-user-login": "ignored"})


async def test_an_operator_starts_a_run_and_sees_why_it_waits(tmp_path):
    world = P2World(tmp_path / "case", ROOT, seed=7)
    app, extra = desk(world, OPERATOR)
    session = Socket(app, extra)
    assert (await session.next())["type"] == "websocket.accept"
    await session.next("hello")
    session.send({"type": "workflows", "project_id": "campus_ops"})
    view = (await session.next("workflows"))["view"]
    assert {t["workflow_id"] for t in view["templates"]} >= {"asset_check", "campus_round"}
    assert view["roles"] == ["approver", "operator"]
    session.send({"type": "workflow_start", "project_id": "campus_ops", "workflow_id": "quick_check",
                  "request_id": "ui-0001", "inputs": {"asset": "asset_red"}})
    run = (await session.next("workflow"))["view"]
    assert run["run"]["workflow_id"] == "quick_check" and run["run"]["started_by"] == OPERATOR
    world.service.tick_workflows()
    while True:
        run = (await session.next("workflow"))["view"]
        if run["waiting"]:
            break
    assert run["waiting"] == ["approval"] and run["missions"][0]["status"] == "awaiting_approval"
    session.send({"type": "workflow_draft", "project_id": "campus_ops", "text": DRAFT_TEXTS["benign"]})
    draft = (await session.next("workflow_draft"))["result"]
    assert draft["status"] == "planned" and draft["active"] is False
    session.send({"type": "workflow_review", "project_id": "campus_ops", "run_id": run["run"]["run_id"],
                  "node_id": "analyze", "decision": "confirmed", "request_id": "ui-0002", "note": ""})
    assert (await session.next("error"))["issue"]["code"] == "auth.project_denied"
    session.send({"type": "workflow_cancel", "project_id": "campus_ops", "run_id": run["run"]["run_id"],
                  "request_id": "ui-0003", "reason": "test"})
    run = (await session.next("workflow"))["view"]
    assert run["run"]["state"] == "cancel_requested" and run["run"]["cancel"]["requested_by"] == OPERATOR
    await session.close()


async def test_viewers_and_reviewers_cannot_start_or_draft(tmp_path):
    world = P2World(tmp_path / "case", ROOT, seed=7)
    for login in (VIEWER, REVIEWER):
        app, extra = desk(world, login)
        session = Socket(app, extra)
        assert (await session.next())["type"] == "websocket.accept"
        await session.next("hello")
        session.send({"type": "workflow_start", "project_id": "campus_ops", "workflow_id": "quick_check",
                      "request_id": "ui-9", "inputs": {"asset": "asset_red"}})
        assert (await session.next("error"))["issue"]["code"] == "auth.project_denied"
        session.send({"type": "workflow_draft", "project_id": "campus_ops", "text": DRAFT_TEXTS["benign"]})
        assert (await session.next("error"))["issue"]["code"] == "auth.project_denied"
        session.send({"type": "workflows", "project_id": "harbor_ops"})
        assert (await session.next("error"))["issue"]["code"] == "service.not_found"
        await session.close()
    assert world.store.runs("campus_ops") == []
