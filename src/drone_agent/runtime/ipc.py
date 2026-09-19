"""Authenticated local protobuf RPC; TCP is reserved for loopback tests.

认证的本地 protobuf RPC；TCP 仅供回环测试。
"""

import importlib

import grpc

from drone_agent.contracts import ControlCommandEnvelope, MissionOperation, TaskLease
from drone_agent.runtime.wire import decode, encode


def stubs():
    return (
        importlib.import_module("drone.contracts.v1.contracts_pb2"),
        importlib.import_module("drone.control.v1.control_pb2"),
        importlib.import_module("drone.control.v1.control_pb2_grpc"),
    )


async def serve(guardian, address, *, test_tcp=False):
    shared, control, rpc = stubs()
    if not address.startswith("unix:") and not (test_tcp and address.startswith("127.0.0.1:")):
        raise ValueError("guardian requires a private Unix socket")

    class Service(rpc.GuardianControlServicer):
        async def checked(self, context, operation):
            if not context.auth_context().get("transport_security_type") == [b"local"]:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "local credentials required")
            try:
                result = operation()
                import inspect

                return await result if inspect.isawaitable(result) else result
            except (ValueError, KeyError, TypeError) as error:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))

        async def InstallLease(self, request, context):
            value = await self.checked(
                context, lambda: guardian.install_lease(TaskLease.model_validate(decode(request)))
            )
            return encode(value, shared.EgressDecision())

        async def Heartbeat(self, request, context):
            value = await self.checked(context, lambda: guardian.heartbeat(decode(request)))
            return encode(value, control.GuardianStatus())

        async def SubmitIntent(self, request, context):
            # Shield accepted dispatch from a client RPC deadline. / 客户端 RPC 截止不能取消已接受的派发。
            import asyncio

            value = await self.checked(
                context, lambda: asyncio.shield(guardian.submit(ControlCommandEnvelope.model_validate(decode(request))))
            )
            return encode(value, shared.EgressDecision())

        async def ReconcileCommand(self, request, context):
            from drone_agent.contracts import IdempotencyKey

            key = IdempotencyKey.model_validate(decode(request))
            value = await self.checked(context, lambda: guardian.ledger.reconcile(key.as_string()))
            receipt = {"not_received": "not_received", "accepted": "recorded", "unknown": "unknown"}[value["receipt"]]
            return encode(
                {
                    "schema_version": "0.1.0",
                    "key": key.model_dump(),
                    "receipt_status": receipt,
                    "observed_at": guardian.status()["timestamp"],
                    "reason": value.get("reason", ""),
                },
                control.CommandReconciliation(),
            )

        async def GetObservation(self, request, context):
            if request.robot_id != guardian.registry.capability.robot_id:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "robot mismatch")
            value = await self.checked(context, guardian.adapter.snapshot)
            return encode(value, shared.FlightObservation())

        async def Operate(self, request, context):
            value = await self.checked(
                context, lambda: guardian.operate(MissionOperation.model_validate(decode(request)))
            )
            return encode(value, control.GuardianStatus())

    server = grpc.aio.server(options=[("grpc.max_receive_message_length", 1024 * 1024)])
    rpc.add_GuardianControlServicer_to_server(Service(), server)
    kind = grpc.LocalConnectionType.LOCAL_TCP if test_tcp else grpc.LocalConnectionType.UDS
    if not server.add_secure_port(address, grpc.local_server_credentials(kind)):
        raise RuntimeError("could not bind local guardian endpoint")
    await server.start()
    return server


class GuardianClient:
    def __init__(self, address, *, test_tcp=False):
        self.shared, self.control, rpc = stubs()
        if not address.startswith("unix:") and not (test_tcp and address.startswith("127.0.0.1:")):
            raise ValueError("only local guardian endpoints are allowed")
        kind = grpc.LocalConnectionType.LOCAL_TCP if test_tcp else grpc.LocalConnectionType.UDS
        self.channel = grpc.aio.secure_channel(address, grpc.local_channel_credentials(kind))
        self.stub = rpc.GuardianControlStub(self.channel)

    async def install(self, lease):
        return decode(await self.stub.InstallLease(encode(lease, self.shared.TaskLease()), timeout=2))

    async def heartbeat(self, pulse):
        return decode(await self.stub.Heartbeat(encode(pulse, self.control.ExecutiveHeartbeat()), timeout=1))

    async def observation(self, robot_id):
        from drone_agent.contracts import FlightObservation

        return FlightObservation.model_validate(
            decode(
                await self.stub.GetObservation(
                    self.shared.ObservationRequest(schema_version="0.1.0", robot_id=robot_id), timeout=1
                )
            )
        )

    async def submit(self, envelope, timeout=0.75):
        return decode(
            await self.stub.SubmitIntent(encode(envelope, self.shared.ControlCommandEnvelope()), timeout=timeout)
        )

    async def reconcile(self, key):
        return decode(await self.stub.ReconcileCommand(encode(key, self.shared.IdempotencyKey()), timeout=1))

    async def operate(self, operation):
        return decode(await self.stub.Operate(encode(operation, self.shared.MissionOperation()), timeout=2))

    async def close(self):
        await self.channel.close()
