"""M2 onboard behaviour: signed acceptance, epoch watermark, phase binding and the phased inspection path.

Uses a small deterministic flight fake (no PX4) that follows registered routes and writes camera
evidence the way the real adapter does, so the guardian and executive run their real code paths.

M2 机载行为：签名接受、代次水位、相位绑定与相位化巡检流程。使用一个小型确定性飞行替身（不含 PX4），
它沿登记航线移动并按真实适配器的方式写入相机证据，使 guardian 与 executive 走真实代码路径。
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from drone_agent.admission.compiler import compile_spec
from drone_agent.contracts import (
    ControlCommandEnvelope,
    EffectVerdict,
    Evidence,
    FlightObservation,
    Frame,
    IdempotencyKey,
    Pose,
    Position,
    RecoveryBehavior,
    RecoveryPolicy,
    TaskLease,
    TimeWindow,
    utcnow,
)
from drone_agent.guardian.core import Guardian
from drone_agent.mission.executive import Executive
from drone_agent.mission.verify import verify_asset_image
from drone_agent.runtime.ledger import Journal
from drone_agent.runtime.recording import Recorder
from drone_agent.runtime.robot_state import RobotAuthorityState
from drone_agent.runtime.signing import SignatureRejected, SigningKey, TrustStore
from tests.admission.scene import approve, compile_context, inspect_task, registry, spec

ROOT = Path(__file__).resolve().parents[2]
KEY = SigningKey.generate()
TRUST = TrustStore.from_entries([KEY.trust_entry()])
POLICY = ROOT / "configs/recovery_policies/multirotor_m1_v1.yaml"


def fast_registry():
    """The M2 registry with short hold times so a full mission runs in about a second.

    保持时间缩短后的 M2 登记表，使整个任务约一秒跑完。
    """
    value = copy.copy(registry())
    value.data = copy.deepcopy(registry().data)
    for key in ("takeoff", "approach", "return_home", "land"):
        value.data["thresholds"][key]["hold_duration_s"] = 0.2
    return value


def image(signature: str | None) -> bytes:
    """160x120 grey frame with a 40x40 square of the signature colour; None gives a flat frame.

    160x120 灰色帧，中间 40x40 为特征颜色方块；None 返回无特征的平帧。
    """
    colour = {"red": (230, 20, 20), "blue": (20, 20, 230)}.get(signature)
    pixels = bytearray()
    for row in range(120):
        for column in range(160):
            inside = colour is not None and 40 <= row < 80 and 60 <= column < 100
            pixels += bytes(colour if inside else (128, 128, 128))
    return bytes(pixels)


class FlightFake:
    """Follows registered routes one waypoint per observation and captures frames like the adapter.

    每次观测前进一个航点，并像适配器一样拍摄帧。
    """

    camera_available = True
    external_takeover = False

    def __init__(self, reg, artifacts: Path, frames=None):
        self.registry, self.artifacts = reg, artifacts
        self.capabilities = reg.capability.model_copy(deep=True)
        self.capabilities.recovery_behaviors = {RecoveryBehavior.HOLD, RecoveryBehavior.RTL,
                                                RecoveryBehavior.LAND_HERE, RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE}
        self.position, self.path = [0.0, 0.0, 0.0], []
        self.airborne, self.mode, self.sample = False, "HOLD", 0
        self.writes, self.frames = [], list(frames or [])
        self.sim_clock = None

    def snapshot(self):
        if self.path:
            self.position = list(self.path.pop(0))
        self.sample += 1
        now = utcnow()
        return FlightObservation(
            timestamp=now, valid_until=now + timedelta(seconds=0.5), sample_id=self.sample, robot_id="uav_01",
            pose=Pose(frame=Frame(**self.registry.data["frame"]),
                      position=Position(x=self.position[0], y=self.position[1], z=self.position[2],
                                        covariance=(1, 0, 0, 0, 1, 0, 0, 0, 1))),
            velocity_enu_mps=[0, 0, 0], armed=self.airborne, in_air=self.airborne, flight_mode=self.mode,
            localization_healthy=True, home_healthy=True, battery_fraction=1.0, source="deterministic_test")

    def fly(self, route):
        steps = []
        for waypoint in route:
            steps += [waypoint] * 3
        self.path = steps

    async def execute(self, node, permitted, phase=None):
        assert permitted()
        self.writes.append((node.skill_id.rsplit(".", 1)[1], phase))
        p = node.params
        if node.skill_id == "skill.flight.takeoff":
            self.airborne, self.mode = True, "TAKEOFF"
            self.path = [[0, 0, p["altitude_m_agl"]]] * 3
        elif phase == "approach":
            self.mode = "MISSION"
            self.fly(self.registry.route(p["approach_route_id"]))
        elif phase == "capture":
            self.capture(node)
        elif node.skill_id == "skill.flight.return_home":
            self.fly(self.registry.route(p["return_route_id"]))
        elif node.skill_id == "skill.flight.land":
            self.mode, self.airborne, self.path = "LAND", False, [[0, 0, 0]] * 3

    def capture(self, node):
        signature = self.frames.pop(0) if self.frames else self.registry.data["assets"][node.params["asset_id"]]["visual_signature"]
        observation = self.snapshot()
        raw = image(signature)
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"images/{digest}-{uuid.uuid4().hex[:6]}.rgb"
        (self.artifacts / "images").mkdir(exist_ok=True)
        (self.artifacts / relative).write_bytes(raw)
        contract = Evidence(evidence_id="image:" + digest, kind="image", media_ref=relative, sha256=digest,
                            time_window=TimeWindow(timestamp=observation.timestamp, valid_until=observation.valid_until),
                            captured_pose=observation.pose, subject_ids=[node.params["asset_id"]],
                            quality={"width": 160, "height": 120}, produced_by_skill_instance=node.task_id)
        record = {"media_ref": relative, "sha256": digest, "asset_id": node.params["asset_id"], "width": 160,
                  "height": 120, "capture_timestamp": observation.timestamp.isoformat(),
                  "sim_time": self.sim_clock() if self.sim_clock else 0.0,
                  "observation": observation.model_dump(mode="json"), "skill_instance": node.task_id,
                  "source": "deterministic_test", "contract": contract.model_dump(mode="json")}
        (self.artifacts / f"evidence-{node.task_id}.json").write_text(json.dumps(record))

    async def recover(self, behavior, permitted):
        if permitted():
            self.writes.append(("recover", behavior.value))
            if behavior in (RecoveryBehavior.RTL, RecoveryBehavior.LAND_HERE):
                self.airborne, self.path, self.mode = False, [[0, 0, 0]] * 3, "LAND"

    async def resume(self, permitted):
        self.writes.append(("resume", None))


def signed_package(**overrides):
    package = approve(compile_spec(spec(**overrides), compile_context()).package)
    package.approval = KEY.sign(package.approval)
    return package


@pytest.fixture
def onboard(tmp_path):
    reg = fast_registry()
    adapter = FlightFake(reg, tmp_path)
    journal = Journal(tmp_path / "guardian.jsonl")
    state = RobotAuthorityState(tmp_path / "robot/authority.json", "uav_01")
    guardian = Guardian(adapter=adapter, package=signed_package(), registry=reg, journal=journal,
                        policy=RecoveryPolicy.from_yaml(POLICY), executive_id="executive-test", simulation=True,
                        trust=TRUST, robot_state=state)
    yield guardian, adapter, tmp_path, state
    journal.close()


def lease_for(guardian, epoch):
    now = utcnow()
    return TaskLease(robot_id="uav_01", mission_id=guardian.package.mission_id,
                     mission_version=guardian.package.mission_version, lease_epoch=epoch, holder="executive-test",
                     issued_at=now, expires_at=now + timedelta(seconds=10),
                     resources=sorted({r.resource_id for n in guardian.package.nodes for r in n.resources}))


def test_signed_mode_records_the_verified_package_and_rejects_unsigned(onboard, tmp_path):
    guardian = onboard[0]
    verified = [r for r in guardian.journal.rows if r["kind"] == "package_verified"]
    assert verified and verified[0]["data"]["signer_key_id"] == KEY.key_id
    unsigned = approve(compile_spec(spec(), compile_context()).package)
    with pytest.raises(SignatureRejected) as caught:
        Guardian(adapter=onboard[1], package=unsigned, registry=guardian.registry,
                 journal=Journal(tmp_path / "other.jsonl"), policy=RecoveryPolicy.from_yaml(POLICY),
                 executive_id="executive-test", simulation=True, trust=TRUST)
    assert caught.value.code == "onboard.unsigned"


def test_new_versions_must_use_epochs_above_the_robot_watermark(onboard):
    guardian, _, _, state = onboard
    state.record_epoch(3)
    guardian.epoch_floor = state.minimum_epoch()
    assert guardian.install_lease(lease_for(guardian, 2)).reason == "epoch_below_robot_watermark"
    assert guardian.install_lease(lease_for(guardian, 4)).accepted
    assert RobotAuthorityState(state.path, "uav_01").epoch_watermark == 4


def envelope(guardian, step, payload, seq):
    now = utcnow()
    key = IdempotencyKey(mission_id=guardian.package.mission_id, mission_version=1, step_id=step,
                         command_id=uuid.uuid4().hex, robot_id="uav_01", lease_epoch=1)
    return ControlCommandEnvelope(key=key, command_seq=seq, issued_at=now, valid_until=now + timedelta(seconds=2),
                                  intent_kind="skill", payload=payload)


async def test_phase_binding_and_order_are_enforced_by_the_guardian(onboard):
    guardian, adapter, _, _ = onboard
    lease = lease_for(guardian, 1)
    assert guardian.install_lease(lease).accepted
    now = utcnow()
    guardian.heartbeat({"schema_version": "0.1.0", "robot_id": "uav_01", "mission_id": lease.mission_id,
                        "mission_version": 1, "lease_epoch": 1, "executive_instance": "executive-test",
                        "heartbeat_seq": 1, "progress_seq": 1, "timestamp": now, "valid_until": now + timedelta(seconds=1)})
    adapter.airborne, adapter.position = True, [0, 0, 4]
    node = next(n for n in guardian.package.nodes if n.skill_id == "skill.inspect.asset")
    base = {"skill_id": node.skill_id, "params": node.params}
    assert (await guardian.submit(envelope(guardian, node.task_id, base, 0))).reason == "intent_not_bound_to_package"
    assert (await guardian.submit(envelope(guardian, node.task_id, {**base, "phase": "hover"}, 1))).reason == \
        "intent_not_bound_to_package"
    assert (await guardian.submit(envelope(guardian, node.task_id, {**base, "phase": "capture"}, 2))).reason == \
        "phase_out_of_order"
    assert (await guardian.submit(envelope(guardian, node.task_id, {**base, "phase": "approach"}, 3))).accepted
    assert guardian.phase == "cruise"
    assert (await guardian.submit(envelope(guardian, node.task_id, {**base, "phase": "approach"}, 4))).reason == \
        "phase_out_of_order"
    adapter.path, adapter.position = [], [4, 4, 4]
    assert (await guardian.submit(envelope(guardian, node.task_id, {**base, "phase": "capture"}, 5))).accepted
    assert guardian.phase == "inspect"
    assert adapter.writes == [("asset", "approach"), ("asset", "capture")]
    takeoff = guardian.package.nodes[0]
    with_phase = {"skill_id": takeoff.skill_id, "params": takeoff.params, "phase": "approach"}
    assert (await guardian.submit(envelope(guardian, takeoff.task_id, with_phase, 6))).reason == \
        "intent_not_bound_to_package"


class Client:
    def __init__(self, guardian, adapter):
        self.guardian, self.adapter = guardian, adapter

    async def install(self, lease):
        return self.guardian.install_lease(lease).model_dump(mode="json")

    async def heartbeat(self, value):
        result = self.guardian.heartbeat(value)
        result["safety_verdict"] = result["safety_verdict"].value
        return result

    async def observation(self, robot_id):
        return self.adapter.snapshot()

    async def submit(self, command):
        return (await self.guardian.submit(command)).model_dump(mode="json")

    async def operate(self, operation):
        result = await self.guardian.operate(operation)
        result["safety_verdict"] = result["safety_verdict"].value
        return result


async def fly(guardian, adapter, path, frames=()):
    adapter.frames = list(frames)
    journal, recorder = Journal(path / "executive.jsonl"), Recorder(path / "executive.mcap")
    try:
        executive = Executive(client=Client(guardian, adapter), registry=guardian.registry, package=guardian.package,
                              journal=journal, recorder=recorder, artifacts=path, executive_id="executive-test",
                              epoch=1, trust=TRUST)
        result = await executive.run()
        if guardian.recovery_task:
            await guardian.recovery_task
        return result, journal.rows
    finally:
        journal.close()
        recorder.close()


async def test_phased_inspection_completes_with_verified_evidence(onboard):
    guardian, adapter, path, _ = onboard
    result, rows = await fly(guardian, adapter, path)
    assert result["completed"], result
    assert adapter.writes == [("takeoff", None), ("asset", "approach"), ("asset", "capture"),
                              ("return_home", None), ("land", None)]
    inspect = result["outcomes"]["inspect_asset_red"]
    assert (inspect["execution_status"], inspect["effect_verdict"]) == ("succeeded", "verified")
    accepted = [r for r in rows if r["kind"] == "mission_accepted"][0]
    assert accepted["data"]["signer_key_id"] == KEY.key_id
    phases = [r["data"].get("phase") for r in rows if r["kind"] == "command_submitted"]
    assert phases == [None, "approach", "capture", None, None]


async def test_a_degraded_first_frame_is_retaken_then_verified(onboard):
    guardian, adapter, path, _ = onboard
    result, rows = await fly(guardian, adapter, path, frames=[None])
    assert result["completed"]
    captures = [r["data"]["capture_attempt"] for r in rows if r["data"].get("phase") == "capture"]
    assert captures == [1, 2]


async def test_exhausted_retakes_end_unverified_and_block_successors(onboard):
    guardian, adapter, path, _ = onboard
    result, rows = await fly(guardian, adapter, path, frames=[None, None, None])
    assert not result["completed"]
    inspect = result["outcomes"]["inspect_asset_red"]
    assert (inspect["execution_status"], inspect["effect_verdict"]) == ("failed", "unverified")
    assert result["not_run"] == ["return_home", "land"]
    assert [w for w in adapter.writes if w[1] == "capture"] == [("asset", "capture")] * 3
    assert ("recover", "rtl") in adapter.writes


def test_signature_colour_decides_the_evidence(tmp_path):
    reg = registry()
    package = signed_package(tasks=[inspect_task("asset_blue")], targets=[])
    node = next(n for n in package.nodes if n.skill_id == "skill.inspect.asset")
    fake = FlightFake(reg, tmp_path)
    fake.airborne, fake.position = True, [-4, 6, 4]
    fake.frames = ["blue", "red", None]
    verdicts = []
    for _ in range(3):
        fake.capture(node)
        record = json.loads((tmp_path / f"evidence-{node.task_id}.json").read_text())
        verdicts.append(verify_asset_image(record, tmp_path, node, reg))
    assert verdicts == [EffectVerdict.VERIFIED, EffectVerdict.UNVERIFIED, EffectVerdict.UNVERIFIED]
    fake.position = [4, 4, 4]
    fake.frames = ["blue"]
    fake.capture(node)
    record = json.loads((tmp_path / f"evidence-{node.task_id}.json").read_text())
    assert verify_asset_image(record, tmp_path, node, reg) == EffectVerdict.REFUTED
