"""X1 rover stall probe 2 (D088, S1 diagnosis): the first drive after each arming, as in every S1 ground mission.

Each round: Hold, arm, wait 1.5 s, upload a route that starts at the robot's own position (as the compiler plans it,
with MAVSDK's speed-change items), wait 1 s, start, and watch for 6 s. A stall is no 0.3 m of motion; on a stall the
probe tries one kick (Hold, 0.5 s, start again) and watches 6 s more. Then Hold, wait until still, disarm. Rounds
alternate between this process's own session (P) and a fresh MAVSDK session per round (F, a child process, like a fresh
guardian). RO_SPEED_LIM is set to 1.0 first, and every round records its peak speed. Not a gate.

X1 地面车停滞探针 2（D088，S1 诊断）：每次解锁后的第一次行驶，与每次 S1 地面任务相同。每轮：保持、解锁、等 1.5 s、上传从车所在
位置出发的航线（与编译器规划的一样，带 MAVSDK 的改速项）、等 1 s、启动、观察 6 s。停滞指 6 s 内移动不到 0.3 m；停滞时
尝试一次补救（保持、0.5 s、再启动）并再观察 6 s。之后保持、静止后上锁。各轮在本进程的会话（P）与每轮新开的 MAVSDK
会话（F，子进程，如同新的 guardian）之间交替。先把 RO_SPEED_LIM 设为 1.0，每轮记录峰值速度。不是门禁。
"""

import asyncio
import json
import math
import subprocess
import sys
import time

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan


async def connect(port: int) -> System:
    system = System(port=port)
    await system.connect(system_address="udpin://127.0.0.1:14540")
    async for state in system.core.connection_state():
        if state.is_connected:
            break
    return system


async def one_round(system: System, values: dict, sign: float) -> dict:
    home = values["home"]
    pv = values["pv"]
    east, north = pv.position.east_m, pv.position.north_m

    def item(e, n, last):
        latitude = home.latitude_deg + math.degrees(n / 6378137)
        longitude = home.longitude_deg + math.degrees(e / (6378137 * math.cos(math.radians(home.latitude_deg))))
        return MissionItem(latitude, longitude, 0.0, 1.0, not last, float("nan"), float("nan"),
                           MissionItem.CameraAction.NONE, 0.0, float("nan"), 0.5, float("nan"), float("nan"),
                           MissionItem.VehicleAction.NONE)

    plan = MissionPlan([item(east, north, False), item(east + sign * 1.5, north, False),
                        item(east + sign * 3.0, north, True)])
    record = {"start": [round(east, 2), round(north, 2)]}
    await system.action.hold()
    await system.action.arm()
    await asyncio.sleep(1.5)
    await system.mission.set_return_to_launch_after_mission(False)
    await system.mission.upload_mission(plan)
    await asyncio.sleep(1.0)
    await system.mission.start_mission()

    async def watch(seconds: float) -> tuple[bool, float]:
        began, peak = time.monotonic(), 0.0
        while time.monotonic() - began < seconds:
            now = values["pv"]
            peak = max(peak, math.hypot(now.velocity.north_m_s, now.velocity.east_m_s))
            if math.dist((now.position.east_m, now.position.north_m), (east, north)) > 0.3:
                return True, peak
            await asyncio.sleep(0.1)
        return False, peak

    moved, peak = await watch(6.0)
    record["stalled"] = not moved
    if not moved:
        progress = values.get("progress")
        record["progress_at_stall"] = [progress.current, progress.total] if progress else None
        await system.action.hold()
        await asyncio.sleep(0.5)
        await system.mission.start_mission()
        moved, peak2 = await watch(6.0)
        record["kick_recovered"] = moved
        peak = max(peak, peak2)
    began = time.monotonic()
    while time.monotonic() - began < 20:
        now = values["pv"]
        speed = math.hypot(now.velocity.north_m_s, now.velocity.east_m_s)
        record["peak_mps"] = round(max(peak, speed), 2)
        peak = max(peak, speed)
        progress = values.get("progress")
        if progress is not None and progress.current >= progress.total and speed < 0.05:
            break
        await asyncio.sleep(0.1)
    await system.action.hold()
    await asyncio.sleep(1.5)
    await system.action.disarm()
    await asyncio.sleep(1.0)
    return record


async def session(port: int, rounds: int, sign_start: float, out: list, label: str, params: bool) -> None:
    system = await connect(port)
    values: dict = {}

    async def watch(name, stream):
        async for value in stream():
            values[name] = value

    tasks = [asyncio.create_task(watch(n, s)) for n, s in (
        ("pv", system.telemetry.position_velocity_ned), ("home", system.telemetry.home),
        ("health", system.telemetry.health), ("progress", system.mission.mission_progress))]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (values.get("home") and values.get("pv") and values.get("health")
                                               and values["health"].is_home_position_ok):
        await asyncio.sleep(0.2)
    if params:
        await system.param.set_param_float("SIM_BAT_DRAIN", 100000.0)
        await system.param.set_param_float("RO_SPEED_LIM", 1.0)
    for index in range(rounds):
        sign = sign_start if index % 2 == 0 else -sign_start
        try:
            record = await one_round(system, values, sign)
        except Exception as error:  # noqa: BLE001 - a failed round is a finding
            record = {"error": repr(error)[:200]}
        record["session"] = label
        out.append(record)
        print(json.dumps(record), flush=True)
    for task in tasks:
        task.cancel()


async def main(out_path: str, rounds: int) -> None:
    rows: list = []
    if len(sys.argv) > 3 and sys.argv[3] == "fresh":
        # A child round in its own session, like a fresh guardian. / 子进程中的一轮，使用自己的会话，如同新的 guardian。
        await session(50051, 1, float(sys.argv[4]), rows, "F", sys.argv[5] == "params")
        print("ROW " + json.dumps(rows[0]), flush=True)
        return
    for index in range(rounds):
        child = subprocess.run([sys.executable, __file__, out_path, "1", "fresh", "1.0" if index % 2 == 0 else "-1.0",
                                "params" if index == 0 else "-"], capture_output=True, text=True, timeout=180)
        line = next((ln for ln in child.stdout.splitlines() if ln.startswith("ROW ")), None)
        row = json.loads(line[4:]) if line else {"session": "F", "error": child.stderr[-300:]}
        rows.append(row)
        print(json.dumps(row), flush=True)
    summary = {}
    for label in "F":
        mine = [r for r in rows if r.get("session") == label and "error" not in r]
        summary[label] = {"rounds": len(mine), "stalls": sum(1 for r in mine if r.get("stalled")),
                          "kick_recovered": sum(1 for r in mine if r.get("kick_recovered")),
                          "peak_mps_max": max((r.get("peak_mps", 0) for r in mine), default=None),
                          "errors": sum(1 for r in rows if r.get("session") == label and "error" in r)}
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({"summary": summary, "rows": rows}, handle, indent=1)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(sys.argv[1], int(sys.argv[2])), 5400))
