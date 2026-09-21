"""Private ASGI console: bounded local IPC and read-only evidence, without SSH or Docker access.

私有 ASGI 控制台：有界本机 IPC 与只读证据，不持 SSH 或 Docker 权限。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
from pathlib import Path

from drone_agent.console.application import MAX_BODY, ConsoleApplication, json_response
from drone_agent.console.server import RUN_ID, Bridge
from drone_agent.eval.viewer import build_page, sha256_file

MAX_RESPONSE = 8 * 1024 * 1024


class CloudBackend:
    def __init__(self, *, endpoint: Path, records: Path, releases: Path, outputs: Path, source_sha: str):
        self.endpoint, self.records, self.releases, self.outputs = endpoint, records, releases, outputs
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ValueError("a committed console revision is required")
        self.source_sha = source_sha

    def request(self, value: dict) -> dict:
        envelope = json.dumps({"schema_version": "0.1.0", "request": value}).encode() + b"\n"
        if len(envelope) > 8192:
            raise ValueError("broker request too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(self.endpoint))
            client.sendall(envelope)
            with client.makefile("rb") as stream:
                raw = stream.readline(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
            raise RuntimeError("incomplete or oversized broker response; reconcile state")
        response = json.loads(raw)
        if response.get("ok") is not True:
            raise RuntimeError(response.get("error", "broker request rejected"))
        return response["result"]

    def health(self) -> dict:
        state = self.request({"action": "live_status"})
        return {"status": "ready", "mode": "tailnet", "console_source_sha": self.source_sha,
                "runtime_source_sha": state["source_sha"]}

    def fetch(self, job: dict) -> dict:
        """Build an evidence page only after all judge-bound files match their recorded digests.

        所有裁判绑定文件的摘要一致后，才生成证据页面。
        """
        deployment, run_id = job["deployment_id"], job["run_id"]
        if not RUN_ID.fullmatch(deployment) or not RUN_ID.fullmatch(run_id) or job["seed"] not in (7, 19, 41):
            raise ValueError("invalid recorded run identity")
        case_name = f"interactive-{job['seed']}"
        if job["case"] != case_name:
            raise ValueError("unexpected case identity")
        case = self.records / deployment / ("m1-" + run_id) / case_name
        source = self.releases / deployment / "source"
        if not case.resolve().is_relative_to(self.records.resolve()) or not source.resolve().is_relative_to(self.releases.resolve()):
            raise ValueError("evidence path escaped the project")
        manifest = json.loads((source.parent / "manifest.json").read_text())
        receipt = json.loads((case.parent / "suite.json").read_text())
        if manifest["source_sha"] != job["source_sha"] or receipt["source_sha"] != job["source_sha"]:
            raise ValueError("recorded source revision differs")
        result = next(r for r in receipt["results"] if r.get("scenario") == "interactive" and r.get("seed") == job["seed"])
        for name, expected in result["artifacts"].items():
            file = case / name
            if not file.resolve().is_relative_to(case.resolve()) or file.is_symlink():
                raise ValueError("invalid evidence file reference")
            if not file.is_file() or sha256_file(file) != expected:
                raise ValueError("recorded evidence digest mismatch")
        destination = self.outputs / deployment / run_id
        if not destination.resolve().is_relative_to(self.outputs.resolve()):
            raise ValueError("invalid evidence output path")
        destination.mkdir(parents=True, exist_ok=True)
        pending, page = destination / "viewer.pending.html", destination / "viewer.html"
        build_page([case], root=source, output=pending, receipt=receipt)
        pending.replace(page)
        metadata = {"schema_version": "0.1.0", "run_id": run_id, "deployment_id": deployment,
                    "source_sha": job["source_sha"], "page_sha256": sha256_file(page)}
        marker = destination / "page.pending.json"
        marker.write_text(json.dumps(metadata))
        marker.replace(destination / "page.json")
        return {"viewer": str(page), "mismatched": [], "files_checked": len(result["artifacts"])}

    def restore_pages(self, bridge: Bridge) -> None:
        for marker in self.outputs.glob("*/*/page.json"):
            if not RUN_ID.fullmatch(marker.parent.name) or not RUN_ID.fullmatch(marker.parent.parent.name):
                continue
            if not marker.resolve().is_relative_to(self.outputs.resolve()):
                continue
            try:
                value = json.loads(marker.read_text())
                page = marker.with_name("viewer.html")
                if value["run_id"] != marker.parent.name or value["deployment_id"] != marker.parent.parent.name:
                    continue
                if page.is_symlink() or sha256_file(page) != value["page_sha256"]:
                    continue
                bridge.pages[value["run_id"]] = page
                bridge.exports[value["run_id"]] = {"status": "ready", "url": "/evidence/" + value["run_id"]}
            except (OSError, ValueError, KeyError):
                continue


class ASGIConsole:
    def __init__(self, application: ConsoleApplication):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        method = scope["method"]
        headers = [(key.decode("latin-1"), value.decode("latin-1")) for key, value in scope["headers"]]
        rejection = self.application.authorized(method, headers)
        body = bytearray()
        response = rejection
        if response is None:
            try:
                while True:
                    event = await asyncio.wait_for(receive(), 10)
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > MAX_BODY:
                        response = json_response(413, {"error": "request too large"})
                        break
                    if not event.get("more_body"):
                        break
            except TimeoutError:
                response = json_response(408, {"error": "request body timeout"})
        if response is None:
            query = scope.get("query_string", b"").decode("ascii")
            path = scope["path"] + ("?" + query if query else "")
            response = await asyncio.to_thread(self.application.handle, method, path, headers, bytes(body))
        await send({"type": "http.response.start", "status": response.status,
                    "headers": [(k.encode(), v.encode()) for k, v in response.headers]})
        await send({"type": "http.response.body", "body": response.body})


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--endpoint", type=Path, default=Path("/broker/broker.sock"))
    parser.add_argument("--records", type=Path, default=Path("/records"))
    parser.add_argument("--releases", type=Path, default=Path("/releases"))
    parser.add_argument("--outputs", type=Path, default=Path("/outputs"))
    args = parser.parse_args()
    backend = CloudBackend(endpoint=args.endpoint, records=args.records, releases=args.releases,
                           outputs=args.outputs, source_sha=args.source_sha)
    bridge = Bridge(backend.request, backend.fetch)
    backend.restore_pages(bridge)
    application = ConsoleApplication(bridge, args.origin, tailnet=True, health=backend.health)
    uvicorn.run(ASGIConsole(application), host="0.0.0.0", port=8768, workers=1, lifespan="off",
                proxy_headers=False, access_log=False, limit_concurrency=16, timeout_keep_alive=5,
                timeout_graceful_shutdown=10, server_header=False, ws="none", http="h11")


if __name__ == "__main__":
    main()
