"""FleetTransport between the mission service and robot uplinks: mTLS gRPC plus an in-process twin (WP-M2-12).

The robot dials out (D031); the service never connects to an aircraft. Every call is bound to the robot
identity in the client certificate's URI SAN (D030): a message naming another robot, or a mission that
was not delivered to this robot, is refused. What travels is tasks and evidence only: signed packages
and operator requests down, journal rows, evidence, media, status and capability up. `DeliverPackage`,
`PublishFact` and `OfferHandoff` stay unimplemented in M2 (push delivery, onboard facts and handoff are
later milestones). Ingest is at-least-once; the ledger deduplicates.

任务服务与机器人 uplink 之间的 FleetTransport：mTLS gRPC 及其进程内等价实现（WP-M2-12）。

机器人主动拨出（D031）；服务从不连接飞行器。每次调用都绑定到客户端证书 URI SAN 中的机器人身份
（D030）：报文中写了别的机器人，或写了并未投递给该机器人的任务，一律拒绝。传输的只有任务与证据：
下行是已签名任务包与操作请求，上行是账本行、证据、媒体、状态与能力。M2 不实现 `DeliverPackage`、
`PublishFact` 与 `OfferHandoff`（推送投递、机载事实与交接属于后续里程碑）。入账至少一次，账本去重。
"""

from __future__ import annotations

import hashlib
import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from drone_agent.contracts import (
    CapabilityDescriptor,
    Evidence,
    ExecutionEvent,
    MissionPackage,
    OperatorRequest,
    RobotStatus,
    utcnow,
)
from drone_agent.fleet.pki import robot_id_from_uri

if TYPE_CHECKING:
    from drone_agent.fleet.ledger import BusinessLedger

RAW_RGB = "image/x-raw-rgb"
MAX_MEDIA_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Receipt:
    """What the service answered for one published item. / 服务对一次发布的答复。"""

    accepted: bool
    reason: str = ""
    message_id: str = ""


