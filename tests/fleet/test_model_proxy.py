"""The mission service's egress proxy tunnels only to the allowlisted model endpoint (D036).

任务服务的出站代理只为允许列表中的模型端点建立隧道（D036）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from drone_agent.fleet.model_proxy import MAX_HEAD, ModelProxy, parse_allow, request_target


async def started(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0, limit=MAX_HEAD)
    return server, server.sockets[0].getsockname()[1]


async def echo(reader, writer):
    while data := await reader.read(1024):
        writer.write(b"echo:" + data)
        await writer.drain()
    writer.close()


async def exchange(port: int, head: bytes, body: bytes = b"") -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    reply = b""
    if body and status.startswith(b"HTTP/1.1 200"):
        writer.write(body)
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(len(body) + 5), 5)
    writer.close()
    return status, reply


@pytest.fixture
async def proxy():
    upstream, upstream_port = await started(echo)
    connected = []

    async def stranger(reader, writer):
        connected.append(True)
        writer.close()

    other, other_port = await started(stranger)
    lines: list[str] = []
    model_proxy = ModelProxy(parse_allow([f"127.0.0.1:{upstream_port}"]), log=lines.append)
    server, port = await started(model_proxy.handle)
    yield port, upstream_port, other_port, connected, lines
    for item in (server, upstream, other):
        item.close()


async def test_an_allowlisted_target_is_tunnelled_end_to_end(proxy):
    port, upstream_port, _, _, lines = proxy
    status, reply = await exchange(port, f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
                                   b"secret payload")
    assert status.startswith(b"HTTP/1.1 200") and reply == b"echo:secret payload"
    decisions = [json.loads(line) for line in lines]
    assert decisions[-1]["decision"] == "tunnel_opened" and "secret" not in "".join(lines)


async def test_any_other_target_method_or_head_is_refused_without_connecting(proxy):
    port, _, other_port, connected, lines = proxy
    status, _ = await exchange(port, f"CONNECT 127.0.0.1:{other_port} HTTP/1.1\r\n\r\n".encode())
    assert status.startswith(b"HTTP/1.1 403") and connected == []
    status, _ = await exchange(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    assert status.startswith(b"HTTP/1.1 403")
    status, _ = await exchange(port, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert status.startswith(b"HTTP/1.1 405")
    status, _ = await exchange(port, b"CONNECT " + b"a" * (MAX_HEAD + 10) + b" HTTP/1.1\r\n\r\n")
    assert status.startswith(b"HTTP/1.1 400")
    status, _ = await exchange(port, b"BROKEN\r\n\r\n")
    assert status.startswith(b"HTTP/1.1 400")
    assert [json.loads(line)["decision"] for line in lines] == [
        "target_refused", "target_refused", "method_refused", "malformed", "malformed"]


def test_the_allowlist_is_exact_host_and_port():
    assert parse_allow(["API.minimaxi.com:443 "]) == frozenset({"api.minimaxi.com:443"})
    for bad in ([], ["api.minimaxi.com"], ["*.minimaxi.com:443"], ["https://api.minimaxi.com:443"]):
        with pytest.raises(ValueError):
            parse_allow(bad)
    assert request_target(b"CONNECT api.minimaxi.com:443 HTTP/1.1\r\n\r\n") == ("CONNECT", "api.minimaxi.com:443")
    assert request_target(b"CONNECT api.minimaxi.com:443 SPDY/3\r\n\r\n") is None
