"""The desk's P3 scheduling entry: the queue with its reasons, task submission and cancel, role and project checks.

任务台的 P3 调度入口：带原因的队列、任务单提交与取消、角色与项目检查。
"""

from __future__ import annotations

import yaml

from drone_agent.console.mission import LocalApi, MissionConsole, scene_scope
from drone_agent.eval.p3_world import OPERATOR, VIEWER, P3World
from tests.console.test_mission_console import ORIGIN, Socket, headers
from tests.fleet.harness import ROOT, SCENE


class DeskWorld(P3World):
    """The resident desk's robot runs PX4 SITL; this world labels its logical flights the same. / 与常驻任务台同标注。"""

    backend = "px4_sitl"


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


async def test_a_tailnet_login_submits_on_the_desk_catalogs_and_the_task_flies_to_completion(tmp_path):
    """The resident desk's catalogs and a login with `@`: the page's frames alone take a task from submission through
    the scheduler's assignment and a person's approval to a verified flight.

    常驻任务台的目录与含 `@` 的登录名：只用页面帧就把任务单从提交、调度器分配、人的审批带到经证实的飞行。
    """
    login = "tailnet:ops@example.com"
    members = tmp_path / "members.yaml"
    members.write_text(yaml.safe_dump({"format": "drone.project-members/v1", "schema_version": "0.1.0", "members": [
        {"principal": login, "project_id": "campus_s1", "roles": ["operator", "approver"]}]}), encoding="utf-8")
    world = DeskWorld(tmp_path / "case", ROOT, seed=7, catalog=ROOT / "configs/sites/p1_s1_v1.yaml", members=members,
                    scheduling=ROOT / "configs/scheduling/p3_desk_v1.yaml",
                    workflows=ROOT / "configs/workflows/p2_s1_v1.yaml")
    await world.settle()
    app, extra = desk(world, login)
    session = Socket(app, extra)
    assert (await session.next())["type"] == "websocket.accept"
    hello = await session.next("hello")
    assert {p["project_id"]: p["scheduling"] for p in hello["projects"]}["campus_s1"] is True
    submit = {"type": "task_submit", "project_id": "campus_s1", "asset_id": "asset_red", "volume_id": "campus_training",
              "candidates": [], "priority": 0, "request_id": "ui-0123456789abcdef"}
    session.send(submit)
    first = (await session.next("task"))["view"]["task"]
    session.send(submit)
    assert (await session.next("task"))["view"]["task"]["task_id"] == first["task_id"], "a retried request is idempotent"
    assert "@" not in world.tasks.task(first["task_id"])["idempotency_key"]
    detail = await until_task(session, world, lambda v: v["task"]["state"] == "assigned")
    assert detail["decisions"][0]["robot_id"] == "uav_01"
    mission_id = detail["assignments"][0]["mission_id"]
    for _ in range(100):
        await world.step()
        if world.status(mission_id) == "awaiting_approval":
            break
    session.send({"type": "watch", "mission_id": mission_id})
    view = (await session.next("mission"))["view"]
    record = next(v for v in view["versions"] if v["version"] == view["mission"]["current_version"])
    session.send({"type": "approve", "mission_id": mission_id, "version": record["version"],
                  "package_hash": record["package_hash"]})
    for _ in range(3000):
        await world.step()
        if world.task_final(first["task_id"]):
            break
    task = world.tasks.task(first["task_id"])
    assert task["state"] == "completed" and task["outcome"]["robot_id"] == "uav_01"
    await session.close()
    for uav in world.uavs.values():
        await uav.settle()
    world.ledger.close()
