"""FleetTransport: identity binding in the hub and a real loopback mTLS gRPC roundtrip (WP-M2-12, D030/D031).

FleetTransport：hub 中的身份绑定，以及真实回环 mTLS gRPC 往返（WP-M2-12，D030/D031）。
"""

from __future__ import annotations

import hashlib
import runpy
import sys
from pathlib import Path

import grpc
import pytest

from drone_agent.contracts import utcnow
from drone_agent.fleet import pki
from drone_agent.fleet.events import journal_row_to_event
from drone_agent.fleet.transport import RAW_RGB, FleetHub, GrpcFleetClient, serve_fleet
from drone_agent.runtime.signing import TrustStore, verify_package
from tests.fleet.harness import build_loop, request

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    target = tmp_path_factory.mktemp("fleet-stubs")
    runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"](target)
    sys.path.insert(0, str(target))
    yield target
    sys.path.remove(str(target))
    for name in [m for m in sys.modules if m == "drone" or m.startswith("drone.")]:
        del sys.modules[name]


def row(seq: int = 0) -> dict:
    from drone_agent.runtime.ledger import content_hash

    value = {"seq": seq, "previous": "0" * 64, "timestamp": utcnow().isoformat(), "monotonic_ns": 1,
             "kind": "skill_state", "data": {"step_id": "takeoff", "state": "running"}}
    return {**value, "sha256": content_hash(value)}


async def mission(loop) -> str:
    view = await loop.service.submit(request())
    mission_id = view["mission"]["mission_id"]
    loop.service.approve(mission_id, 1, approver="tailnet:operator@example.test",
                         package_hash=view["versions"][0]["package_hash"])
    return mission_id


async def test_hub_binds_every_item_to_the_authenticated_robot_and_its_missions(tmp_path):
    loop = build_loop(tmp_path)
    hub = loop.hub
    mission_id = await mission(loop)
    event = journal_row_to_event(row(), journal="executive", robot_id="uav_01", mission_id=mission_id, version=1)
    assert hub.publish_event("uav_02", event).reason == "auth.robot_mismatch"
    assert hub.publish_event("uav_01", event).accepted
    assert hub.publish_event("uav_01", event).reason == "duplicate"
    stranger = journal_row_to_event(row(), journal="executive", robot_id="uav_01", mission_id="m-unknown", version=1)
    assert hub.publish_event("uav_01", stranger).reason == "service.not_found"
    assert hub.fetch_deliveries("uav_01", "uav_02", 0, 10)["refused"] == "auth.robot_mismatch"
    assert hub.fetch_deliveries("uav_02", "uav_02", 0, 10)["items"] == []
    delivery = hub.fetch_deliveries("uav_01", "uav_01", 0, 10)["items"][0]
    assert hub.acknowledge("uav_02", "uav_02", delivery["delivery_id"], True, "").reason == "service.not_found"


def media_item(evidence_id: str, data: bytes, **overrides) -> dict:
    value = {"robot_id": "uav_01", "mission_id": "", "mission_version": 1, "evidence_id": evidence_id,
             "sha256": hashlib.sha256(data).hexdigest(), "media_type": RAW_RGB, "width": 2, "height": 1, "data": data}
    value.update(overrides)
    return value


async def test_media_must_match_recorded_evidence_digest_and_size(tmp_path):
    from drone_agent.contracts import Evidence, TimeWindow

    loop = build_loop(tmp_path)
    mission_id = await mission(loop)
    data = bytes(range(6))
    evidence = Evidence(evidence_id="image:x", kind="image", media_ref="images/x.rgb",
                        sha256=hashlib.sha256(data).hexdigest(), time_window=TimeWindow(timestamp=utcnow()),
                        produced_by_skill_instance="inspect_asset_red", mission_id=mission_id, mission_version=1,
                        robot_id="uav_01")
    hub: FleetHub = loop.hub
    assert hub.publish_media("uav_01", media_item("image:x", data, mission_id=mission_id)).reason == "service.not_found"
    assert hub.publish_evidence("uav_01", evidence.model_copy(update={"mission_id": None})).reason == \
        "service.invalid_request"
    assert hub.publish_evidence("uav_01", evidence).accepted
    assert hub.publish_media("uav_01", media_item("image:x", data[:4] + b"zz", mission_id=mission_id,
                                                  sha256=evidence.sha256)).reason == "service.invalid_request"
    assert hub.publish_media("uav_01", media_item("image:x", data, mission_id=mission_id, width=3)).reason == \
        "service.invalid_request"
    assert hub.publish_media("uav_01", media_item("image:x", data, mission_id=mission_id)).accepted
    assert hub.media(f"{evidence.sha256}.rgb") == data and hub.media("../ledger.sqlite3") is None


async def test_mtls_roundtrip_binds_identity_to_the_certificate(tmp_path, stubs):
    loop = build_loop(tmp_path)
    mission_id = await mission(loop)
    ca = pki.new_ca()
    server = pki.service_credential(ca)
    robot, other = pki.robot_credential(ca, "uav_01"), pki.robot_credential(ca, "uav_02")
    rogue_ca = pki.new_ca("rogue")
    rogue = pki.robot_credential(rogue_ca, "uav_01")
    grpc_server, port = await serve_fleet(loop.hub, "127.0.0.1:0", cert_pem=server.cert_pem, key_pem=server.key_pem,
                                          ca_pem=ca.cert_pem)
    target = f"127.0.0.1:{port}"
    clients = []

    def client(credential, robot_id="uav_01", ca_pem=ca.cert_pem):
        value = GrpcFleetClient(target, robot_id, cert_pem=credential.cert_pem, key_pem=credential.key_pem,
                                ca_pem=ca_pem, timeout_s=5)
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
        assert loop.ledger.events(mission_id)[0]["body"]["data"] == event.data
        assert (await good.acknowledge(batch["items"][0]["delivery_id"], True, "accepted")).accepted
        # A valid certificate for another robot cannot speak for uav_01. / 另一台机器人的有效证书不能代表 uav_01。
        impostor = client(other)
        assert (await impostor.publish_event(event)).reason == "auth.robot_mismatch"
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await client(other, robot_id="uav_01").fetch_deliveries(0)
        assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
        # A certificate from another CA never completes the handshake. / 其他 CA 的证书无法完成握手。
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await client(rogue, ca_pem=ca.cert_pem).fetch_deliveries(0)
        assert caught.value.code() is grpc.StatusCode.UNAVAILABLE
    finally:
        for value in clients:
            await value.close()
        await grpc_server.stop(0)


def test_robot_identity_comes_only_from_the_uri_san():
    ca = pki.new_ca()
    assert pki.robot_id_from_uri("drone-agent://robot/uav_01") == "uav_01"
    assert pki.robot_id_from_uri("drone-agent://robot/uav_01/extra") is None
    assert pki.robot_id_from_uri("https://robot/uav_01") is None
    credential = pki.robot_credential(ca, "uav_01")
    assert credential.fingerprint == pki.fingerprint(credential.cert) and len(credential.fingerprint) == 64
