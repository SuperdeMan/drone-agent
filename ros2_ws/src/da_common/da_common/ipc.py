"""Framed Unix-socket client for the ROS 2 side of drone.autonomy.v1 (D039).

Runs on the system Python with the system protobuf; imports nothing from the drone_agent package (D014). A background
thread keeps one connection to a guardian or executive socket, reconnecting every half second, and hands every
received frame to a callback. Frames are a 4-byte big-endian length plus one serialized AutonomyFrame, at most
64 KiB; anything else closes the connection.

drone.autonomy.v1 在 ROS 2 一侧的带帧 Unix 套接字客户端（D039）。

运行在系统 Python 与系统 protobuf 上；不从 drone_agent 包导入任何内容（D014）。后台线程与 guardian 或 executive 的
套接字保持一条连接，断开后每半秒重连，并把收到的每一帧交给回调。帧是 4 字节大端长度加一个序列化的
AutonomyFrame，最大 64 KiB；其他内容一律关闭连接。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from datetime import datetime, timezone

from drone.autonomy.v1 import autonomy_pb2 as pb

MAX_FRAME_BYTES = 64 * 1024
SCHEMA_VERSION = "0.1.0"


def stamp(message, when: datetime | None = None) -> None:
    """Fill a google.protobuf.Timestamp from an aware UTC datetime. / 用带时区 UTC 时间填充 Timestamp。"""
    when = when or datetime.now(timezone.utc)
    message.FromDatetime(when.astimezone(timezone.utc).replace(tzinfo=None))


def to_datetime(message) -> datetime:
    return message.ToDatetime().replace(tzinfo=timezone.utc)


def vector(message, values) -> None:
    message.schema_version = SCHEMA_VERSION
    message.x, message.y, message.z = (float(v) for v in values)


class FrameClient:
    def __init__(self, path: str, on_frame=None, *, name: str = "client"):
        self.path, self.on_frame, self.name = path, on_frame, name
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"{name}-ipc", daemon=True)
        self.sent = self.send_failures = self.received = 0

    def start(self) -> FrameClient:
        self.thread.start()
        return self

    def close(self) -> None:
        self.stopped.set()
        self._drop()

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def _drop(self) -> None:
        with self.lock:
            sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def send(self, frame: pb.AutonomyFrame) -> bool:
        body = frame.SerializeToString()
        if not 0 < len(body) <= MAX_FRAME_BYTES:
            raise ValueError("frame size outside the protocol bounds")
        with self.lock:
            sock = self.sock
            if sock is None:
                self.send_failures += 1
                return False
            try:
                sock.sendall(struct.pack(">I", len(body)) + body)
                self.sent += 1
                return True
            except OSError:
                self.send_failures += 1
        self._drop()
        return False

    def _read_exactly(self, sock: socket.socket, size: int) -> bytes | None:
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _run(self) -> None:
        while not self.stopped.is_set():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.path)
            except OSError:
                sock.close()
                time.sleep(0.5)
                continue
            with self.lock:
                self.sock = sock
            try:
                while not self.stopped.is_set():
                    header = self._read_exactly(sock, 4)
                    if header is None:
                        break
                    size = struct.unpack(">I", header)[0]
                    if not 0 < size <= MAX_FRAME_BYTES:
                        break
                    body = self._read_exactly(sock, size)
                    if body is None:
                        break
                    frame = pb.AutonomyFrame.FromString(body)
                    self.received += 1
                    if self.on_frame is not None:
                        self.on_frame(frame)
            except (OSError, ValueError):
                pass
            finally:
                self._drop()
