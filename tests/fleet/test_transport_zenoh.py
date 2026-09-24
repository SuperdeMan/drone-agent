"""The Zenoh FleetTransport carries the same messages as gRPC under mTLS and a per-robot ACL (WP-M3-16, D046).

Zenoh FleetTransport 在 mTLS 与按机器人访问控制下承载与 gRPC 相同的报文（WP-M3-16，D046）。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from drone_agent.contracts import utcnow
from drone_agent.fleet import pki
from drone_agent.fleet.events import journal_row_to_event
from drone_agent.fleet.transport_zenoh import ZenohFleetClient, client_config, serve_fleet_zenoh, server_config
from drone_agent.runtime.signing import TrustStore, verify_package
from tests.fleet.harness import build_loop
from tests.fleet.test_transport import mission, row, stubs  # noqa: F401 - pytest fixture reuse / 复用夹具


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_only_tls_endpoints_and_valid_robot_ids_are_configured():
    ca = pki.new_ca()
    server = pki.service_credential(ca)
    with pytest.raises(ValueError, match="TLS"):
        server_config("tcp/0.0.0.0:7447", cert_pem=server.cert_pem, key_pem=server.key_pem, ca_pem=ca.cert_pem,
                      robots=("uav_01",))
    with pytest.raises(ValueError, match="robot id"):
        server_config("tls/0.0.0.0:7447", cert_pem=server.cert_pem, key_pem=server.key_pem, ca_pem=ca.cert_pem,
                      robots=("uav_01/**",))
    config = server_config("tls/0.0.0.0:7447", cert_pem=server.cert_pem, key_pem=server.key_pem, ca_pem=ca.cert_pem,
                           robots=("uav_01",))
    acl = config["access_control"]
    assert acl["default_permission"] == "deny" and config["transport"]["link"]["tls"]["enable_mtls"] is True
    assert acl["rules"][0]["key_exprs"] == ["drone/fleet/v1/uav_01/**"]
    assert acl["subjects"] == [{"id": "uav_01", "cert_common_names": ["uav_01"]}]
    robot = pki.robot_credential(ca, "uav_01")
    with pytest.raises(ValueError, match="TLS"):
        client_config("tcp/mission-service:7447", cert_pem=robot.cert_pem, key_pem=robot.key_pem, ca_pem=ca.cert_pem)
    # Robot certificates carry the same id as common name and URI SAN (D030, D046). / 通用名与 URI SAN 为同一 ID。
    from cryptography.x509.oid import NameOID

    assert robot.cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "uav_01"


async def test_zenoh_roundtrip_binds_identity_to_the_certificate_and_reconnects(tmp_path, stubs):  # noqa: F811
    loop = build_loop(tmp_path)
    mission_id = await mission(loop)
    ca = pki.new_ca()
    server_cred = pki.service_credential(ca)
    robot, other = pki.robot_credential(ca, "uav_01"), pki.robot_credential(ca, "uav_02")
    rogue = pki.robot_credential(pki.new_ca("rogue"), "uav_01")
    port = free_port()
    endpoint = f"tls/127.0.0.1:{port}"

    async def serve():
        return await serve_fleet_zenoh(loop.hub, endpoint, cert_pem=server_cred.cert_pem,
                                       key_pem=server_cred.key_pem, ca_pem=ca.cert_pem, robots=("uav_01", "uav_02"))

    server = await serve()
    clients = []

    def client(credential, robot_id="uav_01"):
        value = ZenohFleetClient(endpoint, robot_id, cert_pem=credential.cert_pem, key_pem=credential.key_pem,
                                 ca_pem=ca.cert_pem, timeout_s=3)
        clients.append(value)
        return value

    try:
        good = client(robot)
        batch = await good.fetch_deliveries(0)
        package = batch["items"][0]["package"]
        # The signed package survives the wire and still verifies onboard. / 签名任务包经线路后在机载仍可验签。
        assert verify_package(package, TrustStore.from_entries([loop.key.trust_entry()]), robot_id="uav_01",
                              now=utcnow()) == loop.key.key_id
        event = journal_row_to_event(row(), journal="guardian", robot_id="uav_01", mission_id=mission_id, version=1)
        assert (await good.publish_event(event)).accepted
        assert (await good.publish_event(event)).reason == "duplicate"
        assert loop.ledger.events(mission_id)[0]["body"]["data"] == event.data
        assert (await good.acknowledge(batch["items"][0]["delivery_id"], True, "accepted")).accepted
        # Its own key space, but the payload names another robot: the hub refuses it, as over gRPC.
        # 在自己的键空间内、载荷却写着别的机器人：hub 拒绝，与 gRPC 相同。
        assert (await client(other, "uav_02").publish_event(event)).reason == "auth.robot_mismatch"
        # A valid certificate for uav_02 cannot query uav_01's key space: the ACL answers nothing.
        # uav_02 的有效证书不能查询 uav_01 的键空间：访问控制不作应答。
        with pytest.raises(ConnectionError, match="no reply"):
            await client(other, "uav_01").fetch_deliveries(0)
        # A certificate from another CA never completes the handshake. / 其他 CA 的证书无法完成握手。
        with pytest.raises(ConnectionError, match="unreachable"):
            await client(rogue).fetch_deliveries(0)
        # A service outage looks like a cut link; the session comes back by itself. / 服务停机如同断链；会话自行恢复。
        await server.stop()
        with pytest.raises(ConnectionError):
            await good.publish_event(event)
        server = await serve()
        for _ in range(40):
            await asyncio.sleep(0.25)
            try:
                receipt = await good.publish_event(event)
                break
            except ConnectionError:
                continue
        else:
            pytest.fail("the Zenoh client did not reconnect within 10 s")
        assert receipt.reason == "duplicate"
    finally:
        for value in clients:
            await value.close()
        await server.stop()
