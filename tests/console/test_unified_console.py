"""Unified origin routing, identity and revision checks (D037). / 统一来源的路由、身份和版本检查（D037）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from drone_agent.console.unified import UnifiedConsole
from tests.console.test_mission_console import ORIGIN, console, headers, http
from tests.fleet.harness import build_loop


def unified(tmp_path):
    calls = []
    app = console(build_loop(tmp_path), source_sha="a" * 40)

    def request(value):
        calls.append(value)
        return {"source_sha": "a" * 40, "job": None, "fresh": False, "allowed_actions": []}

    backend = SimpleNamespace(request=request, fetch=lambda _job: {}, restore_pages=lambda _bridge: None,
                              health=lambda: {"runtime_source_sha": "a" * 40, "status": "ready"})
    return UnifiedConsole(app, backend), calls


async def test_modes_and_assets_share_one_origin_without_starting_flights(tmp_path):
    app, calls = unified(tmp_path)
    status, body = await http(app, "GET", "/")
    assert status == 200 and b'href="/fixed/"' in body
    status, body = await http(app, "GET", "/fixed/")
    assert status == 200 and b'src="/fixed/live.js"' in body and b'content="/fixed"' in body
    assert b'aria-current="page">' in body and b'__NAV__' not in body
    assert (await http(app, "GET", "/fixed/live.js"))[0] == 200
    assert (await http(app, "GET", "/fixed/api/state"))[0] == 200
    assert calls == [{"action": "live_status"}]
    assert (await http(app, "GET", "/fixedx/api/state"))[0] == 404


async def test_fixed_writes_need_identity_origin_and_nonce(tmp_path):
    app, calls = unified(tmp_path)
    body = json.dumps({"run_id": "20260924T100000Z-1234abcd", "seed": 7}).encode()
    valid = dict(origin=ORIGIN, content_type="application/json", x_console_nonce=app.fixed.application.nonce,
                 tailscale_user_login="operator@example.test")
    for missing in ("tailscale_user_login", "origin", "x_console_nonce"):
        values = {k: v for k, v in valid.items() if k != missing}
        assert (await http(app, "POST", "/fixed/api/start", body=body, extra=headers(**values)))[0] == 403
    assert calls == []


async def test_unified_health_fails_when_either_revision_differs(tmp_path, monkeypatch):
    app, _ = unified(tmp_path)
    monkeypatch.setenv("DRONE_SOURCE_SHA", "a" * 40)
    status, body = await http(app, "GET", "/health")
    assert status == 200 and json.loads(body)["modes"] == ["mission", "fixed"]
    app.backend.health = lambda: {"runtime_source_sha": "b" * 40, "status": "ready"}
    assert (await http(app, "GET", "/health"))[0] == 503
