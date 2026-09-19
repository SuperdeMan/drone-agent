"""Gazebo sensor relay and independent truth collector; never connect to MAVLink.

Gazebo 传感器转接与独立真值采集；永不连接 MAVLink。
"""

import base64
import json
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from gz.msgs10.image_pb2 import Image
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node, SubscribeOptions

sensor, truth = Path("/sensor"), Path("/truth")
sensor.mkdir(exist_ok=True)
truth.mkdir(exist_ok=True)
node = Node()
lock = threading.Lock()
output = (truth / "truth.jsonl").open("a", buffering=1)
stopped = threading.Event()


def timestamp(header):
    return header.stamp.sec + header.stamp.nsec / 1e9


def image_callback(msg):
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
        if pose.name == "x500_0":
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sim_time": timestamp(msg.header),
                "position": [pose.position.x, pose.position.y, pose.position.z],
                "orientation": [pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w],
            }
            with lock:
                output.write(json.dumps(row) + "\n")


opts = SubscribeOptions()
opts.msgs_per_sec = 20
assert node.subscribe(Pose_V, "/world/default/pose/info", pose_callback, opts)
assert node.subscribe(Image, "/drone/cam_0/image", image_callback)
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stopped.set())
while not stopped.wait(0.2):
    time.monotonic()
output.close()
