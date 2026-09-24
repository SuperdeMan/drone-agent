"""Perception node (WP-M3-07): downward RGB -> colour-signature detections -> BeliefFact to the executive.

Each detection becomes a belief WorldFact with a frame, map version, 3x3 covariance, confidence and a two-second
validity; unmatched detections keep an "unknown:" subject and so stay candidates. At most one fact per subject per
second is sent. The node never sees simulator truth.

感知节点（WP-M3-07）：下视 RGB -> 颜色特征检测 -> 发给 executive 的 BeliefFact。

每个检测成为一条 belief WorldFact，带坐标系、地图版本、3x3 协方差、置信度与两秒有效期；未匹配的检测以 "unknown:"
为主语，保持候选身份。每个主语每秒最多发送一条。本节点从不接触仿真真值。
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rclpy
import yaml
from da_common.ipc import SCHEMA_VERSION, FrameClient, pb
from da_common.px4 import topic
from px4_msgs.msg import VehicleAttitude, VehicleLocalPosition
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from da_perception.detector import detect, match_asset

VERSION = "da_perception.color/0.1.0"


class PerceptionNode(Node):
    def __init__(self, args):
        super().__init__("da_perception")
        self.args = args
        scene = yaml.safe_load(Path(args.scene).read_text(encoding="utf-8"))
        self.frame, self.assets = scene["frame"], scene["assets"]
        self.pose = self.attitude = None
        self.eph, self.pose_time = 1.0, 0.0
        self.last_sent: dict[str, float] = {}
        self.log = Path(args.evidence).open("a", buffering=1)
        self.link = FrameClient(args.socket, name="perception").start()
        qos = qos_profile_sensor_data
        self.create_subscription(VehicleLocalPosition, topic("/fmu/out/vehicle_local_position", VehicleLocalPosition),
                                 self.on_position, qos)
        self.create_subscription(VehicleAttitude, topic("/fmu/out/vehicle_attitude", VehicleAttitude),
                                 self.on_attitude, qos)
        self.create_subscription(Image, args.rgb_topic, self.on_image, qos)

    def on_position(self, message):
        if message.xy_valid and message.z_valid:
            self.pose = (float(message.x), float(message.y), float(message.z))
            self.eph, self.pose_time = float(message.eph), time.monotonic()

    def on_attitude(self, message):
        self.attitude = tuple(float(v) for v in message.q)

    def on_image(self, message):
        received = time.monotonic()
        if self.pose is None or self.attitude is None or received - self.pose_time > 0.5 or message.encoding != "rgb8":
            return
        rgb = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.width, 3)
        for detection in detect(rgb, hfov=self.args.hfov, position_ned=self.pose, q_wxyz=self.attitude,
                                eph_m=self.eph):
            asset = match_asset(detection, self.assets)
            subject = asset or f"unknown:{detection.signature}"
            if received - self.last_sent.get(subject, 0.0) < 1.0:
                continue
            self.last_sent[subject] = received
            self.send(subject, detection, (time.monotonic() - received) * 1000)

    def send(self, subject, detection, latency_ms):
        now = datetime.now(timezone.utc)
        variance = detection.sigma_m ** 2
        fact = {
            "fact_id": uuid.uuid4().hex, "subject": subject, "predicate": "observed_at",
            "value": {"signature": detection.signature, "pixel_fraction": round(detection.fraction, 4)},
            "frame": {"frame_id": self.frame["frame_id"], "map_version": self.frame["map_version"]},
            "position": {"x": detection.position_enu[0], "y": detection.position_enu[1], "z": 0.0,
                         "covariance": [variance, 0, 0, 0, variance, 0, 0, 0, variance]},
            "timestamp": now.isoformat(), "valid_until": (now + timedelta(seconds=2)).isoformat(),
            "source": "sensor", "source_version": VERSION,
            "confidence": round(min(0.99, 0.5 + 5 * detection.fraction), 3), "world_kind": "belief",
        }
        frame = pb.AutonomyFrame()
        body = frame.belief_fact
        body.schema_version = SCHEMA_VERSION
        body.producer_id = "da_perception"
        body.fact = json.dumps(fact).encode()
        body.latency_ms = latency_ms
        body.replan_trigger = False
        sent = self.link.send(frame)
        self.log.write(json.dumps({"wall_time": time.time(), "subject": subject, "sent": sent,
                                   "sigma_m": round(detection.sigma_m, 3), "position": detection.position_enu}) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/belief/belief.sock")
    parser.add_argument("--scene", default="/workspace/configs/scenarios/m3_campus_v3.yaml")
    parser.add_argument("--rgb-topic", default="/uav_01/cam_0/image")
    parser.add_argument("--hfov", type=float, default=1.4)
    parser.add_argument("--evidence", default="/artifacts/perception.jsonl")
    args, ros_args = parser.parse_known_args()
    rclpy.init(args=ros_args)
    node = PerceptionNode(args)
    try:
        rclpy.spin(node)
    finally:
        node.link.close()
        node.log.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
