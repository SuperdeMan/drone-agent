"""Mission-service API socket and process wiring. / 任务服务 API 套接字与进程装配。"""

from __future__ import annotations

import json
import os
import sys

import pytest

from drone_agent.fleet.api import ApiClient, dispatch, serve_api
from drone_agent.fleet.main import build_planner
from drone_agent.mission.registry import Registry
from tests.fleet.harness import RED_REQUEST, ROOT, SCENE, build_loop, draft


async def test_dispatch_rejects_unknown_shapes_and_anonymous_writes(tmp_path):
    loop = build_loop(tmp_path)
    assert (await dispatch(loop.service, {"method": "health"}))["issue"]["code"] == "service.invalid_request"
    bad = await dispatch(loop.service, {"method": "submit", "actor": "tailnet:a@b", "trust": "first_party",
                                        "params": {"text": "x"}})
    assert bad["issue"]["code"] == "service.invalid_request"
    anonymous = await dispatch(loop.service, {"method": "submit", "actor": "", "trust": "first_party", "params": {
        "text": RED_REQUEST, "volume_id": "campus_training", "asset_ids": [], "idempotency_key": "k-1"}})
    assert anonymous["issue"]["code"] == "auth.identity_missing"
    health = await dispatch(loop.service, {"method": "health", "actor": "", "trust": "anonymous", "params": {}})
    assert health["ok"] and health["result"]["signer_key_id"] == loop.key.key_id
    audit = await dispatch(loop.service, {"method": "audit", "actor": "a2a:x", "trust": "third_party",
                                          "params": {"code": "planner.refused", "message": "not an auth issue"}})
    assert audit["issue"]["code"] == "service.invalid_request"


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets; exercised in the Linux checks image")
async def test_api_socket_roundtrip(tmp_path):
    loop = build_loop(tmp_path)
    path = tmp_path / "run" / "api.sock"
    server = await serve_api(loop.service, path)
    try:
        assert oct(os.stat(path).st_mode & 0o777) == "0o660"
        client = ApiClient(path, actor="harness:test-run", trust="first_party")
        submitted = await client.call("submit", text=RED_REQUEST, volume_id="campus_training", asset_ids=[],
                                      idempotency_key="idem-socket-1")
        assert submitted["ok"] and submitted["result"]["mission"]["status"] == "awaiting_approval"
        listed = await client.call("list")
        assert [m["status"] for m in listed["result"]] == ["awaiting_approval"]
    finally:
        server.close()
        await server.wait_closed()


def test_scripted_planner_mode_is_labelled_and_live_mode_reports_a_missing_key(tmp_path, monkeypatch):
    fixture = tmp_path / "fixtures.json"
    fixture.write_text(json.dumps({"format": "drone.planner.scripted/v1", "source": "scripted", "answers": {
        RED_REQUEST: {"tool_calls": [{"id": "c1", "name": "submit_mission_draft", "arguments": draft()}]}}}))

    class Args:
        root, scene, state, fixtures, planner = ROOT, SCENE, tmp_path, fixture, "scripted"

    registry = Registry(ROOT, scene=SCENE)
    planner, label = build_planner(Args, registry)
    try:
        assert label == "scripted" and planner.identity.model == "scripted-fixture"
    finally:
        planner.tools.close()
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY_FILE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    Args.planner = "live"
    planner, label = build_planner(Args, registry)
    assert planner is None and label.startswith("unavailable:") and "MINIMAX_API_KEY" in label
