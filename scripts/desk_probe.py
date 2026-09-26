"""Acceptance probe for the resident mission desk through the real Tailnet HTTPS entry (D035).

Run it on a tailnet device against the origin that `dev_stack.py desk-cloud --status` returns:
- `http`: page, script (compared with this checkout), health, Agent Card, A2A refusals, cross-origin WebSocket;
- `session`: one mission over hri.v0 exactly as the page drives it (submit, approve, optional pause and resume or
  cancel at a step), reconnecting like the page, until the mission ends and the supervisor's judge is published;
- `spoof`: a WebSocket session that forges the Serve identity header;
- `resources` (P1): the caller's projects, each project's sites, docks and robots with status age, source and the
  eligibility kept apart from the link state, one dock's detail, and the refusal of injection or control frames;
- `workflow` (P2): the project's templates and schedules, one inactive draft from the configured planner, the
  refusal of injection, control and activation frames, and optionally one run started, approved mission by mission,
  reviewed and given repair feedback as the page does, until every run is final and each flight was judged;
- `tasks` (P3): the project's queue with each robot's assignment and preview verdict, the assets the page offers,
  the airspace holds, the refusal of injection, control and assignment frames, and optionally one task submitted as
  the page does, its assigned mission approved, until the task is final and its flight was judged.
Receipts never contain the tailnet host name or the operator's login, and the probe decides nothing: it records
what the desk, the service and the judge reported.

经真实 Tailnet HTTPS 入口验收常驻任务台（D035）。在 tailnet 设备上针对 `dev_stack.py desk-cloud --status`
返回的入口运行：`http` 检查页面、脚本（与本检出比对）、健康、Agent Card、A2A 拒绝与跨源 WebSocket；
`session` 按页面相同方式经 hri.v0 驱动一个任务（提交、审批、可选在某步骤暂停后恢复或取消），像页面一样
断线重连，直到任务结束且监管者发布裁判结果；`spoof` 伪造 Serve 身份头；`resources`（P1）记录调用方的项目，
每个项目的站点、机场与机器人（状态年龄、来源，以及与链路状态分开的可派遣判定），一个机场的详情，以及对注入或
控制帧的拒绝；`workflow`（P2）记录项目的模板与排班、配置的规划器给出的一份未生效草案、对注入、控制与激活帧的拒绝，
并可按页面方式启动一次运行、逐任务审批、复核并给出维修反馈，直到每个运行终结且每次飞行都有裁判结果；`tasks`（P3）记录
项目的队列（各机器人的分配与预览判定）、页面提供的资产、空域持有、对注入、控制与分配帧的拒绝，并可按页面方式提交一个任务单、
审批其被分配的任务，直到任务单终结且其飞行有裁判结果。回执从不包含
tailnet 主机名或操作者登录名；探针不做任何判定，只记录任务台、服务与裁判报告的内容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, CloseConnection, Ping, RejectConnection, Request, TextMessage
from wsproto.utilities import LocalProtocolError

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = ("completed", "incomplete", "declined", "delivery_rejected", "rejected", "refused", "planning_failed",
            "cancelled", "dispatch_expired")
# P1: a delivery cancelled or expired before its claim was never handed out, so no flight or judge follows.
# P1：领取前取消或过期的交付从未交出，之后不会有飞行与裁判。
NEVER_FLIES = ("declined", "rejected", "refused", "planning_failed", "cancelled", "dispatch_expired")


class Client:
    """One hri.v0 WebSocket over TLS with the system trust store. / 使用系统信任库的一条 TLS hri.v0 WebSocket。"""

    def __init__(self, origin: str, *, origin_header: str | None = None, extra_headers=(), expect_accept=True):
        url = urlsplit(origin)
        raw = socket.create_connection((url.hostname, url.port or 443), timeout=20)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=url.hostname)
        self.ws = WSConnection(ConnectionType.CLIENT)
        headers = [(b"origin", (origin_header or origin).encode()), *extra_headers]
        self.sock.sendall(self.ws.send(Request(host=url.netloc, target="/ws/session", extra_headers=headers)))
        self.queue: deque = deque()
        self.parts: list[str] = []
        self.accepted, self.rejected = False, None
        deadline = time.monotonic() + 20
        while not self.accepted and self.rejected is None:
            if time.monotonic() > deadline:
                raise TimeoutError("WebSocket handshake")
            self.pump(2)
        if self.rejected is not None and expect_accept:
            # E.g. the page restarting behind Serve; the caller reconnects. / 例如页面在 Serve 后重启；调用方重连。
            self.sock.close()
            raise ConnectionError(f"WebSocket handshake rejected ({self.rejected})")

    def pump(self, timeout: float) -> None:
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(1 << 16)
        except (TimeoutError, ssl.SSLWantReadError):
            return
        if not data:
            raise ConnectionError("connection closed")
        self.ws.receive_data(data)
        for event in self.ws.events():
            if isinstance(event, AcceptConnection):
                self.accepted = True
            elif isinstance(event, RejectConnection):
                self.rejected = event.status_code
            elif isinstance(event, TextMessage):
                self.parts.append(event.data)
                if event.message_finished:
                    self.queue.append(json.loads("".join(self.parts)))
                    self.parts = []
            elif isinstance(event, Ping):
                self.sock.sendall(self.ws.send(event.response()))
            elif isinstance(event, CloseConnection):
                raise ConnectionError(f"closed by the server ({event.code})")

    def next(self, kinds, timeout: float) -> dict:
        kinds = (kinds,) if isinstance(kinds, str) else tuple(kinds)
        deadline = time.monotonic() + timeout
        while True:
            while self.queue:
                message = self.queue.popleft()
                if message["type"] in kinds:
                    return message
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {kinds} within {timeout} s")
            self.pump(min(left, 2))

    def send(self, value: dict) -> None:
        self.sock.sendall(self.ws.send(TextMessage(data=json.dumps(value, ensure_ascii=False))))

    def close(self) -> None:
        try:
            self.sock.sendall(self.ws.send(CloseConnection(code=1000)))
        except Exception:  # noqa: BLE001 - closing a broken connection / 关闭已损坏的连接
            pass
        self.sock.close()


def fetch(origin: str, path: str, *, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
    request = urllib.request.Request(origin + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context()) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def redact(value, origin: str, login: str | None):
    """Remove the tailnet host and the operator login from a receipt. / 从回执中去掉 tailnet 主机与操作者登录名。"""
    text = json.dumps(value, ensure_ascii=False)
    text = text.replace(urlsplit(origin).hostname, "<tailnet-host>")
    if login:
        text = text.replace(login, "<operator>")
    return json.loads(text)


def http_checks(origin: str) -> dict:
    result = {}
    status, headers, body = fetch(origin, "/")
    csp = {k.lower(): v for k, v in headers.items()}.get("content-security-policy", "")
    result["page"] = {"status": status, "desk_page": b"FLIGHT DESK" in body,
                      "fixed_navigation": b'href="/fixed/"' in body,
                      "csp_names_socket_origin": f"wss://{urlsplit(origin).netloc}" in csp}
    status, _, body = fetch(origin, "/mission.js")
    local = (ROOT / "src/drone_agent/console/mission.js").read_bytes()
    result["script"] = {"status": status, "matches_checkout": hashlib.sha256(body).digest() == hashlib.sha256(local).digest()}
    status, _, body = fetch(origin, "/health")
    health = json.loads(body)
    service = health.get("result") or {}
    result["health"] = {"status": status, "ok": health.get("ok"), "service": service.get("status"),
                        "planner": service.get("planner"), "signer_key_id": service.get("signer_key_id"),
                        "console_source_sha": health.get("console_source_sha"), "service_source_sha": service.get("source_sha"),
                        "supervisor_state": (health.get("supervisor") or {}).get("state")}
    result["health"]["fixed_source_sha"] = (health.get("fixed") or {}).get("runtime_source_sha")
    status, _, body = fetch(origin, "/fixed/")
    result["fixed_page"] = {"status": status, "mounted_script": b'src="/fixed/live.js"' in body,
                            "same_origin_navigation": b'href="/"' in body}
    status, _, body = fetch(origin, "/fixed/live.js")
    local = (ROOT / "src/drone_agent/console/live.js").read_bytes()
    result["fixed_script"] = {"status": status, "matches_checkout": hashlib.sha256(body).digest()
                              == hashlib.sha256(local).digest()}
    status, _, body = fetch(origin, "/fixed/api/state")
    result["fixed_state"] = {"status": status, "source_sha": json.loads(body).get("source_sha")}
    status, _, body = fetch(origin, "/.well-known/agent-card.json")
    result["agent_card"] = {"status": status, "skills": [s["id"] for s in json.loads(body).get("skills", [])]}
    message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}).encode()
    for label, extra in (("a2a_without_token", {}), ("a2a_unknown_token", {"Authorization": "Bearer probe-0123456789abcdef"})):
        status, _, body = fetch(origin, "/a2a", method="POST", body=message,
                                headers={"Content-Type": "application/json", **extra})
        result[label] = {"status": status, "error": json.loads(body).get("error", {}).get("message")}
    foreign = Client(origin, origin_header="https://attacker.invalid", expect_accept=False)
    result["cross_origin_websocket"] = {"accepted": foreign.accepted, "rejected_status": foreign.rejected}
    foreign.close()
    return result


def spoof(origin: str) -> dict:
    client = Client(origin, extra_headers=[(b"tailscale-user-login", b"mallory@example.invalid")])
    hello = client.next("hello", 30)
    client.close()
    identity = hello.get("identity") or ""
    return {"forged_header_sent": True, "forged_identity_used": identity == "tailnet:mallory@example.invalid",
            "identity_scheme": identity.split(":")[0] if identity else None, "can_write": hello["can_write"]}, \
        identity.removeprefix("tailnet:") or None


def compact(view: dict, started: float) -> dict:
    live = view.get("live")
    cloud = view.get("cloud") or {}
    return {"t": round(time.monotonic() - started, 1), "status": view["mission"]["status"],
            "versions": [[v["version"], v["status"]] for v in view["versions"]],
            "live": [live["step_id"], live["state"], live["allowed_actions"]] if live else None,
            "cloud": [[f["version"], f["status"]] for f in cloud.get("flights", [])],
            "judge": (cloud.get("judge") or {}).get("passed")}


def open_session(origin: str) -> tuple[Client, str | None, dict]:
    client = Client(origin)
    hello = client.next("hello", 30)
    login = (hello.get("identity") or "").removeprefix("tailnet:") or None
    receipt = {"hello": {"identity_scheme": (hello.get("identity") or "none").split(":")[0],
                         "can_write": hello["can_write"], "planner": hello.get("planner")},
               "host_states": [client.next("host", 10)["status"]], "timeline": [], "errors": [], "sent": [],
               "reconnects": 0}
    return client, login, receipt


def reconnect(origin: str, mission_id: str, deadline: float) -> Client:
    """Open a new session and watch the mission again, as the page does. / 像页面一样重开会话并重新订阅。"""
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            client = Client(origin)
            client.send({"type": "watch", "mission_id": mission_id})
            return client
        except (ConnectionError, OSError, TimeoutError, ssl.SSLError, LocalProtocolError):
            continue
    raise TimeoutError("could not reconnect before the deadline")


FORBIDDEN_FRAMES = ({"type": "inject", "fault": "lid_jam"}, {"type": "docks.report", "report": {}},
                    {"type": "arm"}, {"type": "flight", "command": "takeoff"}, {"type": "simulator.inject"})


def resources(origin: str) -> tuple[dict, str | None]:
    """The P1 resource entry as the page drives it; injection or control frames must be refused.

    按页面方式驱动 P1 资源入口；注入或控制帧必须被拒绝。
    """
    client = Client(origin)
    hello = client.next("hello", 30)
    login = (hello.get("identity") or "").removeprefix("tailnet:") or None
    receipt = {"hello": {"identity_scheme": (hello.get("identity") or "none").split(":")[0],
                         "can_write": hello["can_write"]},
               "projects": [{k: p.get(k) for k in ("project_id", "legacy", "roles", "robots")}
                            for p in hello.get("projects", [])], "resources": {}, "detail": None, "refused": []}
    for project in [p for p in hello.get("projects", []) if not p.get("legacy")]:
        client.send({"type": "resources", "project_id": project["project_id"]})
        reply = client.next(("resources", "error"), 30)
        view = reply.get("view") or {}
        receipt["resources"][project["project_id"]] = {
            "catalog": view.get("catalog"), "error": reply.get("issue"),
            "docks": [{"dock_id": d["dock_id"], "source": d["source"], "pad": d["pad"],
                       "status": {k: (d["status"] or {}).get(k) for k in (
                           "link", "fresh", "age_s", "session", "lid", "aircraft", "energy", "environment", "upkeep",
                           "lock")} if d["status"] else None}
                      for site in view.get("sites", []) for d in site["docks"]],
            "robots": [{"robot_id": r["robot_id"], "execution_backend": r["execution_backend"],
                        "capability_source": r["capability_source"], "status": r["status"],
                        "eligibility": r["eligibility"]} for site in view.get("sites", []) for r in site["robots"]]}
        docks = receipt["resources"][project["project_id"]]["docks"]
        if docks and receipt["detail"] is None:
            client.send({"type": "resource", "project_id": project["project_id"], "resource_id": docks[0]["dock_id"]})
            detail = client.next(("resource", "error"), 30).get("detail") or {}
            receipt["detail"] = {"kind": detail.get("kind"), "actions": len(detail.get("actions", [])),
                                 "events": len(detail.get("events", [])),
                                 "reservations": len(detail.get("reservations", []))}
    for frame in FORBIDDEN_FRAMES:
        client.send(frame)
        reply = client.next("error", 30)
        receipt["refused"].append({"frame": frame["type"], "error": reply.get("message")})
    client.close()
    status, _, script = fetch(origin, "/mission.js")
    receipt["script"] = {"status": status, "control_or_injection_frames": sorted(
        name for name in ("inject", "docks.report", "simulator", "\"arm\"", "takeoff") if name.encode() in script)}
    return receipt, login


def session(origin: str, args) -> tuple[dict, str | None]:
    started = time.monotonic()
    client, login, receipt = open_session(origin)
    bound = {"project_id": args.project, "robot_id": args.robot} if args.project and args.robot else {}
    client.send({"type": "text", "rid": uuid.uuid4().hex[:16], "text": args.text, "volume_id": args.volume,
                 "asset_ids": args.asset or [], **bound})
    first = client.next(("mission", "error"), args.plan_timeout)
    if first["type"] == "error":
        receipt["errors"].append(first)
        client.close()
        return receipt, login
    receipt["planning_s"] = round(time.monotonic() - started, 1)
    return follow(origin, client, first["view"], args, receipt, started), login


def watch(origin: str, args) -> tuple[dict, str | None]:
    started = time.monotonic()
    client, login, receipt = open_session(origin)
    client.send({"type": "watch", "mission_id": args.mission})
    view = client.next("mission", 30)["view"]
    return follow(origin, client, view, args, receipt, started), login


def follow(origin: str, client: Client, view: dict, args, receipt: dict, started: float) -> dict:
    """Follow one mission until it ends and its judge is published, acting as asked. / 跟随任务直到结束并有裁判结果。"""
    mission_id = view["mission"]["mission_id"]

    def operate(action: str) -> None:
        request_id = f"op-{action}-{uuid.uuid4().hex[:12]}"
        client.send({"type": "operate", "mission_id": mission_id, "action": action, "request_id": request_id})
        receipt["sent"].append({"t": round(time.monotonic() - started, 1), "action": action, "request_id": request_id,
                                "step": (view.get("live") or {}).get("step_id")})

    approved, paused_at, done = set(), None, {"pause": False, "resume": False, "cancel": False}
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        state = compact(view, started)
        if not receipt["timeline"] or {k: v for k, v in receipt["timeline"][-1].items() if k != "t"} != \
                {k: v for k, v in state.items() if k != "t"}:
            receipt["timeline"].append(state)
        latest = view["versions"][-1]
        if args.approve and latest["status"] == "awaiting_approval" and latest["version"] not in approved:
            client.send({"type": "approve", "mission_id": mission_id, "version": latest["version"],
                         "package_hash": latest["package_hash"]})
            approved.add(latest["version"])
            receipt["sent"].append({"t": round(time.monotonic() - started, 1), "action": "approve",
                                    "version": latest["version"]})
        live = view.get("live")
        if live and args.pause_step and not done["pause"] and live["step_id"] == args.pause_step \
                and "pause" in live["allowed_actions"]:
            operate("pause")
            done["pause"], paused_at = True, time.monotonic()
        if live and done["pause"] and not done["resume"] and live["state"] == "paused" \
                and time.monotonic() - paused_at >= args.pause_s and "resume" in live["allowed_actions"]:
            operate("resume")
            done["resume"] = True
        if live and args.cancel_step and not done["cancel"] and live["step_id"] == args.cancel_step \
                and "cancel" in live["allowed_actions"]:
            operate("cancel")
            done["cancel"] = True
        cloud = view.get("cloud") or {}
        status = view["mission"]["status"]
        if status in NEVER_FLIES or (status in TERMINAL and cloud.get("judge")):
            break
        try:
            message = client.next(("mission", "host", "error"), max(1.0, min(30.0, deadline - time.monotonic())))
        except TimeoutError:
            continue
        except (ConnectionError, OSError, ssl.SSLError):
            receipt["reconnects"] += 1
            client = reconnect(origin, mission_id, deadline)
            continue
        if message["type"] == "mission" and message["view"]["mission"]["mission_id"] == mission_id:
            view = message["view"]
        elif message["type"] == "host":
            if message["status"] != receipt["host_states"][-1]:
                receipt["host_states"].append(message["status"])
        elif message["type"] == "error":
            receipt["errors"].append({"t": round(time.monotonic() - started, 1), **message})
    media = []
    for evidence in view.get("evidence", []):
        if not evidence.get("media"):
            continue
        client.send({"type": "media", "mission_id": mission_id, "evidence_id": evidence["evidence_id"]})
        reply = client.next("media", 60)
        png = reply.get("png", "")
        media.append({"evidence_id": evidence["evidence_id"], "png_data_uri_bytes": len(png),
                      "sha256": hashlib.sha256(png.encode()).hexdigest()})
        if args.media_dir:
            import base64

            Path(args.media_dir).mkdir(parents=True, exist_ok=True)
            (Path(args.media_dir) / f"{evidence['evidence_id']}.png").write_bytes(base64.b64decode(png.split(",", 1)[1]))
    client.close()
    receipt["duration_s"] = round(time.monotonic() - started, 1)
    receipt["media"] = media
    receipt["mission"] = {
        "mission_id": mission_id, "status": view["mission"]["status"], "replans": view["mission"]["replans"],
        "request": {k: view["request"][k] for k in ("text", "channel", "requested_by", "approved_volume_id", "asset_ids")},
        "versions": [{k: v.get(k) for k in ("version", "status", "origin", "package_hash", "planner", "admission",
                                             "approval", "journals", "decision", "provenance")} for v in view["versions"]],
        "operations": view["operations"], "report": view["report"],
        "evidence": [{k: e.get(k) for k in ("evidence_id", "version", "step_id", "sha256", "verification", "provenance")}
                     for e in view.get("evidence", [])],
        "issues": [i["code"] for i in view["issues"]], "facts": view["facts"], "cloud": view.get("cloud"),
        "binding": view.get("binding"), "dispatch": view.get("dispatch"),
    }
    return receipt


WORKFLOW_FORBIDDEN = (*FORBIDDEN_FRAMES, {"type": "workflow_activate", "draft": {}},
                      {"type": "workflow_approve_all", "project_id": "campus_s1"})
RUN_FINAL = ("completed", "failed", "outcome_unknown", "cancelled")


def workflow(origin: str, args) -> tuple[dict, str | None]:
    """A P2 workflow over hri.v0 exactly as the page drives it; the probe only records. / 按页面方式驱动 P2 工作流；探针只记录。"""
    started = time.monotonic()
    client = Client(origin)
    hello = client.next("hello", 30)
    login = (hello.get("identity") or "").removeprefix("tailnet:") or None
    receipt = {"hello": {"identity_scheme": (hello.get("identity") or "none").split(":")[0],
                         "can_write": hello["can_write"], "planner": hello.get("planner")},
               "project": args.project, "templates": [], "draft": None, "refused": [], "timeline": [], "runs": {},
               "missions": {}, "sent": [], "errors": []}
    client.send({"type": "workflows", "project_id": args.project})
    listing = client.next(("workflows", "error"), 30)
    view = listing.get("view") or {}
    receipt["roles"] = view.get("roles")
    receipt["catalog"] = view.get("catalog")
    receipt["templates"] = [{"workflow_id": t["workflow_id"], "version": t["version"], "sha256": t["sha256"],
                             "triggers": [{"trigger_id": x["trigger_id"], "kind": x["kind"],
                                           "state": (x.get("state") or {}).get("state")} for x in t["triggers"]]}
                            for t in view.get("templates", [])]
    if args.draft_text:
        client.send({"type": "workflow_draft", "project_id": args.project, "text": args.draft_text})
        reply = client.next(("workflow_draft", "error"), args.plan_timeout)
        result = reply.get("result") or {}
        receipt["draft"] = {"status": result.get("status"), "active": result.get("active"),
                            "error": reply.get("issue"), "errors": result.get("errors"),
                            "spec_sha256": result.get("spec_sha256"), "use": result.get("use"),
                            "nodes": [[n["node_id"], n["activity"]] for n in (result.get("spec") or {}).get("nodes", [])],
                            "robots": sorted({n["params"]["robot_id"] for n in (result.get("spec") or {}).get("nodes", [])
                                              if n["activity"] == "submit_mission"})}
    for frame in WORKFLOW_FORBIDDEN:
        client.send(frame)
        reply = client.next("error", 30)
        receipt["refused"].append({"frame": frame["type"], "error": reply.get("message")})
    if not args.start:
        client.close()
        receipt["duration_s"] = round(time.monotonic() - started, 1)
        return receipt, login
    client.send({"type": "workflow_start", "project_id": args.project, "workflow_id": args.workflow,
                 "request_id": "probe-" + uuid.uuid4().hex[:16],
                 "inputs": {"asset": args.asset_input} if args.asset_input else {}})
    first = client.next(("workflow", "error"), 60)
    if first["type"] == "error":
        receipt["errors"].append(first)
        client.close()
        return receipt, login
    root = first["view"]["run"]["run_id"]
    receipt["root_run"] = root
    runs, missions, reviewed, repaired = {root: first["view"]}, {}, set(), set()
    deadline = time.monotonic() + args.timeout

    def refresh(run_id: str) -> None:
        # The page also pushes the watched run whenever it changes, so a reply can queue behind pushed frames: keep
        # every run view read and stop only at the requested run.
        # 页面在被监视运行变化时也会推送，应答可能排在推送帧之后：保存读到的每个运行视图，读到所请求的运行才停。
        client.send({"type": "workflow_watch", "project_id": args.project, "run_id": run_id})
        while True:
            reply = client.next(("workflow", "error"), 60)
            if reply["type"] == "error":
                return
            runs[reply["view"]["run"]["run_id"]] = reply["view"]
            if reply["view"]["run"]["run_id"] == run_id:
                return

    def mission(mission_id: str) -> dict:
        client.send({"type": "watch", "mission_id": mission_id})
        while True:
            reply = client.next(("mission", "error"), 60)
            if reply["type"] == "error" or reply["view"]["mission"]["mission_id"] == mission_id:
                return reply.get("view") or {}

    while time.monotonic() < deadline:
        try:
            for run_id in list(runs):
                refresh(run_id)
                for child in runs[run_id]["children"]:
                    if child["run_id"] not in runs:
                        refresh(child["run_id"])
            state = {"t": round(time.monotonic() - started, 1),
                     "runs": {r: [v["run"]["state"], v["waiting"]] for r, v in runs.items()}}
            if not receipt["timeline"] or receipt["timeline"][-1]["runs"] != state["runs"]:
                receipt["timeline"].append(state)
            for run_id, view in list(runs.items()):
                for item in view["missions"]:
                    detail = mission(item["mission_id"])
                    missions[item["mission_id"]] = detail
                    latest = (detail.get("versions") or [{}])[-1]
                    if latest.get("status") == "awaiting_approval":
                        client.send({"type": "approve", "mission_id": item["mission_id"],
                                     "version": latest["version"], "package_hash": latest["package_hash"]})
                        receipt["sent"].append({"t": round(time.monotonic() - started, 1), "action": "approve",
                                                "mission_id": item["mission_id"], "version": latest["version"]})
                for node in view["nodes"]:
                    key = (run_id, node["node_id"])
                    if node["state"] == "waiting" and node["activity"] == "human_review" and key not in reviewed:
                        client.send({"type": "workflow_review", "project_id": args.project, "run_id": run_id,
                                     "node_id": node["node_id"], "decision": args.review,
                                     "request_id": "probe-" + uuid.uuid4().hex[:16], "note": "desk probe review"})
                        reviewed.add(key)
                        receipt["sent"].append({"t": round(time.monotonic() - started, 1), "action": "review",
                                                "node_id": node["node_id"], "decision": args.review})
                    if node["state"] == "waiting" and node["activity"] == "await_repair":
                        for order in view["orders"]:
                            if order["state"] == "open" and order["order_id"] not in repaired:
                                client.send({"type": "workflow_repair", "project_id": args.project,
                                             "order_id": order["order_id"],
                                             "request_id": "probe-" + uuid.uuid4().hex[:16],
                                             "note": "desk probe repair feedback"})
                                repaired.add(order["order_id"])
                                receipt["sent"].append({"t": round(time.monotonic() - started, 1),
                                                        "action": "repair", "order_id": order["order_id"]})
            flown = [m for m in missions.values() if m and m["mission"]["status"] in TERMINAL]
            judged = all((m.get("cloud") or {}).get("judge") for m in flown
                         if m["mission"]["status"] not in NEVER_FLIES)
            # Done only when every reinspection run a tracked run started is tracked too.
            # 被跟踪运行启动的每个复检运行也都已跟踪，才算结束。
            followed = all(child["run_id"] in runs for view in runs.values() for child in view["children"])
            if runs and followed and all(v["run"]["state"] in RUN_FINAL for v in runs.values()) and judged \
                    and len(flown) == len(missions):
                break
        except (ConnectionError, OSError, ssl.SSLError, TimeoutError) as error:
            receipt["errors"].append({"t": round(time.monotonic() - started, 1), "error": type(error).__name__})
            client.close()
            time.sleep(3)
            client = Client(origin)
            client.next("hello", 30)
            continue
        time.sleep(5)
    client.close()
    receipt["duration_s"] = round(time.monotonic() - started, 1)
    receipt["runs"] = {run_id: {"run": {k: v["run"][k] for k in ("run_id", "workflow_id", "version", "state",
                                                                "trigger_source", "started_by", "outcome")},
                                "nodes": [[n["node_id"], n["activity"], n["state"], n["reason"]] for n in v["nodes"]],
                                "analyses": [{k: a[k] for k in ("asset_id", "analyzer", "source", "verdict")}
                                             for a in v["analyses"]],
                                "reviews": [{k: r[k] for k in ("decision", "reviewer")} for r in v["reviews"]],
                                "orders": [{k: o[k] for k in ("order_id", "state", "reinspection_run")}
                                           for o in v["orders"]],
                                "missions": v["missions"]} for run_id, v in runs.items()}
    receipt["missions"] = {mission_id: {
        "status": m["mission"]["status"], "binding": m.get("binding"), "dispatch": m.get("dispatch"),
        "request": {k: m["request"][k] for k in ("channel", "requested_by")},
        "versions": [{k: v.get(k) for k in ("version", "status", "origin", "approval", "provenance")}
                     for v in m["versions"]],
        "report": m.get("report"), "cloud": m.get("cloud")} for mission_id, m in missions.items() if m}
    return receipt, login


TASK_FORBIDDEN = (*FORBIDDEN_FRAMES, {"type": "task_assign", "task_id": "tk-probe", "robot_id": "uav_01"},
                  {"type": "task_approve_all", "project_id": "campus_s1"}, {"type": "scheduler_tick"})
TASK_FINAL = ("completed", "failed", "outcome_unknown", "rejected", "cancelled")


def tasks(origin: str, args) -> tuple[dict, str | None]:
    """The P3 scheduling entry over hri.v0 exactly as the page drives it; the probe only records.

    按页面方式驱动 P3 调度入口；探针只记录。
    """
    started = time.monotonic()
    client = Client(origin)
    hello = client.next("hello", 30)
    login = (hello.get("identity") or "").removeprefix("tailnet:") or None
    receipt = {"hello": {"identity_scheme": (hello.get("identity") or "none").split(":")[0],
                         "can_write": hello["can_write"], "planner": hello.get("planner")},
               "project": args.project,
               "projects": [{k: p.get(k) for k in ("project_id", "legacy", "roles", "robots", "scheduling")}
                            for p in hello.get("projects", [])],
               "queue": None, "refused": [], "timeline": [], "task": None, "missions": {}, "sent": [], "errors": []}
    client.send({"type": "tasks_watch", "project_id": args.project})
    listing = client.next(("tasks", "error"), 30)
    view = listing.get("view") or {}
    airspace = view.get("airspace") or {}
    receipt["queue"] = {"error": listing.get("issue"), "roles": view.get("roles"), "catalog": view.get("catalog"),
                        "assets": view.get("assets"), "robots": view.get("robots"), "tasks": len(view.get("tasks", [])),
                        "airspace": {"frame": airspace.get("frame"), "cell_m": airspace.get("cell_m"),
                                     "holds": len(airspace.get("holds", [])),
                                     "envelopes": len(airspace.get("envelopes", []))}}
    for frame in TASK_FORBIDDEN:
        client.send(frame)
        reply = client.next("error", 30)
        receipt["refused"].append({"frame": frame["type"], "error": reply.get("message")})
    if not args.start:
        client.close()
        receipt["duration_s"] = round(time.monotonic() - started, 1)
        return receipt, login
    client.send({"type": "task_submit", "project_id": args.project, "asset_id": args.asset, "volume_id": args.volume,
                 "candidates": [], "priority": 0, "request_id": "probe-" + uuid.uuid4().hex[:16]})
    first = client.next(("task", "error"), 60)
    if first["type"] == "error":
        receipt["errors"].append(first)
        client.close()
        return receipt, login
    task_id = first["view"]["task"]["task_id"]
    receipt["task_id"] = task_id
    latest, missions, approved = first["view"], {}, set()
    deadline = time.monotonic() + args.timeout

    def refresh() -> dict:
        # The page pushes the watched task whenever it changes; keep reading until this task's view arrives.
        # 页面在被监视任务单变化时推送；一直读到本任务单的视图为止。
        client.send({"type": "task_watch", "project_id": args.project, "task_id": task_id})
        while True:
            reply = client.next(("task", "error"), 60)
            if reply["type"] == "error":
                return latest
            if reply["view"]["task"]["task_id"] == task_id:
                return reply["view"]

    def mission(mission_id: str) -> dict:
        client.send({"type": "watch", "mission_id": mission_id})
        while True:
            reply = client.next(("mission", "error"), 60)
            if reply["type"] == "error" or reply["view"]["mission"]["mission_id"] == mission_id:
                return reply.get("view") or {}

    while time.monotonic() < deadline:
        try:
            latest = refresh()
            current = latest["task"]
            state = {"t": round(time.monotonic() - started, 1), "state": current["state"], "epoch": current["epoch"],
                     "robot_id": current["robot_id"], "waiting": current.get("waiting")}
            if not receipt["timeline"] or {k: v for k, v in receipt["timeline"][-1].items() if k != "t"} != \
                    {k: v for k, v in state.items() if k != "t"}:
                receipt["timeline"].append(state)
            for assignment in latest["assignments"]:
                if not assignment["mission_id"]:
                    continue
                detail = mission(assignment["mission_id"])
                missions[assignment["mission_id"]] = detail
                version = (detail.get("versions") or [{}])[-1]
                key = (assignment["mission_id"], version.get("version"))
                if assignment["state"] == "active" and version.get("status") == "awaiting_approval" \
                        and key not in approved:
                    client.send({"type": "approve", "mission_id": assignment["mission_id"],
                                 "version": version["version"], "package_hash": version["package_hash"]})
                    approved.add(key)
                    receipt["sent"].append({"t": round(time.monotonic() - started, 1), "action": "approve",
                                            "mission_id": assignment["mission_id"], "version": version["version"]})
            flown = [m for m in missions.values() if m and m["mission"]["status"] in TERMINAL]
            judged = all((m.get("cloud") or {}).get("judge") for m in flown
                         if m["mission"]["status"] not in NEVER_FLIES)
            if current["state"] in TASK_FINAL and judged and len(flown) == len(missions):
                break
        except (ConnectionError, OSError, ssl.SSLError, TimeoutError) as error:
            receipt["errors"].append({"t": round(time.monotonic() - started, 1), "error": type(error).__name__})
            client.close()
            time.sleep(3)
            client = Client(origin)
            client.next("hello", 30)
            continue
        time.sleep(5)
    client.close()
    receipt["duration_s"] = round(time.monotonic() - started, 1)
    receipt["task"] = {
        "task": {k: latest["task"].get(k) for k in ("task_id", "project_id", "asset_id", "volume_id", "state", "epoch",
                                                     "robot_id", "mission_id", "reason", "source", "requested_by",
                                                     "outcome")},
        "assignments": latest["assignments"],
        "decisions": [{k: d[k] for k in ("decision_id", "verdict", "robot_id", "epoch", "snapshot_sha256",
                                         "ranking_version", "policy_version", "candidates")}
                      for d in latest["decisions"]]}
    receipt["missions"] = {mission_id: {
        "status": m["mission"]["status"], "binding": m.get("binding"), "dispatch": m.get("dispatch"),
        "request": {k: m["request"][k] for k in ("channel", "requested_by")},
        "versions": [{k: v.get(k) for k in ("version", "status", "origin", "approval", "provenance")}
                     for v in m["versions"]],
        "report": m.get("report"), "cloud": m.get("cloud")} for mission_id, m in missions.items() if m}
    return receipt, login


def fixed_session(origin: str, args) -> dict:
    """Exercise the mounted M1 page protocol and record its independent result. / 验证挂载的 M1 页面协议并记录独立结果。"""
    status, _, body = fetch(origin, "/fixed/")
    nonce = re.search(rb'<meta name="console-nonce" content="([^"]+)">', body)
    if status != 200 or nonce is None:
        raise RuntimeError("fixed console page unavailable")
    security = {"Origin": origin, "Content-Type": "application/json", "X-Console-Nonce": nonce[1].decode()}

    def write(path, value):
        status, _, data = fetch(origin, "/fixed" + path, method="POST", body=json.dumps(value).encode(), headers=security)
        if status != 202:
            raise RuntimeError(f"fixed request rejected ({status}): {data[:200]!r}")
        return json.loads(data)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    started = write("/api/start", {"run_id": run_id, "seed": args.seed})
    receipt = {"mode": args.mode, "run_id": run_id, "seed": args.seed, "start": started, "operations": []}
    # Visiting the other mode is read-only and must leave this same run active. / 访问另一模式只读，必须保留同一运行。
    receipt["mode_switch_http"] = fetch(origin, "/")[0]
    deadline = time.monotonic() + args.timeout
    sent, accepted, paused_at = set(), set(), None
    while time.monotonic() < deadline:
        status, _, raw = fetch(origin, "/fixed/api/state")
        if status != 200:
            raise RuntimeError(f"fixed state unavailable ({status})")
        view = json.loads(raw)
        if (view.get("job") or {}).get("run_id") != run_id:
            raise RuntimeError("another run replaced the requested flight")
        operation = view.get("operation")
        if operation and operation["status"] in ("accepted", "rejected") and operation not in receipt["operations"]:
            receipt["operations"].append(operation)
            if operation["status"] == "accepted":
                accepted.add(operation["action"])
        if view["job"].get("completion"):
            receipt["final"] = {key: view.get(key) for key in ("source_sha", "deployment_id", "job", "mission_result")}
            break
        action, step = None, view.get("step") or {}
        allowed = view.get("allowed_actions", [])
        if step.get("step_id") == "fly_route":
            if args.mode == "cancel" and "cancel" not in sent and "cancel" in allowed:
                action = "cancel"
            if args.mode == "pause" and "pause" not in sent and "pause" in allowed:
                action = "pause"
        if "pause" in accepted and "resume" in allowed:
            paused_at = paused_at or time.monotonic()
            if time.monotonic() - paused_at >= 3 and "resume" not in sent:
                action = "resume"
        if action:
            write("/api/operate", {"run_id": run_id, "request_id": uuid.uuid4().hex, "operation": action,
                                    "mission_id": step["mission_id"], "step_id": step["step_id"]})
            sent.add(action)
        time.sleep(1)
    if "final" not in receipt:
        raise TimeoutError(f"fixed run {run_id} still pending; reconcile before retrying")
    required = {"pause", "resume"} if args.mode == "pause" else {"cancel"} if args.mode == "cancel" else set()
    receipt["requested_operations_observed"] = required <= accepted
    write("/api/evidence", {"run_id": run_id})
    for _ in range(60):
        _, _, raw = fetch(origin, "/fixed/api/evidence?run=" + run_id)
        result = json.loads(raw)
        if result["status"] in ("ready", "failed"):
            receipt["evidence"] = result
            if result["status"] == "ready":
                status, _, page = fetch(origin, "/fixed" + result["url"])
                receipt["evidence_page"] = {"status": status, "sha256": hashlib.sha256(page).hexdigest()}
            break
        time.sleep(1)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("http", "session", "watch", "spoof", "fixed", "resources", "workflow", "tasks"):
        command = commands.add_parser(name)
        command.add_argument("--origin", required=True, help="https://<node>.<tailnet>.ts.net:8448")
        command.add_argument("--output", type=Path)
    fixed = commands.choices["fixed"]
    fixed.add_argument("--mode", choices=("nominal", "pause", "cancel"), default="nominal")
    fixed.add_argument("--seed", type=int, choices=(7, 19, 41), default=7)
    fixed.add_argument("--timeout", type=float, default=1200)
    run, follow_existing = commands.choices["session"], commands.choices["watch"]
    run.add_argument("--text", required=True)
    run.add_argument("--volume", default="campus_training")
    run.add_argument("--asset", action="append")
    run.add_argument("--plan-timeout", type=float, default=300)
    run.add_argument("--project", help="P1: submit into this project (with --robot)")
    run.add_argument("--robot", help="P1: the robot the operator chose")
    follow_existing.add_argument("--mission", required=True)
    flow = commands.choices["workflow"]
    flow.add_argument("--project", default="campus_s1")
    flow.add_argument("--draft-text", help="ask the configured planner for one inactive template draft")
    flow.add_argument("--start", action="store_true", help="also start, approve, review and repair one run")
    flow.add_argument("--workflow", default="asset_check")
    flow.add_argument("--asset-input", default="asset_red")
    flow.add_argument("--review", choices=("confirmed", "dismissed"), default="confirmed")
    flow.add_argument("--plan-timeout", type=float, default=300)
    flow.add_argument("--timeout", type=float, default=2700)
    queue = commands.choices["tasks"]
    queue.add_argument("--project", default="campus_s1")
    queue.add_argument("--start", action="store_true", help="also submit one task and approve its assigned mission")
    queue.add_argument("--asset", default="asset_red")
    queue.add_argument("--volume", default="campus_training")
    queue.add_argument("--timeout", type=float, default=2700)
    for command in (run, follow_existing):
        command.add_argument("--approve", action="store_true")
        command.add_argument("--pause-step")
        command.add_argument("--pause-s", type=float, default=6)
        command.add_argument("--cancel-step")
        command.add_argument("--timeout", type=float, default=1500)
        command.add_argument("--media-dir")
    args = parser.parse_args()
    login = None
    if args.command == "http":
        result = http_checks(args.origin)
    elif args.command == "fixed":
        result = fixed_session(args.origin, args)
    elif args.command == "spoof":
        result, login = spoof(args.origin)
    elif args.command == "resources":
        result, login = resources(args.origin)
    elif args.command == "workflow":
        result, login = workflow(args.origin, args)
    elif args.command == "tasks":
        result, login = tasks(args.origin, args)
    elif args.command == "watch":
        result, login = watch(args.origin, args)
    else:
        result, login = session(args.origin, args)
    result = redact({"schema_version": "0.1.0", "probe": args.command, **result}, args.origin, login)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
