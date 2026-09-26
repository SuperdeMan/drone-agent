# Ported from embodied-agent src/embodied/hri/server.py @ 24d780a, changes: aiohttp replaced by a raw ASGI app
# served by uvicorn with wsproto (D033); voice, TTS and the scene camera dropped; the hri.v0 frames re-targeted to
# mission submit / watch / approve / decline / operate / media; the in-session danger confirm replaced by approvals
# bound to the package hash; identity from Tailscale-User-Login; A2A JSON-RPC and the Agent Card added.
"""Mission console v0 and A2A gateway: an ASGI app that only speaks to the mission-service API.

hri.v0 over `WS /ws/session` carries JSON text frames:
  down: {"type":"hello","protocol":"hri.v0","identity":...,"can_write":bool,"volumes":[...],"assets":[...],
         "planner":...}
        {"type":"missions","items":[...]}   {"type":"mission","view":{...,"cloud":{...}}}   {"type":"media",...}
        {"type":"host","status":{...}}      {"type":"error","message":...,"issue":{...}}
        {"type":"resources","view":{...}}   {"type":"resource","detail":{...}}     (P1 catalog mode, D055)
        {"type":"workflows","view":{...}}   {"type":"workflow","view":{...}}   {"type":"workflow_draft","result":{...}}
        {"type":"tasks","view":{...}}   {"type":"task","view":{...}}     (P3 scheduling, D059)
  up:   {"type":"text","rid":...,"text":...,"volume_id":...,"asset_ids":[...],"project_id"?,"robot_id"?}  submit
        {"type":"resources","project_id":...}   {"type":"resource","project_id":...,"resource_id":...}
        {"type":"maintenance","project_id":...,"dock_id":...,"action":"set|release","reason":...}  (admin)
        {"type":"workflows","project_id":...}   {"type":"workflow_watch","project_id":...,"run_id":...}   (P2, D057)
        {"type":"workflow_start","project_id":...,"workflow_id":...,"request_id":...,"inputs":{...}}
        {"type":"workflow_cancel","project_id":...,"run_id":...,"request_id":...,"reason":...}
        {"type":"workflow_schedule","project_id":...,"workflow_id":...,"trigger_id":...,"action":...,"reason":...}
        {"type":"workflow_review","project_id":...,"run_id":...,"node_id":...,"decision":...,"request_id":...,"note":...}
        {"type":"workflow_repair","project_id":...,"order_id":...,"request_id":...,"note":...}
        {"type":"workflow_draft","project_id":...,"text":...}
        {"type":"tasks_watch","project_id":...}   {"type":"task_watch","project_id":...,"task_id":...}   (P3, D059)
        {"type":"task_submit","project_id":...,"asset_id":...,"volume_id":...,"candidates":[...],"priority":N,
         "request_id":...}
        {"type":"task_cancel","project_id":...,"task_id":...,"request_id":...,"reason":...}
        {"type":"watch","mission_id":...}   {"type":"list"}   {"type":"media","mission_id":...,"evidence_id":...}
        {"type":"approve","mission_id":...,"version":N,"package_hash":...}
        {"type":"decline","mission_id":...,"version":N,"reason":...}
        {"type":"operate","mission_id":...,"action":"pause|resume|cancel","request_id":...}
Writes need an identity: `Tailscale-User-Login` injected by Tailscale Serve (the backend listens on loopback
only) or the local bridge user. Without one the session is read-only. Nothing here can reach the guardian:
approvals become signed packages in the service and operations become operator requests the aircraft checks.
With an operations catalog (P1) the page also lists the caller's projects and their sites, docks and robots with
status age, source and the reasons a robot cannot be dispatched; it has no fault injection or flight control. With
a workflow catalog (P2) it shows the project's templates, runs with every wait and failure reason, reviews, work
orders and drafts; a run's missions are still approved one by one in the mission view, and a draft never runs.
With a scheduling catalog (P3) it shows the task queue, each robot's assignment and preview verdict, every waiting and
excluded candidate with its reasons, and the airspace holds; the scheduler assigns, a person still approves each
assigned mission in the mission view, and nothing here chooses a robot for the scheduler.

A2A: `GET /.well-known/agent-card.json` and JSON-RPC 2.0 at `POST /a2a` (`message/send`, `tasks/get`) with a
bearer token whose SHA-256 is configured. External agents are third-party: they may submit requests, which
wait for human approval, and read status and reports; control-level keys, flight scopes, approvals and
operations are rejected and audited.

On the resident desk (D035) the page also shows the simulation supervisor's public records, read-only from
`--supervisor`: the flight host state (`host`) and, per mission, the cloud flights and the independent judge
(`view.cloud`). They are displayed, never used to decide anything here.

任务控制台 v0 与 A2A 网关：只与任务服务 API 通信的 ASGI 应用。写操作需要身份：Tailscale Serve 注入的
`Tailscale-User-Login`（后端只监听回环）或本机桥用户；没有身份的会话只读。这里没有任何路径能触达
guardian：审批在服务中变成已签名任务包，操作变成飞行器会复核的操作请求。带运营目录时（P1），页面还列出调用方
的项目及其站点、机场与机器人，附状态年龄、来源与不可派遣原因；页面没有故障注入或飞控接口。带工作流目录时（P2），页面
展示项目的模板、带全部等待与失败原因的运行、复核、工单与草案；运行的任务仍在任务视图中逐个审批，草案从不运行。带调度
目录时（P3），页面展示任务单队列、各机器人的分配与预览判定、每个等待与被排除候选及其原因，以及空域持有；由调度器分配，
每个已分配任务仍由人在任务视图中审批，这里不替调度器选机。A2A 调用方是第三方：可以提交
请求（等待人工审批）并读取状态与报告；控制级键、飞行 scope、审批与操作请求一律拒绝并记审计。

常驻任务台（D035）另从 `--supervisor` 只读展示仿真监管者的公开记录：飞行主机状态（`host`），以及每个
任务的云端飞行与独立裁判（`view.cloud`）。它们只用于展示，这里不据此做任何决定。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from drone_agent.console.application import CSP, Response, desk_navigation, json_response, validate_origin
from drone_agent.contracts.mission import FORBIDDEN_PARAM_KEYS
from drone_agent.runtime.permission import ACTUATION_PREFIXES

PAGE = Path(__file__).with_name("mission.html")
MAX_FRAME = 16 * 1024
MAX_BODY = 16 * 1024
MAX_PUBLIC_RECORD = 256 * 1024
MISSION_ID = re.compile(r"^m-[0-9a-f]{12}$")
PROTOCOL = "hri.v0"
A2A_FORBIDDEN_KEYS = FORBIDDEN_PARAM_KEYS | {"approval", "approve", "approver", "package_hash", "signature",
                                             "operate", "operator_request", "lease"}
A2A_STATES = {"planning": "submitted", "verifying": "working", "awaiting_approval": "submitted", "approved": "submitted",
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
        self.resources_project: str | None = None
        self.last_resources: str | None = None
        self.workflows_project: str | None = None
        self.last_workflows: str | None = None
        self.watched_run: tuple[str, str] | None = None
        self.last_run: str | None = None
        self.tasks_project: str | None = None
        self.last_tasks: str | None = None
        self.watched_task: tuple[str, str] | None = None
        self.last_task: str | None = None
        self.last_host: str | None = None
        self.busy = False

    async def send(self, payload: dict) -> None:
        await self._send({"type": "websocket.send", "text": json.dumps(payload, ensure_ascii=False)})

    async def call(self, method: str, **params):
        try:
            result = await self.console.api.call(method, self.identity, self.trust, **params)
        except (OSError, TimeoutError, RuntimeError, ValueError) as error:
            # Unreachable or slow service: report it; the request may still finish there. / 服务不可达或太慢：如实报告。
            await self.send({"type": "error", "message": f"mission service unavailable ({type(error).__name__}); "
                                                         "refresh the mission list later",
                             "issue": {"code": "service.degraded"}})
            return None
        if not result.get("ok"):
            issue = result.get("issue") or {}
            await self.send({"type": "error", "message": issue.get("message") or issue.get("code", "rejected"),
                             "issue": issue})
            return None
        return result["result"]

    async def hello(self) -> None:
        payload = {"type": "hello", "protocol": PROTOCOL, "identity": self.identity or None,
                   "can_write": bool(self.identity), **self.console.scope}
        if "planner" not in payload:
            health = await self.call("health")
            payload["planner"] = (health or {}).get("planner")
        payload["projects"] = await self.projects()
        await self.send(payload)
        await self.push_host(force=True)
        await self.list()

    async def push_host(self, force: bool = False) -> None:
        status = self.console.host_status()
        text = json.dumps(status, sort_keys=True)
        if force or text != self.last_host:
            self.last_host = text
            await self.send({"type": "host", "status": status})

    async def list(self) -> None:
        items = await self.call("list")
        if items is not None:
            await self.send({"type": "missions", "items": items})

    async def projects(self) -> list[dict]:
        """The caller's projects in catalog mode; none without a catalog or identity. / 目录模式下调用方的项目。"""
        if not self.identity:
            return []
        try:
            result = await self.console.api.call("projects", self.identity, self.trust)
        except (OSError, TimeoutError, RuntimeError, ValueError):
            return []
        return result.get("result") or [] if result.get("ok") else []

    async def push_resources(self, force: bool = False) -> None:
        if not self.resources_project:
            return
        view = await self.call("resources.list", project_id=self.resources_project)
        if view is None:
            self.resources_project = None
            return
        text = json.dumps(view, sort_keys=True, default=str)
        if force or text != self.last_resources:
            self.last_resources = text
            await self.send({"type": "resources", "view": view})

    async def push_workflows(self, force: bool = False) -> None:
        if not self.workflows_project:
            return
        view = await self.call("workflows.list", project_id=self.workflows_project)
        if view is None:
            self.workflows_project = None
            return
        text = json.dumps(view, sort_keys=True, default=str)
        if force or text != self.last_workflows:
            self.last_workflows = text
            await self.send({"type": "workflows", "view": view})

    async def push_run(self, force: bool = False) -> None:
        if not self.watched_run:
            return
        project_id, run_id = self.watched_run
        view = await self.call("workflows.get", project_id=project_id, run_id=run_id)
        if view is None:
            self.watched_run = None
            return
        text = json.dumps(view, sort_keys=True, default=str)
        if force or text != self.last_run:
            self.last_run = text
            await self.send({"type": "workflow", "view": view})

    async def push_tasks(self, force: bool = False) -> None:
        if not self.tasks_project:
            return
        view = await self.call("tasks.list", project_id=self.tasks_project)
        if view is None:
            self.tasks_project = None
            return
        text = json.dumps(view, sort_keys=True, default=str)
        if force or text != self.last_tasks:
            self.last_tasks = text
            await self.send({"type": "tasks", "view": view})

    async def push_task(self, force: bool = False) -> None:
        if not self.watched_task:
            return
        project_id, task_id = self.watched_task
        view = await self.call("tasks.get", project_id=project_id, task_id=task_id)
        if view is None:
            self.watched_task = None
            return
        text = json.dumps(view, sort_keys=True, default=str)
        if force or text != self.last_task:
            self.last_task = text
            await self.send({"type": "task", "view": view})

    def task_key(self, request_id: str) -> str:
        """A task key in the scheduler's safe alphabet: a digest of the caller and the page's request id, so a login
        with `@` still submits, a retried request stays idempotent and the key never carries the login.

        调度器安全字符集内的任务单键：调用方与页面请求号的摘要，因此含 `@` 的登录名也能提交，重试的请求保持幂等，键中也不含登录名。
        """
        return "desk:" + hashlib.sha256(f"{self.identity}\n{request_id}".encode()).hexdigest()[:40]

    async def task(self, kind: str, message: dict) -> None:
        """P3 frames: named API calls the service checks against the caller's project role; the scheduler assigns.

        P3 帧：由服务按调用方项目角色检查的具名 API 调用；由调度器分配。
        """
        project = str(message.get("project_id", ""))[:120]
        if kind == "tasks_watch":
            self.tasks_project = project or None
            await self.push_tasks(force=True)
            return
        if kind == "task_watch":
            self.watched_task = (project, str(message.get("task_id", ""))[:120])
            await self.push_task(force=True)
            return
        request_id = str(message.get("request_id", ""))[:80]
        if kind == "task_submit":
            candidates = message.get("candidates") if isinstance(message.get("candidates"), list) else []
            try:
                priority = int(message.get("priority", 0))
            except (TypeError, ValueError):
                priority = 0
            result = await self.call("tasks.submit", project_id=project, asset_id=str(message.get("asset_id", ""))[:120],
                                     volume_id=str(message.get("volume_id", ""))[:120],
                                     candidates=[str(c)[:120] for c in candidates[:16]], priority=priority,
                                     idempotency_key=self.task_key(request_id))
        else:
            result = await self.call("tasks.cancel", project_id=project, task_id=str(message.get("task_id", ""))[:120],
                                     request_id=request_id, reason=str(message.get("reason", ""))[:300])
        if result is not None and "task" in result:
            self.watched_task = (project, result["task"]["task_id"])
            await self.push_task(force=True)
        await self.push_tasks(force=True)

    async def workflow(self, kind: str, message: dict) -> None:
        """P2 frames: every one is a named API call the service checks against the caller's project role.

        P2 帧：每一个都是由服务按调用方项目角色检查的具名 API 调用。
        """
        project = str(message.get("project_id", ""))
        text = {key: str(message.get(key, ""))[:300] for key in ("run_id", "workflow_id", "trigger_id", "node_id",
                                                                 "order_id", "action", "decision", "reason", "note",
                                                                 "request_id")}
        if kind == "workflows":
            self.workflows_project = project or None
            await self.push_workflows(force=True)
            return
        if kind == "workflow_watch":
            self.watched_run = (project, text["run_id"])
            await self.push_run(force=True)
            return
        if kind == "workflow_draft":
            result = await self.call("workflows.draft", project_id=project, text=str(message.get("text", ""))[:2000])
            if result is not None:
                await self.send({"type": "workflow_draft", "result": result})
            return
        inputs = message.get("inputs") if isinstance(message.get("inputs"), dict) else {}
        calls = {
            "workflow_start": ("workflows.start", {"workflow_id": text["workflow_id"], "request_id": text["request_id"],
                                                   "inputs": {str(k): str(v) for k, v in inputs.items()}}),
            "workflow_cancel": ("workflows.cancel", {"run_id": text["run_id"], "request_id": text["request_id"],
                                                     "reason": text["reason"]}),
            "workflow_schedule": ("workflows.schedule", {"workflow_id": text["workflow_id"],
                                                         "trigger_id": text["trigger_id"], "action": text["action"],
                                                         "reason": text["reason"]}),
            "workflow_review": ("workflows.review", {"run_id": text["run_id"], "node_id": text["node_id"],
                                                     "decision": text["decision"], "request_id": text["request_id"],
                                                     "note": text["note"]}),
            "workflow_repair": ("workflows.repair", {"order_id": text["order_id"], "request_id": text["request_id"],
                                                     "note": text["note"]}),
        }
        method, params = calls[kind]
        result = await self.call(method, project_id=project, **params)
        if result is not None and "run" in result:
            self.watched_run = (project, result["run"]["run_id"])
            await self.push_run(force=True)
        await self.push_workflows(force=True)

    async def push_view(self, force: bool = False) -> None:
        if not self.watched:
            return
        view = await self.call("view", mission_id=self.watched)
        if view is None:
            self.watched = None
            return
        view["cloud"] = self.console.cloud_record(self.watched)
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
        elif kind == "resources":
            self.resources_project = str(message.get("project_id", "")) or None
            await self.push_resources(force=True)
        elif kind == "resource":
            detail = await self.call("resources.get", project_id=str(message.get("project_id", "")),
                                     resource_id=str(message.get("resource_id", "")))
            if detail is not None:
                await self.send({"type": "resource", "detail": detail})
        elif kind == "maintenance":
            result = await self.call("resources.maintenance", project_id=str(message.get("project_id", "")),
                                     dock_id=str(message.get("dock_id", "")), action=str(message.get("action", "")),
                                     reason=str(message.get("reason", ""))[:300])
            if result is not None:
                await self.push_resources(force=True)
        elif kind == "text":
            if self.busy:
                await self.send({"type": "error", "message": "a request is still being planned"})
                return
            self.busy = True
            common = {"text": str(message.get("text", "")), "volume_id": str(message.get("volume_id", "")),
                      "asset_ids": [str(a) for a in message.get("asset_ids") or []],
                      "idempotency_key": f"{self.identity}:{message.get('rid', '')}"[:120]}
            try:
                if message.get("project_id") and message.get("robot_id"):
                    # P1: the project and robot the operator chose; the service checks the role. / 操作者所选项目与机器人。
                    view = await self.call("missions.submit", project_id=str(message["project_id"]),
                                           robot_id=str(message["robot_id"]), **common)
                else:
                    view = await self.call("submit", **common)
            finally:
                self.busy = False
            if view is not None:
                self.watched = view["mission"]["mission_id"]
                await self.push_view(force=True)
                await self.list()
        elif kind in ("workflows", "workflow_watch", "workflow_start", "workflow_cancel", "workflow_schedule",
                      "workflow_review", "workflow_repair", "workflow_draft"):
            await self.workflow(kind, message)
        elif kind in ("tasks_watch", "task_watch", "task_submit", "task_cancel"):
            await self.task(kind, message)
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
    fixed = False
    def __init__(self, api, origin: str, *, tailnet: bool, scope: dict, local_user: str | None = None,
                 clients: dict[str, str] | None = None, poll_s: float = 1.0, supervisor: Path | None = None,
                 source_sha: str | None = None):
        self.api, self.origin, self.tailnet = api, validate_origin(origin, tailnet=tailnet), tailnet
        self.scope, self.local_user, self.clients, self.poll_s = scope, local_user, clients or {}, poll_s
        self.supervisor, self.source_sha = supervisor, source_sha
        # Name the WebSocket origin explicitly next to 'self'. / 在 'self' 之外显式列出 WebSocket 源。
        socket_scheme = "wss" if self.origin.startswith("https:") else "ws"
        self.csp = CSP.replace("connect-src 'self'",
                               f"connect-src 'self' {socket_scheme}://{urlsplit(self.origin).netloc}")

    # ── supervisor records (display only) / 监管者记录（仅展示）──

    def _public(self, name: str) -> dict | None:
        """A bounded JSON record from the supervisor's read-only directory; anything else is none.

        监管者只读目录中的有界 JSON 记录；其他情况一律视为没有。
        """
        if self.supervisor is None:
            return None
        path = self.supervisor / name
        try:
            if path.is_symlink() or path.stat().st_size > MAX_PUBLIC_RECORD:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def host_status(self) -> dict | None:
        return self._public("status.json")

    def cloud_record(self, mission_id: str) -> dict | None:
        return self._public(f"missions/{mission_id}.json") if MISSION_ID.fullmatch(mission_id) else None

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
            page = PAGE.read_text(encoding="utf-8").replace("__NAV__", desk_navigation("mission") if self.fixed else "")
            return await self.respond(send, Response(200, page.encode(), "text/html; charset=utf-8",
                                                     csp=self.csp))
        if method == "GET" and path == "/mission.js":
            return await self.respond(send, Response(200, PAGE.with_suffix(".js").read_bytes(),
                                                     "text/javascript; charset=utf-8"))
        if method == "GET" and path == "/health":
            try:
                result = await self.api.call("health", "", "anonymous")
            except (OSError, TimeoutError, RuntimeError, ValueError) as error:
                result = {"ok": False, "error": f"mission service unavailable ({type(error).__name__})"}
            return await self.respond(send, json_response(200 if result.get("ok") else 503, {
                **result, "console_source_sha": self.source_sha, "supervisor": self.host_status()}))
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
                await session.push_host()
                await session.push_view()
                await session.push_resources()
                await session.push_workflows()
                await session.push_run()
                await session.push_tasks()
                await session.push_task()

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


