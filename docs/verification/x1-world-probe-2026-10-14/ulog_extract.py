"""Arming transitions, navigation states and the local position track of a PX4 flight log (X1 world probe).

PX4 飞行日志中的解锁迁移、导航状态与本地位置轨迹（X1 世界探针）。
"""

import json
import math
import sys
from pathlib import Path

from pyulog import ULog


def extract(directory: Path) -> dict:
    found = {"files": [], "status": [], "position": []}
    for path in sorted(directory.rglob("*.ulg")):
        log = ULog(str(path), message_name_filter_list=["vehicle_status", "vehicle_local_position"])
        found["files"].append({"name": path.name, "bytes": path.stat().st_size, "start_us": log.start_timestamp,
                               "last_us": log.last_timestamp,
                               "params": {k: log.initial_parameters.get(k) for k in ("SDLOG_MODE", "SIM_BAT_DRAIN")}})
        for dataset in log.data_list:
            data = dataset.data
            if dataset.name == "vehicle_status":
                previous = None
                for i in range(len(data["timestamp"])):
                    state = (int(data["arming_state"][i]), int(data["nav_state"][i]))
                    if state != previous:
                        found["status"].append({"t_us": int(data["timestamp"][i]), "arming_state": state[0],
                                                "nav_state": state[1]})
                    previous = state
            elif dataset.name == "vehicle_local_position":
                last = -1e18
                for i in range(len(data["timestamp"])):
                    t = int(data["timestamp"][i])
                    if t - last >= 200000:
                        x, y = float(data["x"][i]), float(data["y"][i])
                        if math.isfinite(x) and math.isfinite(y):
                            found["position"].append({"t_us": t, "x": round(x, 3), "y": round(y, 3)})
                            last = t
    return found


if __name__ == "__main__":
    Path(sys.argv[2]).write_text(json.dumps(extract(Path(sys.argv[1]))))
