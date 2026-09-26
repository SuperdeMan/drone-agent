"""The desk's P3 scheduling entry: the queue with its reasons, task submission and cancel, role and project checks.

任务台的 P3 调度入口：带原因的队列、任务单提交与取消、角色与项目检查。
"""

from __future__ import annotations

from drone_agent.console.mission import LocalApi, MissionConsole, scene_scope
from drone_agent.eval.p3_world import OPERATOR, VIEWER, P3World
from tests.console.test_mission_console import ORIGIN, Socket, headers
from tests.fleet.harness import ROOT, SCENE


def desk(world: P3World, login: str) -> tuple[MissionConsole, dict]:
    app = MissionConsole(LocalApi(world.service), ORIGIN, tailnet=True, scope=scene_scope(ROOT, SCENE), poll_s=0.05)
    app.identity = lambda _headers: login
    return app, headers(origin=ORIGIN, **{"tailscale-user-login": "ignored"})


async def until_task(session: Socket, world: P3World, predicate) -> dict:
    """Step the world while reading task frames until one matches. / 步进世界并读取任务单帧，直到满足条件。"""
    for _ in range(200):
        await world.step()
        try:
            view = (await session.next("task", timeout=0.2))["view"]
        except TimeoutError:
            continue
        if predicate(view):
            return view
    raise AssertionError("no matching task frame")


async def test_an_operator_submits_a_task_and_sees_who_was_chosen_and_why(tmp_path):
    world = P3World(tmp_path / "case", ROOT, seed=7)
    await world.settle()
    app, extra = desk(world, OPERATOR)
    session = Socket(app, extra)
    assert (await session.next())["type"] == "websocket.accept"
    hello = await session.next("hello")
    assert {p["project_id"]: p["scheduling"] for p in hello["projects"]}["campus_ops"] is True
    session.send({"type": "tasks_watch", "project_id": "campus_ops"})
    view = (await session.next("tasks"))["view"]
    assert view["roles"] == ["approver", "operator"]
    assert view["assets"]["asset_mid"]["robots"] == ["uav_a", "uav_b"]
    session.send({"type": "task_submit", "project_id": "campus_ops", "asset_id": "asset_mid",
                  "volume_id": "campus_training", "candidates": [], "priority": 0, "request_id": "ui-0001"})
    detail = (await session.next("task"))["view"]
    assert detail["task"]["requested_by"] == OPERATOR and detail["task"]["state"] == "queued"
    detail = await until_task(session, world, lambda v: v["task"]["state"] == "assigned")
    decision = detail["decisions"][0]
    assert decision["verdict"] == "assign" and decision["robot_id"] == "uav_a"
    assert all(c["reasons"] for c in decision["candidates"] if c["verdict"] != "eligible")
    assert detail["assignments"][0]["mission_status"] == "awaiting_approval"
    session.send({"type": "task_cancel", "project_id": "campus_ops", "task_id": detail["task"]["task_id"],
                  "request_id": "ui-0002", "reason": "desk test"})
    detail = await until_task(session, world, lambda v: v["task"]["cancel"] is not None)
    assert detail["task"]["cancel"]["requested_by"] == OPERATOR
    await session.close()
    world.ledger.close()


async def test_viewers_cannot_submit_and_other_projects_stay_hidden(tmp_path):
    world = P3World(tmp_path / "case", ROOT, seed=7)
    app, extra = desk(world, VIEWER)
    session = Socket(app, extra)
    assert (await session.next())["type"] == "websocket.accept"
    await session.next("hello")
    session.send({"type": "task_submit", "project_id": "campus_ops", "asset_id": "asset_mid",
                  "volume_id": "campus_training", "candidates": [], "priority": 0, "request_id": "ui-9"})
    assert (await session.next("error"))["issue"]["code"] == "auth.project_denied"
    session.send({"type": "tasks_watch", "project_id": "harbor_ops"})
    assert (await session.next("error"))["issue"]["code"] == "service.not_found"
    session.send({"type": "task_watch", "project_id": "campus_ops", "task_id": "tk-missing"})
    assert (await session.next("error"))["issue"]["code"] == "service.not_found"
    await session.close()
    assert world.tasks.tasks() == []
    world.ledger.close()
