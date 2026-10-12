"""Power log of the measuring host: whether it ran on mains power, sampled every few seconds (D085).

A laptop on battery throttles its processor, and a timing measurement taken then says nothing about the code. The
growth ladder's second attempt lost seven minutes of its decisive window to a battery switch that the CPU benchmark
before and after the run could not see, so the gate also requires this log to show mains power throughout every
measurement. Only Windows reports the line status here; elsewhere the samples say `unknown`, which the gate treats as
not on mains power.

测量主机的电源记录：每隔几秒记录一次是否接交流电（D085）。笔记本用电池时会降频，此时的计时测量不能说明代码。规模阶梯
第二轮的关键窗口有七分钟在电池上运行，而运行前后的 CPU 基准看不到这一点，因此门禁还要求本记录在每次测量的全程都显示
接交流电。这里只有 Windows 报告供电状态；其他系统的样本为 `unknown`，门禁视为未接交流电。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


class _Status(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def sample() -> dict:
    """One sample: `ac` is "online", "battery" or "unknown". / 一个样本：`ac` 为 "online"、"battery" 或 "unknown"。"""
    at = datetime.now(UTC).isoformat()
    if sys.platform != "win32":
        return {"at": at, "ac": "unknown"}
    status = _Status()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return {"at": at, "ac": "unknown"}
    line = {0: "battery", 1: "online"}.get(status.ACLineStatus, "unknown")
    return {"at": at, "ac": line, "battery_saver": bool(status.SystemStatusFlag & 1)}


def covered(log: list[dict], start: str, end: str, *, max_gap_s: float = 30.0) -> dict:
    """Whether the log shows mains power for the whole interval, with no gap longer than `max_gap_s`.

    记录是否在整个区间内都显示接交流电，且样本间隔不超过 `max_gap_s`。
    """
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    inside = [s for s in log if begin <= datetime.fromisoformat(s["at"]) <= finish]
    moments = [begin, *(datetime.fromisoformat(s["at"]) for s in inside), finish]
    gap = max((b - a).total_seconds() for a, b in zip(moments, moments[1:], strict=False))
    off = [s["at"] for s in inside if s["ac"] != "online"]
    return {"ok": bool(inside) and not off and gap <= max_gap_s, "samples": len(inside), "not_online": off[:5],
            "largest_gap_s": round(gap, 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True, help="JSONL file to append to")
    parser.add_argument("--every", type=float, default=5.0)
    parser.add_argument("--stop", type=Path, required=True, help="the log ends once this file exists")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    while not args.stop.exists():
        with open(args.output, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(sample()) + "\n")
        time.sleep(args.every)


if __name__ == "__main__":
    main()
