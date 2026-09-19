"""Exercise durable authority and recovery at failure boundaries.

验证故障边界上的持久化控制权与恢复行为。
"""

import asyncio
import copy
import runpy
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path

import grpc
import pytest

from drone_agent.contracts import (
    ControlCommandEnvelope,
    EffectVerdict,
    FlightObservation,
    Frame,
    IdempotencyKey,
    MissionAction,
    MissionOperation,
    Pose,
    Position,
    RecoveryBehavior,
    RecoveryPolicy,
    RecoveryTrigger,
    SafetyVerdict,
    TaskLease,
    utcnow,
)
from drone_agent.eval.fixture import make_package
from drone_agent.guardian.core import Guardian
from drone_agent.mission.registry import Registry
from drone_agent.mission.verify import EffectVerifier, verify_image
from drone_agent.runtime.ipc import GuardianClient, serve
from drone_agent.runtime.ledger import CommandLedger, Journal, read_log
from drone_agent.runtime.recording import Recorder, replay
from drone_agent.runtime.wire import decode, encode

ROOT = Path(__file__).resolve().parents[2]


class Adapter:
    camera_available = True
    external_takeover = False

    def __init__(self, registry):
        self.capabilities = registry.capability.model_copy(deep=True)
        self.writes = []
        self.airborne = False
        self.altitude = 0
        self.delay = 0
        self.battery = 1.0
        self.mode = "HOLD"
        self.failsafe = False
        self.stale = False
        self.sample = 0

    def snapshot(self):
        now = utcnow() - timedelta(seconds=3 if self.stale else 0)
        self.sample += 1
        return FlightObservation(
            timestamp=now,
            valid_until=now + timedelta(seconds=0.5),
            sample_id=self.sample,
            robot_id="uav_01",
            pose=Pose(
                frame=Frame(frame_id="map_enu", map_version="campus_v1"),
                position=Position(x=0, y=0, z=self.altitude, covariance=(1, 0, 0, 0, 1, 0, 0, 0, 1)),
            ),
            velocity_enu_mps=[0, 0, 0],
            armed=self.airborne,
            in_air=self.airborne,
            flight_mode=self.mode,
            localization_healthy=True,
            home_healthy=True,
            battery_fraction=self.battery,
            fc_failsafe=self.failsafe,
            source="deterministic_test",
        )

    async def execute(self, node, permitted):
        assert permitted()
        self.writes.append(node.skill_id)
        await asyncio.sleep(self.delay)

    async def recover(self, behavior, permitted):
        if permitted():
            self.writes.append(behavior)

    async def resume(self, permitted):
        assert permitted()
        self.writes.append("resume")


@pytest.fixture
def runtime(tmp_path):
    registry = Registry(ROOT)
    package = make_package(registry, 7, "mission-test")
    adapter = Adapter(registry)
    journal = Journal(tmp_path / "authority.jsonl")
    guardian = Guardian(
        adapter=adapter,
        package=package,
        registry=registry,
        journal=journal,
        policy=RecoveryPolicy.from_yaml(ROOT / "configs/recovery_policies/multirotor_m1_v1.yaml"),
        executive_id="executive-test",
        simulation=True,
    )
    now = utcnow()
    lease = TaskLease(
        robot_id="uav_01",
        mission_id=package.mission_id,
        mission_version=1,
        lease_epoch=1,
        holder="executive-test",
        issued_at=now,
        expires_at=now + timedelta(seconds=10),
        resources=["uav_01.motion", "uav_01.camera"],
    )
    assert guardian.install_lease(lease).accepted
    guardian.heartbeat(pulse(lease))
    yield guardian, adapter, lease, tmp_path
    journal.close()


def pulse(lease, seq=1):
    now = utcnow()
    return {
        "schema_version": "0.1.0",
        "robot_id": lease.robot_id,
        "mission_id": lease.mission_id,
        "mission_version": lease.mission_version,
        "lease_epoch": lease.lease_epoch,
        "executive_instance": lease.holder,
        "heartbeat_seq": seq,
        "progress_seq": seq,
        "timestamp": now,
        "valid_until": now + timedelta(seconds=1),
    }