class FleetHub:
    """Service-side handlers; the caller passes the authenticated robot id. / 服务端处理；调用方传入已认证的机器人 ID。"""

    def __init__(self, ledger: BusinessLedger, media_dir: Path):
        self.ledger, self.media_dir = ledger, media_dir
        self.listeners: list[Callable[[str, str, str | None], None]] = []

    def _notify(self, robot_id: str, kind: str, mission_id: str | None) -> None:
        for listener in self.listeners:
            listener(robot_id, kind, mission_id)

    def _bound(self, robot_id: str, claimed: str | None, mission_id: str | None = None) -> Receipt | None:
        """None when the item may be ingested, else the refusal. / 可入账时为 None，否则为拒绝。"""
        if not robot_id or claimed != robot_id:
            return Receipt(False, "auth.robot_mismatch")
        if mission_id is not None:
            mission = self.ledger.mission(mission_id)
            if mission is None or mission["robot_id"] != robot_id:
                return Receipt(False, "service.not_found")
        return None

    def publish_event(self, robot_id: str, event: ExecutionEvent) -> Receipt:
        refused = self._bound(robot_id, event.robot_id, event.mission_id)
        if refused:
            return refused
        if not {"journal", "seq", "sha256", "previous", "kind", "data"} <= set(event.data):
            return Receipt(False, "service.invalid_request", event.event_id)
        inserted = self.ledger.record_event(robot_id, event)
        if inserted:
            self._notify(robot_id, "event", event.mission_id)
        return Receipt(True, "" if inserted else "duplicate", event.event_id)

    def publish_evidence(self, robot_id: str, evidence: Evidence) -> Receipt:
        if not evidence.mission_id or evidence.mission_version is None:
            return Receipt(False, "service.invalid_request", evidence.evidence_id)
        refused = self._bound(robot_id, evidence.robot_id, evidence.mission_id)
        if refused:
            return refused
        self.ledger.record_evidence(robot_id, evidence)
        self._notify(robot_id, "evidence", evidence.mission_id)
        return Receipt(True, "", evidence.evidence_id)

    def publish_media(self, robot_id: str, media: dict) -> Receipt:
        """Store media whose digest matches the evidence already recorded. / 存储与已记录证据摘要一致的媒体。"""
        if not media.get("mission_id") or not media.get("evidence_id"):
            return Receipt(False, "service.invalid_request")
        refused = self._bound(robot_id, media.get("robot_id"), media["mission_id"])
        if refused:
            return refused
        data, digest = media.get("data") or b"", media.get("sha256", "")
        width, height = int(media.get("width") or 0), int(media.get("height") or 0)
        known = {row["evidence_id"]: row for row in self.ledger.evidence(media["mission_id"])}
        row = known.get(media["evidence_id"])
        if row is None or row["robot_id"] != robot_id or row["sha256"] != digest:
            return Receipt(False, "service.not_found", media["evidence_id"])
        if (len(data) > MAX_MEDIA_BYTES or hashlib.sha256(data).hexdigest() != digest
                or media.get("media_type") != RAW_RGB or len(data) != width * height * 3):
            return Receipt(False, "service.invalid_request", media["evidence_id"])
        self.media_dir.mkdir(parents=True, exist_ok=True)
        path = self.media_dir / f"{digest}.rgb"
        if not path.exists():
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(data)
            os.replace(temporary, path)
        self.ledger.attach_media(media["evidence_id"], path.name, RAW_RGB, width, height)
        self._notify(robot_id, "media", media["mission_id"])
        return Receipt(True, "", media["evidence_id"])

    def media(self, name: str) -> bytes | None:
        path = (self.media_dir / name).resolve()
        if not path.is_relative_to(self.media_dir.resolve()) or not path.is_file():
            return None
        return path.read_bytes()

    def publish_status(self, robot_id: str, status: RobotStatus) -> Receipt:
        refused = self._bound(robot_id, status.robot_id)
        if refused:
            return refused
        self.ledger.record_status(status)
        self._notify(robot_id, "status", None)
        return Receipt(True)

    def publish_capability(self, robot_id: str, capability: CapabilityDescriptor) -> Receipt:
        refused = self._bound(robot_id, capability.robot_id)
        if refused:
            return refused
        self.ledger.record_capability(capability)
        self._notify(robot_id, "capability", None)
        return Receipt(True)

    def fetch_deliveries(self, robot_id: str, claimed: str, after_cursor: int, max_items: int) -> dict:
        if not robot_id or claimed != robot_id:
            return {"items": [], "cursor": after_cursor, "refused": "auth.robot_mismatch"}
        rows = self.ledger.deliveries_after(robot_id, after_cursor, max(1, min(max_items, 10)))
        items = []
        for row in rows:
            payload = row["payload"]
            items.append({
                "delivery_id": row["delivery_id"], "cursor": row["cursor"], "kind": row["kind"],
                "package": MissionPackage.model_validate(payload) if row["kind"] == "mission_package" else None,
                "operation": OperatorRequest.model_validate(payload) if row["kind"] == "operator_request" else None,
                "issued_at": row["created_at"],
            })
        return {"items": items, "cursor": rows[-1]["cursor"] if rows else after_cursor}

    def acknowledge(self, robot_id: str, claimed: str, delivery_id: str, accepted: bool, reason: str) -> Receipt:
        if not robot_id or claimed != robot_id:
            return Receipt(False, "auth.robot_mismatch")
        if not self.ledger.ack_delivery(robot_id, delivery_id, accepted, reason):
            return Receipt(False, "service.not_found", delivery_id)
        delivery = self.ledger.delivery(delivery_id)
        self._notify(robot_id, "ack", delivery["mission_id"])
        return Receipt(True, "", delivery_id)


class LocalFleetClient:
    """The uplink's client interface bound to a hub in the same process (tests, local runs).

    与同进程 hub 绑定的 uplink 客户端接口（测试、本地运行）。
    """

    def __init__(self, hub: FleetHub, robot_id: str):
        self.hub, self.robot_id = hub, robot_id

    async def publish_event(self, event: ExecutionEvent) -> Receipt:
        return self.hub.publish_event(self.robot_id, event)

    async def publish_evidence(self, evidence: Evidence) -> Receipt:
        return self.hub.publish_evidence(self.robot_id, evidence)

    async def publish_media(self, media: dict) -> Receipt:
        return self.hub.publish_media(self.robot_id, media)

    async def publish_status(self, status: RobotStatus) -> Receipt:
        return self.hub.publish_status(self.robot_id, status)

    async def publish_capability(self, capability: CapabilityDescriptor) -> Receipt:
        return self.hub.publish_capability(self.robot_id, capability)

    async def fetch_deliveries(self, after_cursor: int, max_items: int = 10) -> dict:
        return self.hub.fetch_deliveries(self.robot_id, self.robot_id, after_cursor, max_items)

    async def acknowledge(self, delivery_id: str, accepted: bool, reason: str = "") -> Receipt:
        return self.hub.acknowledge(self.robot_id, self.robot_id, delivery_id, accepted, reason)

    async def close(self) -> None:
        return None


