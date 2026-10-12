"""The desk's read-only asset archive frames (WP-P6-04, D087): list, timeline and compare through the entry.

任务台只读的资产档案帧（WP-P6-04，D087）：经入口的列表、时间线与比较。
"""

from __future__ import annotations

from drone_agent.console.mission import LocalApi, MissionConsole, scene_scope
from drone_agent.fleet.assets import open_archive
from tests.console.test_mission_console import ORIGIN, Socket, headers
from tests.fleet.harness import ROOT, SCENE
from tests.fleet.test_asset_archive import desk_service


async def test_a_viewer_reads_the_archive_and_a_stranger_does_not(tmp_path):
    service = desk_service(tmp_path)
    open_archive(service, backups=tmp_path / "b")
    app = MissionConsole(LocalApi(service), ORIGIN, tailnet=True, scope=scene_scope(ROOT, SCENE), poll_s=0.05)
    for login, readable in (("harness:p5-viewer", True), ("harness:stranger", False)):
        app.identity = lambda _headers, login=login: login
        session = Socket(app, headers(origin=ORIGIN, **{"tailscale-user-login": "ignored"}))
        assert (await session.next())["type"] == "websocket.accept"
        await session.next("hello")
        session.send({"type": "asset_list", "project_id": "campus_s1"})
        if not readable:
            assert (await session.next("error"))["issue"]["code"] == "service.not_found"
            continue
        listed = (await session.next("asset_list"))["result"]
        asset = next(a["asset_key"] for a in listed["assets"] if a["asset_id"] == "asset_red")
        assert asset == "campus_s1/*/asset_red"
        session.send({"type": "asset_timeline", "project_id": "campus_s1", "asset": asset, "limit": 5})
        timeline = (await session.next("asset_timeline"))["result"]
        assert [i["kind"] for i in timeline["columns"]["fact"]] == ["revision_seen"]
        session.send({"type": "asset_compare", "project_id": "campus_s1", "asset": asset})
        # Nothing captured yet: an explicit refusal, never an empty comparison. / 尚无采集：明确拒绝，从不给空比较。
        assert (await session.next("error"))["issue"]["code"] == "service.not_found"
    service.business.jobs.stop()
    service.ledger.close()