def envelope(guardian, *, seq=0, command="command-1"):
    now, node = utcnow(), guardian.package.nodes[0]
    return ControlCommandEnvelope(
        key=IdempotencyKey(
            mission_id=guardian.package.mission_id,
            mission_version=1,
            step_id=node.task_id,
            command_id=command,
            robot_id="uav_01",
            lease_epoch=1,
        ),
        command_seq=seq,
        issued_at=now,
        valid_until=now + timedelta(seconds=2),
        intent_kind="skill",
        payload={"skill_id": node.skill_id, "params": node.params},
    )


async def test_duplicate_command_has_exactly_one_physical_dispatch(runtime):
    guardian, adapter, _, _ = runtime
    command = envelope(guardian)
    assert (await guardian.submit(command)).accepted
    assert (await guardian.submit(command)).accepted
    assert len(adapter.writes) == 1
    command.payload["params"]["altitude_m_agl"] = 5
    assert not (await guardian.submit(command)).accepted
    assert len(adapter.writes) == 1


@pytest.mark.parametrize(
    "defect", ["old_epoch", "future_epoch", "future_time", "expired", "raw", "params", "robot", "step", "mission"]
)
async def test_invalid_intent_never_reaches_adapter(runtime, defect):
    guardian, adapter, _, _ = runtime
    command = envelope(guardian)
    if defect == "old_epoch":
        command.key.lease_epoch = 0
    elif defect == "future_epoch":
        command.key.lease_epoch = 2
    elif defect == "future_time":
        command.valid_until += timedelta(seconds=30)
        command.issued_at += timedelta(seconds=20)
    elif defect == "expired":
        command.issued_at -= timedelta(seconds=30)
        command.valid_until -= timedelta(seconds=20)
    elif defect == "raw":
        command.intent_kind = "offboard_velocity"
    elif defect == "params":
        command.payload = {"skill_id": "skill.flight.takeoff", "params": {"altitude_m_agl": 100}}
    elif defect == "robot":
        command.key.robot_id = "other"
    elif defect == "step":
        command.key.step_id = "other"
    else:
        command.key.mission_id = "other"
    assert not (await guardian.submit(command)).accepted
    assert not adapter.writes


def test_restarted_authority_preserves_unknown_and_epoch(runtime):
    guardian, _, lease, _ = runtime
    command = envelope(guardian)
    guardian.ledger.record(
        "intent", {"key": command.key.as_string(), "digest": "test", "envelope": command.model_dump(mode="json")}
    )
    restarted = CommandLedger(guardian.journal)
    assert restarted.highest_epoch == 1 and restarted.lease is None
    assert restarted.reconcile(command.key.as_string())["receipt"] == "unknown"
    guardian.ledger = restarted
    assert not guardian.install_lease(lease).accepted
    lease.lease_epoch = 2
    assert guardian.install_lease(lease).accepted


@pytest.mark.parametrize("defect", ["torn", "tamper", "reorder"])
def test_log_corruption_fails_closed(tmp_path, defect):
    path = tmp_path / "log"
    journal = Journal(path)
    journal.append("one", {"x": 1})
    journal.append("two", {"x": 2})
    journal.close()
    raw = path.read_bytes()
    if defect == "torn":
        raw = raw[:-1]
    elif defect == "tamper":
        raw = raw.replace(b'"x":1', b'"x":9')
    else:
        raw = b"".join(reversed(raw.splitlines(keepends=True)))
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read_log(path)


@pytest.mark.parametrize("defect", ["identity", "sequence", "expiry", "future", "progress"])
def test_bad_heartbeat_cannot_refresh_liveness(runtime, defect):
    guardian, _, lease, _ = runtime
    original = guardian.last_progress
    value = pulse(lease, 2)
    if defect == "identity":
        value["executive_instance"] = "intruder"
    elif defect == "sequence":
        value["heartbeat_seq"] = 1
    elif defect == "expiry":
        value["valid_until"] = utcnow() - timedelta(seconds=1)
    elif defect == "future":
        value["timestamp"] += timedelta(seconds=30)
    else:
        value["progress_seq"] = 0
    with pytest.raises(ValueError):
        guardian.heartbeat(value)
    assert guardian.last_progress == original


