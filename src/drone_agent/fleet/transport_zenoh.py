"""FleetTransport over Zenoh beside the mTLS gRPC one (WP-M3-16, D046).

Same messages, same hub, same direction: the robot dials out in Zenoh client mode (D031) and every call is a query on
`drone/fleet/v1/<robot_id>/<operation>` whose payload and reply are the serialized drone.fleet.v1 / drone.contracts.v1
messages the gRPC transport carries, so the wire schema does not change. The service session listens on TLS only and
requires a client certificate from the deployment CA. Identity is enforced by Zenoh access control: default deny, and
each robot's certificate common name may query only its own key space, so the robot segment of a key the service
answers is authenticated. The PKI issues every robot certificate with the same id as common name and URI SAN (D030),
and the hub still refuses any payload naming another robot. Zenoh callbacks run on Zenoh threads; every hub call is
marshalled onto the service's event loop, like the gRPC handlers. Keys and certificates stay in memory (inline base64
configuration), never in temporary files.

与 mTLS gRPC 并列的 Zenoh FleetTransport（WP-M3-16，D046）。

报文、hub 与方向都不变：机器人以 Zenoh client 模式主动拨出（D031），每次调用都是对
`drone/fleet/v1/<robot_id>/<operation>` 的一次查询，其载荷与应答就是 gRPC 传输所承载的 drone.fleet.v1 /
drone.contracts.v1 序列化报文，线路 schema 不变。服务端会话只监听 TLS，并要求部署 CA 签发的客户端证书。身份由 Zenoh
访问控制保证：默认拒绝，每台机器人证书的通用名只能查询自己的键空间，因此服务应答的键中的机器人段是经认证的。PKI 为
每台机器人签发的证书中通用名与 URI SAN 为同一 ID（D030），hub 仍拒绝写着其他机器人的载荷。Zenoh 回调运行在 Zenoh 线程
上；每次 hub 调用都与 gRPC 处理器一样转到服务的事件循环中执行。密钥与证书只在内存中（内联 base64 配置），从不写临时文件。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading

from drone_agent.contracts import CapabilityDescriptor, Evidence, ExecutionEvent, RobotStatus
from drone_agent.fleet.transport import (
    MAX_MESSAGE_BYTES,
    FleetHub,
    Receipt,
    _receipt,
    _stubs,
    decode_batch,
    encode_batch,
)

PREFIX = "drone/fleet/v1"
OPERATIONS = ("publish_event", "publish_evidence", "publish_media", "publish_status", "publish_capability",
              "fetch_deliveries", "acknowledge")
ROBOT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _tls_only(endpoint: str) -> str:
    if not endpoint.startswith("tls/"):
        raise ValueError("the fleet link runs over TLS only")
    return endpoint


def server_config(listen: str, *, cert_pem: bytes, key_pem: bytes, ca_pem: bytes, robots: tuple[str, ...]) -> dict:
    """Peer session listening on TLS with mandatory client certificates and a per-robot ACL.

    在 TLS 上监听、强制客户端证书并按机器人设访问控制的 peer 会话。
    """
    if not robots or not all(ROBOT_ID.match(robot) for robot in robots):
        raise ValueError("the fleet endpoint needs at least one valid robot id")
    return {
        "mode": "peer",
        "listen": {"endpoints": [_tls_only(listen)]},
        "scouting": {"multicast": {"enabled": False}, "gossip": {"enabled": False}},
        "transport": {"link": {"tls": {"root_ca_certificate_base64": _b64(ca_pem),
                                       "listen_private_key_base64": _b64(key_pem),
                                       "listen_certificate_base64": _b64(cert_pem), "enable_mtls": True}}},
        "access_control": {
            "enabled": True,
            "default_permission": "deny",
            "rules": [{"id": f"fleet_{robot}", "messages": ["query", "reply"], "flows": ["ingress", "egress"],
                       "permission": "allow", "key_exprs": [f"{PREFIX}/{robot}/**"]} for robot in robots],
            "subjects": [{"id": robot, "cert_common_names": [robot]} for robot in robots],
            "policies": [{"rules": [f"fleet_{robot}"], "subjects": [robot]} for robot in robots],
        },
    }


def client_config(endpoint: str, *, cert_pem: bytes, key_pem: bytes, ca_pem: bytes) -> dict:
    """Client session that dials the service over TLS and verifies its name. / 经 TLS 拨向服务并校验其名称的 client 会话。"""
    return {
        "mode": "client",
        "connect": {"endpoints": [_tls_only(endpoint)],
                    "retry": {"period_init_ms": 500, "period_max_ms": 4000, "period_increase_factor": 2}},
        "scouting": {"multicast": {"enabled": False}, "gossip": {"enabled": False}},
        "transport": {"link": {"tls": {"root_ca_certificate_base64": _b64(ca_pem),
                                       "connect_private_key_base64": _b64(key_pem),
                                       "connect_certificate_base64": _b64(cert_pem), "enable_mtls": True,
                                       "verify_name_on_connect": True}}},
    }


class ZenohFleetServer:
    """Service side: one queryable per operation, answered on the event loop. / 服务端：每个操作一个可查询实体，在事件循环中应答。"""

    def __init__(self, hub: FleetHub, listen: str, *, cert_pem: bytes, key_pem: bytes, ca_pem: bytes,
                 robots: tuple[str, ...], loop: asyncio.AbstractEventLoop):
        import zenoh

        self.hub, self.loop = hub, loop
        self.shared, self.fleet, _ = _stubs()
        config = server_config(listen, cert_pem=cert_pem, key_pem=key_pem, ca_pem=ca_pem, robots=robots)
        self.session = zenoh.open(zenoh.Config.from_json5(json.dumps(config)))
        self.queryables = [self.session.declare_queryable(f"{PREFIX}/*/{operation}", self._handler(operation))
                           for operation in OPERATIONS]

    def _handler(self, operation: str):
        def handle(query) -> None:
            parts = str(query.key_expr).split("/")
            robot_id = parts[3] if len(parts) == 5 else ""
            body = query.payload.to_bytes() if query.payload is not None else b""
            if not ROBOT_ID.match(robot_id) or len(body) > MAX_MESSAGE_BYTES:
                query.reply_err(b"invalid_argument")
                return
            future = asyncio.run_coroutine_threadsafe(self._dispatch(operation, robot_id, body), self.loop)
            try:
                reply = future.result(timeout=10)
            except PermissionError as error:
                query.reply_err(("permission_denied:" + str(error))[:200].encode())
            except Exception as error:  # noqa: BLE001 - every failure is an error reply, never a silent drop / 失败一律答错误
                query.reply_err(("invalid_argument:" + type(error).__name__)[:200].encode())
            else:
                query.reply(query.key_expr, reply)

        return handle

    async def _dispatch(self, operation: str, robot_id: str, body: bytes) -> bytes:
        from drone_agent.runtime.wire import decode

        hub, fleet, shared = self.hub, self.fleet, self.shared
        if operation == "fetch_deliveries":
            request = fleet.DeliveryRequest.FromString(body)
            batch = hub.fetch_deliveries(robot_id, request.robot_id, request.after_cursor, request.max_items or 10)
            if batch.get("refused"):
                raise PermissionError(batch["refused"])
            return encode_batch(fleet, batch).SerializeToString()
        if operation == "acknowledge":
            request = fleet.DeliveryAck.FromString(body)
            receipt = hub.acknowledge(robot_id, request.robot_id, request.delivery_id, request.accepted, request.reason)
        elif operation == "publish_media":
            request = fleet.EvidenceMedia.FromString(body)
            receipt = hub.publish_media(robot_id, {field.name: value for field, value in request.ListFields()})
        else:
            model, message, handler = {
                "publish_event": (ExecutionEvent, shared.ExecutionEvent, hub.publish_event),
                "publish_evidence": (Evidence, shared.Evidence, hub.publish_evidence),
                "publish_status": (RobotStatus, shared.RobotStatus, hub.publish_status),
                "publish_capability": (CapabilityDescriptor, shared.CapabilityDescriptor, hub.publish_capability),
            }[operation]
            receipt = handler(robot_id, model.model_validate(decode(message.FromString(body))))
        return _receipt(fleet, receipt).SerializeToString()

    async def stop(self) -> None:
        def close():
            for queryable in self.queryables:
                queryable.undeclare()
            self.session.close()

        await asyncio.to_thread(close)


async def serve_fleet_zenoh(hub: FleetHub, listen: str, *, cert_pem: bytes, key_pem: bytes, ca_pem: bytes,
                            robots: tuple[str, ...]) -> ZenohFleetServer:
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(ZenohFleetServer, hub, listen, cert_pem=cert_pem, key_pem=key_pem,
                                   ca_pem=ca_pem, robots=robots, loop=loop)


class ZenohFleetClient:
    """The uplink's Zenoh client; the session opens on first use and reconnects by itself.

    uplink 的 Zenoh 客户端；会话在首次使用时打开，之后自行重连。
    """

    def __init__(self, endpoint: str, robot_id: str, *, cert_pem: bytes, key_pem: bytes, ca_pem: bytes,
                 timeout_s: float = 5.0):
        if not ROBOT_ID.match(robot_id):
            raise ValueError("invalid robot id")
        self.shared, self.fleet, _ = _stubs()
        self.robot_id, self.timeout = robot_id, timeout_s
        self.config = client_config(endpoint, cert_pem=cert_pem, key_pem=key_pem, ca_pem=ca_pem)
        self.session = None
        self.lock = threading.Lock()

    def _open(self):
        import zenoh

        with self.lock:
            if self.session is None:
                try:
                    self.session = zenoh.open(zenoh.Config.from_json5(json.dumps(self.config)))
                except zenoh.ZError as error:
                    raise ConnectionError(f"mission service unreachable: {str(error)[:120]}") from None
            return self.session

    def _call(self, operation: str, body: bytes, timeout: float) -> bytes:
        session = self._open()
        for reply in session.get(f"{PREFIX}/{self.robot_id}/{operation}", payload=body, timeout=timeout):
            if reply.ok is not None:
                return reply.ok.payload.to_bytes()
            reason = reply.err.payload.to_bytes().decode(errors="replace")[:200]
            raise (PermissionError if reason.startswith("permission_denied") else ValueError)(reason)
        # Access control drops a query outside the robot's own key space without an answer, exactly like a cut link.
        # 访问控制对机器人自身键空间之外的查询不作应答，与断链表现相同。
        raise ConnectionError("no reply from the mission service")

    async def _request(self, operation: str, message, timeout: float | None = None) -> bytes:
        return await asyncio.to_thread(self._call, operation, message.SerializeToString(), timeout or self.timeout)

    async def _publish(self, operation: str, message, timeout: float | None = None) -> Receipt:
        reply = self.fleet.DeliveryReceipt.FromString(await self._request(operation, message, timeout))
        return Receipt(bool(reply.accepted), reply.reason, reply.message_id)

    async def publish_event(self, event: ExecutionEvent) -> Receipt:
        from drone_agent.runtime.wire import encode

        return await self._publish("publish_event", encode(event, self.shared.ExecutionEvent()))

    async def publish_evidence(self, evidence: Evidence) -> Receipt:
        from drone_agent.runtime.wire import encode

        return await self._publish("publish_evidence", encode(evidence, self.shared.Evidence()))

    async def publish_media(self, media: dict) -> Receipt:
        return await self._publish("publish_media", self.fleet.EvidenceMedia(schema_version="0.1.0", **media),
                                   self.timeout * 4)

    async def publish_status(self, status: RobotStatus) -> Receipt:
        from drone_agent.runtime.wire import encode

        return await self._publish("publish_status", encode(status, self.shared.RobotStatus()))

    async def publish_capability(self, capability: CapabilityDescriptor) -> Receipt:
        from drone_agent.runtime.wire import encode

        return await self._publish("publish_capability", encode(capability, self.shared.CapabilityDescriptor()))

    async def fetch_deliveries(self, after_cursor: int, max_items: int = 10) -> dict:
        body = await self._request("fetch_deliveries", self.fleet.DeliveryRequest(
            schema_version="0.1.0", robot_id=self.robot_id, after_cursor=after_cursor, max_items=max_items))
        return decode_batch(self.fleet, self.fleet.DeliveryBatch.FromString(body))

    async def acknowledge(self, delivery_id: str, accepted: bool, reason: str = "") -> Receipt:
        return await self._publish("acknowledge", self.fleet.DeliveryAck(
            schema_version="0.1.0", robot_id=self.robot_id, delivery_id=delivery_id, accepted=accepted,
            reason=reason[:300]))

    async def close(self) -> None:
        if self.session is not None:
            session, self.session = self.session, None
            await asyncio.to_thread(session.close)