def _stubs():
    return (importlib.import_module("drone.contracts.v1.contracts_pb2"),
            importlib.import_module("drone.fleet.v1.fleet_pb2"),
            importlib.import_module("drone.fleet.v1.fleet_pb2_grpc"))


def peer_robot_id(context) -> str | None:
    """The robot id from the verified client certificate's URI SAN. / 从已验证客户端证书的 URI SAN 取机器人 ID。"""
    from cryptography import x509

    auth = context.auth_context()
    if auth.get("transport_security_type") != [b"ssl"] or not auth.get("x509_pem_cert"):
        return None
    cert = x509.load_pem_x509_certificate(auth["x509_pem_cert"][0])
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    robots = {robot_id_from_uri(uri) for uri in names.get_values_for_type(x509.UniformResourceIdentifier)} - {None}
    return robots.pop() if len(robots) == 1 else None


def _receipt(fleet, receipt: Receipt):
    from google.protobuf.timestamp_pb2 import Timestamp

    stamp = Timestamp()
    stamp.FromDatetime(utcnow())
    return fleet.DeliveryReceipt(schema_version="0.1.0", message_id=receipt.message_id, accepted=receipt.accepted,
                                 reason=receipt.reason, received_at=stamp)


async def serve_fleet(hub: FleetHub, address: str, *, cert_pem: bytes, key_pem: bytes, ca_pem: bytes):
    """Start the mTLS gRPC endpoint; client certificates are mandatory. / 启动 mTLS gRPC 端点；客户端证书必需。"""
    import grpc

    from drone_agent.runtime.wire import decode, encode

    shared, fleet, rpc = _stubs()

    class Service(rpc.FleetTransportServicer):
        async def _robot(self, context) -> str:
            robot_id = peer_robot_id(context)
            if robot_id is None:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "robot certificate required")
            return robot_id

        async def _ingest(self, request, context, model, handler):
            robot_id = await self._robot(context)
            try:
                item = model.model_validate(decode(request))
            except (ValueError, KeyError, TypeError) as error:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error)[:200])
            return _receipt(fleet, handler(robot_id, item))

        async def PublishEvent(self, request, context):
            return await self._ingest(request, context, ExecutionEvent, hub.publish_event)

        async def PublishEvidence(self, request, context):
            return await self._ingest(request, context, Evidence, hub.publish_evidence)

        async def PublishStatus(self, request, context):
            return await self._ingest(request, context, RobotStatus, hub.publish_status)

        async def PublishCapability(self, request, context):
            return await self._ingest(request, context, CapabilityDescriptor, hub.publish_capability)

        async def PublishMedia(self, request, context):
            robot_id = await self._robot(context)
            media = {field.name: value for field, value in request.ListFields()}
            return _receipt(fleet, hub.publish_media(robot_id, media))

        async def FetchDeliveries(self, request, context):
            robot_id = await self._robot(context)
            batch = hub.fetch_deliveries(robot_id, request.robot_id, request.after_cursor, request.max_items or 10)
            if batch.get("refused"):
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, batch["refused"])
            reply = fleet.DeliveryBatch(schema_version="0.1.0", cursor=batch["cursor"])
            for item in batch["items"]:
                message = reply.items.add(schema_version="0.1.0", delivery_id=item["delivery_id"],
                                          cursor=item["cursor"])
                message.kind = fleet.DeliveryKind.Value("DELIVERY_KIND_" + item["kind"].upper())
                if item["package"] is not None:
                    encode(item["package"], message.package)
                if item["operation"] is not None:
                    encode(item["operation"], message.operation)
                message.issued_at.FromDatetime(datetime.fromisoformat(item["issued_at"]))
            return reply

        async def AcknowledgeDelivery(self, request, context):
            robot_id = await self._robot(context)
            return _receipt(fleet, hub.acknowledge(robot_id, request.robot_id, request.delivery_id, request.accepted,
                                                   request.reason))

    server = grpc.aio.server(options=[("grpc.max_receive_message_length", MAX_MESSAGE_BYTES)])
    rpc.add_FleetTransportServicer_to_server(Service(), server)
    credentials = grpc.ssl_server_credentials([(key_pem, cert_pem)], root_certificates=ca_pem,
                                              require_client_auth=True)
    port = server.add_secure_port(address, credentials)
    if not port:
        raise RuntimeError("could not bind the fleet endpoint")
    await server.start()
    return server, port


