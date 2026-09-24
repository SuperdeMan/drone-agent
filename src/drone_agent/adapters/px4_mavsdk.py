"""PX4 adapter over MAVSDK: mission mode, recovery commands and the entry into the external mode.

There is no Offboard, raw setpoint or kill interface. M3 (D039) adds one fixed MAVLink command that asks PX4 to
enter the external mode registered by the egress node (the setpoints themselves travel over uXRCE-DDS from that
node, never through here), the mode names of the external nav states and the `land_at` recovery (reposition,
then land). The guardian, not the adapter, decides whether `external_mode` is currently offered.

基于 MAVSDK 的 PX4 适配器：任务模式、恢复命令与进入外部模式。

不提供 Offboard、原始设定值或 kill 接口。M3（D039）增加一条固定的 MAVLink 命令，请求 PX4 进入出口节点注册的
外部模式（设定值本身经 uXRCE-DDS 由该节点发送，从不经过这里）；增加外部 nav 状态的模式名与 `land_at` 恢复（先重定位
再降落）。当前是否提供 `external_mode` 由 guardian 而不是适配器决定。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
from drone_agent.runtime.ledger import canonical, durable_artifact


class Px4Adapter:
    def __init__(self, registry, artifacts: Path, sensor: Path):
        self.registry, self.artifacts, self.sensor = registry, artifacts, sensor
        self.values, self.stamps = {}, {}
        self.received = {}
        self.fc_failsafe = False
        self.raw_battery_fraction = None
        self.tasks = []
        self.sample = 0
        self.external_takeover = False
        self.expected_modes = None
        self.mode_grace = 0.0
        self.transition_from = None
        self.camera_available = sensor.is_file()
        self.capabilities = registry.capability.model_copy(deep=True)
        self.capabilities.recovery_behaviors = {
            RecoveryBehavior.HOLD,
            RecoveryBehavior.RTL,
            RecoveryBehavior.LAND_HERE,
            RecoveryBehavior.LAND_AT,
            RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE,
        }
        if not self.camera_available:
            # Every camera-dependent skill disappears with the camera. / 相机缺席时所有依赖相机的技能一并缺席。
            self.capabilities.skills = [
                s for s in self.capabilities.skills
                if s.skill_id not in {"skill.flight.capture_image", "skill.inspect.asset"}
            ]
            self.capabilities.sensors = [s for s in self.capabilities.sensors if s.sensor_id != "cam_0"]
        self.command_log = []
        self.control_context = {}
        self.evidence = {}
        # Simulation-only fault hook at the sensor boundary; None in every non-injected run.
        # 仅供仿真的传感器边界故障钩子；非注入运行中恒为 None。
        self.frame_filter = None
        self._system = None

    async def connect(self, address="udpin://0.0.0.0:14540"):
        from mavsdk import System

        self._system = System()
        await self._system.connect(system_address=address)
        async with asyncio.timeout(45):
            async for state in self._system.core.connection_state():
                if state.is_connected:
                    break
        streams = {
            "position": self._system.telemetry.position_velocity_ned,
            "global": self._system.telemetry.position,
            "health": self._system.telemetry.health,
            "armed": self._system.telemetry.armed,
            "in_air": self._system.telemetry.in_air,
            "mode": self._system.telemetry.flight_mode,
            "battery": self._system.telemetry.battery,
            "home": self._system.telemetry.home,
            "gps": self._system.telemetry.raw_gps,
            "progress": self._system.mission.mission_progress,
        }
        for name, stream in streams.items():
            self.tasks.append(asyncio.create_task(self._watch(name, stream)))
        self.tasks.append(asyncio.create_task(self._watch_link()))
        self.tasks.append(asyncio.create_task(self._watch_system_status()))
        self.tasks.append(asyncio.create_task(self._watch_current_mode()))
        await self._request_current_mode()
        for method, rate in (
            ("set_rate_position_velocity_ned", 10),
            ("set_rate_position", 5),
            ("set_rate_raw_gps", 5),
            ("set_rate_battery", 2),
            ("set_rate_home", 2),
        ):
            await getattr(self._system.telemetry, method)(rate)
        async with asyncio.timeout(60):
            while True:
                obs = self.snapshot()
                if (
                    obs.pose
                    and obs.localization_healthy
                    and obs.home_healthy
                    and obs.armed is not None
                    and obs.flight_mode != "UNKNOWN"
                    and "current_mode" in self.received
                ):
                    break
                await asyncio.sleep(0.1)

    async def _watch(self, name, stream):
        async for value in stream():
            if name == "mode" and time.monotonic() - self.received.get("current_mode", 0) < 2.5:
                continue
            self.values[name], self.stamps[name] = value, utcnow()
            self.received[name] = time.monotonic()
            if name == "position":
                self.sample += 1
            if name == "mode":
                self.check_mode(value.name)

    def check_mode(self, mode):
        if self.expected_modes:
            pending_previous = time.monotonic() <= self.mode_grace and mode == self.transition_from
            if mode not in self.expected_modes and not pending_previous and not self.landed_mode_transition(mode):
                self.external_takeover = True

    async def _request_current_mode(self):
        from mavsdk.mavlink_direct import MavlinkMessage

        # A fixed telemetry request, never a caller-selectable flight command. / 固定的遥测请求，不能由调用者选择飞行命令。
        fields = {
            "target_system": 1,
            "target_component": 1,
            "command": 511,
            "confirmation": 0,
            "param1": 436,
            "param2": 100000,
            "param3": 0,
            "param4": 0,
            "param5": 0,
            "param6": 0,
            "param7": 0,
        }
        await self._system.mavlink_direct.send_message(
            MavlinkMessage("COMMAND_LONG", 245, 190, 1, 1, json.dumps(fields))
        )

    @staticmethod
    def px4_mode(custom):
        main, sub = (int(custom) >> 16) & 255, (int(custom) >> 24) & 255
        if main == 4 and 11 <= sub <= 18:
            # PX4 external modes 1..8 (nav states 23..30), registered by ROS 2 components (D039).
            # PX4 外部模式 1..8（nav 状态 23..30），由 ROS 2 组件注册（D039）。
            return f"EXTERNAL{sub - 10}"
        if main == 4:
            return {
                1: "READY",
                2: "TAKEOFF",
                3: "HOLD",
                4: "MISSION",
                5: "RETURN_TO_LAUNCH",
                6: "LAND",
                8: "FOLLOW_ME",
                9: "LAND",
                10: "TAKEOFF",
            }.get(sub, "UNKNOWN")
        return {1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 5: "ACRO", 6: "OFFBOARD", 7: "STABILIZED"}.get(main, "UNKNOWN")

    def current_mode(self, fields):
        actual = self.px4_mode(fields["custom_mode"])
        self.received["current_mode"] = self.received["mode"] = time.monotonic()
        self.values["mode"] = SimpleNamespace(name=actual)
        self.stamps["mode"] = utcnow()
        self.check_mode(actual)
        if fields.get("intended_custom_mode"):
            self.check_mode(self.px4_mode(fields["intended_custom_mode"]))

    async def _watch_current_mode(self):
        async for message in self._system.mavlink_direct.message("CURRENT_MODE"):
            if message.component_id == 1:
                self.current_mode(json.loads(message.fields_json))

    def landed_mode_transition(self, mode):
        position = self.values.get("position")
        return bool(
            mode in {"MISSION", "READY"}
            and self.expected_modes
            and "LAND" in self.expected_modes
            and self.values.get("in_air") is False
            and position
            and time.monotonic() - self.received.get("position", 0) < 0.5
            and abs(position.position.down_m) < 0.5
        )

    async def _watch_link(self):
        # Read the PX4 heartbeat on the same guardian-owned connection. / 在 guardian 唯一连接上读取 PX4 心跳。
        async for message in self._system.mavlink_direct.message("HEARTBEAT"):
            fields = json.loads(message.fields_json)
            if fields.get("autopilot") != 12 or message.component_id != 1:
                continue
            self.received["link"] = time.monotonic()
            self.fc_failsafe = fields.get("system_status") in {5, 6}

    async def _watch_system_status(self):
        async for message in self._system.mavlink_direct.message("SYS_STATUS"):
            if message.component_id != 1:
                continue
            fields = json.loads(message.fields_json)
            remaining = fields.get("battery_remaining", -1)
            self.raw_battery_fraction = remaining / 100 if 0 <= remaining <= 100 else None
            self.received["battery_status"] = time.monotonic()

    def snapshot(self):
        now = utcnow()
        stamp = self.stamps.get("position", now - timedelta(days=1))
        pose, velocity = None, []
        p, gps = self.values.get("position"), self.values.get("gps")
        if p:
            covariance = None
            if gps and time.monotonic() - self.received.get("gps", 0) < 3:
                h, v = gps.horizontal_uncertainty_m, gps.vertical_uncertainty_m
                if math.isfinite(h) and math.isfinite(v) and h > 0 and v > 0:
                    covariance = (h * h, 0, 0, 0, h * h, 0, 0, 0, v * v)
            pose = Pose(
                frame=Frame(**self.registry.data["frame"]),
                position=Position(
                    x=p.position.east_m, y=p.position.north_m, z=-p.position.down_m, covariance=covariance
                ),
            )
            velocity = [p.velocity.east_m_s, p.velocity.north_m_s, -p.velocity.down_m_s]
        health, battery, mode = (self.values.get(key) for key in ("health", "battery", "mode"))
        progress = self.values.get("progress")
        # MAVSDK state streams may emit only changes; link and sensor ages are independent.
        # MAVSDK 状态流可能只在变化时发布；链路与传感器新鲜度分开检查。
        statuses_fresh = time.monotonic() - self.received.get("link", 0) < 2.5 and all(
            key in self.values for key in ("armed", "in_air", "health", "mode")
        )
        energy = self.raw_battery_fraction if time.monotonic() - self.received.get("battery_status", 0) < 3 else None
        if battery and time.monotonic() - self.received.get("battery", 0) < 3:
            percent = battery.remaining_percent
            if math.isfinite(percent) and 0 <= percent <= 100:
                estimate = percent / 100.0
                energy = estimate if energy is None else min(energy, estimate)
        if time.monotonic() - self.received.get("position", 0) >= 0.5:
            stamp = min(stamp, now - timedelta(seconds=1))
        return FlightObservation(
            timestamp=stamp,
            valid_until=stamp + timedelta(seconds=0.5),
            sample_id=self.sample,
            robot_id=self.registry.capability.robot_id,
            pose=pose,
            velocity_enu_mps=velocity,
            armed=self.values.get("armed") if statuses_fresh else None,
            in_air=self.values.get("in_air") if statuses_fresh else None,
            flight_mode=mode.name if mode else "UNKNOWN",
            localization_healthy=bool(
                statuses_fresh and health and health.is_local_position_ok and health.is_global_position_ok
            ),
            home_healthy=bool(statuses_fresh and health and health.is_home_position_ok),
            battery_fraction=energy if statuses_fresh else None,
            mission_current=progress.current if progress else 0,
            mission_total=progress.total if progress else 0,
            fc_failsafe=self.fc_failsafe,
        )

    async def _call(self, name, method, permitted, *args):
        if not permitted():
            raise PermissionError("control authority changed")
        self.command_log.append(
            {"operation": name, "timestamp": utcnow().isoformat(), "authority": dict(self.control_context)}
        )
        await method(*args)

    def expect(self, modes):
        current = self.values.get("mode")
        self.transition_from = current.name if current else None
        self.expected_modes = set(modes)
        self.mode_grace = time.monotonic() + 2

    async def route(self, points, speed, permitted):
        from mavsdk.mission import MissionItem, MissionPlan

        origin = self.values["home"]
        items = []
        for east, north, altitude in points:
            latitude = origin.latitude_deg + math.degrees(north / 6378137)
            longitude = origin.longitude_deg + math.degrees(
                east / (6378137 * math.cos(math.radians(origin.latitude_deg)))
            )
            items.append(
                MissionItem(
                    latitude,
                    longitude,
                    altitude,
                    speed,
                    False,
                    float("nan"),
                    float("nan"),
                    MissionItem.CameraAction.NONE,
                    1.0,
                    float("nan"),
                    0.8,
                    float("nan"),
                    float("nan"),
                    MissionItem.VehicleAction.NONE,
                )
            )
        self.expect({"MISSION", "HOLD"})
        await self._call("route_no_auto_rtl", self._system.mission.set_return_to_launch_after_mission, permitted, False)
        await self._call("upload_route", self._system.mission.upload_mission, permitted, MissionPlan(items))
        # PX4 validates an uploaded mission asynchronously; its own integration tests wait one second.
        # PX4 异步校验上传航线；官方集成测试也等待一秒后启动。
        await asyncio.sleep(1)
        await self._call("start_route", self._system.mission.start_mission, permitted)
        async with asyncio.timeout(2):
            while self.values["mode"].name != "MISSION":
                if not permitted():
                    raise PermissionError("route authority changed")
                await asyncio.sleep(0.05)

    async def execute(self, node, permitted, phase=None):
        action, p = node.skill_id.rsplit(".", 1)[1], node.params
        if node.skill_id == "skill.inspect.asset":
            # Approach flies the asset's registered observation route; capture takes one real frame (D034).
            # 接近相位飞该资产登记的观察航线；拍摄相位拍一帧真实影像（D034）。
            if phase == "approach":
                await self.route(self.registry.route(p["approach_route_id"]), p["speed_mps"], permitted)
            elif phase == "capture":
                self.evidence[node.task_id] = await self.capture(node, permitted)
            else:
                raise ValueError("undeclared inspection phase")
            return
        if action == "takeoff":
            self.expect({"TAKEOFF", "HOLD"})
            await self._call(
                "takeoff_altitude", self._system.action.set_takeoff_altitude, permitted, p["altitude_m_agl"]
            )
            await self._call("arm", self._system.action.arm, permitted)
            await self._call("takeoff", self._system.action.takeoff, permitted)
        elif action in {"fly_route", "return_home"}:
            await self.route(
                self.registry.route(p.get("route_id", p.get("return_route_id"))), p.get("speed_mps", 2), permitted
            )
        elif action == "land":
            self.expect({"LAND", "HOLD", "READY"})
            await self._call("land", self._system.action.land, permitted)
        elif action == "capture_image":
            self.evidence[node.task_id] = await self.capture(node, permitted)
        else:
            raise ValueError("unsupported skill")

    async def capture(self, node, permitted):
        observation = self.snapshot()
        async with asyncio.timeout(2):
            while True:
                frame = json.loads(self.sensor.read_text())
                stamp = datetime.fromisoformat(frame["timestamp"])
                if (
                    observation.timestamp <= stamp < observation.valid_until
                    and (utcnow() - stamp).total_seconds() < 0.5
                ):
                    break
                observation = self.snapshot()
                await asyncio.sleep(0.05)
        if not permitted():
            raise PermissionError("capture authority changed")
        raw = base64.b64decode(frame["rgb"], validate=True)
        source = "gazebo_rgb_sensor"
        if self.frame_filter is not None:
            filtered = self.frame_filter(raw, frame)
            if filtered != raw:
                raw, source = filtered, "gazebo_rgb_sensor+injected_degradation"
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"images/{digest}.rgb"
        path = self.artifacts / relative
        path.parent.mkdir(exist_ok=True)
        durable_artifact(path, raw)
        evidence = {
            "media_ref": relative,
            "sha256": digest,
            "asset_id": node.params["asset_id"],
            "width": frame["width"],
            "height": frame["height"],
            "capture_timestamp": frame["timestamp"],
            "sim_time": frame["sim_time"],
            "observation": observation.model_dump(mode="json"),
            "skill_instance": node.task_id,
            "source": source,
        }
        evidence["contract"] = Evidence(
            evidence_id="image:" + digest,
            kind="image",
            media_ref=relative,
            sha256=digest,
            time_window=TimeWindow(timestamp=stamp, valid_until=observation.valid_until),
            captured_pose=observation.pose,
            subject_ids=[node.params["asset_id"]],
            quality={"width": frame["width"], "height": frame["height"]},
            produced_by_skill_instance=node.task_id,
        ).model_dump(mode="json")
        durable_artifact(self.artifacts / f"evidence-{node.task_id}.json", canonical(evidence))
        return evidence

    async def recover(self, behavior, permitted, site=None):
        if behavior == RecoveryBehavior.LAND_AT:
            # Reposition over the registered site at the current height; the guardian lands once above it.
            # 在当前高度重定位到登记降落点上方；到达后由 guardian 降落。
            if site is None or "home" not in self.values:
                raise ValueError("land_at needs a registered site and a home reference")
            origin = self.values["home"]
            here = self.snapshot()
            height = here.pose.position.z if here.pose else None
            if height is None:
                raise ValueError("land_at needs a fresh height")
            latitude = origin.latitude_deg + math.degrees(site[1] / 6378137)
            longitude = origin.longitude_deg + math.degrees(
                site[0] / (6378137 * math.cos(math.radians(origin.latitude_deg)))
            )
            self.expect({"HOLD", "LAND", "READY"})
            await self._call(
                "land_at_reposition",
                self._system.action.goto_location,
                permitted,
                latitude,
                longitude,
                origin.absolute_altitude_m + height,
                float("nan"),
            )
        elif behavior == RecoveryBehavior.HOLD:
            self.expect({"HOLD"})
            await self._call("hold", self._system.action.hold, permitted)
        elif behavior == RecoveryBehavior.RTL:
            self.expect({"RETURN_TO_LAUNCH", "LAND", "HOLD", "READY"})
            await self._call("rtl", self._system.action.return_to_launch, permitted)
        elif behavior == RecoveryBehavior.LAND_HERE:
            self.expect({"LAND", "HOLD", "READY"})
            await self._call("land_here", self._system.action.land, permitted)
        elif behavior != RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE:
            raise ValueError("unsupported recovery behavior")

    @staticmethod
    def external_mode_name(nav_state: int) -> str:
        if not 23 <= int(nav_state) <= 30:
            raise ValueError("not a PX4 external nav state")
        return f"EXTERNAL{int(nav_state) - 22}"

    async def request_external_mode(self, nav_state, permitted):
        """One fixed DO_SET_MODE into the registered external nav state; setpoints never pass through here.

        一条固定的 DO_SET_MODE，进入已注册的外部 nav 状态；设定值从不经过这里。
        """
        from mavsdk.mavlink_direct import MavlinkMessage

        name = self.external_mode_name(nav_state)
        fields = {
            "target_system": 1,
            "target_component": 1,
            "command": 176,
            "confirmation": 0,
            "param1": 1,
            "param2": 4,
            "param3": int(nav_state) - 23 + 11,
            "param4": 0,
            "param5": 0,
            "param6": 0,
            "param7": 0,
        }
        self.expect({name, "HOLD"})
        await self._call(
            "external_mode",
            self._system.mavlink_direct.send_message,
            permitted,
            MavlinkMessage("COMMAND_LONG", 245, 190, 1, 1, json.dumps(fields)),
        )

    async def resume(self, permitted):
        self.expect({"MISSION", "HOLD"})
        await self._call("resume_route", self._system.mission.start_mission, permitted)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self._system:
            self._system._stop_mavsdk_server()
