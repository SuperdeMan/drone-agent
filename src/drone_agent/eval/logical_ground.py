"""Logical ground robot for X1 (D088): the real guardian and executive drive it instead of the PX4 rover.

It drives 2D waypoints at the commanded speed against the wall clock and reports that velocity, ends every route in
Hold the way the measured PX4 rover does, coasts to a stop over the measured half second when told to hold, keeps the
rest of a route while stopped and continues it on resume, drains a battery while armed, and refuses the disarm before
anything happens unless a fresh observation shows it stationary in Hold, exactly like the real adapter. `in_air` is
reported in one of three ways because the measured rover reports it true whenever armed: nothing onboard may read it
for a ground robot. Frames come from a forward camera that draws the declared anomaly blocks of the harness world. Its
observations are labelled `deterministic_test`, its imagery is a test fixture, and nothing it produces is ever reported
as physical driving: it proves logic, not dynamics. The onboard tests and the X1 S0 world use it.

X1 的逻辑地面车（D088）：真实的 guardian 与 executive 驾驶它来代替 PX4 地面车。

它按墙钟以指令速度驶过二维航点并报告该速度；与实测的 PX4 地面车一样，每条航线以 Hold 结束；收到保持指令时按实测的半秒
滑行停住；停住期间保留航线的剩余部分，恢复时接着走；解锁期间消耗电量；与真实适配器完全相同，除非新鲜观测显示车已在
Hold 中静止，否则上锁在任何动作之前即被拒绝。`in_air` 有三种报告方式，因为实测地面车只要解锁就报告 true：机载代码对地面
车不得读取它。画面来自一台前视相机，画出编排世界中声明的异常物方块。其观测标为 `deterministic_test`，影像是测试素材，产生
的任何内容都不会被报告为物理行驶：它证明的是逻辑，不是动力学。机载测试与 X1 S0 世界使用它。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
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
from drone_agent.eval.logical_flight import Battery

GROUND = "skill.ground."
IN_AIR = ("armed", "false", "none")  # how in_air is reported / in_air 的报告方式
COAST_S = 0.5  # measured: Hold stops 1.2 m/s within 0.5 s (2026-10-14) / 实测：Hold 让 1.2 m/s 在 0.5 s 内停住
SKY, GROUND_LIGHT, GROUND_DARK = (170, 190, 210), (112, 108, 96), (88, 86, 78)


@dataclass(frozen=True)
class Block:
    """A declared anomaly block standing on the ground: centre, edge length and RGB colour.

    立在地面上的声明异常物方块：中心、边长与 RGB 颜色。
    """

    x: float
    y: float
    size_m: float
    colour: tuple[int, int, int]


def view(camera: dict, position, heading: float, blocks=()) -> bytes:
    """A level forward camera's RGB frame: sky above the horizon, a textured ground below, and every block in view.

    The camera sits `height_m` above the robot's position looking along `heading` (radians, counter-clockwise from +x);
    image right is the robot's right. The ground texture is fixed in the image and only gives the frame edge contrast.

    水平前视相机的 RGB 帧：地平线以上是天空，以下是有纹理的地面，再画出视野内的每个方块。相机位于车辆位置上方
    `height_m` 处，沿 `heading`（弧度，自 +x 逆时针）观察；画面右方是车辆右方。地面纹理固定在画面中，只为帧提供边缘对比度。
    """
    width, height, h = camera["width_px"], camera["height_px"], camera["height_m"]
    f = (width / 2) / math.tan(math.radians(camera["hfov_deg"]) / 2)
    horizon = height / 2
    rows = []
    for v in range(height):
        if v < horizon:
            rows.append(bytearray(bytes(SKY) * width))
        else:
            row = bytearray()
            for u in range(width):
                row += bytes(GROUND_LIGHT if (u // 8 + v // 8) % 2 else GROUND_DARK)
            rows.append(row)
    cos, sin = math.cos(heading), math.sin(heading)
    # Far blocks first so near ones cover them. / 先画远的方块，近的覆盖远的。
    placed = []
    for block in blocks:
        dx, dy = block.x - position[0], block.y - position[1]
        forward, left = dx * cos + dy * sin, -dx * sin + dy * cos
        if forward > 0.1:
            placed.append((forward, left, block))
    for forward, left, block in sorted(placed, key=lambda item: -item[0]):
        u0 = width / 2 - f * left / forward
        half = f * (block.size_m / 2) / forward
        top = horizon + f * (h - block.size_m) / forward
        bottom = horizon + f * h / forward
        for v in range(max(0, math.floor(top)), min(height, math.ceil(bottom))):
            for u in range(max(0, math.floor(u0 - half)), min(width, math.ceil(u0 + half))):
                rows[v][u * 3:u * 3 + 3] = bytes(block.colour)
    return b"".join(bytes(row) for row in rows)


def flat(camera: dict) -> bytes:
    """A featureless frame (lens covered or out of focus): no edge contrast, so it never verifies.

    无特征的帧（镜头遮挡或失焦）：没有边缘对比度，因此从不通过核验。
    """
    return bytes((128, 128, 128)) * (camera["width_px"] * camera["height_px"])


def quick_ground(registry):
    """The ground site with short hold times, used only by tests and S0; the service keeps the original.

    保持时间缩短的地面站点图，只供测试与 S0 使用；服务保留原始地图。
    """
    value = copy.copy(registry)
    value.data = copy.deepcopy(registry.data)
    for key in ("nav_to", "return_home", "arm", "stop", "disarm"):
        value.data["thresholds"][key]["hold_duration_s"] = 0.2
    return value


class GroundFake:
    """Drives 2D routes against the wall clock, holds, resumes, captures forward frames and guards the disarm.

    按墙钟驶过二维航线、保持、恢复、拍摄前视画面，并守住上锁。
    """

    camera_available = True
    external_takeover = False

    def __init__(self, reg, artifacts: Path, *, battery: Battery | None = None, drain_s: float = 1800.0,
                 in_air: str = "armed", blocks=(), position=None, heading: float = math.pi / 2):
        if in_air not in IN_AIR:
            raise ValueError(f"in_air must be one of {IN_AIR}")
        self.registry, self.artifacts = reg, artifacts
        self.robot_id = reg.capability.robot_id
        self.capabilities = reg.capability.model_copy(deep=True)
        self.capabilities.recovery_behaviors = {RecoveryBehavior.STOP, RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE}
        self.control_context = {}
        self.position = list(position or reg.parking())
        self.heading, self.velocity = heading, (0.0, 0.0)
        self.path, self.speed = [], 0.0
        self.armed, self.mode, self.in_air = False, "HOLD", in_air
        self.coast = None  # (start position, start velocity, start time) while coasting / 滑行时的起点、初速与起始时刻
        self.battery, self.drain_s = battery or Battery(), drain_s
        self.blocks = list(blocks)
        self.writes, self.frames, self.sample = [], [], 0
        # Ground truth per observation: (monotonic, x, y, armed). / 每次观测时的真值：(monotonic, x, y, armed)。
        self.trace: list[tuple[float, float, float, bool]] = []
        self.last = time.monotonic()
        # Harness-only faults. / 只在编排中使用的故障。
        self.stale = False  # observations arrive one second old / 观测晚到一秒
        self.localization_ok = True
        self.fc_failsafe = False
        self.stalled = False  # the wheels do not turn / 车轮不转

    # ── motion / 运动 ──

    def advance(self) -> None:
        now = time.monotonic()
        dt, self.last = now - self.last, now
        if self.armed:
            self.battery.drain(dt / self.drain_s)
        if self.coast is not None:
            (x0, y0), (vx, vy), started = self.coast
            t = min(now - started, COAST_S)
            travelled = t - t * t / (2 * COAST_S)  # linear deceleration / 线性减速
            self.position = [x0 + vx * travelled, y0 + vy * travelled]
            scale = 1 - t / COAST_S
            self.velocity = (vx * scale, vy * scale)
            if t >= COAST_S:
                self.coast, self.velocity = None, (0.0, 0.0)
            return
        if self.mode != "MISSION" or not self.path or self.stalled:
            self.velocity = (0.0, 0.0)
            return
        if dt <= 0:
            # Two observations in one clock tick (about 15 ms on Windows): the robot still moves at its speed.
            # 两次观测落在同一个时钟刻度（Windows 上约 15 ms）：车仍以原速度行驶。
            return
        budget, (x, y) = self.speed * dt, self.position
        direction = None
        while budget > 0 and self.path:
            tx, ty = self.path[0]
            gap = math.dist((x, y), (tx, ty))
            if gap > 0:
                direction = ((tx - x) / gap, (ty - y) / gap)
            if gap <= budget:
                x, y, budget = tx, ty, budget - gap
                self.path.pop(0)
            else:
                x, y, budget = x + direction[0] * budget, y + direction[1] * budget, 0.0
        self.position = [x, y]
        if direction is not None:
            self.heading = math.atan2(direction[1], direction[0])
        if self.path:
            self.velocity = (direction[0] * self.speed, direction[1] * self.speed) if direction else (0.0, 0.0)
        else:
            # PX4 ends a mission in Hold (measured 2026-10-14). / PX4 在任务结束时转入 Hold（2026-10-14 实测）。
            self.mode, self.velocity = "HOLD", (0.0, 0.0)

    def drive(self, points, speed: float) -> None:
        self.advance()
        self.path, self.speed, self.mode = [tuple(map(float, p)) for p in points], float(speed), "MISSION"

    def hold(self) -> None:
        """Hold where it is: coast to a stop and keep the rest of the route. / 原地保持：滑行停住，保留航线剩余部分。"""
        self.advance()
        if self.mode == "MISSION" and self.velocity != (0.0, 0.0):
            self.coast = (tuple(self.position), self.velocity, time.monotonic())
        self.mode = "HOLD"

    def snapshot(self) -> FlightObservation:
        self.advance()
        self.sample += 1
        self.trace.append((time.monotonic(), self.position[0], self.position[1], self.armed))
        now = utcnow()
        stamp = now - timedelta(seconds=1) if self.stale else now
        in_air = {"armed": self.armed, "false": False, "none": None}[self.in_air]
        return FlightObservation(
            timestamp=stamp, valid_until=stamp + timedelta(seconds=0.5), sample_id=self.sample,
            robot_id=self.robot_id,
            pose=Pose(frame=Frame(**self.registry.data["frame"]),
                      position=Position(x=self.position[0], y=self.position[1], z=0.0,
                                        covariance=(0.04, 0, 0, 0, 0.04, 0, 0, 0, 0.09))),
            velocity_enu_mps=[self.velocity[0], self.velocity[1], 0.0], armed=self.armed, in_air=in_air,
            flight_mode=self.mode, localization_healthy=self.localization_ok, home_healthy=True,
            battery_fraction=round(self.battery.fraction, 4), fc_failsafe=self.fc_failsafe,
            source="deterministic_test")

    # ── the adapter surface the guardian calls / guardian 调用的适配器接口 ──

    async def execute(self, node, permitted, phase=None):
        if not node.skill_id.startswith(GROUND):
            raise PermissionError("skill and embodiment differ")
        if not permitted():
            raise PermissionError("control authority changed")
        name, p = node.skill_id.removeprefix(GROUND), node.params
        self.writes.append((name, phase))
        if name == "arm":
            self.mode, self.armed = "HOLD", True
        elif name in {"nav_to", "return_home"}:
            self.drive(p["waypoints"], p["speed_mps"])
        elif name == "recheck" and phase == "approach":
            self.drive([p["goal"], p["face_point"]], p["speed_mps"])
        elif name == "recheck" and phase == "capture":
            self.capture(node)
        elif name == "stop":
            self.hold()
        elif name == "disarm":
            await self.disarm_stationary(permitted)
        else:
            raise ValueError("unsupported ground skill or phase")

    async def disarm_stationary(self, permitted):
        """The real adapter's guard: refused before anything happens unless fresh, stationary and in Hold.

        与真实适配器相同的守卫：除非新鲜、静止且处于 Hold，否则在任何动作之前即被拒绝。
        """
        obs = self.snapshot()
        speed = math.hypot(obs.velocity_enu_mps[0], obs.velocity_enu_mps[1])
        if (not obs.timestamp <= utcnow() < obs.valid_until
                or speed > self.registry.data["thresholds"]["stationary_mps"] or obs.flight_mode != "HOLD"):
            raise PermissionError("disarm only a stationary robot in Hold")
        if not permitted():
            raise PermissionError("control authority changed")
        self.writes.append(("disarm_stationary", None))
        self.armed = False

    async def recover(self, behavior, permitted):
        if not permitted():
            return
        self.writes.append(("recover", behavior.value))
        if behavior == RecoveryBehavior.STOP:
            self.hold()
        elif behavior != RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE:
            raise ValueError("unsupported ground recovery")

    async def resume(self, permitted):
        if not permitted():
            raise PermissionError("control authority changed")
        self.writes.append(("resume", None))
        self.advance()
        if self.path:
            self.mode = "MISSION"

    def capture(self, node):
        camera = self.registry.data["camera"]
        if self.frames:
            raw = self.frames.pop(0) or flat(camera)
        else:
            raw = view(camera, self.position, self.heading, self.blocks)
        observation = self.snapshot()
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"images/{digest}-{uuid.uuid4().hex[:6]}.rgb"
        (self.artifacts / "images").mkdir(exist_ok=True)
        (self.artifacts / relative).write_bytes(raw)
        contract = Evidence(evidence_id="image:" + digest, kind="image", media_ref=relative, sha256=digest,
                            time_window=TimeWindow(timestamp=observation.timestamp, valid_until=observation.valid_until),
                            captured_pose=observation.pose, subject_ids=[node.params["asset_id"]],
                            quality={"width": camera["width_px"], "height": camera["height_px"]},
                            produced_by_skill_instance=node.task_id)
        record = {"media_ref": relative, "sha256": digest, "asset_id": node.params["asset_id"],
                  "width": camera["width_px"], "height": camera["height_px"],
                  "capture_timestamp": observation.timestamp.isoformat(), "sim_time": 0.0,
                  "observation": observation.model_dump(mode="json"), "skill_instance": node.task_id,
                  "source": "deterministic_test", "contract": contract.model_dump(mode="json")}
        (self.artifacts / f"evidence-{node.task_id}.json").write_text(json.dumps(record))