class GrpcFleetClient:
    """The uplink's mTLS client. / uplink 的 mTLS 客户端。"""

    def __init__(self, target: str, robot_id: str, *, cert_pem: bytes, key_pem: bytes, ca_pem: bytes,
                 server_name: str | None = None, timeout_s: float = 5.0):
        import grpc

        self.shared, self.fleet, rpc = _stubs()
        self.robot_id, self.timeout = robot_id, timeout_s
        options = [("grpc.max_send_message_length", MAX_MESSAGE_BYTES)]
        if server_name:
            options.append(("grpc.ssl_target_name_override", server_name))
        credentials = grpc.ssl_channel_credentials(root_certificates=ca_pem, private_key=key_pem,
                                                   certificate_chain=cert_pem)
        self.channel = grpc.aio.secure_channel(target, credentials, options=options)
        self.stub = rpc.FleetTransportStub(self.channel)

    @staticmethod
    def _receipt(reply) -> Receipt:
        return Receipt(bool(reply.accepted), reply.reason, reply.message_id)

    async def publish_event(self, event: ExecutionEvent) -> Receipt:
        from drone_agent.runtime.wire import encode

        return self._receipt(await self.stub.PublishEvent(encode(event, self.shared.ExecutionEvent()),
                                                          timeout=self.timeout))

    async def publish_evidence(self, evidence: Evidence) -> Receipt:
        from drone_agent.runtime.wire import encode

        return self._receipt(await self.stub.PublishEvidence(encode(evidence, self.shared.Evidence()),
                                                             timeout=self.timeout))

    async def publish_media(self, media: dict) -> Receipt:
        message = self.fleet.EvidenceMedia(schema_version="0.1.0", **media)
        return self._receipt(await self.stub.PublishMedia(message, timeout=self.timeout * 4))

    async def publish_status(self, status: RobotStatus) -> Receipt:
        from drone_agent.runtime.wire import encode

        return self._receipt(await self.stub.PublishStatus(encode(status, self.shared.RobotStatus()),
                                                           timeout=self.timeout))

    async def publish_capability(self, capability: CapabilityDescriptor) -> Receipt:
        from drone_agent.runtime.wire import encode

        return self._receipt(await self.stub.PublishCapability(
            encode(capability, self.shared.CapabilityDescriptor()), timeout=self.timeout))

    async def fetch_deliveries(self, after_cursor: int, max_items: int = 10) -> dict:
        from drone_agent.runtime.wire import decode

        reply = await self.stub.FetchDeliveries(self.fleet.DeliveryRequest(
            schema_version="0.1.0", robot_id=self.robot_id, after_cursor=after_cursor, max_items=max_items),
            timeout=self.timeout)
        items = []
        for message in reply.items:
            kind = self.fleet.DeliveryKind.Name(message.kind).removeprefix("DELIVERY_KIND_").lower()
            items.append({
                "delivery_id": message.delivery_id, "cursor": message.cursor, "kind": kind,
                "package": MissionPackage.model_validate(decode(message.package))
                if message.HasField("package") else None,
                "operation": OperatorRequest.model_validate(decode(message.operation))
                if message.HasField("operation") else None,
                "issued_at": message.issued_at.ToDatetime(tzinfo=timezone.utc).isoformat(),
            })
        return {"items": items, "cursor": reply.cursor}

    async def acknowledge(self, delivery_id: str, accepted: bool, reason: str = "") -> Receipt:
        return self._receipt(await self.stub.AcknowledgeDelivery(self.fleet.DeliveryAck(
            schema_version="0.1.0", robot_id=self.robot_id, delivery_id=delivery_id, accepted=accepted,
            reason=reason[:300]), timeout=self.timeout))

    async def close(self) -> None:
        await self.channel.close()
