"""Logical flight adapter for S0 (P1, D055): the real guardian and executive fly it instead of PX4.

It follows registered routes one waypoint step per observation, captures frames the way the PX4 adapter does and
drains a battery that the logical dock recharges. Its observations are labelled `deterministic_test`, its imagery is
a test fixture, and nothing it produces is ever reported as physical flight: S0 proves logic, not aerodynamics.
The M2 onboard tests use the same adapter.

S0 的逻辑飞行适配器（P1，D055）：真实的 guardian 与 executive 驾驶它来代替 PX4。

它每次观测沿登记航线前进一个航点步、按 PX4 适配器的方式拍摄帧，并消耗一块由逻辑机场充电的电池。其观测标为
`deterministic_test`，影像是测试素材，产生的任何内容都不会被报告为物理飞行：S0 证明的是逻辑，不是气动。
M2 的机载测试也使用同一个适配器。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

from drone_agent.contracts import (
    Evidence,
    FlightObservation,
    Frame,
    Pose,
    Position,
    RecoveryBehavior,
    TimeWindow,
    utcnow,
)


class Battery:
    """The one physical battery of a logical aircraft, shared by its flight and its dock. / 逻辑飞行器的唯一电池。"""

    def __init__(self, fraction: float = 1.0):
        self.fraction = fraction

    def drain(self, amount: float) -> None:
        self.fraction = max(0.0, self.fraction - amount)

    def charge(self, amount: float) -> None:
        self.fraction = min(1.0, self.fraction + amount)


def image(signature: str | None) -> bytes:
    """160x120 grey frame with a 40x40 square of the signature colour; None gives a flat frame.

    160x120 灰色帧，中间 40x40 为特征颜色方块；None 返回无特征的平帧。
    """
    colour = {"red": (230, 20, 20), "green": (20, 230, 20), "blue": (20, 20, 230)}.get(signature)
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

    def __init__(self, reg, artifacts: Path, frames=None, *, battery: Battery | None = None,
                 drain_per_sample: float = 0.0):
        self.registry, self.artifacts = reg, artifacts
        self.robot_id = reg.capability.robot_id
        self.capabilities = reg.capability.model_copy(deep=True)
        self.capabilities.recovery_behaviors = {RecoveryBehavior.HOLD, RecoveryBehavior.RTL,
                                                RecoveryBehavior.LAND_HERE, RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE}
        self.position, self.path = [0.0, 0.0, 0.0], []
        # Harness-only fault: the motors never lift the aircraft off the pad. / 只在编排中使用的故障：电机无法离开机位。
        self.climb_blocked = False
        self.airborne, self.mode, self.sample = False, "HOLD", 0
        self.writes, self.frames = [], list(frames or [])
        self.sim_clock = None
        self.battery, self.drain_per_sample = battery, drain_per_sample

    def snapshot(self):
        if self.path:
            self.position = list(self.path.pop(0))
        self.sample += 1
        if self.battery is not None and self.airborne:
            self.battery.drain(self.drain_per_sample)
        now = utcnow()
        return FlightObservation(
            timestamp=now, valid_until=now + timedelta(seconds=0.5), sample_id=self.sample, robot_id=self.robot_id,
            pose=Pose(frame=Frame(**self.registry.data["frame"]),
                      position=Position(x=self.position[0], y=self.position[1], z=self.position[2],
                                        covariance=(1, 0, 0, 0, 1, 0, 0, 0, 1))),
            velocity_enu_mps=[0, 0, 0], armed=self.airborne, in_air=self.airborne, flight_mode=self.mode,
            localization_healthy=True, home_healthy=True,
            battery_fraction=round(self.battery.fraction, 4) if self.battery is not None else 1.0,
            source="deterministic_test")

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
            self.mode = "TAKEOFF"
            if self.climb_blocked:
                self.path = [[0, 0, 0]] * 3
            else:
                self.airborne = True
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
        signature = self.frames.pop(0) if self.frames else \
            self.registry.data["assets"][node.params["asset_id"]]["visual_signature"]
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


class Client:
    """The executive's guardian client, wired straight to an in-process guardian. / 直连进程内 guardian 的客户端。"""

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