def local_service(root: Path, scene: Path, state: Path):
    """An in-process mission service for the local desk: plans, admits and signs, but no robot connects (D023).

    Live planning uses MINIMAX_API_KEY from the environment; without it the planner answers only the M2 suite's
    request texts from a labelled scripted fixture, and the page says so.

    本机任务台的进程内任务服务：可规划、准入与签名，但没有机器人连接（D023）。有 MINIMAX_API_KEY 时实调规划；
    没有时只按带标注的脚本夹具回答 M2 场景集中的请求原文，页面会如实标明。
    """
    from types import SimpleNamespace

    from drone_agent.fleet.ledger import BusinessLedger
    from drone_agent.fleet.main import build_planner
    from drone_agent.fleet.service import MissionService
    from drone_agent.fleet.transport import FleetHub
    from drone_agent.mission.registry import Registry
    from drone_agent.planner.replan import ApprovalPolicy
    from drone_agent.runtime.signing import SigningKey

    state.mkdir(parents=True, exist_ok=True)
    key_path = state / "approval-signing.key"
    if not key_path.exists():
        SigningKey.generate().save(key_path)
    registry = Registry(root, scene=scene)
    planner, label = build_planner(SimpleNamespace(planner="auto", root=root, scene=scene, state=state,
                                                   fixtures=None), registry)
    if planner is not None:
        planner.label = label
    ledger = BusinessLedger(state / "ledger.sqlite3")
    service = MissionService(root=root, scene=scene, ledger=ledger, hub=FleetHub(ledger, state / "media"),
                             signing_key=SigningKey.load(key_path),
                             approval_policy=ApprovalPolicy.from_yaml(root / "configs/approval_policy.yaml"),
                             planner=planner)
    return service, label


