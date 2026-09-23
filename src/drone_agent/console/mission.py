# Ported from embodied-agent src/embodied/hri/server.py @ 24d780a, changes: aiohttp replaced by a raw ASGI app
# served by uvicorn with wsproto (D033); voice, TTS and the scene camera dropped; the hri.v0 frames re-targeted to
# mission submit / watch / approve / decline / operate / media; the in-session danger confirm replaced by approvals
# bound to the package hash; identity from Tailscale-User-Login; A2A JSON-RPC and the Agent Card added.
"""Mission console v0 and A2A gateway: an ASGI app that only speaks to the mission-service API.

hri.v0 over `WS /ws/session` carries JSON text frames:
  down: {"type":"hello","protocol":"hri.v0","identity":...,"can_write":bool,"volumes":[...],"assets":[...]}
        {"type":"missions","items":[...]}   {"type":"mission","view":{...}}   {"type":"media",...}
        {"type":"error","message":...,"issue":{...}}
  up:   {"type":"text","rid":...,"text":...,"volume_id":...,"asset_ids":[...]}      submit a request
        {"type":"watch","mission_id":...}   {"type":"list"}   {"type":"media","mission_id":...,"evidence_id":...}
        {"type":"approve","mission_id":...,"version":N,"package_hash":...}
        {"type":"decline","mission_id":...,"version":N,"reason":...}
        {"type":"operate","mission_id":...,"action":"pause|resume|cancel","request_id":...}
Writes need an identity: `Tailscale-User-Login` injected by Tailscale Serve (the backend listens on loopback
only) or the local bridge user. Without one the session is read-only. Nothing here can reach the guardian:
approvals become signed packages in the service and operations become operator requests the aircraft checks.

A2A: `GET /.well-known/agent-card.json` and JSON-RPC 2.0 at `POST /a2a` (`message/send`, `tasks/get`) with a
bearer token whose SHA-256 is configured. External agents are third-party: they may submit requests, which
wait for human approval, and read status and reports; control-level keys, flight scopes, approvals and
operations are rejected and audited.

任务控制台 v0 与 A2A 网关：只与任务服务 API 通信的 ASGI 应用。写操作需要身份：Tailscale Serve 注入的
`Tailscale-User-Login`（后端只监听回环）或本机桥用户；没有身份的会话只读。这里没有任何路径能触达
guardian：审批在服务中变成已签名任务包，操作变成飞行器会复核的操作请求。A2A 调用方是第三方：可以提交
请求（等待人工审批）并读取状态与报告；控制级键、飞行 scope、审批与操作请求一律拒绝并记审计。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from drone_agent.console.application import Response, json_response, validate_origin
from drone_agent.contracts.mission import FORBIDDEN_PARAM_KEYS
from drone_agent.runtime.permission import ACTUATION_PREFIXES

PAGE = Path(__file__).with_name("mission.html")
MAX_FRAME = 16 * 1024
MAX_BODY = 16 * 1024
PROTOCOL = "hri.v0"
A2A_FORBIDDEN_KEYS = FORBIDDEN_PARAM_KEYS | {"approval", "approve", "approver", "package_hash", "signature",
                                             "operate", "operator_request", "lease"}
A2A_STATES = {"planning": "submitted", "awaiting_approval": "submitted", "approved": "submitted",
              "queued": "submitted", "approving": "working", "delivered": "working", "running": "working",
              "completed": "completed", "incomplete": "failed", "planning_failed": "failed", "refused": "rejected",
              "rejected": "rejected", "declined": "rejected", "delivery_rejected": "rejected"}


class LocalApi:
    """Calls `fleet.api.dispatch` in-process (tests, single-process demos). / 进程内调用 dispatch。"""

    def __init__(self, service):
        self.service = service

    async def call(self, method: str, actor: str, trust: str, **params) -> dict:
        from drone_agent.fleet.api import dispatch

        return await dispatch(self.service, {"method": method, "actor": actor, "trust": trust, "params": params})


class SocketApi:
    """Calls the mission-service API socket. / 调用任务服务 API 套接字。"""

    def __init__(self, path: Path):
        self.path = path

    async def call(self, method: str, actor: str, trust: str, **params) -> dict:
        from drone_agent.fleet.api import ApiClient

        return await ApiClient(self.path, actor=actor, trust=trust).call(method, **params)


def a2a_clients(path: Path | None) -> dict[str, str]:
    """token SHA-256 -> client id; the file never holds a token. / token 的 SHA-256 -> 客户端 ID；文件从不含 token。"""
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {entry["token_sha256"]: entry["client_id"] for entry in data.get("clients", [])}


def forbidden_content(value) -> tuple[str, str] | None:
    """(issue code, detail) when a payload carries control keys or flight scopes. / 载荷含控制键或飞行 scope 时返回。"""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, inner in item.items():
                name = str(key).lower()
                if name in A2A_FORBIDDEN_KEYS:
                    return "auth.control_intent_rejected", f"control-level key {key!r}"
                if name in ("scope", "scopes"):
                    scopes = inner if isinstance(inner, list) else [inner]
                    if any(str(s).split(".", 1)[0] in ACTUATION_PREFIXES for s in scopes):
                        return "auth.flight_scope_denied", "flight, gimbal and payload scopes are never granted"
                pending.append(inner)
        elif isinstance(item, list):
            pending.extend(item)
    return None


class Session:
    """One hri.v0 WebSocket session. / 一个 hri.v0 WebSocket 会话。"""

    def __init__(self, console: MissionConsole, identity: str, send):
        self.console, self.identity, self._send = console, identity, send
        self.trust = "first_party" if identity else "anonymous"
        self.watched: str | None = None
        self.last_view: str | None = None
        self.busy = False

    async def send(self, payload: dict) -> None:
        await self._send({"type": "websocket.send", "text": json.dumps(payload, ensure_ascii=False)})

    async def call(self, method: str, **params):
        result = await self.console.api.call(method, self.identity, self.trust, **params)
        if not result.get("ok"):
            issue = result.get("issue") or {}
            await self.send({"type": "error", "message": issue.get("message") or issue.get("code", "rejected"),
                             "issue": issue})
            return None
        return result["result"]

    async def hello(self) -> None:
        await self.send({"type": "hello", "protocol": PROTOCOL, "identity": self.identity or None,
                         "can_write": bool(self.identity), **self.console.scope})
        await self.list()

    async def list(self) -> None:
        items = await self.call("list")
        if items is not None:
            await self.send({"type": "missions", "items": items})

    async def push_view(self, force: bool = False) -> None:
        if not self.watched:
            return
        view = await self.call("view", mission_id=self.watched)
        if view is None:
            self.watched = None
            return
        text = json.dumps(view, sort_keys=True, default=str)
        if force or text != self.last_view:
            self.last_view = text
            await self.send({"type": "mission", "view": view})

    async def handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
            kind = message.get("type")
        except (ValueError, AttributeError):
            await self.send({"type": "error", "message": "bad json"})
            return
        if kind == "list":
            await self.list()
        elif kind == "watch":
            self.watched = str(message.get("mission_id", ""))
            await self.push_view(force=True)
        elif kind == "media":
            media = await self.call("media", mission_id=str(message.get("mission_id")),
                                    evidence_id=str(message.get("evidence_id")))
            if media is not None:
                await self.send({"type": "media", **media})
        elif kind == "text":
            if self.busy:
                await self.send({"type": "error", "message": "a request is still being planned"})
                return
            self.busy = True
            try:
                view = await self.call("submit", text=str(message.get("text", "")),
                                       volume_id=str(message.get("volume_id", "")),
                                       asset_ids=[str(a) for a in message.get("asset_ids") or []],
                                       idempotency_key=f"{self.identity}:{message.get('rid', '')}"[:120])
            finally:
                self.busy = False
            if view is not None:
                self.watched = view["mission"]["mission_id"]
                await self.push_view(force=True)
                await self.list()
        elif kind in ("approve", "decline", "operate"):
            params = {"approve": ("mission_id", "version", "package_hash"),
                      "decline": ("mission_id", "version", "reason"),
                      "operate": ("mission_id", "action", "request_id")}[kind]
            view = await self.call(kind, **{key: message.get(key) for key in params})
            if view is not None:
                await self.push_view(force=True)
        else:
            await self.send({"type": "error", "message": f"unknown type {kind!r}"})


class MissionConsole:
    def __init__(self, api, origin: str, *, tailnet: bool, scope: dict, local_user: str | None = None,
                 clients: dict[str, str] | None = None, poll_s: float = 1.0):
        self.api, self.origin, self.tailnet = api, validate_origin(origin, tailnet=tailnet), tailnet
        self.scope, self.local_user, self.clients, self.poll_s = scope, local_user, clients or {}, poll_s

    # ── identity and request checks / 身份与请求检查 ──

    def headers(self, scope) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for key, value in scope.get("headers", []):
            values.setdefault(key.decode("latin-1").lower(), []).append(value.decode("latin-1"))
        return values

    def rejected(self, headers: dict[str, list[str]], *, browser: bool) -> str | None:
        if headers.get("host") != [urlsplit(self.origin).netloc]:
            return "invalid host"
        origin = headers.get("origin")
        if (browser and origin != [self.origin]) or (origin and origin != [self.origin]):
            return "cross-origin access rejected"
        return None

    def identity(self, headers: dict[str, list[str]]) -> str:
        if not self.tailnet:
            return f"local:{self.local_user}" if self.local_user else ""
        login = headers.get("tailscale-user-login", [])
        # Serve injects exactly one value for tailnet users; tagged devices get none. / Serve 为 tailnet 用户注入一个值。
        return f"tailnet:{login[0]}" if len(login) == 1 and login[0] and login[0].isprintable() else ""

    # ── ASGI / ASGI 入口 ──

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            await self.http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self.websocket(scope, receive, send)

    async def respond(self, send, response: Response) -> None:
        await send({"type": "http.response.start", "status": response.status,
                    "headers": [(k.encode(), v.encode()) for k, v in response.headers]})
        await send({"type": "http.response.body", "body": response.body})

    async def http(self, scope, receive, send):
        headers, method, path = self.headers(scope), scope["method"], scope["path"]
        problem = self.rejected(headers, browser=False)
        if problem:
            return await self.respond(send, json_response(403, {"error": problem}))
        body = bytearray()
        if method == "POST":
            while True:
                event = await asyncio.wait_for(receive(), 10)
                body.extend(event.get("body", b""))
                if len(body) > MAX_BODY:
                    return await self.respond(send, json_response(413, {"error": "request too large"}))
                if not event.get("more_body"):
                    break
        if method == "GET" and path == "/":
            return await self.respond(send, Response(200, PAGE.read_bytes(), "text/html; charset=utf-8"))
        if method == "GET" and path == "/mission.js":
            return await self.respond(send, Response(200, PAGE.with_suffix(".js").read_bytes(),
                                                     "text/javascript; charset=utf-8"))
        if method == "GET" and path == "/health":
            result = await self.api.call("health", "", "anonymous")
            return await self.respond(send, json_response(200 if result.get("ok") else 503, result))
        if method == "GET" and path == "/.well-known/agent-card.json":
            return await self.respond(send, json_response(200, self.agent_card()))
        if method == "POST" and path == "/a2a":
            status, value = await self.a2a(headers, bytes(body))
            return await self.respond(send, json_response(status, value))
        return await self.respond(send, json_response(404, {"error": "not found"}))

    async def websocket(self, scope, receive, send):
        headers = self.headers(scope)
        event = await receive()
        if event["type"] != "websocket.connect":
            return
        if scope["path"] != "/ws/session" or self.rejected(headers, browser=True):
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "websocket.accept"})
        session = Session(self, self.identity(headers), send)
        await session.hello()

        async def watch():
            while True:
                await asyncio.sleep(self.poll_s)
                await session.push_view()

        watcher = asyncio.create_task(watch())
        try:
            while True:
                event = await receive()
                if event["type"] == "websocket.disconnect":
                    break
                text = event.get("text")
                if text is None or len(text) > MAX_FRAME:
                    await session.send({"type": "error", "message": "text frames up to 16 KiB only"})
                    continue
                await session.handle(text)
        finally:
            watcher.cancel()

    # ── A2A / A2A 网关 ──

    def agent_card(self) -> dict:
        return {
            "protocolVersion": "0.3.0", "name": "drone-agent mission service", "version": "m2",
            "description": "Submits inspection requests for human approval and reports their verified outcome. "
                           "It never accepts flight control, approvals or operations from agents.",
            "url": self.origin + "/a2a", "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["application/json"],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "security": [{"bearer": []}],
            "skills": [
                {"id": "mission.submit", "name": "Request an inspection",
                 "description": "Natural-language inspection request inside a registered volume; waits for human "
                                "approval before any flight.", "tags": ["inspection", "request"]},
                {"id": "mission.read", "name": "Read mission status and report",
                 "description": "Status and the three-column report of missions this client submitted.",
                 "tags": ["status", "report"]}],
        }

    def client(self, headers: dict[str, list[str]]) -> str | None:
        values = headers.get("authorization", [])
        if len(values) != 1 or not values[0].startswith("Bearer "):
            return None
        digest = hashlib.sha256(values[0][7:].encode()).hexdigest()
        found = None
        for known, client_id in self.clients.items():
            if hmac.compare_digest(known, digest):
                found = client_id
        return found

    async def a2a(self, headers, body: bytes) -> tuple[int, dict]:
        def error(code: int, message: str, data: dict | None = None, rid=None) -> dict:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message, **({"data": data}
                                                                                              if data else {})}}

        client = self.client(headers)
        if client is None:
            return 401, error(-32001, "auth.token_invalid")
        try:
            request = json.loads(body)
            rid, method, params = request.get("id"), request["method"], request.get("params") or {}
            if request.get("jsonrpc") != "2.0" or not isinstance(params, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            return 400, error(-32600, "invalid JSON-RPC request")
        actor = f"a2a:{client}"
        found = forbidden_content(params)
        if found is None and method not in ("message/send", "tasks/get"):
            found = ("auth.method_not_allowed", f"{method} is not available to external agents")
        if found is not None:
            await self.api.call("audit", actor, "third_party", code=found[0], message=found[1][:200])
            return 403, error(-32003, found[0], {"detail": found[1]}, rid)
        if method == "tasks/get":
            result = await self.api.call("summary", actor, "third_party", mission_id=str(params.get("id", "")))
        else:
            message = params.get("message") or {}
            text = " ".join(p.get("text", "") for p in message.get("parts", []) if p.get("kind") == "text").strip()
            metadata = params.get("metadata") or {}
            result = await self.api.call("submit", actor, "third_party", text=text,
                                         volume_id=str(metadata.get("volume_id", "")),
                                         asset_ids=[str(a) for a in metadata.get("asset_ids") or []],
                                         idempotency_key=str(message.get("messageId", ""))[:120])
        if not result.get("ok"):
            issue = result.get("issue") or {}
            return 200, error(-32000, issue.get("code", "rejected"), {"issue": issue}, rid)
        return 200, {"jsonrpc": "2.0", "id": rid, "result": self.task(result["result"])}

    @staticmethod
    def task(summary: dict) -> dict:
        state = A2A_STATES.get(summary["status"], "unknown")
        task = {"kind": "task", "id": summary["mission_id"], "contextId": summary["mission_id"],
                "status": {"state": state, "timestamp": summary["updated_at"],
                           "message": {"kind": "message", "role": "agent", "messageId": f"{summary['mission_id']}-status",
                                       "parts": [{"kind": "text", "text": f"mission status: {summary['status']}"}]}},
                "metadata": {"service_status": summary["status"], "issues": summary["issues"]}}
        if summary.get("report"):
            task["artifacts"] = [{"artifactId": f"{summary['mission_id']}-report", "name": "three-column report",
                                  "parts": [{"kind": "data", "data": summary["report"]}]}]
        return task


def scene_scope(root: Path, scene: Path) -> dict:
    """Volumes and assets the console offers; admission still decides. / 控制台提供的体积与资产；仍由准入决定。"""
    data = yaml.safe_load(scene.read_text(encoding="utf-8"))
    volumes = [{"volume_id": key, "airspace_mode": value.get("airspace_mode"), "description": value.get("description")}
               for key, value in data.get("volumes", {}).items()]
    assets = [{"asset_id": key, "volume": value.get("volume"), "description": value.get("description")}
              for key, value in data.get("assets", {}).items()]
    return {"volumes": volumes, "assets": assets}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--tailnet", action="store_true", help="identity from Tailscale-User-Login (D033)")
    parser.add_argument("--api", type=Path, default=Path("/run/mission/api.sock"))
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--a2a-clients", type=Path, help="JSON file with client ids and token SHA-256 values")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    console = MissionConsole(SocketApi(args.api), args.origin, tailnet=args.tailnet,
                             scope=scene_scope(args.root, args.scene),
                             local_user=None if args.tailnet else getpass.getuser(),
                             clients=a2a_clients(args.a2a_clients))
    uvicorn.run(console, host=args.host, port=args.port, workers=1, lifespan="off", proxy_headers=False,
                access_log=False, limit_concurrency=32, timeout_keep_alive=5, timeout_graceful_shutdown=10,
                server_header=False, ws="wsproto", ws_max_size=MAX_FRAME, http="h11")


if __name__ == "__main__":
    main()
