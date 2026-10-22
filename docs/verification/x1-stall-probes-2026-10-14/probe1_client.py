"""X1 rover stall probe client (D088, S1 diagnosis): does a PX4 v1.17 rover mission stall on its speed-change items?

Three interleaved variants of a short two-point mission from standstill, many times each:
  S — waypoints with a speed, so MAVSDK inserts a DO_CHANGE_SPEED item before each (what the adapter does now);
  N — waypoints without a speed (no DO_CHANGE_SPEED items);
  K — like N, plus one DO_CHANGE_SPEED command (action.set_current_speed) after the start.
A stall is a mission that started (MISSION mode) but did not move 0.3 m within 8 s. Each mission records its mission
progress, the peak speed and the outcome. Runs in the aircraft image's venv in the SITL container's network namespace,
bound to 127.0.0.1:14540, where only instance 1 (the rover) sends. Not a gate.

X1 地面车停滞探针客户端（D088，S1 诊断）：PX4 v1.17 地面车任务是否停在改速项上？从静止开始交替执行三种短的两点任务，
各执行多次：S 带速度的航点（MAVSDK 在每个航点前插入 DO_CHANGE_SPEED，即适配器现在的做法）；N 不带速度的航点（没有
改速项）；K 同 N，启动后另发一次改速命令。停滞指任务已启动（MISSION 模式）但 8 s 内移动不到 0.3 m。每次任务记录进度、峰值
速度与结果。不是门禁。
"""

import asyncio
import json
import math
import sys
import time

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan

ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 30


async def main(out: str) -> None:
    system = System()
    await system.connect(system_address="udpin://127.0.0.1:14540")
    async for state in system.core.connection_state():
        if state.is_connected:
            break
    values: dict = {}

    async def watch(name, stream):
        try:
            async for value in stream():
                values[name] = value
        except Exception as error:  # noqa: BLE001 - a stream failure is a finding
            values.setdefault("errors", []).append(f"{name}:{error!r}"[:160])

    tasks = [asyncio.create_task(watch(n, s)) for n, s in (
        ("pv", system.telemetry.position_velocity_ned), ("home", system.telemetry.home),
        ("health", system.telemetry.health), ("mode", system.telemetry.flight_mode),
        ("armed", system.telemetry.armed), ("progress", system.mission.mission_progress))]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not (values.get("home") and values.get("health")
                                               and values["health"].is_home_position_ok):
        await asyncio.sleep(0.2)
    home = values["home"]
    await system.param.set_param_float("SIM_BAT_DRAIN", 100000.0)
    await system.action.hold()
    await system.action.arm()
    await asyncio.sleep(1.0)

    def here():
        pv = values["pv"]
        return pv.position.east_m, pv.position.north_m

    def item(east, north, speed, last):
        latitude = home.latitude_deg + math.degrees(north / 6378137)
        longitude = home.longitude_deg + math.degrees(east / (6378137 * math.cos(math.radians(home.latitude_deg))))
        return MissionItem(latitude, longitude, 0.0, speed, not last, float("nan"), float("nan"),
                           MissionItem.CameraAction.NONE, 0.0, float("nan"), 0.5, float("nan"), float("nan"),
                           MissionItem.VehicleAction.NONE)

    rows = []
    for index in range(ROUNDS * 3):
        variant = "SNK"[index % 3]
        east, north = here()
        # Two points 1.5 m and 3 m away, alternating east and west so the rover stays near home.
        # 相距 1.5 m 与 3 m 的两点，东西交替，使车留在 home 附近。
        sign = -1.0 if east > 0 else 1.0
        speed = 1.0 if variant == "S" else float("nan")
        plan = MissionPlan([item(east + sign * 1.5, north, speed, False), item(east + sign * 3.0, north, speed, True)])
        record = {"index": index, "variant": variant, "start": [round(east, 2), round(north, 2)]}
        try:
            await system.mission.set_return_to_launch_after_mission(False)
            await system.mission.upload_mission(plan)
            await asyncio.sleep(1.0)
            await system.mission.start_mission()
            if variant == "K":
                await system.action.set_current_speed(1.0)
        except Exception as error:  # noqa: BLE001
            record["error"] = repr(error)[:200]
            rows.append(record)
            continue
        began, moved_at, peak, progress = time.monotonic(), None, 0.0, []
        while time.monotonic() - began < 25:
            pv = values["pv"]
            speed_now = math.hypot(pv.velocity.north_m_s, pv.velocity.east_m_s)
            peak = max(peak, speed_now)
            current = values.get("progress")
            if current is not None and (not progress or progress[-1] != [current.current, current.total]):
                progress.append([current.current, current.total])
            if moved_at is None and math.dist(here(), (east, north)) > 0.3:
                moved_at = time.monotonic() - began
            if moved_at is None and time.monotonic() - began > 8:
                break
            if moved_at is not None and current is not None and current.current >= current.total and speed_now < 0.05:
                break
            await asyncio.sleep(0.1)
        record.update(moved_after_s=None if moved_at is None else round(moved_at, 2), stalled=moved_at is None,
                      peak_mps=round(peak, 2), progress=progress[:8],
                      mode=getattr(values.get("mode"), "name", None))
        rows.append(record)
        print(json.dumps(record), flush=True)
        await system.action.hold()
        await asyncio.sleep(1.5)
    summary = {}
    for variant in "SNK":
        mine = [r for r in rows if r["variant"] == variant and "error" not in r]
        summary[variant] = {"missions": len(mine), "stalls": sum(r["stalled"] for r in mine),
                            "peak_mps_max": max((r["peak_mps"] for r in mine), default=None),
                            "errors": sum(1 for r in rows if r["variant"] == variant and "error" in r)}
    for task in tasks:
        task.cancel()
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"summary": summary, "rows": rows, "stream_errors": values.get("errors", [])}, handle, indent=1)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(sys.argv[1]), 3000))
