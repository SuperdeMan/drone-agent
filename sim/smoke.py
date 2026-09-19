"""Read-only M0 smoke: PX4 identity, unarmed telemetry and a live Gazebo world.

只读 M0 冒烟：PX4 身份、未解锁遥测与持续运行的 Gazebo 世界。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

PX4_ROOT = Path("/opt/PX4-Autopilot")
PX4_COMMIT = "d6f12ad1c4f70ad3230afd7d86e971421e02fef4"


def read_topic(topic: str, duration: float = 3) -> str:
    """Observe a topic for a bounded interval, then close only this subscriber.

    在有界时间内观察话题，结束后仅关闭该订阅进程。
    """
    process = subprocess.Popen(
        ["gz", "topic", "-e", "-t", topic],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=duration)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        output, _ = process.communicate(timeout=2)
    return output


def observe_telemetry(timeout: float) -> dict:
    """Receive and validate MAVLink; never send heartbeats, requests or commands.

    接收并校验 MAVLink；不发送心跳、请求或命令。
    """
    from pymavlink import mavutil

    connection = mavutil.mavlink_connection("udpin:0.0.0.0:14540")
    deadline = time.monotonic() + timeout
    heartbeat_count = 0
    system_id = None
    boot_times = []
    try:
        while time.monotonic() < deadline:
            message = connection.recv_match(blocking=True, timeout=min(1, max(0, deadline - time.monotonic())))
            if message is None:
                continue
            if message.get_type() == "HEARTBEAT" and message.autopilot == mavutil.mavlink.MAV_AUTOPILOT_PX4:
                if message.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
                    raise RuntimeError("M0 smoke must remain unarmed")
                system_id = message.get_srcSystem()
                heartbeat_count += 1
            if message.get_type() == "LOCAL_POSITION_NED" and message.get_srcSystem() == system_id:
                if not all(math.isfinite(getattr(message, key)) for key in ("x", "y", "z", "vx", "vy", "vz")):
                    raise RuntimeError("non-finite position telemetry")
                boot_times.append(message.time_boot_ms)
            if heartbeat_count >= 2 and len(boot_times) >= 2 and boot_times[-1] > boot_times[0]:
                return {
                    "autopilot": "PX4", "armed": False, "system_id": system_id,
                    "heartbeat_count": heartbeat_count, "position_samples": len(boot_times),
                    "first_boot_ms": boot_times[0], "last_boot_ms": boot_times[-1], "commands_sent": 0,
                }
    finally:
        connection.close()
    raise RuntimeError("timed out waiting for advancing, unarmed PX4 telemetry")


def smoke(timeout: float) -> dict:
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PX4_ROOT, text=True).strip()
    if revision != PX4_COMMIT:
        raise RuntimeError(f"unexpected PX4 source: {revision}")
    subprocess.run(["pgrep", "-x", "px4"], check=True, capture_output=True)
    telemetry = observe_telemetry(timeout)
    stats = read_topic("/world/default/stats")
    iterations = [int(value) for value in re.findall(r"iterations:\s*(\d+)", stats)]
    if len(iterations) < 2 or iterations[-1] <= iterations[0]:
        raise RuntimeError("Gazebo world clock is not advancing")
    poses = read_topic("/world/default/pose/info")
    if not re.search(r'name:\s*"x500_0"', poses):
        raise RuntimeError("x500_0 was not observed in the Gazebo world")
    return {
        "schema_version": "0.1.0", "status": "passed", "scope": "unarmed_environment_smoke",
        "timestamp": datetime.now(timezone.utc).isoformat(), "px4_version": "1.17.0", "px4_commit": revision,
        "os_release": Path("/etc/os-release").read_text(),
        "px4_binary_sha256": hashlib.sha256((PX4_ROOT / "build/px4_sitl_default/bin/px4").read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "gazebo_packages": subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Package}=${Version}\n", "gz-harmonic", "libgz-sim8"], text=True,
        ).splitlines(),
        "telemetry": telemetry,
        "gazebo": {"world": "default", "model": "x500_0", "first_iteration": iterations[0], "last_iteration": iterations[-1]},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = smoke(args.timeout)
    except Exception as error:
        result = {"schema_version": "0.1.0", "status": "failed", "reason": str(error)}
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if result["status"] == "passed" else 1)
