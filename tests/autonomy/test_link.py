"""Role-bound endpoints cut off peers that speak outside their role (D039).

按角色划分的端点会断开越出角色的对端（D039）。
"""

import asyncio
from datetime import timedelta

import pytest

from drone_agent.autonomy.frames import pack, read_frame, stub
from drone_agent.autonomy.link import LocalEndpoint, Role
from drone_agent.autonomy.messages import AuthorizedSetpoint, LocalTask, Vector3
from drone_agent.contracts import utcnow
from tests.autonomy.test_messages_and_frames import segment


def setpoint(seq=0):
    return AuthorizedSetpoint(robot_id="uav_01", mission_id="m", mission_version=1, step_id="s", lease_epoch=1,
                              command_seq=seq, issued_at=utcnow(), ttl_ms=300, position_ned=Vector3(x=1, y=0, z=-4),
                              max_horizontal_speed_mps=1, max_vertical_speed_mps=1)


def task():
    now = utcnow()
    return LocalTask(robot_id="uav_01", mission_id="m", mission_version=1, step_id="s", lease_epoch=1,
                     task_id="s.1.1", frame_id="map_enu", map_version="campus_v3", goal=Vector3(x=0, y=28, z=4),
                     goal_tolerance_m=0.8, max_speed_mps=2, scope_min=Vector3(x=-40, y=-40, z=2),
                     scope_max=Vector3(x=40, y=40, z=10), issued_at=now, deadline=now + timedelta(seconds=60))


async def endpoint(role, **kwargs):
    server = LocalEndpoint(role, test_tcp=("127.0.0.1", 0), **kwargs)
    await server.start()
    return server


async def settle(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_autonomy_role_accepts_candidates_and_serves_the_task():
    seen = []
    server = await endpoint(Role.AUTONOMY, on_message=lambda kind, model: seen.append(kind))
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(pack(segment()))
    await writer.drain()
    await settle(lambda: seen == ["trajectory_segment"])
    assert server.fresh("trajectory_segment", 1.0) is not None
    assert await server.send(task()) == 1
    kind, received = await read_frame(reader)
    assert kind == "local_task" and received.task_id == "s.1.1"
    with pytest.raises(ValueError):
        await server.send(setpoint())
    writer.close()
    await server.close()


async def test_a_peer_speaking_outside_its_role_is_disconnected():
    server = await endpoint(Role.AUTONOMY)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(pack(setpoint()))
    await writer.drain()
    await settle(lambda: server.counters.protocol_errors == 1)
    assert server.latest == {} and not server.connected
    assert await reader.read() == b""
    await server.close()


async def test_invalid_messages_are_dropped_and_malformed_frames_disconnect():
    server = await endpoint(Role.AUTONOMY)
    _, writer = await asyncio.open_connection("127.0.0.1", server.port)
    frame = stub().AutonomyFrame()
    frame.trajectory_segment.schema_version = "0.1.0"  # Missing every required field. / 缺少全部必填字段。
    body = frame.SerializeToString()
    writer.write(len(body).to_bytes(4, "big") + body + pack(segment()))
    await writer.drain()
    await settle(lambda: server.counters.received == 1)
    assert server.counters.rejected == 1 and server.connected
    writer.write((0).to_bytes(4, "big"))
    await writer.drain()
    await settle(lambda: not server.connected)
    assert server.counters.protocol_errors == 1
    await server.close()


async def test_egress_role_only_sends_authorized_setpoints():
    server = await endpoint(Role.EGRESS)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    await settle(lambda: server.connected)
    assert await server.send(setpoint(3)) == 1
    kind, received = await read_frame(reader)
    assert kind == "authorized_setpoint" and received.command_seq == 3
    with pytest.raises(ValueError):
        await server.send(task())
    writer.close()
    await server.close()


def test_production_endpoints_need_a_unix_path():
    with pytest.raises(ValueError):
        LocalEndpoint(Role.EGRESS)
    with pytest.raises(ValueError):
        LocalEndpoint(Role.EGRESS, test_tcp=("0.0.0.0", 0))
