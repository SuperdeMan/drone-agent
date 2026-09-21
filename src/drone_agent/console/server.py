"""Loopback-only browser bridge to the existing authenticated SSH development channel.

仅监听回环地址的浏览器桥，复用已认证的 SSH 开发通道。
"""

from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from drone_agent.eval.viewer import _label, png_data_uri

RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
TEMPLATE = Path(__file__).with_name("live.html")


class Bridge:
    def __init__(self, request, fetch):
        self.request, self.fetch = request, fetch
        self.state_lock = threading.Lock()
        self.evidence_lock = threading.Lock()
        self.cached, self.cached_at = None, 0
        self.exports = {}
        self.pages = {}
        self.history, self.history_run, self.last_sample = [], None, None

    def state(self) -> dict:
        with self.state_lock:
            if self.cached is not None and time.monotonic() - self.cached_at < 0.7:
                return self.cached
            value = self.request({"action": "live_status"})
            for camera in (value.get("camera"), value.get("capture")):
                if not camera:
                    continue
                width, height = camera["width"], camera["height"]
                if type(width) is not int or type(height) is not int or not (0 < width <= 1920 and 0 < height <= 1080):
                    raise ValueError("unexpected camera dimensions")
                camera["png"] = png_data_uri(base64.b64decode(camera.pop("rgb"), validate=True), width, height)
            run_id = (value.get("job") or {}).get("run_id")
            if run_id != self.history_run:
                self.history, self.history_run, self.last_sample = [], run_id, None
            obs = (value.get("runtime") or {}).get("observation") or {}
            if obs.get("pose") and obs.get("sample_id") != self.last_sample:
                position = obs["pose"]["position"]
                self.history.append({"timestamp": obs["timestamp"], "position": [position[k] for k in ("x", "y", "z")]})
                self.history = self.history[-1200:]
                self.last_sample = obs["sample_id"]
            value["estimate"] = list(self.history)
            for event in value.get("events", []):
                event["label"] = _label(event["kind"], event["data"])
            self.cached, self.cached_at = value, time.monotonic()
            return value

    def start(self, body: dict) -> dict:
        if set(body) != {"run_id", "seed"}:
            raise ValueError("start accepts only a fixed simulation seed and run identity")
        result = self.request({"action": "live_start", **body})
        self.cached_at = 0
        return result

    def operate(self, body: dict) -> dict:
        if set(body) != {"run_id", "request_id", "operation", "mission_id", "step_id"}:
            raise ValueError("invalid operation fields")
        result = self.request({"action": "live_operate", **body})
        self.cached_at = 0
        return result

    def evidence(self, run_id: str, *, start: bool = False) -> dict:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run identity")
        with self.evidence_lock:
            if run_id in self.exports:
                if not start or self.exports[run_id]["status"] != "failed":
                    return self.exports[run_id]
            if not start:
                return {"status": "absent"}
            state = self.state()
            job = state.get("job")
            if not job or job["run_id"] != run_id or job["phase"] not in {"finished", "failed"}:
                raise ValueError("evidence is available only after this cloud run finishes")
            if not (job.get("completion") or {}).get("results"):
                raise ValueError("no judged case available for evidence export")
            self.exports[run_id] = {"status": "fetching"}

            def collect():
                try:
                    result = self.fetch(job)
                    if result.get("mismatched") or not result.get("viewer"):
                        raise ValueError("evidence export did not produce a verified viewer")
                    with self.evidence_lock:
                        self.pages[run_id] = Path(result["viewer"])
                        self.exports[run_id] = {"status": "ready", "url": "/evidence/" + run_id}
                except Exception as error:
                    with self.evidence_lock:
                        self.exports[run_id] = {"status": "failed", "reason": str(error)}

            threading.Thread(target=collect, daemon=True).start()
            return self.exports[run_id]


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, bridge: Bridge):
        self.bridge = bridge
        self.nonce = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer

    def log_message(self, *_args):
        # Never log session nonces or operator payloads. / 不把会话 nonce 或操作者载荷写入日志。
        return

    def respond(self, status: int, value, *, html: bool = False):
        content = value if html else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def authenticated(self, *, write: bool) -> bool:
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.respond(403, {"error": "invalid host"})
            return False
        origin = self.headers.get("Origin")
        if (write and origin != self.server.origin) or (origin and origin != self.server.origin):
            self.respond(403, {"error": "cross-origin access rejected"})
            return False
        if write and not hmac.compare_digest(self.headers.get("X-Console-Nonce", ""), self.server.nonce):
            self.respond(403, {"error": "reload this console session before submitting"})
            return False
        return True

    def do_GET(self):
        if not self.authenticated(write=False):
            return
        url = urlsplit(self.path)
        try:
            if url.path == "/":
                page = TEMPLATE.read_text(encoding="utf-8").replace("__NONCE__", self.server.nonce)
                self.respond(200, page.encode(), html=True)
            elif url.path == "/live.js":
                content = TEMPLATE.with_suffix(".js").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
            elif url.path == "/api/state":
                self.respond(200, self.server.bridge.state())
            elif url.path == "/api/evidence":
                run_id = parse_qs(url.query).get("run", [""])[0]
                self.respond(200, self.server.bridge.evidence(run_id))
            elif url.path.startswith("/evidence/"):
                run_id = url.path.removeprefix("/evidence/")
                path = self.server.bridge.pages.get(run_id)
                if path is None:
                    self.respond(404, {"error": "verified evidence page not available"})
                else:
                    self.respond(200, path.read_bytes(), html=True)
            else:
                self.respond(404, {"error": "not found"})
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            self.respond(503, {"error": str(error), "fresh": False})

    def do_POST(self):
        if not self.authenticated(write=True):
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.respond(415, {"error": "JSON required"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096 or self.headers.get("Transfer-Encoding"):
                raise ValueError("invalid request length")
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
            if self.path == "/api/start":
                result = self.server.bridge.start(body)
            elif self.path == "/api/operate":
                result = self.server.bridge.operate(body)
            elif self.path == "/api/evidence" and set(body) == {"run_id"}:
                result = self.server.bridge.evidence(body["run_id"], start=True)
            else:
                self.respond(404, {"error": "not found"})
                return
            self.respond(202, result)
        except (ValueError, RuntimeError, OSError, TypeError, subprocess.TimeoutExpired) as error:
            self.respond(409, {"error": str(error)})


def serve_console(*, port: int, request, fetch) -> None:
    """Serve until interrupted; exiting the page bridge leaves a cloud job running.

    持续提供入口直至中断；退出页面桥不终止云端任务。
    """
    server = ConsoleHTTPServer(port, Bridge(request, fetch))
    print(json.dumps({"console_url": server.origin, "target": "cloud", "mode": "fixed_simulation"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
