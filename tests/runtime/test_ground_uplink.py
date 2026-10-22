"""X1 ground uplink (D088): a ground robot answers handoff offers from its own map and state, keeps each answer and
resends it unchanged, never acknowledges an offer, settles it through the real hub and engine, and reports
`ground_phase` instead of a flight phase.

X1 地面 uplink（D088）：地面车依据自己的地图与状态回答交接提议，保存每个回答并原样重发，从不单独确认提议，经真实的
hub 与引擎了结提议，并报告 `ground_phase` 而不是飞行阶段。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from drone_agent.contracts import (
    FactSource,
    FlightObservation,
    Frame,
    Pose,
    Position,
    WorldFact,
    utcnow,
)
from drone_agent.fleet.transport import FleetHub, LocalFleetClient, Receipt
from drone_agent.runtime.ledger import canonical
from drone_agent.runtime.signing import SigningKey, TrustStore
from drone_agent.runtime.uplink import Uplink
from tests.admission.ground_scene import COMPOUND, MID, ground, ground_package
from tests.fleet.test_handoff import engine_for

TRUST = TrustStore.from_entries([SigningKey.generate().trust_entry()])


def target(point, *, kind="point", radius=None, map_version="x1_ground_a_v1") -> dict:
    value = {"kind": kind, "description": "orange block, about 0.5 m"}
    if radius is not None:
        value["radius_m"] = radius
    return WorldFact(fact_id="f1", subject="finding-1", predicate=f"x1.{'target_point' if kind == 'point' else 'search_region'}",
                     value=value, frame=Frame(frame_id="map_enu", map_version=map_version),
                     position=Position(x=point[0], y=point[1], z=0.0, covariance=(0.5, 0, 0, 0, 0.5, 0, 0, 0, 0)),
                     timestamp=utcnow(), source=FactSource.SENSOR).model_dump(mode="json")


def offer(point=MID, *, ttl=60, handoff_id="ho-1", **options) -> dict:
    return {"schema_version": "0.1.0", "handoff_id": handoff_id, "task_id": "task-1", "from_robot_id": "coordinator",
            "to_robot_id": "ugv_01", "expires_at": (utcnow() + timedelta(seconds=ttl)).isoformat(),
            "evidence_refs": ["ev-1"], "target": target(point, **options)}


class Replies:
    """The uplink's client reduced to what offers need; replies are recorded. / 只保留提议所需接口的客户端；记录回复。"""

    def __init__(self, *offers):
        self.items = [{"delivery_id": f"d-{o['handoff_id']}", "cursor": 1, "kind": "handoff_offer", "package": None,
                       "operation": None, "offer": o} for o in offers]
        self.replies = []

    async def fetch_deliveries(self, after, limit=10):
        return {"items": self.items, "cursor": 1}

    async def acknowledge(self, *args):
        raise AssertionError("an offer is never acknowledged")

    async def reply_handoff(self, reply):
        self.replies.append(reply)
        return Receipt(True, "", reply["handoff_id"])


def rover(tmp_path, client, *, with_ground=True, clock=utcnow) -> Uplink:
    return Uplink(client, robot_id="ugv_01", trust=TRUST, capability=ground().capability,
                  inbox=tmp_path / "inbox", mailbox=tmp_path / "mailbox", aircraft=tmp_path / "aircraft",
                  state_path=tmp_path / "uplink/uplink-state.json", clock=clock,
                  ground=ground() if with_ground else None)


def observation(position=(0.0, 0.0), *, armed=False, speed=0.0, battery=0.9) -> dict:
    now = utcnow()
    return FlightObservation(
        timestamp=now, valid_until=now + timedelta(seconds=0.5), sample_id=1, robot_id="ugv_01",
        pose=Pose(frame=Frame(frame_id="map_enu", map_version="x1_ground_a_v1"),
                  position=Position(x=position[0], y=position[1], z=0.0, covariance=(0.04, 0, 0, 0, 0.04, 0, 0, 0, 0.09))),
        velocity_enu_mps=[speed, 0.0, 0.0], armed=armed, in_air=armed, flight_mode="HOLD", localization_healthy=True,
        home_healthy=True, battery_fraction=battery, source="deterministic_test").model_dump(mode="json")


def flown(uplink: Uplink, tmp_path, *, done: bool, obs: dict | None = None) -> None:
    """One accepted ground mission version, finished or not, with its last status. / 一个已接受的地面任务版本。"""
    package = ground_package()
    history = tmp_path / "inbox/history"
    history.mkdir(parents=True, exist_ok=True)
    (history / f"{package.mission_id}-v1.json").write_bytes(canonical(package.model_dump(mode="json")))
    uplink.state["accepted"][package.mission_id] = {"version": 1, "versions": [1],
                                                    "package_hash": package.package_hash}
    directory = tmp_path / "aircraft" / package.mission_id / "v1"
    directory.mkdir(parents=True, exist_ok=True)
    if done:
        (directory / "result.json").write_text(json.dumps({"completed": True}))
    if obs is not None:
        (directory / "status.json").write_text(json.dumps({"observation": obs, "active_step": None}))


