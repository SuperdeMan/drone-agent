"""Mission console v0 (hri.v0) and the A2A gateway: identity, approval binding and the third-party ceiling.

任务控制台 v0（hri.v0）与 A2A 网关：身份、审批绑定与第三方权限上限。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket

import pytest

from drone_agent.console.mission import LocalApi, MissionConsole, scene_scope
from drone_agent.fleet.api import dispatch
from tests.fleet.harness import RED_REQUEST, ROOT, SCENE, build_loop

ORIGIN = "https://desk.example-tailnet.ts.net"
TOKEN = "a2a-test-token-0123456789"


def console(loop, **overrides) -> MissionConsole:
    values = dict(tailnet=True, scope=scene_scope(ROOT, SCENE),
                  clients={hashlib.sha256(TOKEN.encode()).hexdigest(): "cockpit-agent"}, poll_s=0.05)
    values.update(overrides)
    return MissionConsole(LocalApi(loop.service), ORIGIN, **values)


def headers(**extra) -> list[tuple[bytes, bytes]]:
    values = {"host": "desk.example-tailnet.ts.net", **extra}
    return [(k.replace("_", "-").encode(), v.encode()) for k, v in values.items()]


async def http(app, method, path, *, body=b"", extra=None):
    sent = []
    scope = {"type": "http", "method": method, "path": path, "headers": extra if extra is not None else headers()}
    events = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return events.pop(0)

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent[0]["status"], sent[1]["body"]


class Socket:
    """Drives one ASGI WebSocket session. / 驱动一个 ASGI WebSocket 会话。"""

    def __init__(self, app, extra):
        self.inbox, self.outbox = asyncio.Queue(), asyncio.Queue()
        scope = {"type": "websocket", "path": "/ws/session", "headers": extra}
        self.inbox.put_nowait({"type": "websocket.connect"})
        self.task = asyncio.create_task(app(scope, self.inbox.get, self.outbox.put))

    async def next(self, kind: str | None = None, timeout: float = 5) -> dict:
        while True:
            message = await asyncio.wait_for(self.outbox.get(), timeout)
            if message["type"] != "websocket.send":
                return message
            value = json.loads(message["text"])
            if kind is None or value["type"] == kind:
                return value

    def send(self, value: dict) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(value)})

    async def close(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(self.task, 5)


async def test_pages_require_the_configured_host(tmp_path):
    app = console(build_loop(tmp_path))
    assert (await http(app, "GET", "/"))[0] == 200
    assert (await http(app, "GET", "/", extra=headers(host="evil.example")))[0] == 403
    assert (await http(app, "GET", "/", extra=headers(origin="https://evil.example")))[0] == 403
    status, body = await http(app, "GET", "/.well-known/agent-card.json")
    card = json.loads(body)
    assert status == 200 and [s["id"] for s in card["skills"]] == ["mission.submit", "mission.read"]
    assert card["url"] == ORIGIN + "/a2a"


async def test_cross_origin_websockets_are_refused(tmp_path):
    session = Socket(console(build_loop(tmp_path)), headers(origin="https://evil.example"))
    assert (await session.next())["type"] == "websocket.close"


async def test_tailnet_operator_submits_approves_and_the_package_is_signed(tmp_path):
    loop = build_loop(tmp_path)
    session = Socket(console(loop), headers(origin=ORIGIN, tailscale_user_login="alice@example.test"))
    assert (await session.next())["type"] == "websocket.accept"
    hello = await session.next("hello")
    assert (hello["protocol"], hello["identity"], hello["can_write"]) == ("hri.v0", "tailnet:alice@example.test", True)
    assert [v["volume_id"] for v in hello["volumes"]] == ["campus_training", "campus_rooftop"]
    session.send({"type": "text", "rid": "r1", "text": RED_REQUEST, "volume_id": "campus_training", "asset_ids": []})
    view = (await session.next("mission"))["view"]
    version = view["versions"][0]
    assert view["mission"]["status"] == "awaiting_approval"
    assert view["request"]["requested_by"] == "tailnet:alice@example.test"
    session.send({"type": "approve", "mission_id": view["mission"]["mission_id"], "version": 1,
                  "package_hash": "0" * 64})
    assert (await session.next("error"))["issue"]["code"] == "approval.stale_version"
    session.send({"type": "approve", "mission_id": view["mission"]["mission_id"], "version": 1,
                  "package_hash": version["package_hash"]})
    view = (await session.next("mission"))["view"]
    approval = view["versions"][0]["approval"]
    assert (approval["approver"], approval["signer_key_id"]) == ("tailnet:alice@example.test", loop.key.key_id)
    assert view["mission"]["status"] == "queued"
    await session.close()


@pytest.mark.parametrize("extra", [{}, {"tailscale_user_login": ""}])
async def test_sessions_without_one_tailnet_identity_are_read_only(tmp_path, extra):
    loop = build_loop(tmp_path)
    session = Socket(console(loop), headers(origin=ORIGIN, **extra))
    await session.next()
    assert (await session.next("hello"))["can_write"] is False
    session.send({"type": "text", "rid": "r1", "text": RED_REQUEST, "volume_id": "campus_training", "asset_ids": []})
    assert (await session.next("error"))["issue"]["code"] == "auth.identity_missing"
    assert loop.ledger.missions() == []
    await session.close()


async def test_duplicated_identity_headers_are_not_trusted(tmp_path):
    app = console(build_loop(tmp_path))
    extra = headers(origin=ORIGIN) + [(b"tailscale-user-login", b"alice@example.test"),
                                      (b"tailscale-user-login", b"mallory@example.test")]
    session = Socket(app, extra)
    await session.next()
    assert (await session.next("hello"))["identity"] is None
    await session.close()


async def a2a(app, payload, token=TOKEN):
    extra = headers(authorization=f"Bearer {token}", content_type="application/json")
    status, body = await http(app, "POST", "/a2a", body=json.dumps(payload).encode(), extra=extra)
    return status, json.loads(body)


def send_message(text=RED_REQUEST, message_id="msg-0001", **metadata):
    return {"jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "messageId": message_id, "parts": [{"kind": "text", "text": text}]},
                       "metadata": {"volume_id": "campus_training", **metadata}}}


async def test_a2a_submits_for_human_approval_and_reads_only_its_own_missions(tmp_path):
    loop = build_loop(tmp_path)
    app = console(loop)
    assert (await a2a(app, send_message(), token="wrong"))[0] == 401
    status, reply = await a2a(app, send_message())
    task = reply["result"]
    assert status == 200 and task["status"]["state"] == "submitted"
    assert task["metadata"]["service_status"] == "awaiting_approval" and "versions" not in task
    mission_id = task["id"]
    status, reply = await a2a(app, {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": mission_id}})
    assert reply["result"]["id"] == mission_id
    request = loop.ledger.request(loop.ledger.mission(mission_id)["request_id"])["body"]
    assert (request["requested_by"], request["channel"], request["trust_level"]) == (
        "a2a:cockpit-agent", "a2a", "third_party")
    # Another client's mission does not exist for this caller. / 其他客户端的任务对本调用方不存在。
    other = await dispatch(loop.service, {"method": "summary", "actor": "a2a:someone-else", "trust": "third_party",
                                          "params": {"mission_id": mission_id}})
    assert other["issue"]["code"] == "service.not_found"
    for method, params in (("approve", {"mission_id": mission_id, "version": 1, "package_hash": "x"}),
                           ("operate", {"mission_id": mission_id, "action": "cancel", "request_id": "op-a2a-0001"}),
                           ("view", {"mission_id": mission_id})):
        result = await dispatch(loop.service, {"method": method, "actor": "a2a:cockpit-agent", "trust": "first_party",
                                               "params": params})
        assert result["issue"]["code"] in ("auth.scope_missing", "auth.method_not_allowed"), method


@pytest.mark.parametrize("payload,code", [
    (send_message(thrust=0.7), "auth.control_intent_rejected"),
    (send_message(route={"setpoint": [1, 2, 3]}), "auth.control_intent_rejected"),
    (send_message(approval={"approver": "me"}), "auth.control_intent_rejected"),
    (send_message(scopes=["mission.read", "flight.control"]), "auth.flight_scope_denied"),
    ({"jsonrpc": "2.0", "id": 3, "method": "tasks/cancel", "params": {"id": "m-1"}}, "auth.method_not_allowed"),
])
async def test_a2a_control_intent_is_rejected_and_audited(tmp_path, payload, code):
    loop = build_loop(tmp_path)
    status, reply = await a2a(console(loop), payload)
    assert status == 403 and reply["error"]["message"] == code
    assert loop.ledger.missions() == []
    audited = loop.ledger._rows("SELECT code, body FROM issues")
    assert [row["code"] for row in audited] == [code] and "a2a:cockpit-agent" in audited[0]["body"]


async def test_real_uvicorn_wsproto_session_says_hello(tmp_path):
    import uvicorn
    import wsproto
    from wsproto.events import AcceptConnection, Request, TextMessage

    loop = build_loop(tmp_path)
    app = MissionConsole(LocalApi(loop.service), "http://127.0.0.1:18769", tailnet=False,
                         scope=scene_scope(ROOT, SCENE), local_user="tester")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app.origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", ws="wsproto",
                                           ws_max_size=16 * 1024, http="h11", log_level="warning"))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        client = wsproto.WSConnection(wsproto.ConnectionType.CLIENT)
        writer.write(client.send(Request(host=f"127.0.0.1:{port}", target="/ws/session",
                                         extra_headers=[(b"origin", app.origin.encode())])))
        await writer.drain()
        accepted, hello = False, None
        while hello is None:
            client.receive_data(await asyncio.wait_for(reader.read(65536), 5))
            for event in client.events():
                accepted = accepted or isinstance(event, AcceptConnection)
                if isinstance(event, TextMessage) and json.loads(event.data)["type"] == "hello":
                    hello = json.loads(event.data)
        assert accepted and hello.get("identity") == "local:tester" and hello["can_write"], hello
        writer.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)


async def test_local_desk_plans_and_signs_with_a_labelled_scripted_planner(tmp_path, monkeypatch):
    from drone_agent.console.mission import local_service

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY_FILE", raising=False)
    service, label = local_service(ROOT, SCENE, tmp_path)
    try:
        assert label == "scripted"
        app = MissionConsole(LocalApi(service), "http://127.0.0.1:8769", tailnet=False,
                             scope={**scene_scope(ROOT, SCENE), "planner": label}, local_user="tester")
        session = Socket(app, [(b"host", b"127.0.0.1:8769"), (b"origin", b"http://127.0.0.1:8769")])
        await session.next()
        assert (await session.next("hello"))["planner"] == "scripted"
        session.send({"type": "text", "rid": "r1", "text": RED_REQUEST, "volume_id": "campus_training",
                      "asset_ids": []})
        view = (await session.next("mission"))["view"]
        assert view["versions"][0]["planner"]["model_id"] == "scripted-fixture"
        await session.close()
    finally:
        service.planner.tools.close()
        service.ledger.close()