async def test_live_transport_with_frozen_business_progress_recovers(runtime):
    guardian, adapter, lease, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    guardian.active_step, guardian.phase = guardian.package.nodes[1], "cruise"
    guardian.last_progress = time.monotonic() - 3
    value = pulse(lease, 2)
    value["progress_seq"] = 1
    guardian.heartbeat(value)
    await guardian.tick()
    await guardian.recovery_task
    assert guardian.reason == "executive_heartbeat_lost"
    assert adapter.writes == [RecoveryBehavior.HOLD]


@pytest.mark.parametrize("index", range(13))
async def test_every_m1_recovery_edge_dispatches_and_follows_delay(runtime, index):
    guardian, adapter, _, _ = runtime
    edge = guardian.policy.edges[index]
    adapter.airborne, adapter.altitude = True, 4
    guardian.phase = edge.guard.get("flight_phase", "cruise")
    if isinstance(guardian.phase, list):
        guardian.phase = guardian.phase[0]
    if edge.guard.get("rtl_reachable") is False:
        adapter.battery = 0.15
    guardian.authorized_to_continue = False
    await guardian.intervene(edge.trigger)
    if edge.target == RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE:
        assert guardian.taken_over and not adapter.writes
        return
    await guardian.recovery_task
    assert guardian.recovery is edge
    assert adapter.writes == [edge.target]
    if edge.then:
        guardian.recovery_started -= edge.after_s + 1
        await guardian.tick()
        await guardian.recovery_task
        assert adapter.writes[-1] == edge.then


async def test_recovery_preempts_slow_dispatch_before_next_write(runtime):
    guardian, adapter, _, _ = runtime
    adapter.delay = 30
    command = envelope(guardian)
    task = asyncio.create_task(guardian.submit(command))
    await asyncio.sleep(0.01)
    adapter.airborne = True
    await guardian.intervene(RecoveryTrigger.USER_CANCEL)
    result = await task
    await guardian.recovery_task
    assert not result.accepted
    assert guardian.ledger.reconcile(command.key.as_string())["receipt"] == "unknown"
    assert adapter.writes[-1] == RecoveryBehavior.LAND_HERE


async def test_failsafe_preempts_recovery_and_no_later_command_can_retake(runtime):
    guardian, adapter, lease, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    guardian.active_step, guardian.phase = guardian.package.nodes[1], "cruise"
    await guardian.intervene(RecoveryTrigger.USER_CANCEL)
    adapter.failsafe = True
    await guardian.tick()
    before = len(adapter.writes)
    lease.lease_epoch = 2
    assert not guardian.install_lease(lease).accepted
    await guardian.tick()
    assert guardian.taken_over and len(adapter.writes) == before


async def test_pause_cannot_downgrade_energy_recovery(runtime):
    guardian, adapter, _, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    guardian.phase = "cruise"
    await guardian.intervene(RecoveryTrigger.ENERGY_LOW)
    await guardian.recovery_task
    await guardian.intervene(RecoveryTrigger.USER_PAUSE)
    assert guardian.recovery.trigger == RecoveryTrigger.ENERGY_LOW


async def test_resume_requires_explicit_safe_pause_and_observation(runtime):
    guardian, adapter, lease, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    guardian.active_step, guardian.phase = guardian.package.nodes[1], "cruise"
    operation = MissionOperation(
        robot_id="uav_01",
        mission_id=lease.mission_id,
        mission_version=1,
        lease_epoch=1,
        executive_instance=lease.holder,
        action=MissionAction.RESUME,
        request_id="resume",
    )
    with pytest.raises(ValueError):
        await guardian.operate(operation)
    await guardian.intervene(RecoveryTrigger.USER_PAUSE)
    await guardian.recovery_task
    adapter.stale = True
    with pytest.raises(ValueError):
        await guardian.operate(operation)
    adapter.stale = False
    await guardian.operate(operation)
    assert guardian.safety == SafetyVerdict.PROCEED
    assert adapter.writes[-1] == "resume"


def test_repeated_sample_cannot_satisfy_stable_takeoff(runtime):
    guardian, adapter, _, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    verifier = EffectVerifier(guardian.registry, guardian.package.nodes[0])
    obs = adapter.snapshot()
    assert verifier.observe(obs, 1) == EffectVerdict.UNVERIFIED
    assert verifier.observe(obs, 4) == EffectVerdict.UNVERIFIED
    obs.sample_id += 1
    assert verifier.observe(obs, 4) == EffectVerdict.VERIFIED


