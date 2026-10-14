"""X1 rover probe (D088): how PX4 v1.17.0's gz_rover_differential behaves in SITL, measured before any onboard code.

Runs inside the aircraft image (its venv has MAVSDK 3.17.4) in the network namespace of a container running the
rover SITL. Each phase records what it saw and never aborts the others; a failed call is a finding, not a crash.
Nothing here is a release gate.

X1 地面车探针（D088）：在写任何机载代码之前，实测 PX4 v1.17.0 的 gz_rover_differential 在 SITL 中的行为。
在机载镜像（其 venv 带 MAVSDK 3.17.4）中运行，共享运行地面车 SITL 的容器的网络命名空间。每个阶段记录所见，不会中止其他
阶段；调用失败本身就是发现，而不是崩溃。这里没有任何发布门禁。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan

PARAMS_INT = ["COM_RC_IN_MODE", "NAV_RCL_ACT", "COM_RCL_EXCEPT", "COM_ARM_WO_GPS", "CBRK_SUPPLY_CHK", "SYS_AUTOSTART",
              "COM_DISARM_PRFLT", "MAV_TYPE"]
PARAMS_FLOAT = ["COM_DISARM_PRFLT", "COM_DISARM_LAND", "NAV_ACC_RAD", "RD_WHEEL_TRACK", "RO_MAX_THR_SPEED",
                "RO_SPEED_LIM", "RO_SPEED_P", "RO_YAW_RATE_LIM", "NAV_LOITER_RAD", "SIM_BAT_DRAIN", "RTL_RETURN_ALT",
                "COM_OF_LOSS_T", "GF_MAX_HOR_DIST"]


class Probe:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.values: dict = {}
        self.samples: list[dict] = []
        self.events: list[dict] = []
        self.system = System()

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def log(self, kind: str, **body) -> None:
        entry = {"t": self.now(), "kind": kind, **body}
        self.events.append(entry)
        print(json.dumps(entry, default=str), flush=True)

    async def watch(self, name, stream) -> None:
        try:
            async for value in stream():
                if name in ("armed", "in_air", "mode", "landed") and self.values.get(name) != value:
                    self.log("change", stream=name, value=getattr(value, "name", value))
                self.values[name] = value
        except Exception as error:  # noqa: BLE001 - a stream failure is itself a finding
            self.log("stream_error", stream=name, error=repr(error)[:200])

    def sample(self) -> dict:
        pv, battery = self.values.get("pv"), self.values.get("battery")
        mode, landed = self.values.get("mode"), self.values.get("landed")
        return {"t": self.now(), "in_air": self.values.get("in_air"), "armed": self.values.get("armed"),
                "mode": getattr(mode, "name", None), "landed": getattr(landed, "name", None),
                "n": round(pv.position.north_m, 3) if pv else None, "e": round(pv.position.east_m, 3) if pv else None,
                "vn": round(pv.velocity.north_m_s, 3) if pv else None,
                "ve": round(pv.velocity.east_m_s, 3) if pv else None,
                "battery": round(battery.remaining_percent, 4) if battery else None}

    async def sampler(self) -> None:
        while True:
            self.samples.append(self.sample())
            await asyncio.sleep(0.2)

    def speed(self) -> float:
        pv = self.values.get("pv")
        return math.hypot(pv.velocity.north_m_s, pv.velocity.east_m_s) if pv else float("nan")

    def position(self) -> tuple[float, float]:
        pv = self.values["pv"]
        return pv.position.east_m, pv.position.north_m

    async def call(self, name: str, function, *args, timeout: float = 10) -> bool:
        try:
            await asyncio.wait_for(function(*args), timeout)
        except Exception as error:  # noqa: BLE001
            self.log("call", name=name, ok=False, error=repr(error)[:300])
            return False
        self.log("call", name=name, ok=True)
        return True

    async def until(self, predicate, timeout: float, step: float = 0.1) -> float | None:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if predicate():
                return round(time.monotonic() - start, 2)
            await asyncio.sleep(step)
        return None

    async def ensure_armed(self) -> bool:
        if self.values.get("armed"):
            return True
        await self.call("hold_before_arm", self.system.action.hold)
        if not await self.call("arm", self.system.action.arm):
            return False
        return await self.until(lambda: self.values.get("armed") is True, 5) is not None

    async def mission(self, label: str, points, speed: float) -> bool:
        home = self.values["home"]
        items = []
        for east, north in points:
            latitude = home.latitude_deg + math.degrees(north / 6378137)
            longitude = home.longitude_deg + math.degrees(east / (6378137 * math.cos(math.radians(home.latitude_deg))))
            items.append(MissionItem(latitude, longitude, 0.0, speed, False, float("nan"), float("nan"),
                                     MissionItem.CameraAction.NONE, 0.0, float("nan"), 0.5, float("nan"),
                                     float("nan"), MissionItem.VehicleAction.NONE))
        await self.call(f"{label}_no_auto_rtl", self.system.mission.set_return_to_launch_after_mission, False)
        if not await self.call(f"{label}_upload", self.system.mission.upload_mission, MissionPlan(items)):
            return False
        await asyncio.sleep(1)
        return await self.call(f"{label}_start", self.system.mission.start_mission)


async def run(out: str, budget_s: float) -> None:
    probe = Probe()
    system = probe.system
    result: dict = {"format": "drone.x1-rover-probe/v1", "phases": {}}
    await system.connect(system_address="udpin://0.0.0.0:14540")
    async with asyncio.timeout(90):
        async for state in system.core.connection_state():
            if state.is_connected:
                break
    probe.log("connected")
    streams = {"pv": system.telemetry.position_velocity_ned, "armed": system.telemetry.armed,
               "in_air": system.telemetry.in_air, "mode": system.telemetry.flight_mode,
               "landed": system.telemetry.landed_state, "battery": system.telemetry.battery,
               "home": system.telemetry.home, "health": system.telemetry.health,
               "progress": system.mission.mission_progress}
    tasks = [asyncio.create_task(probe.watch(name, stream)) for name, stream in streams.items()]
    tasks.append(asyncio.create_task(probe.sampler()))
    ready = await probe.until(lambda: probe.values.get("health") is not None
                              and probe.values["health"].is_global_position_ok
                              and probe.values["health"].is_home_position_ok and "home" in probe.values, 120)
    result["phases"]["health_ready_s"] = ready
    params = {}
    for name in PARAMS_INT:
        try:
            params[name] = await asyncio.wait_for(system.param.get_param_int(name), 5)
        except Exception as error:  # noqa: BLE001
            params.setdefault(name, f"int: {type(error).__name__}")
    for name in PARAMS_FLOAT:
        try:
            params[name] = round(await asyncio.wait_for(system.param.get_param_float(name), 5), 4)
        except Exception as error:  # noqa: BLE001
            if not isinstance(params.get(name), (int, float)):
                params[name] = f"float: {type(error).__name__}"
    result["params"] = params
    deadline = time.monotonic() + budget_s

    # 1. Disarmed and idle. / 未解锁、静止。
    await asyncio.sleep(5)
    result["phases"]["idle"] = probe.sample()

    # 2. Armed and stationary: does PX4 disarm a rover by itself? / 解锁且静止：PX4 会不会自行上锁？
    armed = await probe.ensure_armed()
    auto = await probe.until(lambda: probe.values.get("armed") is False, 20) if armed else None
    result["phases"]["armed_stationary"] = {"armed": armed, "auto_disarm_after_s": auto, "last": probe.sample()}

    # 3. A two-leg mission away from home; what follows its end? / 远离 home 的两段任务；结束后会怎样？
    if await probe.ensure_armed() and await probe.mission("mission_a", [(6.0, 0.0), (6.0, 6.0)], 1.0):
        done = await probe.until(lambda: probe.values.get("progress") is not None
                                 and probe.values["progress"].total > 0
                                 and probe.values["progress"].current >= probe.values["progress"].total, 60)
        stopped = await probe.until(lambda: probe.speed() < 0.05, 15)
        east, north = probe.position()
        after_end = await probe.until(lambda: probe.values.get("armed") is False, 15)
        result["phases"]["mission_end"] = {"done_after_s": done, "stopped_after_s": stopped,
                                           "end_error_m": round(math.hypot(east - 6.0, north - 6.0), 3),
                                           "mode_after": probe.sample()["mode"], "auto_disarm_after_s": after_end,
                                           "last": probe.sample()}

    # 4. RTL from where the mission left the rover: straight back or along the mission? / 从任务终点 RTL：直线回
    #    home，还是沿任务路径？
    if await probe.ensure_armed():
        start = probe.position()
        track = []
        await probe.call("rtl", system.action.return_to_launch)
        t_end = time.monotonic() + 60
        while time.monotonic() < t_end:
            east, north = probe.position()
            track.append((round(east, 3), round(north, 3)))
            if math.hypot(east, north) < 0.8 and probe.speed() < 0.05:
                break
            await asyncio.sleep(0.2)
        ax, ay = start
        length = math.hypot(ax, ay) or 1.0
        deviation = max((abs(ay * x - ax * y) / length for x, y in track), default=None)
        result["phases"]["rtl"] = {"start": [round(ax, 3), round(ay, 3)], "samples": len(track),
                                   "end": track[-1] if track else None,
                                   "max_offset_from_straight_m": round(deviation, 3) if deviation is not None else None,
                                   "mode_after": probe.sample()["mode"],
                                   "auto_disarm_after_s": await probe.until(lambda: probe.values.get("armed") is False,
                                                                            15)}

    # 5. Hold while driving: how fast and how far does it stop? / 行驶中 Hold：多快停下、滑行多远？
    if await probe.ensure_armed() and await probe.mission("mission_b", [(0.0, 12.0)], 1.5):
        await probe.until(lambda: probe.speed() > 1.0, 15)
        before = probe.position()
        speed_before = round(probe.speed(), 3)
        await probe.call("hold_while_driving", system.action.hold)
        stopped = await probe.until(lambda: probe.speed() < 0.05, 15)
        after = probe.position()
        result["phases"]["hold_while_driving"] = {"speed_before": speed_before, "stopped_after_s": stopped,
                                                  "distance_m": round(math.dist(before, after), 3),
                                                  "mode_after": probe.sample()["mode"]}

    # 6. Disarm while driving: does PX4 accept a plain disarm? / 行驶中 disarm：PX4 是否接受普通上锁命令？
    if time.monotonic() < deadline and await probe.ensure_armed() \
            and await probe.mission("mission_c", [(-8.0, 0.0)], 1.5):
        await probe.until(lambda: probe.speed() > 1.0, 15)
        speed_before = round(probe.speed(), 3)
        accepted = await probe.call("disarm_while_driving", system.action.disarm)
        disarmed = await probe.until(lambda: probe.values.get("armed") is False, 5)
        stopped = await probe.until(lambda: probe.speed() < 0.05, 15)
        result["phases"]["disarm_while_driving"] = {"speed_before": speed_before, "accepted": accepted,
                                                    "disarmed_after_s": disarmed, "stopped_after_s": stopped,
                                                    "last": probe.sample()}

    # 7. A stationary disarm in HOLD, the path the onboard code would take. / HOLD 中静止上锁，即机载代码会走的路径。
    if await probe.ensure_armed():
        await probe.call("hold_before_disarm", system.action.hold)
        await probe.until(lambda: probe.speed() < 0.05, 10)
        accepted = await probe.call("disarm_stationary", system.action.disarm)
        result["phases"]["disarm_stationary"] = {"accepted": accepted,
                                                 "disarmed_after_s": await probe.until(
                                                     lambda: probe.values.get("armed") is False, 5)}

    # The in_air values seen against armed and motion. / in_air 与解锁、运动状态的对应。
    seen = {}
    for row in probe.samples:
        if row["armed"] is None:
            continue
        moving = row["vn"] is not None and math.hypot(row["vn"], row["ve"]) > 0.1
        key = f"armed={row['armed']} moving={moving}"
        seen.setdefault(key, set()).add(str(row["in_air"]))
    result["in_air_seen"] = {key: sorted(values) for key, values in seen.items()}
    result["landed_seen"] = sorted({str(r["landed"]) for r in probe.samples})
    result["modes_seen"] = sorted({str(r["mode"]) for r in probe.samples})
    driving = [r for r in probe.samples if r["armed"] and r["vn"] is not None and math.hypot(r["vn"], r["ve"]) > 0.5]
    if len(driving) > 5 and driving[0]["battery"] is not None:
        span = driving[-1]["t"] - driving[0]["t"]
        result["battery_drop_while_driving_per_min"] = round((driving[0]["battery"] - driving[-1]["battery"])
                                                             / max(span, 1) * 60, 5)
    result["events"] = probe.events
    result["samples"] = probe.samples[::2]
    for task in tasks:
        task.cancel()
    with open(out, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=1, default=str)
    print(json.dumps({k: v for k, v in result.items() if k not in ("events", "samples")}, default=str), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--budget-s", type=float, default=420)
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(run(args.out, args.budget_s), args.budget_s + 180))


if __name__ == "__main__":
    main()
