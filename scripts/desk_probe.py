"""Acceptance probe for the resident mission desk through the real Tailnet HTTPS entry (D035).

Run it on a tailnet device against the origin that `dev_stack.py desk-cloud --status` returns:
- `http`: page, script (compared with this checkout), health, Agent Card, A2A refusals, cross-origin WebSocket;
- `session`: one mission over hri.v0 exactly as the page drives it (submit, approve, optional pause and resume or
  cancel at a step), reconnecting like the page, until the mission ends and the supervisor's judge is published;
- `spoof`: a WebSocket session that forges the Serve identity header.
Receipts never contain the tailnet host name or the operator's login, and the probe decides nothing: it records
what the desk, the service and the judge reported.

经真实 Tailnet HTTPS 入口验收常驻任务台（D035）。在 tailnet 设备上针对 `dev_stack.py desk-cloud --status`
返回的入口运行：`http` 检查页面、脚本（与本检出比对）、健康、Agent Card、A2A 拒绝与跨源 WebSocket；
`session` 按页面相同方式经 hri.v0 驱动一个任务（提交、审批、可选在某步骤暂停后恢复或取消），像页面一样
断线重连，直到任务结束且监管者发布裁判结果；`spoof` 伪造 Serve 身份头。回执从不包含 tailnet 主机名或操作者
登录名；探针不做任何判定，只记录任务台、服务与裁判报告的内容。
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
TERMINAL = ("completed", "incomplete", "declined", "delivery_rejected", "rejected", "refused", "planning_failed")
NEVER_FLIES = ("declined", "rejected", "refused", "planning_failed")


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


def session(origin: str, args) -> tuple[dict, str | None]:
    started = time.monotonic()
    client, login, receipt = open_session(origin)
    client.send({"type": "text", "rid": uuid.uuid4().hex[:16], "text": args.text, "volume_id": args.volume,
                 "asset_ids": args.asset or []})
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
                                             "approval", "journals", "decision")} for v in view["versions"]],
        "operations": view["operations"], "report": view["report"],
        "evidence": [{k: e.get(k) for k in ("evidence_id", "version", "step_id", "sha256", "verification")}
                     for e in view.get("evidence", [])],
        "issues": [i["code"] for i in view["issues"]], "facts": view["facts"], "cloud": view.get("cloud"),
    }
    return receipt


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
    for name in ("http", "session", "watch", "spoof", "fixed"):
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
    follow_existing.add_argument("--mission", required=True)
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