@pytest.mark.parametrize("defect", ["hash", "scope", "frame", "resource", "params", "camera"])
def test_package_acceptance_rechecks_all_boundaries(runtime, defect):
    guardian, _, _, _ = runtime
    package = copy.deepcopy(guardian.package)
    if defect == "hash":
        package.package_hash = "wrong"
    elif defect == "scope":
        package.spatial_scope.approved_volume_id = "unknown"
    elif defect == "frame":
        package.spatial_scope.frame.map_version = "wrong"
    elif defect == "resource":
        package.nodes[0].resources = []
    elif defect == "params":
        package.nodes[0].params = {"altitude_m_agl": 100}
    if defect != "hash":
        package.package_hash = package.compute_hash()
        package.approval.package_hash = package.package_hash
    with pytest.raises(Exception):
        guardian.registry.validate_package(package, camera_available=defect != "camera")


def test_missing_or_forged_image_never_verified(runtime):
    guardian, _, _, path = runtime
    node = guardian.package.nodes[2]
    assert verify_image({}, path, node, guardian.registry) == EffectVerdict.UNKNOWN
    assert verify_image({"media_ref": "../outside.rgb"}, path, node, guardian.registry) != EffectVerdict.VERIFIED


def test_mcap_offline_replay_preserves_every_record(tmp_path):
    path = tmp_path / "trace.mcap"
    recorder = Recorder(path)
    recorder.write("mission/event", {"effect_verdict": "unknown"})
    recorder.write("flight/observation", {"sample_id": 2**54})
    recorder.close()
    assert list(replay(path)) == [
        ("mission/event", {"effect_verdict": "unknown"}),
        ("flight/observation", {"sample_id": 2**54}),
    ]


@pytest.fixture(scope="module")
def wire(tmp_path_factory):
    target = tmp_path_factory.mktemp("stubs")
    runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"](target)
    sys.path.insert(0, str(target))
    from drone.contracts.v1 import contracts_pb2

    yield contracts_pb2
    sys.path.remove(str(target))


def test_wire_preserves_domain_types_and_safe_defaults(runtime, wire):
    guardian, adapter, _, _ = runtime
    command = envelope(guardian)
    encoded = encode(command, wire.ControlCommandEnvelope())
    assert ControlCommandEnvelope.model_validate(decode(encoded)) == command
    obs = adapter.snapshot()
    assert FlightObservation.model_validate(decode(encode(obs, wire.FlightObservation()))) == obs
    with pytest.raises(ValueError):
        decode(wire.StepOutcome(schema_version="0.1.0", effect_verdict=0))


async def test_real_authenticated_ipc_roundtrip(runtime, wire):
    guardian, adapter, lease, _ = runtime
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"
    server = await serve(guardian, address, test_tcp=True)
    client = GuardianClient(address, test_tcp=True)
    try:
        assert (await client.install(lease))["accepted"]
        assert (await client.heartbeat(pulse(lease, 2)))["safety_verdict"] == "proceed"
        command = envelope(guardian)
        assert (await client.submit(command))["accepted"]
        assert (await client.reconcile(command.key))["receipt_status"] == "recorded"
        assert (await client.observation("uav_01")).sample_id > 0
        lease.holder = "intruder"
        assert not (await client.install(lease))["accepted"]
        assert len(adapter.writes) == 1
    finally:
        await client.close()
        await server.stop(0)


def test_nonlocal_rpc_endpoint_is_rejected():
    with pytest.raises(ValueError):
        GuardianClient("0.0.0.0:9999")


async def test_timeout_reconciliation_does_not_reexecute(runtime, wire):
    guardian, adapter, _, _ = runtime
    adapter.delay = 0.2
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"
    server = await serve(guardian, address, test_tcp=True)
    client = GuardianClient(address, test_tcp=True)
    try:
        command = envelope(guardian)
        with pytest.raises(grpc.aio.AioRpcError):
            await client.submit(command, timeout=0.05)
        assert (await client.reconcile(command.key))["receipt_status"] == "unknown"
        await asyncio.sleep(0.25)
        assert (await client.reconcile(command.key))["receipt_status"] == "recorded"
        assert (await client.submit(command))["accepted"]
        assert len(adapter.writes) == 1
    finally:
        await client.close()
        await server.stop(0)


