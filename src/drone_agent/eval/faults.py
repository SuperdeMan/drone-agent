"""Explicit simulation-only faults at the measured runtime input boundary.

显式的仅仿真故障，在已标注的运行时输入边界注入。
"""

import asyncio
import json
from datetime import timedelta

from drone_agent.contracts import utcnow


def install_injection(guardian, path):
    snapshot = guardian.adapter.snapshot
    original_tick = guardian.tick
    original_execute = guardian.adapter.execute
    observed = set()

    def instruction():
        if not path.exists():
            return {}
        value = json.loads(path.read_text())
        if value.get("id") not in observed:
            guardian.record("fault_injected", injection=value, boundary="runtime_input")
            observed.add(value.get("id"))
        return value

    def modified_snapshot():
        obs = snapshot()
        fault = instruction().get("kind")
        if fault == "observation_stale":
            obs.timestamp = utcnow() - timedelta(seconds=3)
            obs.valid_until = utcnow() - timedelta(seconds=2)
        elif fault in {"energy_low", "energy_critical"}:
            obs.battery_fraction = 0.25 if fault == "energy_low" else 0.15
        elif fault == "geofence":
            obs.velocity_enu_mps = [30, 0, 0]
        elif fault == "localization_lost":
            obs.localization_healthy = False
        elif fault == "fc_failsafe":
            obs.fc_failsafe = True
        return obs

    async def tick():
        fault = instruction().get("kind")
        if fault == "lease_expired" and guardian.gate.lease:
            guardian.gate.revoke()
        if fault == "uplink_lost":
            guardian.uplink_ok, guardian.authorized_to_continue = False, False
        if fault == "mode_changed":
            guardian.adapter.external_takeover = True
        await original_tick()

    async def execute(node, permitted, **kwargs):
        await original_execute(node, permitted, **kwargs)
        fault = instruction()
        if fault.get("kind") == "command_timeout" and node.task_id == fault.get("step_id", "fly_route"):
            await asyncio.sleep(7)

    degraded = {"count": 0}

    def degrade(raw, frame):
        # Flatten the first `count` captured frames to uniform grey: no signature colour, no edges.
        # 把前 `count` 次拍摄的帧压成均匀灰色：没有特征颜色，也没有边缘。
        fault = instruction()
        if fault.get("kind") != "image_degraded" or degraded["count"] >= int(fault.get("count", 1)):
            return raw
        degraded["count"] += 1
        guardian.record("fault_injected", injection={**fault, "applied": degraded["count"]}, boundary="sensor_frame")
        return bytes([128]) * len(raw)

    guardian.adapter.snapshot = modified_snapshot
    guardian.adapter.execute = execute
    guardian.adapter.frame_filter = degrade
    guardian.tick = tick
