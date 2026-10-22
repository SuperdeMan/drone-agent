"""D088 handoff over the fleet transport: offers handed out only while open, replies through the hub, and the shared
encoding of `HandoffOffer` and `HandoffReply` used by the gRPC and Zenoh transports.

D088 经车队传输的交接：提议只在未结时交付，回复经 hub 处理，以及 gRPC 与 Zenoh 传输共用的 `HandoffOffer` 与
`HandoffReply` 编码。
"""

from __future__ import annotations

import importlib
import runpy
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.fleet.transport import (
    FleetHub,
    LocalFleetClient,
    decode_batch,
    encode_batch,
    reply_dict,
    reply_message,
)
from drone_agent.fleet.transport_zenoh import OPERATIONS
from tests.fleet.test_handoff import Engine, accept, engine_for

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fleet(tmp_path_factory):
    # The generated stubs, removed again so other modules load their own (as in test_transport).
    # 生成的桩代码，用完即移除，其他模块各自加载（同 test_transport）。
    target = tmp_path_factory.mktemp("handoff-stubs")
    runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"](target)
    sys.path.insert(0, str(target))
    yield importlib.import_module("drone.fleet.v1.fleet_pb2")
    sys.path.remove(str(target))
    for name in [m for m in sys.modules if m == "drone" or m.startswith("drone.")]:
        del sys.modules[name]


async def hub_for(tmp_path) -> tuple[object, Engine, FleetHub]:
    ledger, x = await engine_for(tmp_path)
    hub = FleetHub(ledger, tmp_path / "media")
    hub.handoffs = x.engine
    return ledger, x, hub


async def test_an_open_offer_is_fetched_answered_and_then_leaves_the_queue(tmp_path):
    ledger, x, hub = await hub_for(tmp_path)
    try:
        handoff = x.offer()
        rover = LocalFleetClient(hub, "ugv_01")
        batch = await rover.fetch_deliveries(0)
        [item] = batch["items"]
        assert item["kind"] == "handoff_offer" and item["package"] is None and item["operation"] is None
        assert item["offer"]["handoff_id"] == handoff["handoff_id"]
        assert (await LocalFleetClient(hub, "uav_01").fetch_deliveries(0))["items"] == []
        receipt = await rover.reply_handoff(accept(handoff))
        assert receipt.accepted and receipt.message_id == handoff["handoff_id"]
        assert x.store.handoff(handoff["handoff_id"])["state"] == "accepted"
        assert (await rover.fetch_deliveries(0))["items"] == []
        again = await rover.reply_handoff(accept(handoff))
        assert (again.accepted, again.reason) == (False, "handoff.not_open")
    finally:
        ledger.close()


async def test_an_expired_offer_is_never_handed_out(tmp_path):
    ledger, x, hub = await hub_for(tmp_path)
    try:
        x.offer(ttl_s=5)
        x.clock.now += timedelta(seconds=6)
        assert (await LocalFleetClient(hub, "ugv_01").fetch_deliveries(0))["items"] == []
        x.engine.tick()
        assert ledger.deliveries_after("ugv_01", 0) == []
    finally:
        ledger.close()


async def test_without_an_engine_offers_stay_held_and_replies_are_refused(tmp_path):
    ledger, x, hub = await hub_for(tmp_path)
    try:
        handoff = x.offer()
        hub.handoffs = None
        rover = LocalFleetClient(hub, "ugv_01")
        assert (await rover.fetch_deliveries(0))["items"] == []
        assert (await rover.reply_handoff(accept(handoff))).reason == "handoff.unavailable"
        assert hub.reply_handoff("ugv_01", "ugv_02", accept(handoff)).reason == "auth.robot_mismatch"
    finally:
        ledger.close()


async def test_offer_and_reply_survive_the_shared_wire_encoding(tmp_path, fleet):
    ledger, x, hub = await hub_for(tmp_path)
    try:
        handoff = x.offer()
        batch = hub.fetch_deliveries("ugv_01", "ugv_01", 0, 10)
        decoded = decode_batch(fleet, fleet.DeliveryBatch.FromString(encode_batch(fleet, batch).SerializeToString()))
        assert decoded["items"][0]["kind"] == "handoff_offer"
        assert decoded["items"][0]["offer"] == batch["items"][0]["offer"]
        reply = accept(handoff, reason_code=None)
        wire = fleet.HandoffReply.FromString(reply_message(fleet, "ugv_01", reply).SerializeToString())
        back = reply_dict(wire)
        assert (back["handoff_id"], back["accepted"], back["robot_id"]) == (handoff["handoff_id"], True, "ugv_01")
        assert (back["path_length_m"], back["map_version"]) == (12.0, "ground_a_v1") and "reason_code" not in back
    finally:
        ledger.close()


def test_the_zenoh_transport_carries_the_reply_like_grpc():
    assert "reply_handoff" in OPERATIONS
