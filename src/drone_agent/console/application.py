"""Shared HTTP semantics for the local bridge and the private cloud console.

本机桥与私有云端控制台共用的 HTTP 语义。
"""

from __future__ import annotations

import hmac
import json
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TEMPLATE = Path(__file__).with_name("live.html")
MAX_BODY = 4096
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def validate_origin(value: str, *, tailnet: bool = False) -> str:
    """Accept an explicit authority, never forwarded headers or arbitrary origins.

    仅接受显式配置的来源，不从转发头或任意请求推断。
    """
    url = urlsplit(value)
    if url.username or url.password or url.path or url.query or url.fragment or not url.hostname:
        raise ValueError("origin must contain only scheme, host and optional port")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("invalid origin port")
    if tailnet:
        if url.scheme != "https" or not url.hostname.endswith(".ts.net"):
            raise ValueError("cloud console requires an explicit HTTPS tailnet origin")
    elif url.scheme != "http" or url.hostname != "127.0.0.1":
        raise ValueError("local console requires a loopback HTTP origin")
    if not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("invalid origin characters")
    return value


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    csp: str = CSP

    @property
    def headers(self) -> list[tuple[str, str]]:
        return [
            ("content-type", self.content_type), ("content-length", str(len(self.body))),
            ("cache-control", "no-store"), ("x-content-type-options", "nosniff"),
            ("referrer-policy", "no-referrer"), ("x-frame-options", "DENY"),
            ("content-security-policy", self.csp),
        ]


def json_response(status: int, value: dict) -> Response:
    return Response(status, json.dumps(value, ensure_ascii=False).encode())


class ConsoleApplication:
    def __init__(self, bridge, origin: str, *, tailnet: bool = False, health=None):
        self.bridge = bridge
        self.origin = validate_origin(origin, tailnet=tailnet)
        self.nonce = secrets.token_urlsafe(32)
        self.tailnet = tailnet
        self.health = health or (lambda: {"status": "ready", "mode": "local_ssh_bridge"})

    def authorized(self, method: str, headers: list[tuple[str, str]]) -> Response | None:
        values = {}
        for key, value in headers:
            key = key.lower()
            if key in values and key in {"host", "origin", "x-console-nonce", "content-length", "content-type"}:
                return json_response(403, {"error": "duplicate security header"})
            values[key] = value
        if values.get("host") != urlsplit(self.origin).netloc:
            return json_response(403, {"error": "invalid host"})
        write, origin = method == "POST", values.get("origin")
        if (write and origin != self.origin) or (origin and origin != self.origin):
            return json_response(403, {"error": "cross-origin access rejected"})
        if write and not hmac.compare_digest(values.get("x-console-nonce", "").encode(), self.nonce.encode()):
            return json_response(403, {"error": "reload this console session before submitting"})
        return None

    def handle(self, method: str, path: str, headers: list[tuple[str, str]], body: bytes = b"") -> Response:
        rejection = self.authorized(method, headers)
        if rejection:
            return rejection
        try:
            if method == "GET":
                return self.get(path)
            if method != "POST":
                return json_response(405, {"error": "method not allowed"})
            values = {k.lower(): v for k, v in headers}
            if values.get("content-type", "").split(";", 1)[0] != "application/json":
                return json_response(415, {"error": "JSON required"})
            if not 0 < len(body) <= MAX_BODY:
                raise ValueError("invalid request length")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            if path == "/api/start":
                result = self.bridge.start(value)
            elif path == "/api/operate":
                result = self.bridge.operate(value)
            elif path == "/api/evidence" and set(value) == {"run_id"}:
                result = self.bridge.evidence(value["run_id"], start=True)
            else:
                return json_response(404, {"error": "not found"})
            return json_response(202, result)
        except (ValueError, RuntimeError, OSError, TypeError, subprocess.TimeoutExpired) as error:
            return json_response(409 if method == "POST" else 503, {"error": str(error), "fresh": False})

    def get(self, path: str) -> Response:
        url = urlsplit(path)
        if url.path == "/":
            page = TEMPLATE.read_text(encoding="utf-8").replace("__NONCE__", self.nonce)
            return Response(200, page.encode(), "text/html; charset=utf-8")
        if url.path == "/live.js":
            return Response(200, TEMPLATE.with_suffix(".js").read_bytes(), "text/javascript; charset=utf-8")
        if url.path == "/health":
            return json_response(200, self.health())
        if url.path == "/api/state":
            return json_response(200, self.bridge.state())
        if url.path == "/api/evidence":
            run_id = parse_qs(url.query).get("run", [""])[0]
            return json_response(200, self.bridge.evidence(run_id))
        if url.path.startswith("/evidence/"):
            run_id = url.path.removeprefix("/evidence/")
            page = self.bridge.pages.get(run_id)
            if page:
                return Response(200, page.read_bytes(), "text/html; charset=utf-8")
            return json_response(404, {"error": "verified evidence page not available"})
        return json_response(404, {"error": "not found"})
