"""The desk's P1 resource entry: projects in hello, resource frames, project-bound submit, pre-claim cancel.

任务台的 P1 资源入口：hello 中的项目、资源帧、按项目提交与领取前取消。
"""

from __future__ import annotations

from drone_agent.console.mission import LocalApi, MissionConsole, scene_scope
from drone_agent.eval.p1_world import TEXTS, P1World
from tests.console.test_mission_console import ORIGIN, Socket, headers
from tests.fleet.harness import ROOT, SCENE


def desk(world: P1World, login: str) -> tuple[MissionConsole, list]:
    app = MissionConsole(LocalApi(world.service), ORIGIN, tailnet=True, scope=scene_scope(ROOT, SCENE), poll_s=0.05)
    # The desk authenticates tailnet users; the harness identity stands in for one member. / 以编排身份代表成员。
    app.identity = lambda _headers: login
    return app, headers(origin=ORIGIN, **{"tailscale-user-login": "ignored"})


async def test_members_see_their_projects_resources_and_reasons_and_can_cancel_before_the_claim(tmp_path):
    world = P1World(tmp_path / "case", ROOT, seed=7)
    app, extra = desk(world, "harness:p1-operator")
    session = Socket(app, extra)
    assert (await session.next())["type"] == "websocket.accept"
    hello = await session.next("hello")
    assert [p["project_id"] for p in hello["projects"]] == ["campus_ops", "legacy_m2"]
    assert hello["projects"][0]["robots"] == ["uav_a", "uav_b", "uav_c"]
    session.send({"type": "resources", "project_id": "campus_ops"})
    view = (await session.next("resources"))["view"]
    robot = view["sites"][0]["robots"][0]
    assert robot["eligibility"]["verdict"] == "unknown" and robot["eligibility"]["reasons"]
    session.send({"type": "resources", "project_id": "harbor_ops"})
    assert (await session.next("error"))["issue"]["code"] == "service.not_found"
    session.send({"type": "text", "rid": "r1", "text": TEXTS["red"], "volume_id": "campus_training",
                  "asset_ids": [], "project_id": "campus_ops", "robot_id": "uav_b"})
    mission = (await session.next("mission"))["view"]
    assert mission["binding"]["robot_id"] == "uav_b" and mission["dispatch"]["reservations"][0]["state"] == "reserved"
    session.send({"type": "operate", "mission_id": mission["mission"]["mission_id"], "action": "cancel",
                  "request_id": "op-cancel-0001"})
    while True:
        view = (await session.next("mission"))["view"]
        if view["mission"]["status"] == "cancelled":
            break
    assert view["dispatch"]["cancel"]["requested_by"] == "harness:p1-operator"
    assert view["dispatch"]["reservations"][0]["state"] == "released"
    session.send({"type": "maintenance", "project_id": "campus_ops", "dock_id": "dock_a", "action": "set",
                  "reason": "operators are not admins"})
    assert (await session.next("error"))["issue"]["code"] in ("auth.scope_missing", "auth.project_denied")
    await session.close()


async def test_a_viewer_sees_resources_but_cannot_submit_into_the_project(tmp_path):
    world = P1World(tmp_path / "case", ROOT, seed=7)
    app, extra = desk(world, "harness:p1-viewer")
    session = Socket(app, extra)
    assert (await session.next())["type"] == "websocket.accept"
    hello = await session.next("hello")
    assert [(p["project_id"], p["roles"]) for p in hello["projects"]][0] == ("campus_ops", ["viewer"])
    session.send({"type": "text", "rid": "r1", "text": TEXTS["red"], "volume_id": "campus_training",
                  "asset_ids": [], "project_id": "campus_ops", "robot_id": "uav_a"})
    assert (await session.next("error"))["issue"]["code"] in ("auth.project_denied", "auth.scope_missing")
    await session.close()