def test_mavsdk_percent_and_change_only_state_streams(runtime):
    from types import SimpleNamespace as NS

    from drone_agent.adapters.px4_mavsdk import Px4Adapter

    guardian, _, _, path = runtime
    adapter = Px4Adapter(guardian.registry, path, path / "sensor.json")
    adapter.values = {
        "battery": NS(remaining_percent=99.0),
        "armed": False,
        "in_air": False,
        "health": NS(is_local_position_ok=True, is_global_position_ok=True, is_home_position_ok=True),
        "mode": NS(name="HOLD"),
    }
    adapter.received = {"link": time.monotonic(), "battery": time.monotonic()}
    assert adapter.snapshot().battery_fraction == 0.99
    assert adapter.snapshot().armed is False
    adapter.received["link"] -= 3
    assert adapter.snapshot().battery_fraction is None
    assert adapter.snapshot().armed is None


async def test_monotonic_expiry_cannot_be_extended_by_wall_clock(runtime):
    guardian, adapter, _, _ = runtime
    guardian.lease_deadline = time.monotonic() - 1
    assert not guardian.lease_valid()
    assert not (await guardian.submit(envelope(guardian))).accepted
    assert not adapter.writes


async def test_expired_authorization_stops_an_active_mission(runtime):
    guardian, adapter, _, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    guardian.active_step, guardian.phase = guardian.package.nodes[1], "cruise"
    guardian.package.approval.expires_at = utcnow() - timedelta(seconds=1)
    await guardian.tick()
    await guardian.recovery_task
    assert guardian.reason == "mission_authorization_expired"
    assert adapter.writes == [RecoveryBehavior.RTL]


def test_nominal_scenario_never_enters_fault_injection():
    due = runpy.run_path(str(ROOT / "scripts/remote_m1.py"))["injection_due"]
    assert not due({"id": "nominal"}, {"active_step": None})
    assert not due({"id": "nominal"}, {"active_step": "takeoff"})
    assert due({"inject_at": "fly_route"}, {"active_step": "fly_route"})


async def test_heartbeat_loss_cannot_interrupt_an_active_safety_return(runtime):
    guardian, adapter, _, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    guardian.phase = "cruise"
    await guardian.intervene(RecoveryTrigger.USER_CANCEL)
    await guardian.recovery_task
    await guardian.intervene(RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST)
    assert guardian.recovery.trigger == RecoveryTrigger.USER_CANCEL
    assert adapter.writes == [RecoveryBehavior.RTL]


async def test_executive_blocks_successors_when_takeoff_has_no_physical_effect(runtime):
    from drone_agent.mission.executive import Executive

    guardian, adapter, _, path = runtime
    package = guardian.package
    package.nodes[0].timeout_s = 0.2
    package.package_hash = package.compute_hash()
    package.approval.package_hash = package.package_hash

    class Client:
        async def install(self, lease):
            return guardian.install_lease(lease).model_dump(mode="json")

        async def heartbeat(self, value):
            result = guardian.heartbeat(value)
            result["safety_verdict"] = result["safety_verdict"].value
            return result

        async def observation(self, robot_id):
            return adapter.snapshot()

        async def submit(self, command):
            return (await guardian.submit(command)).model_dump(mode="json")

        async def operate(self, operation):
            return await guardian.operate(operation)

    journal, recorder = Journal(path / "executive.jsonl"), Recorder(path / "executive.mcap")
    try:
        executive = Executive(
            client=Client(),
            registry=guardian.registry,
            package=package,
            journal=journal,
            recorder=recorder,
            artifacts=path,
            executive_id="executive-test",
            epoch=2,
        )
        result = await executive.run()
        await guardian.recovery_task
        assert not result["completed"]
        assert result["not_run"] == ["fly_route", "capture_image", "return_home", "land"]
        assert adapter.writes == ["skill.flight.takeoff"]
        assert result["outcomes"]["takeoff"]["effect_verdict"] != "verified"
    finally:
        journal.close()
        recorder.close()


def test_fresh_epoch_cannot_retake_an_airborne_robot(runtime):
    guardian, adapter, lease, _ = runtime
    adapter.airborne = True
    lease.lease_epoch = 2
    assert not guardian.install_lease(lease).accepted