def main() -> None:
    import tempfile

    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true",
                        help="in-process mission service on this machine; plans and signs, nothing flies (D023)")
    parser.add_argument("--origin", help="explicit origin; defaults to http://127.0.0.1:<port> with --local")
    parser.add_argument("--tailnet", action="store_true", help="identity from Tailscale-User-Login (D033)")
    parser.add_argument("--api", type=Path, default=Path("/run/mission/api.sock"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--scene", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=Path(tempfile.gettempdir()) / "drone-agent-desk")
    parser.add_argument("--a2a-clients", type=Path, help="JSON file with client ids and token SHA-256 values")
    parser.add_argument("--supervisor", type=Path, help="read-only public records of the simulation supervisor (D035)")
    parser.add_argument("--source-sha", help="committed revision this page was deployed from, shown by /health")
    parser.add_argument("--fixed-root", type=Path, help="D037 mounts for the existing fixed-flight broker and evidence")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    scene = args.scene or args.root / "configs/scenarios/m2_campus_v2.yaml"
    origin = args.origin or f"http://127.0.0.1:{args.port}"
    scope = scene_scope(args.root, scene)
    if args.local:
        if args.tailnet:
            parser.error("--local serves the loopback desk only")
        service, label = local_service(args.root, scene, args.state)
        api = LocalApi(service)
        scope["planner"] = label
        print(json.dumps({"console_url": origin, "mode": "local", "planner": label, "flights": "cloud only"}),
              flush=True)
    else:
        api = SocketApi(args.api)
    console = MissionConsole(api, origin, tailnet=args.tailnet, scope=scope,
                             local_user=None if args.tailnet else getpass.getuser(),
                             clients=a2a_clients(args.a2a_clients), supervisor=args.supervisor,
                             source_sha=args.source_sha)
    if args.fixed_root:
        from drone_agent.console.unified import unified_console

        console = unified_console(console, args.fixed_root)
    uvicorn.run(console, host=args.host, port=args.port, workers=1, lifespan="off", proxy_headers=False,
                access_log=False, limit_concurrency=32, timeout_keep_alive=5, timeout_graceful_shutdown=10,
                server_header=False, ws="wsproto", ws_max_size=MAX_FRAME, http="h11")


if __name__ == "__main__":
    main()
