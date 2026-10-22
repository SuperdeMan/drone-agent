"""Extract the armed periods of a PX4 SITL robot from its flight logs (X1, D088); run with the image's system Python.

PX4 writes one log file per armed period by default, and in SITL its clock is the Gazebo simulation time (measured
2026-10-14), so each file's span is one armed period on the clock of the truth samples. Every file is listed with its
first and last timestamp and its vehicle status changes, so the judge can check that the span really is armed.

提取 PX4 SITL 机器人飞行日志中的解锁区间（X1，D088）；用镜像的系统 Python 运行。PX4 默认每个解锁期写一个日志文件，SITL 中其时钟
即 Gazebo 仿真时间（2026-10-14 实测），因此每个文件的跨度就是真值样本时钟上的一个解锁区间。逐个文件列出首末时间戳与载具状态
变化，供裁判核对该跨度确实处于解锁。
"""

import json
import sys
from pathlib import Path

from pyulog import ULog


def extract(directory: Path) -> dict:
    files = []
    for path in sorted(directory.rglob("*.ulg")):
        log = ULog(str(path), message_name_filter_list=["vehicle_status"])
        changes, previous = [], None
        for dataset in log.data_list:
            if dataset.name != "vehicle_status":
                continue
            data = dataset.data
            for index in range(len(data["timestamp"])):
                state = (int(data["arming_state"][index]), int(data["nav_state"][index]))
                if state != previous:
                    changes.append({"t_us": int(data["timestamp"][index]), "arming_state": state[0],
                                    "nav_state": state[1]})
                previous = state
        files.append({"name": str(path.relative_to(directory)), "bytes": path.stat().st_size,
                      "start_us": int(log.start_timestamp), "last_us": int(log.last_timestamp), "status": changes})
    return {"schema_version": "0.1.0", "format": "drone.x1-arming/v1", "files": files}


if __name__ == "__main__":
    Path(sys.argv[2]).write_text(json.dumps(extract(Path(sys.argv[1]))))
