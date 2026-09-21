"""Loopback-only browser bridge to the existing authenticated SSH development channel.

仅监听回环地址的浏览器桥，复用已认证的 SSH 开发通道。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from drone_agent.console.application import MAX_BODY, ConsoleApplication, Response, json_response
from drone_agent.eval.viewer import _label, png_data_uri

RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


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
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"
        self.application = ConsoleApplication(bridge, self.origin)
        self.nonce = self.application.nonce


class Handler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer

    def log_message(self, *_args):
        # Do not log session nonces or operator payloads. / 不记录会话 nonce 或操作者载荷。
        return

    def respond(self, response: Response):
        self.send_response(response.status)
        for key, value in response.headers:
            self.send_header(key.title(), value)
        self.end_headers()
        self.wfile.write(response.body)

    def do_GET(self):
        self.respond(self.server.application.handle("GET", self.path, list(self.headers.raw_items())))

    def do_POST(self):
        headers = list(self.headers.raw_items())
        rejection = self.server.application.authorized("POST", headers)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError("invalid request length")
            self.connection.settimeout(10)
            body = self.rfile.read(size)
        except (OSError, ValueError) as error:
            self.respond(rejection or json_response(409, {"error": str(error)}))
            return
        if rejection:
            self.respond(rejection)
            return
        self.respond(self.server.application.handle("POST", self.path, headers, body))


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
