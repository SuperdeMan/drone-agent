"""Role-bound local socket endpoints for the autonomy protocol (D039).

Each role has its own socket in its own directory, and only the container of that role mounts it: the
egress node can only report status and receive authorized setpoints; the planner, local map and
localization nodes can only submit candidates and reports and receive the local task; perception and event
detection can only submit belief facts. A peer that sends a kind outside its role, or a malformed frame, is
disconnected. Domain-invalid messages are counted and dropped. The endpoint keeps only the latest valid
message of each kind with its local receipt time; freshness decisions stay with the owner process.

自主层协议按角色划分的本地套接字端点（D039）。

每个角色有自己目录下的套接字，只有该角色的容器挂载它：出口节点只能上报状态并接收授权设定值；规划、局部
地图与定位节点只能提交候选与报告并接收局部任务；感知与事件检测只能提交信念事实。发送角色范围之外的消息
或格式错误帧的对端会被断开；不满足领域规则的消息计数后丢弃。端点只保留每类最新的有效消息及本地接收时间；
新鲜度判断留给所属进程。
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ValidationError

from drone_agent.autonomy.frames import FrameError, kind_of, pack, read_frame


class Role(StrEnum):
    EGRESS = "egress"
    AUTONOMY = "autonomy"
    BELIEF = "belief"


INBOUND: dict[Role, frozenset[str]] = {
    Role.EGRESS: frozenset({"egress_status"}),
    Role.AUTONOMY: frozenset({"trajectory_segment", "obstacle_set", "localization_report"}),
    Role.BELIEF: frozenset({"belief_fact"}),
}
OUTBOUND: dict[Role, frozenset[str]] = {
    Role.EGRESS: frozenset({"authorized_setpoint", "authorization_revoked"}),
    Role.AUTONOMY: frozenset({"local_task"}),
    Role.BELIEF: frozenset(),
}
WRITE_TIMEOUT_S = 0.05


@dataclass
class Received:
    """The latest valid message of one kind and when this process received it. / 某类最新有效消息及本进程接收时间。"""

    model: BaseModel
    monotonic: float


@dataclass
class Counters:
    received: int = 0
    rejected: int = 0
    protocol_errors: int = 0
    connections: int = 0
    disconnects: int = 0
    sent: int = 0
    send_failures: int = 0
    last_reject: str = ""
    by_kind: dict[str, int] = field(default_factory=dict)


def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """The connecting process UID on Linux Unix sockets; None where unavailable. / Linux Unix 套接字对端 UID；不可用时为 None。"""
    sock = writer.get_extra_info("socket")
    option = getattr(socket, "SO_PEERCRED", None)
    if sock is None or option is None or sock.family != getattr(socket, "AF_UNIX", None):
        return None
    _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i")))
    return uid


class LocalEndpoint:
    """A server socket for one role. / 单一角色的服务端套接字。"""

    def __init__(
        self,
        role: Role,
        path: Path | None = None,
        *,
        on_message: Callable[[str, BaseModel], None] | None = None,
        on_reject: Callable[[str, str], None] | None = None,
        expected_uid: int | None = None,
        test_tcp: tuple[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if path is None and test_tcp is None:
            raise ValueError("a production endpoint needs a private Unix socket path")
        if test_tcp is not None and test_tcp[0] != "127.0.0.1":
            raise ValueError("the test transport is loopback only")
        self.role, self.path, self.test_tcp, self.clock = role, path, test_tcp, clock
        self.on_message, self.on_reject = on_message, on_reject
        self.expected_uid = expected_uid if expected_uid is not None else (os.getuid() if hasattr(os, "getuid") else None)
        self.latest: dict[str, Received] = {}
        self.counters = Counters()
        self.peers: set[asyncio.StreamWriter] = set()
        self.server: asyncio.base_events.Server | None = None
        self.port: int | None = None

    async def start(self) -> None:
        if self.test_tcp is not None:
            self.server = await asyncio.start_server(self._serve, *self.test_tcp)
            self.port = self.server.sockets[0].getsockname()[1]
            return
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        if self.path.is_socket() or self.path.exists():
            self.path.unlink()
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        for writer in list(self.peers):
            writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def connected(self) -> bool:
        return bool(self.peers)

    def reject(self, kind: str, reason: str) -> None:
        self.counters.rejected += 1
        self.counters.last_reject = f"{kind}:{reason}"[:200]
        if self.on_reject:
            self.on_reject(kind, reason)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = peer_uid(writer)
        if uid is not None and self.expected_uid is not None and uid != self.expected_uid:
            self.reject("connection", "peer_uid_mismatch")
            writer.close()
            return
        self.peers.add(writer)
        self.counters.connections += 1
        try:
            while True:
                try:
                    kind, model = await read_frame(reader)
                except ValidationError as error:
                    self.reject("frame", "invalid:" + str(error.errors()[0].get("msg", ""))[:120])
                    continue
                if kind not in INBOUND[self.role]:
                    # A peer speaking outside its role is cut off, not tolerated. / 越出角色的对端直接断开。
                    self.reject(kind, "kind_outside_role")
                    self.counters.protocol_errors += 1
                    break
                self.counters.received += 1
                self.counters.by_kind[kind] = self.counters.by_kind.get(kind, 0) + 1
                self.latest[kind] = Received(model, self.clock())
                if self.on_message:
                    self.on_message(kind, model)
        except FrameError:
            self.counters.protocol_errors += 1
            self.reject("frame", "malformed")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # The peer went away; the owner judges freshness from the last receipt. / 对端离开；新鲜度由所属进程按最后接收时间判断。
        finally:
            self.peers.discard(writer)
            self.counters.disconnects += 1
            writer.close()

    def fresh(self, kind: str, max_age_s: float) -> BaseModel | None:
        """The latest message of `kind` if received within `max_age_s`. / `max_age_s` 内收到的最新 `kind` 消息。"""
        item = self.latest.get(kind)
        if item is None or self.clock() - item.monotonic > max_age_s:
            return None
        return item.model

    async def send(self, model: BaseModel) -> int:
        """Write one outbound frame to every connected peer of this role; returns how many accepted it.

        向本角色所有已连接对端写一帧出站消息；返回写入成功的数量。
        """
        kind = kind_of(model)
        if kind not in OUTBOUND[self.role]:
            raise ValueError(f"{kind} is not an outbound message of the {self.role.value} role")
        data = pack(model)
        written = 0
        for writer in list(self.peers):
            try:
                writer.write(data)
                await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)
                written += 1
            except (TimeoutError, ConnectionError, OSError):
                self.counters.send_failures += 1
                self.peers.discard(writer)
                writer.close()
        self.counters.sent += written
        return written
