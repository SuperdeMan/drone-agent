"""Length-prefixed `AutonomyFrame` codec for the local autonomy sockets (D039).

A frame is a 4-byte big-endian length followed by one serialized `drone.autonomy.v1.AutonomyFrame`. Zero,
oversized, truncated or body-less frames are protocol errors: the connection is dropped, never guessed at.
Decoding reuses the presence-preserving `runtime.wire` codec and then the domain model, so a frame that
parses as protobuf but violates a rule is still rejected.

本地自主层套接字的带长度前缀 `AutonomyFrame` 编解码（D039）。

一帧是 4 字节大端长度后接一个序列化的 `drone.autonomy.v1.AutonomyFrame`。长度为零、超长、截断或没有消息体
的帧都是协议错误：断开连接，不做猜测。解码复用保留字段存在性的 `runtime.wire`，再经过领域模型，因此能按
protobuf 解析但违反规则的帧仍被拒收。
"""

from __future__ import annotations

import asyncio
import importlib

from pydantic import BaseModel

from drone_agent.autonomy.messages import MODELS
from drone_agent.runtime.wire import decode, encode

MAX_FRAME_BYTES = 64 * 1024
HEADER_BYTES = 4


class FrameError(ValueError):
    """A malformed frame; the peer connection must be closed. / 格式错误的帧；必须关闭对端连接。"""


def stub():
    return importlib.import_module("drone.autonomy.v1.autonomy_pb2")


def kind_of(model: BaseModel) -> str:
    for kind, cls in MODELS.items():
        if type(model) is cls:
            return kind
    raise TypeError(f"not an autonomy message: {type(model).__name__}")


def pack(model: BaseModel) -> bytes:
    """Serialize one domain model into a length-prefixed frame. / 把一个领域模型序列化为带长度前缀的帧。"""
    pb = stub()
    kind = kind_of(model)
    frame = pb.AutonomyFrame()
    body = getattr(frame, kind)
    body.CopyFrom(encode(model, type(body)()))
    data = frame.SerializeToString()
    if not 0 < len(data) <= MAX_FRAME_BYTES:
        raise FrameError("frame size outside the protocol bounds")
    return len(data).to_bytes(HEADER_BYTES, "big") + data


def unpack(data: bytes) -> tuple[str, BaseModel]:
    """Parse one frame body (without the length header) into (kind, validated model).

    把一个帧体（不含长度头）解析为（类型，已校验模型）。
    """
    pb = stub()
    try:
        frame = pb.AutonomyFrame.FromString(data)
    except Exception as error:  # noqa: BLE001 - any protobuf parse failure is a protocol error / 任何 protobuf 解析失败都是协议错误
        raise FrameError("unparseable frame") from error
    kind = frame.WhichOneof("body")
    if kind is None or kind not in MODELS:
        raise FrameError("frame without a known body")
    return kind, MODELS[kind].model_validate(decode(getattr(frame, kind)))


async def read_frame(reader: asyncio.StreamReader) -> tuple[str, BaseModel]:
    """Read and validate one frame; raises FrameError, ValidationError or IncompleteReadError.

    读取并校验一帧；可能抛出 FrameError、ValidationError 或 IncompleteReadError。
    """
    header = await reader.readexactly(HEADER_BYTES)
    size = int.from_bytes(header, "big")
    if not 0 < size <= MAX_FRAME_BYTES:
        raise FrameError("frame size outside the protocol bounds")
    return unpack(await reader.readexactly(size))
