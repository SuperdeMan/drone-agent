"""MCP client used by the planner engine: a local stdio server process, or an in-process session for tests.

规划引擎使用的 MCP 客户端：本地 stdio 服务器进程，或供测试使用的进程内会话。
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

from drone_agent.planner.tools.catalog import ToolCatalog
from drone_agent.planner.tools.server import PROTOCOL_VERSION, handle

CLIENT_INFO = {"name": "drone-agent-planner", "version": "0.1.0"}


class ToolCallFailed(RuntimeError):
    """The server returned an error or an isError result. / 服务器返回错误或 isError 结果。"""


class _Session:
    def __init__(self):
        self._ids = itertools.count(1)
        self.server_info: dict = {}
        self.tools: list[dict] = []

    def _exchange(self, message: dict) -> dict | None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def _request(self, method: str, params: dict | None = None) -> dict:
        request_id = next(self._ids)
        reply = self._exchange({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        if reply is None or reply.get("id") != request_id:
            raise ToolCallFailed(f"no reply to {method}")
        if "error" in reply:
            raise ToolCallFailed(f"{method}: {reply['error'].get('message')}")
        return reply["result"]

    def initialize(self) -> _Session:
        result = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                              "clientInfo": CLIENT_INFO})
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise ToolCallFailed(f"unsupported MCP protocol {result.get('protocolVersion')}")
        self.server_info = result.get("serverInfo", {})
        self._exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = self._request("tools/list")["tools"]
        if any(not (tool.get("annotations") or {}).get("readOnlyHint") for tool in self.tools):
            raise ToolCallFailed("a planning tool is not declared read-only")
        return self

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Structured content of a successful call. / 成功调用的结构化内容。"""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            text = " ".join(part.get("text", "") for part in result.get("content", []))
            raise ToolCallFailed(f"{name}: {text}")
        return result["structuredContent"]


class InProcessSession(_Session):
    """Same protocol handling without a process, for deterministic tests. / 无进程的同协议处理，供确定性测试。"""

    def __init__(self, catalog: ToolCatalog):
        super().__init__()
        self.catalog = catalog

    def _exchange(self, message: dict) -> dict | None:
        # Round-trip through JSON so the in-process path sees exactly what a pipe would carry.
        # 经 JSON 往返，使进程内路径看到的与管道传输完全一致。
        reply = handle(self.catalog, json.loads(json.dumps(message)))
        return None if reply is None else json.loads(json.dumps(reply))

    def close(self) -> None:
        return None


class StdioSession(_Session):
    """Spawn `python -m drone_agent.planner.tools.server` and speak MCP over its pipes.

    启动 `python -m drone_agent.planner.tools.server`，经其管道使用 MCP 通信。
    """

    def __init__(self, root: Path, scene: Path, records: Path | None = None, *, python: str | None = None):
        super().__init__()
        command = [python or sys.executable, "-m", "drone_agent.planner.tools.server", "--root", str(root),
                   "--scene", str(scene)]
        if records is not None:
            command += ["--records", str(records)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, encoding="utf-8", bufsize=1)

    def _exchange(self, message: dict) -> dict | None:
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        if "id" not in message:
            return None
        line = self.process.stdout.readline()
        if not line:
            raise ToolCallFailed("tool server closed its output")
        return json.loads(line)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def __enter__(self) -> StdioSession:
        return self.initialize()

    def __exit__(self, *exc) -> None:
        self.close()
