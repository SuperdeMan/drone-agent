"""Extract independent PX4 command acknowledgements and mode transitions.

提取独立 PX4 命令确认与模式迁移。
"""

import json
import math
import sys
from pathlib import Path

from pyulog import ULog


def extract(directory):
    result = {"schema_version": "0.1.0", "commands": [], "acks": [], "modes": []}
    for path in sorted(directory.rglob("*.ulg")):
        log = ULog(str(path), message_name_filter_list=["vehicle_status", "vehicle_command", "vehicle_command_ack"])
        for dataset in log.data_list:
            data = dataset.data
            fields = {
                "vehicle_command": (
                    "commands",
                    [
                        "timestamp",
                        "command",
                        "param1",
                        "param2",
                        "param3",
                        "source_system",
                        "source_component",
                        "from_external",
                    ],
                ),
                "vehicle_command_ack": (
                    "acks",
                    ["timestamp", "command", "result", "target_system", "target_component"],
                ),
                "vehicle_status": ("modes", ["timestamp", "nav_state", "nav_state_user_intention", "arming_state"]),
            }
            if dataset.name not in fields:
                continue
            name, keys = fields[dataset.name]
            previous = None
            for index in range(len(data["timestamp"])):
                row = {key: float(data[key][index]) for key in keys if key in data}
                row = {key: value if math.isfinite(value) else None for key, value in row.items()}
                state = {key: value for key, value in row.items() if key != "timestamp"}
                if name != "modes" or state != previous:
                    result[name].append(row)
                previous = state
    return result


if __name__ == "__main__":
    value = extract(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(value, allow_nan=False))
