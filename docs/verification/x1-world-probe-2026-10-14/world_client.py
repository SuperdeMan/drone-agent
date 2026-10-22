"""X1 world probe client (D088, S1 preparation): read the parameters the X1 image sets on the rover and drive it once,
so its flight log can be aligned with the Gazebo truth. Runs in the aircraft image's venv (MAVSDK 3.17.4) inside the
SITL container's network namespace, bound to 127.0.0.1:14540, where only instance 1 (the rover) sends. Not a gate.

X1 世界探针客户端（D088，S1 准备）：读取 X1 镜像为地面车设置的参数，并让它行驶一次，以便把其飞行日志与 Gazebo 真值对齐。
在机载镜像的 venv（MAVSDK 3.17.4）中运行，位于 SITL 容器的网络命名空间内，绑定 127.0.0.1:14540，只有实例 1（地面车）
发往这里。不是门禁。
"""

import asyncio
import json
import math
import sys
import time
from datetime import datetime, timezone

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def main(out: str) -> None:
    result = {"events": [], "params": {}, "samples": []}
    values = {}

    def event(kind, **body):
        result["events"].append({"at": now(), "kind": kind, **body})

    system = System()
    await system.connect(system_address="udpin://127.0.0.1:14540")
    async for state in system.core.connection_state():
        if state.is_connected:
            break
    event("connected")

    async def watch(name, stream):
        try:
            async for value in stream():
                if name == "armed" and values.get("armed") != value:
                    event("armed_changed", value=value)
                values[name] = value
        except Exception as error:  # noqa: BLE001 - a stream failure is a finding
            event("stream_error", stream=name, error=repr(error)[:200])

    tasks = [asyncio.create_task(watch(n, s)) for n, s in (
        ("armed", system.telemetry.armed), ("pv", system.telemetry.position_velocity_ned),
        ("home", system.telemetry.home), ("health", system.telemetry.health))]

    async def sampler():
        while True:
            pv = values.get("pv")
            if pv is not None:
                result["samples"].append({"at": now(), "n": round(pv.position.north_m, 3),
                                          "e": round(pv.position.east_m, 3), "armed": values.get("armed")})
            await asyncio.sleep(0.2)

    tasks.append(asyncio.create_task(sampler()))
    for name in ("SDLOG_MODE", "SYS_AUTOSTART", "MAV_SYS_ID"):
        try:
            result["params"][name] = await asyncio.wait_for(system.param.get_param_int(name), 5)
        except Exception as error:  # noqa: BLE001
            result["params"][name] = repr(error)[:120]
    for name in ("SIM_BAT_DRAIN", "NAV_ACC_RAD"):
        try:
            result["params"][name] = round(await asyncio.wait_for(system.param.get_param_float(name), 5), 4)
        except Exception as error:  # noqa: BLE001
            result["params"][name] = repr(error)[:120]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (values.get("home") and values.get("health")
                                               and values["health"].is_home_position_ok):
        await asyncio.sleep(0.2)
    event("home", ok=bool(values.get("home")))
    home = values["home"]
    north, east = 0.0, 2.5
    item = MissionItem(home.latitude_deg + math.degrees(north / 6378137),
                       home.longitude_deg + math.degrees(east / (6378137 * math.cos(math.radians(home.latitude_deg)))),
                       0.0, 1.0, False, float("nan"), float("nan"), MissionItem.CameraAction.NONE, 0.0, float("nan"),
                       0.5, float("nan"), float("nan"), MissionItem.VehicleAction.NONE)
    await system.mission.set_return_to_launch_after_mission(False)
    await system.mission.upload_mission(MissionPlan([item]))
    await system.action.hold()
    event("arm_sent")
    await system.action.arm()
    event("arm_acked")
    await asyncio.sleep(1.0)
    await system.mission.start_mission()
    event("mission_started")
    deadline = time.monotonic() + 40
    moved = False
    while time.monotonic() < deadline:
        pv = values.get("pv")
        if pv is not None:
            speed = math.hypot(pv.velocity.north_m_s, pv.velocity.east_m_s)
            if speed > 0.3:
                moved = True
            if moved and speed < 0.05 and math.hypot(pv.position.east_m - east, pv.position.north_m - north) < 1.0:
                break
        await asyncio.sleep(0.1)
    event("arrived", moved=moved)
    await system.action.hold()
    await asyncio.sleep(2.0)
    event("disarm_sent")
    await system.action.disarm()
    event("disarm_acked")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and values.get("armed") is not False:
        await asyncio.sleep(0.1)
    await asyncio.sleep(3.0)
    event("done")
    for task in tasks:
        task.cancel()
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1, default=str)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(sys.argv[1]), 240))