def test_blank_and_flat_color_images_cannot_meet_quality_profile():
    from drone_agent.mission.verify import image_quality

    assert image_quality(bytes(160 * 120 * 3), 160, 120)["red_fraction"] == 0
    quality = image_quality(bytes([255, 0, 0]) * (160 * 120), 160, 120)
    assert quality["red_fraction"] == 1 and quality["edge_contrast"] == 0


@pytest.mark.parametrize("version", [None, "", "1.0.0", "9.0.0"])
def test_missing_or_unsupported_wire_schema_is_rejected(wire, version):
    message = wire.ObservationRequest(robot_id="uav_01")
    if version is not None:
        message.schema_version = version
    with pytest.raises(ValueError, match="schema version"):
        decode(message)


async def test_landing_projection_ends_only_at_the_reserved_site(runtime):
    guardian, adapter, _, _ = runtime
    guardian.active_step, guardian.phase = guardian.package.nodes[4], "landing"
    adapter.airborne, adapter.altitude, adapter.mode = True, 0.8, "LAND"
    original = adapter.snapshot

    def landing_observation():
        obs = original()
        obs.velocity_enu_mps = [0, 0, -0.8]
        return obs

    adapter.snapshot = landing_observation
    await guardian.tick()
    assert guardian.recovery is None

    def outside_landing_site():
        obs = landing_observation()
        obs.pose.position.x = 10
        return obs

    adapter.snapshot = outside_landing_site
    await guardian.tick()
    await guardian.recovery_task
    assert guardian.reason == "geofence_predicted_breach"


def test_unverified_policy_requires_explicit_simulation(runtime):
    guardian, adapter, _, _ = runtime
    with pytest.raises(ValueError, match="unverified edges"):
        Guardian(
            adapter=adapter,
            package=guardian.package,
            registry=guardian.registry,
            journal=guardian.journal,
            policy=guardian.policy,
            executive_id="executive-test",
        )


def test_missing_battery_update_does_not_erase_disarmed_evidence(runtime):
    from types import SimpleNamespace as NS

    from drone_agent.adapters.px4_mavsdk import Px4Adapter

    guardian, _, _, path = runtime
    adapter = Px4Adapter(guardian.registry, path, path / "sensor.json")
    adapter.values = {
        "armed": False,
        "in_air": False,
        "health": NS(is_local_position_ok=True, is_global_position_ok=True, is_home_position_ok=True),
        "mode": NS(name="READY"),
    }
    adapter.received["link"] = time.monotonic()
    obs = adapter.snapshot()
    assert obs.armed is False and obs.in_air is False and obs.battery_fraction is None


def test_every_enabled_edge_has_a_specific_sitl_expectation(runtime):
    import yaml

    guardian, _, _, _ = runtime
    expectations = yaml.safe_load((ROOT / "configs/scenarios/m1_expectations.yaml").read_text())
    suite = yaml.safe_load((ROOT / "configs/scenarios/m1_suite.yaml").read_text())
    assert set(expectations) == {case["id"] for case in suite["scenarios"]}
    assert {e.fault_injection_scenario for e in guardian.policy.edges} == {
        e["edge"] for e in expectations.values() if "edge" in e
    }


def test_native_post_landing_mode_reset_is_not_airborne_takeover(runtime):
    from types import SimpleNamespace as NS

    from drone_agent.adapters.px4_mavsdk import Px4Adapter

    guardian, _, _, path = runtime
    adapter = Px4Adapter(guardian.registry, path, path / "sensor.json")
    adapter.expected_modes = {"LAND", "HOLD", "READY"}
    adapter.values = {"in_air": False, "position": NS(position=NS(down_m=0.1))}
    adapter.received["position"] = time.monotonic()
    assert adapter.landed_mode_transition("MISSION")
    assert not adapter.landed_mode_transition("POSCTL")
    adapter.values["in_air"] = True
    assert not adapter.landed_mode_transition("MISSION")
    adapter.values["in_air"] = False
    adapter.received["position"] -= 1
    assert not adapter.landed_mode_transition("MISSION")


