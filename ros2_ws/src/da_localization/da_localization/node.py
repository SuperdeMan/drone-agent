"""Localization-health node: PX4 estimator outputs -> LocalizationReport to the guardian at 5 Hz (WP-M3-08).

定位健康节点：PX4 估计器输出 -> 以 5 Hz 向 guardian 发送 LocalizationReport（WP-M3-08）。
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

import rclpy
from da_common.ipc import SCHEMA_VERSION, FrameClient, pb, stamp
from da_common.px4 import topic
from px4_msgs.msg import EstimatorStatusFlags, SensorGps, VehicleLocalPosition, VehicleStatus
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data

from da_localization.health import Inputs, assess

VERSION = "0.1.0"


class LocalizationNode(Node):
    def __init__(self, args):
        super().__init__("da_localization")
        self.args, self.inputs = args, Inputs()
        self.link = FrameClient(args.socket, name="localization").start()
        qos = qos_profile_sensor_data
        self.create_subscription(SensorGps, topic("/fmu/out/vehicle_gps_position", SensorGps), self.gps, qos)
        self.create_subscription(EstimatorStatusFlags, topic("/fmu/out/estimator_status_flags", EstimatorStatusFlags),
                                 self.flags, qos)
        self.create_subscription(VehicleLocalPosition, topic("/fmu/out/vehicle_local_position", VehicleLocalPosition),
                                 self.local, qos)
        self.create_subscription(VehicleStatus, topic("/fmu/out/vehicle_status", VehicleStatus), self.status, qos)
        self.create_timer(0.2, self.report)

    def gps(self, message):
        self.inputs.gps_fix_type, self.inputs.gps_satellites = int(message.fix_type), int(message.satellites_used)
        self.inputs.gps_eph_m, self.inputs.gps_time = float(message.eph), time.monotonic()

    def flags(self, message):
        self.inputs.fusing_gps, self.inputs.fusing_ev_pos = bool(message.cs_gnss_pos), bool(message.cs_ev_pos)
        self.inputs.flags_time = time.monotonic()

    def local(self, message):
        self.inputs.xy_valid, self.inputs.z_valid = bool(message.xy_valid), bool(message.z_valid)
        self.inputs.eph_m, self.inputs.local_time = float(message.eph), time.monotonic()

    def status(self, message):
        self.inputs.status_time = time.monotonic()

    def report(self):
        result = assess(self.inputs, time.monotonic())
        frame = pb.AutonomyFrame()
        body = frame.localization_report
        now = datetime.now(timezone.utc)
        body.schema_version = SCHEMA_VERSION
        body.robot_id = self.args.robot_id
        stamp(body.stamp, now)
        stamp(body.valid_until, now + timedelta(milliseconds=500))
        body.gnss_ok, body.gnss_fix_type, body.gnss_satellites = result.gnss_ok, result.gnss_fix_type, result.gnss_satellites
        if result.gnss_eph_m is not None:
            body.gnss_eph_m = result.gnss_eph_m
        body.visual_ok, body.ev_position_fused = result.visual_ok, result.ev_position_fused
        body.gnss_position_fused, body.local_position_ok = result.gnss_position_fused, result.local_position_ok
        if result.position_std_m is not None:
            body.position_std_m = result.position_std_m
        body.source_version = VERSION
        body.px4_status_age_s = min(result.px4_status_age_s, 1e6)
        self.link.send(frame)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/autonomy/autonomy.sock")
    parser.add_argument("--robot-id", default="uav_01")
    args, ros_args = parser.parse_known_args()
    rclpy.init(args=ros_args)
    node = LocalizationNode(args)
    try:
        rclpy.spin(node)
    finally:
        node.link.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
