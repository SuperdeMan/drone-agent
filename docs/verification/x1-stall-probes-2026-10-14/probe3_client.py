"""X1 rover stall probe 3 (D088, S1 diagnosis): one route from the parking spot of a freshly booted rover, facing north.

The route is the variant named on the command line (map frame, metres, the first point the parking spot), uploaded
with MAVSDK's speed-change items as the adapter does, after Hold, arm and 1.5 s. A stall is no 0.3 m of motion in
10 s; on a stall one kick (Hold, 0.5 s, start again) is tried and watched 8 s. Prints one JSON row. Not a gate.

X1 地面车停滞探针 3（D088，S1 诊断）：刚启动、停在停车位朝北的地面车执行一条航线。航线取命令行指定的变体（地图坐标，米，
首点为停车位），与适配器一样带 MAVSDK 改速项上传；之前先保持、解锁、等 1.5 s。10 s 内移动不到 0.3 m 即停滞；停滞时尝试
一次补救（保持、0.5 s、再启动）并观察 8 s。输出一行 JSON。不是门禁。
"""

import asyncio
import json
import math
import sys
import time

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan

VARIANTS = {
    "A": [[0.0, 0.0], [0.625, 1.875], [0.875, 3.875]],
    "B": [[0.0, 0.0], [0.875, 2.625], [1.125, 3.875]],
    "C": [[0.0, 0.0], [0.625, 1.875]],
}


async def main(variant: str) -> None:
    system = System()
    await system.connect(system_address="udpin://127.0.0.1:14540")
    async for state in system.core.connection_state():
        if state.is_connected:
            break
    values: dict = {}

    async def watch(name, stream):
        async for value in stream():
            values[name] = value

    tasks = [asyncio.create_task(watch(n, s)) for n, s in (
        ("pv", system.telemetry.position_velocity_ned), ("home", system.telemetry.home),
        ("health", system.telemetry.health), ("progress", system.mission.mission_progress),
        ("heading", system.telemetry.heading))]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (values.get("home") and values.get("pv") and values.get("health")
                                               and values["health"].is_home_position_ok):
        await asyncio.sleep(0.2)
    home = values["home"]
    start = (values["pv"].position.east_m, values["pv"].position.north_m)
    items = []
    points = VARIANTS[variant]
    for index, (east, north) in enumerate(points):
        latitude = home.latitude_deg + math.degrees(north / 6378137)
        longitude = home.longitude_deg + math.degrees(east / (6378137 * math.cos(math.radians(home.latitude_deg))))
        items.append(MissionItem(latitude, longitude, 0.0, 1.0, index < len(points) - 1, float("nan"), float("nan"),
                                 MissionItem.CameraAction.NONE, 0.0, float("nan"), 0.5, float("nan"), float("nan"),
                                 MissionItem.VehicleAction.NONE))
    row = {"variant": variant, "start": [round(v, 3) for v in start],
           "heading_deg": round(values["heading"].heading_deg, 1) if values.get("heading") else None}
    await system.action.hold()
    await system.action.arm()
    await asyncio.sleep(1.5)
    await system.mission.set_return_to_launch_after_mission(False)
    await system.mission.upload_mission(MissionPlan(items))
    await asyncio.sleep(1.0)
    await system.mission.start_mission()

    async def moved_within(seconds: float) -> bool:
        began = time.monotonic()
        while time.monotonic() - began < seconds:
            pv = values["pv"]
            if math.dist((pv.position.east_m, pv.position.north_m), start) > 0.3:
                return True
            await asyncio.sleep(0.1)
        return False

    moved = await moved_within(10.0)
    row["stalled"] = not moved
    progress = values.get("progress")
    row["progress"] = [progress.current, progress.total] if progress else None
    if not moved:
        await system.action.hold()
        await asyncio.sleep(0.5)
        await system.mission.start_mission()
        row["kick_recovered"] = await moved_within(8.0)
    began = time.monotonic()
    while time.monotonic() - began < 25:
        progress = values.get("progress")
        pv = values["pv"]
        if progress is not None and progress.current >= progress.total \
                and math.hypot(pv.velocity.north_m_s, pv.velocity.east_m_s) < 0.05:
            break
        await asyncio.sleep(0.2)
    pv = values["pv"]
    row["end"] = [round(pv.position.east_m, 2), round(pv.position.north_m, 2)]
    row["finished"] = bool(values.get("progress") and values["progress"].current >= values["progress"].total)
    await system.action.hold()
    await asyncio.sleep(1.0)
    await system.action.disarm()
    for task in tasks:
        task.cancel()
    print("ROW " + json.dumps(row), flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(sys.argv[1]), 150))
