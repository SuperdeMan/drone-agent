"""Allowlisted CONNECT proxy: the mission service's only way out, to the model endpoint (D036).

The mission service holds the approval signing key and the model key and joins internal networks only. This
process joins that internal network and one egress network, accepts `CONNECT host:port` only for the exact
host:port pairs it was started with, and then copies bytes both ways. TLS stays end to end, so it never sees a
request, an answer or a key. Anything else (another target, another method, an oversized or malformed head) is
refused. Every decision is logged as one JSON line with the target and the outcome, never content.

允许列表 CONNECT 代理：任务服务唯一的出站路径，只通往模型端点（D036）。任务服务持有审批签名密钥与模型 key，
只接入内部网络。本进程接入该内部网络与一个出站网络，只接受启动时给定的精确「主机:端口」的 `CONNECT`，随后
双向转发字节。TLS 端到端，因此它看不到请求、回答或 key。其他一切（别的目标、别的方法、过大或畸形的请求头）
都被拒绝。每个决定记录为一行 JSON，只含目标与结果，从不含内容。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone

MAX_HEAD = 8192
HEAD_TIMEOUT_S = 10.0
IDLE_S = 300.0
ENTRY = re.compile(r"^[a-z0-9.-]+:[0-9]{1,5}$")
REASONS = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 502: "Bad Gateway"}


def parse_allow(entries: list[str]) -> frozenset[str]:
    """Exact lower-case `host:port` entries; anything else is a configuration error. / 精确的小写「主机:端口」条目。"""
    allow = {entry.strip().lower() for entry in entries}
    if not allow or any(not ENTRY.fullmatch(entry) for entry in allow):
        raise ValueError("allowlist entries must be host:port")
    return frozenset(allow)


def request_target(head: bytes) -> tuple[str, str] | None:
    """(method, target) of the request line, or None when it is not a well-formed HTTP/1.x line.

    请求行的（方法，目标）；不是格式正确的 HTTP/1.x 请求行时为 None。
    """
    try:
        method, target, version = head.split(b"\r\n", 1)[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError):
        return None
    return (method, target) if version in ("HTTP/1.0", "HTTP/1.1") else None


class ModelProxy:
    def __init__(self, allow: frozenset[str], *, log=print, connect_timeout_s: float = 10.0, idle_s: float = IDLE_S):
        self.allow, self.log, self.connect_timeout_s, self.idle_s = allow, log, connect_timeout_s, idle_s

    def note(self, target: str, decision: str) -> None:
        self.log(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "target": target[:255],
                             "decision": decision}))

    @staticmethod
    async def refuse(writer: asyncio.StreamWriter, status: int) -> None:
        writer.write(f"HTTP/1.1 {status} {REASONS[status]}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        try:
            await writer.drain()
        except ConnectionError:
            pass

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError, ValueError):
                self.note("?", "malformed")
                return await self.refuse(writer, 400)
            parsed = request_target(head)
            if parsed is None:
                self.note("?", "malformed")
                return await self.refuse(writer, 400)
            method, target = parsed
            if method != "CONNECT":
                self.note(target, "method_refused")
                return await self.refuse(writer, 405)
            if target.lower() not in self.allow:
                self.note(target, "target_refused")
                return await self.refuse(writer, 403)
            host, _, port = target.lower().rpartition(":")
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(host, int(port)), self.connect_timeout_s)
            except (OSError, TimeoutError):
                self.note(target, "upstream_unreachable")
                return await self.refuse(writer, 502)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            self.note(target, "tunnel_opened")
            await self.splice(reader, writer, *upstream)
        finally:
            writer.close()

    async def splice(self, client_reader, client_writer, upstream_reader, upstream_writer) -> None:
        """Copy both directions until each side ends or stays idle too long. / 双向复制，直到各自结束或空闲超时。"""

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            try:
                while data := await asyncio.wait_for(source.read(65536), self.idle_s):
                    sink.write(data)
                    await sink.drain()
            except (ConnectionError, TimeoutError, OSError):
                pass
            finally:
                try:
                    sink.write_eof()
                except (OSError, RuntimeError):
                    pass

        try:
            await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer))
        finally:
            upstream_writer.close()


async def serve(proxy: ModelProxy, host: str, port: int) -> None:
    server = await asyncio.start_server(proxy.handle, host, port, limit=MAX_HEAD)
    proxy.log(json.dumps({"listening": f"{host}:{port}", "allow": sorted(proxy.allow)}))
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0:3128")
    parser.add_argument("--allow", action="append", required=True, help="exact host:port to tunnel to (repeatable)")
    args = parser.parse_args()
    host, _, port = args.listen.rpartition(":")
    proxy = ModelProxy(parse_allow(args.allow), log=lambda line: print(line, flush=True))
    asyncio.run(serve(proxy, host, int(port)))


if __name__ == "__main__":
    main()