@pytest.mark.parametrize(("make", "expected"), [
    (lambda: offer(MID), (True, None)),
    (lambda: offer(COMPOUND), (False, "unreachable")),
    (lambda: offer(COMPOUND, kind="search_region", radius=2.0), (True, None)),
    (lambda: offer(MID, map_version="x1_ground_a_v0"), (False, "outside_map")),
    (lambda: offer((40.0, 40.0)), (False, "outside_map")),
    (lambda: offer(MID, ttl=-1), (False, "expired")),
], ids=["point", "fenced", "search_region", "other_map", "off_map", "expired"])
async def test_a_ground_robot_answers_from_its_own_map(tmp_path, make, expected):
    case = make()  # built when the test runs, so its expiry is fresh / 在测试运行时构造，使其到期时间是新的
    client = Replies(case)
    await rover(tmp_path, client).pull()
    [reply] = client.replies
    assert (reply["accepted"], reply.get("reason_code")) == expected
    assert reply["map_version"] == "x1_ground_a_v1"
    if reply["accepted"]:
        # The same answer the compiler will reach. / 与编译器将得到的回答相同。
        position = case["target"]["position"]
        answer = ground().answer((position["x"], position["y"]), case["target"]["value"].get("radius_m"))
        assert (reply["path_length_m"], reply["eta_s"]) == (answer.path_m, answer.eta_s)


async def test_an_unfinished_mission_the_rest_state_and_energy_come_before_the_map(tmp_path):
    def answer(**state) -> tuple:
        directory = tmp_path / state.pop("name")
        client = Replies(offer())
        uplink = rover(directory, client)
        flown(uplink, directory, **state)
        return client, uplink

    client, uplink = answer(name="running", done=False)
    await uplink.pull()
    assert (client.replies[0]["accepted"], client.replies[0]["reason_code"]) == (False, "busy")
    client, uplink = answer(name="stopped", done=True, obs=observation((5.0, 5.0)))
    await uplink.pull()
    assert client.replies[0]["reason_code"] == "busy" and "parking" in client.replies[0]["reason"]
    client, uplink = answer(name="drained", done=True, obs=observation(battery=0.5))
    await uplink.pull()
    assert client.replies[0]["reason_code"] == "energy"
    client, uplink = answer(name="ready", done=True, obs=observation(battery=0.9))
    await uplink.pull()
    assert client.replies[0]["accepted"] is True


async def test_an_answer_is_kept_and_resent_unchanged(tmp_path):
    client = Replies(offer())
    uplink = rover(tmp_path, client)
    await uplink.pull()
    flown(uplink, tmp_path, done=False)  # the robot became busy meanwhile / 其间地面车变忙
    await uplink.pull()
    assert len(client.replies) == 2 and client.replies[0] == client.replies[1] and client.replies[0]["accepted"]
    reloaded = rover(tmp_path, Replies(offer()))
    assert reloaded.state["handoffs"]["ho-1"] == client.replies[0]


async def test_an_aircraft_never_accepts_an_offer(tmp_path):
    client = Replies(offer())
    await rover(tmp_path, client, with_ground=False).pull()
    assert (client.replies[0]["accepted"], client.replies[0]["reason_code"]) == (False, "outside_map")


async def test_the_answer_settles_the_offer_through_the_hub_and_engine(tmp_path):
    ledger, x = await engine_for(tmp_path)
    try:
        hub = FleetHub(ledger, tmp_path / "media")
        hub.handoffs = x.engine
        handoff = x.engine.offer(task_id="task-1", project_id="p1", robot_id="ugv_01",
                                 target=WorldFact.model_validate(target(MID)), evidence_refs=["ev-1"], ttl_s=60,
                                 actor="tester")
        uplink = rover(tmp_path, LocalFleetClient(hub, "ugv_01"), clock=x.clock)
        await uplink.pull()
        stored = x.store.handoff(handoff["handoff_id"])
        assert stored["state"] == "accepted"
        assert (await LocalFleetClient(hub, "ugv_01").fetch_deliveries(0))["items"] == []
        assert uplink.state["handoffs"][handoff["handoff_id"]]["path_length_m"] == ground().answer(MID).path_m
    finally:
        ledger.close()


def test_a_ground_status_reports_its_ground_phase(tmp_path):
    uplink = rover(tmp_path, Replies())
    flown(uplink, tmp_path, done=True, obs=observation())
    status = uplink.status()
    assert (status.flight_phase, status.ground_phase) == ("unknown", "parked")
    flown(uplink, tmp_path, done=True, obs=observation((5.0, 5.0), armed=True, speed=0.8))
    assert uplink.status().ground_phase == "moving"
