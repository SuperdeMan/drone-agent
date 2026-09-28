"""Gazebo sensor relay and independent truth collector; never connect to MAVLink.

Gazebo 传感器转接与独立真值采集；永不连接 MAVLink。
"""

import base64
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from gz.msgs10.image_pb2 import Image
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node, SubscribeOptions

sensor, truth = Path("/sensor"), Path("/truth")
# The Gazebo entity to record; M3 flies PX4's x500_vision (D041). / 要记录的 Gazebo 实体；M3 飞 PX4 的 x500_vision（D041）。
MODEL = os.environ.get("DRONE_TRUTH_MODEL", "x500_0")
# The camera to relay; P3 gives each instance its own scoped topic (D061). / 要转接的相机；P3 每个实例有自己的限定话题（D061）。
CAMERA = os.environ.get("DRONE_CAMERA_TOPIC", "/drone/cam_0/image")
sensor.mkdir(exist_ok=True)
truth.mkdir(exist_ok=True)
node = Node()
lock = threading.Lock()
output = (truth / "truth.jsonl").open("a", buffering=1)
stopped = threading.Event()


def timestamp(header):
    return header.stamp.sec + header.stamp.nsec / 1e9


def image_callback(msg):
    if stopped.is_set():
        return
    pixel_format = msg.DESCRIPTOR.fields_by_name["pixel_format_type"].enum_type.values_by_number[msg.pixel_format_type].name
    if pixel_format != "RGB_INT8" or msg.step != msg.width * 3:
        return
    value = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sim_time": timestamp(msg.header),
        "width": msg.width,
        "height": msg.height,
        "rgb": base64.b64encode(msg.data).decode(),
    }
    temporary = sensor / "pending.json"
    temporary.write_text(json.dumps(value))
    temporary.replace(sensor / "latest.json")


def pose_callback(msg):
    for pose in msg.pose:
        if pose.name == MODEL:
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sim_time": timestamp(msg.header),
                "position": [pose.position.x, pose.position.y, pose.position.z],
                "orientation": [pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w],
            }
            with lock:
                if not output.closed:
                    output.write(json.dumps(row) + "\n")


opts = SubscribeOptions()
opts.msgs_per_sec = 20
assert node.subscribe(Pose_V, "/world/default/pose/info", pose_callback, opts)
assert node.subscribe(Image, CAMERA, image_callback)
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stopped.set())
while not stopped.wait(0.2):
    time.monotonic()
with lock:
    output.close()
# gz-transport keeps calling back from its own threads; a thread that takes the GIL during interpreter
# finalization is exited by CPython 3.12, which aborts through the C++ frames (SIGABRT and a core dump on the host).
# The truth file is closed, so leave without finalization.
# gz-transport 的线程会继续回调；解释器收尾期间取 GIL 的线程会被 CPython 3.12 强制退出，穿过 C++ 帧时触发 abort
# （SIGABRT，宿主机留下 core）。真值文件已关闭，因此跳过解释器收尾直接退出。
os._exit(0)