@pytest.mark.parametrize(
    "kind",
    [
        "command_timeout",
        "observation_stale",
        "energy_low",
        "energy_critical",
        "geofence",
        "localization_lost",
        "fc_failsafe",
        "lease_expired",
        "uplink_lost",
    ],
)
def test_injection_metadata_cannot_crash_observation_collection(runtime, kind):
    import json

    from drone_agent.eval.faults import install_injection

    guardian, adapter, _, path = runtime
    instruction = path / "fault.json"
    instruction.write_text(json.dumps({"id": "test", "kind": kind, "step_id": "fly_route"}))
    install_injection(guardian, instruction)
    adapter.snapshot()
    adapter.snapshot()
    events = [row for row in guardian.journal.rows if row["kind"] == "fault_injected"]
    assert len(events) == 1 and events[0]["data"]["injection"]["kind"] == kind


async def test_cancel_before_dispatch_waits_for_safe_ground_and_never_arms(runtime):
    import json

    from drone_agent.mission.executive import Executive

    guardian, adapter, _, path = runtime

    class Client:
        async def install(self, lease):
            return guardian.install_lease(lease).model_dump(mode="json")

        async def heartbeat(self, value):
            return guardian.heartbeat(value)

        async def observation(self, robot_id):
            return adapter.snapshot()

        async def operate(self, operation):
            return await guardian.operate(operation)

        async def submit(self, command):
            raise AssertionError("cancelled-before-arm mission dispatched a command")

    journal, recorder = Journal(path / "cancel.jsonl"), Recorder(path / "cancel.mcap")
    (path / "operator.json").write_text(json.dumps({"request_id": "cancel-before-arm", "action": "cancel"}))
    try:
        executive = Executive(
            client=Client(),
            registry=guardian.registry,
            package=guardian.package,
            journal=journal,
            recorder=recorder,
            artifacts=path,
            executive_id="executive-test",
            epoch=2,
        )
        started = time.monotonic()
        result = await executive.run()
        assert time.monotonic() - started >= 2
        assert not result["completed"] and not adapter.writes
        assert result["outcomes"]["takeoff"]["execution_status"] == "cancelled"
        assert result["outcomes"]["takeoff"]["effect_verdict"] == "unknown"
        states = [e["data"]["state"] for e in journal.rows if e["kind"] == "skill_state"]
        assert states == ["preparing", "cancel_requested", "recovering", "cancelled"]
    finally:
        journal.close()
        recorder.close()


@pytest.mark.parametrize(
    "percent,raw,expected",
    [(25, 0.9, 0.25), (float("nan"), None, None), (-1, None, None), (101, None, None), (float("nan"), 0.8, 0.8)],
)
def test_battery_fusion_is_conservative_and_unknown_values_do_not_crash(runtime, percent, raw, expected):
    from types import SimpleNamespace as NS

    from drone_agent.adapters.px4_mavsdk import Px4Adapter

    guardian, _, _, path = runtime
    adapter = Px4Adapter(guardian.registry, path, path / "sensor.json")
    adapter.values = {
        "battery": NS(remaining_percent=percent),
        "armed": False,
        "in_air": False,
        "health": NS(is_local_position_ok=True, is_global_position_ok=True, is_home_position_ok=True),
        "mode": NS(name="READY"),
    }
    adapter.raw_battery_fraction = raw
    adapter.received = {key: time.monotonic() for key in ("link", "battery", "battery_status")}
    assert adapter.snapshot().battery_fraction == expected


async def test_late_resume_reply_cannot_override_newer_recovery_or_execute_twice(runtime):
    guardian, adapter, lease, _ = runtime
    adapter.airborne, adapter.altitude = True, 4
    guardian.active_step, guardian.phase = guardian.package.nodes[1], "cruise"
    await guardian.intervene(RecoveryTrigger.USER_PAUSE)
    await guardian.recovery_task
    delivered = asyncio.Event()
    response = asyncio.Event()

    async def delayed_resume(permitted):
        assert permitted()
        adapter.writes.append("resume")
        delivered.set()
        await response.wait()

    adapter.resume = delayed_resume
    operation = MissionOperation(
        robot_id="uav_01",
        mission_id=lease.mission_id,
        mission_version=1,
        lease_epoch=1,
        executive_instance=lease.holder,
        action=MissionAction.RESUME,
        request_id="resume-once",
    )
    pending = asyncio.create_task(guardian.operate(operation))
    await delivered.wait()
    await guardian.operate(operation)
    assert adapter.writes.count("resume") == 1
    await guardian.intervene(RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST)
    await guardian.recovery_task
    response.set()
    await pending
    assert guardian.safety == SafetyVerdict.HOLD
    assert guardian.recovery.trigger == RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST
