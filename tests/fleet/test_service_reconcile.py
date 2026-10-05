"""Startup reconciliation beside the API (D073, 2026-10-05): unsettled missions first, short slices, the API answers.

The 72-hour soak on 4c3de14 showed a service restart blocking its API for about a minute once the desk held hundreds
of missions: every mission was refreshed in one synchronous pass before any request was served.

与 API 并行的启动核对（D073，2026-10-05）：未定的任务优先、分短时间片，API 照常应答。4c3de14 上的 72 小时长稳
表明，任务台累积数百个任务后，服务重启会让 API 停顿约一分钟：所有任务在应答任何请求之前一次同步刷新完毕。
"""

from __future__ import annotations

import asyncio
import time

from drone_agent.fleet.api import dispatch
from drone_agent.fleet.provenance import source_context
from drone_agent.fleet.service import MissionService
from drone_agent.planner.replan import ApprovalPolicy
from tests.fleet.harness import ROOT, SCENE, build_loop, fast_registry, request, scripted_planner

HEALTH = {"method": "health", "actor": "", "trust": "anonymous", "params": {}}


def restarted(loop) -> MissionService:
    """A new service process on the same ledger and media. / 同一账本与媒体上的新服务进程。"""
    return MissionService(root=ROOT, scene=SCENE, ledger=loop.ledger, hub=loop.hub, signing_key=loop.key,
                          approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                          planner=scripted_planner(),
                          provenance_context=source_context(ROOT, SCENE, fast_registry().sha256,
                                                            backend="logical_sim"))


async def test_a_restart_queues_every_mission_with_the_unsettled_ones_first(tmp_path):
    loop = build_loop(tmp_path)
    ids = []
    for n in range(5):
        view = await loop.service.submit(request(key=f"idem-backlog-{n:04d}"))
        ids.append(view["mission"]["mission_id"])
    for mission_id, status in zip(ids, ("completed", "running", "cancelled", "approved", "awaiting_approval"),
                                  strict=True):
        loop.ledger.update_mission(mission_id, status=status)
    service = restarted(loop)
    assert service.dirty == set(), "nothing waits for the first pass of the loop"
    order = list(service.backlog)
    assert sorted(order) == sorted(ids), "every mission is still reconciled after a restart"
    assert set(order[:3]) == {ids[1], ids[3], ids[4]}, "unsettled missions go first"


async def test_the_api_answers_while_a_long_backlog_drains_and_touched_missions_go_first(tmp_path, monkeypatch):
    service = build_loop(tmp_path).service
    refreshed: list[str] = []

    def slow_refresh(mission_id: str) -> dict:
        refreshed.append(mission_id)
        time.sleep(0.02)
        return {}

    async def no_enrich(mission_id: str) -> int:
        return 0

    monkeypatch.setattr(service, "refresh", slow_refresh)
    monkeypatch.setattr(service, "enrich", no_enrich)
    service.backlog.extend(f"m-{n:012x}" for n in range(150))  # about 3 s of synchronous work / 约 3 秒同步工作
    stop = asyncio.Event()
    runner = asyncio.create_task(service.run(stop, period_s=0.05))
    try:
        await asyncio.sleep(0.1)
        started = time.monotonic()
        answer = await asyncio.wait_for(asyncio.create_task(dispatch(service, HEALTH)), 1.0)
        assert answer["ok"] and time.monotonic() - started < 0.5, "the API is served between refreshes"
        assert service.backlog, "the backlog is still draining"
        service.dirty.add("m-touchedtouched")
        await asyncio.sleep(0.6)
        assert "m-touchedtouched" in refreshed and service.backlog, "a touched mission does not wait for the backlog"
    finally:
        stop.set()
        await runner
