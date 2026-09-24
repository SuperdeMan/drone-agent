"""One private origin for the mission desk and the fixed-flight console (D037).

任务台与固定飞行控制台共用一个私网来源（D037）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from drone_agent.console.application import ConsoleApplication, json_response
from drone_agent.console.cloud import ASGIConsole, CloudBackend
from drone_agent.console.server import Bridge


class UnifiedConsole:
    """Route fixed tasks to the existing broker, signed missions to the service. / 固定任务走既有代理，签名任务走服务。"""

    def __init__(self, mission, backend: CloudBackend):
        self.mission, self.backend = mission, backend
        self.mission.fixed = True
        bridge = Bridge(backend.request, backend.fetch)
        backend.restore_pages(bridge)
        self.fixed = ASGIConsole(ConsoleApplication(bridge, mission.origin, tailnet=mission.tailnet,
                                                    health=backend.health, base_path="/fixed"))

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = self.mission.headers(scope)
            problem = self.mission.rejected(headers, browser=False)
            if problem:
                return await self.mission.respond(send, json_response(403, {"error": problem}))
            path = scope["path"]
            if path == "/health" and scope["method"] == "GET":
                try:
                    result = await self.mission.api.call("health", "", "anonymous")
                    fixed = await asyncio.to_thread(self.backend.health)
                    revision = self.mission.source_sha
                    ok = (result.get("ok") and result.get("result", {}).get("source_sha") == revision
                          and fixed.get("runtime_source_sha") == revision)
                    value = {**result, "ok": bool(ok), "console_source_sha": revision, "fixed": fixed,
                             "supervisor": self.mission.host_status(), "modes": ["mission", "fixed"]}
                except (OSError, TimeoutError, RuntimeError, ValueError, KeyError) as error:
                    value = {"ok": False, "error": f"flight desk unavailable ({type(error).__name__})"}
                return await self.mission.respond(send, json_response(200 if value["ok"] else 503, value))
            if path == "/fixed" or path.startswith("/fixed/"):
                if scope["method"] == "POST" and not self.mission.identity(headers):
                    return await self.mission.respond(send, json_response(403, {"error": "operator identity required"}))
                mounted = {**scope, "path": path[len("/fixed"):] or "/"}
                return await self.fixed(mounted, receive, send)
        return await self.mission(scope, receive, send)


def unified_console(mission, root: Path):
    """Only project-scoped mounts, no host control socket or secrets. / 仅项目范围挂载，无宿主控制套接字或密钥。"""
    backend = CloudBackend(endpoint=root / "broker/broker.sock", records=root / "records", releases=root / "releases",
                           outputs=root / "outputs", source_sha=mission.source_sha)
    return UnifiedConsole(mission, backend)
