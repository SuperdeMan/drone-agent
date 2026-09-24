"""Local map + short-horizon planner node (WP-M3-09): depth -> voxels -> ObstacleSet; LocalTask -> TrajectorySegment.

It sees only the depth image, the PX4 estimate and the registered map; it never sees simulator truth. It publishes the
nearby obstacle set at 5 Hz whether or not a task is active, and candidate segments at 5 Hz while the guardian's task
is active. Simulation-only faults are read from an optional file at this node's input boundary and recorded.

局部地图 + 短时域规划节点（WP-M3-09）：深度 -> 体素 -> ObstacleSet；LocalTask -> TrajectorySegment。

它只看到深度图、PX4 估计与登记地图，从不接触仿真真值。无论任务是否激活，都以 5 Hz 发布邻近障碍集合；guardian 的
任务激活期间以 5 Hz 发布候选片段。仅供仿真的故障从本节点输入边界的可选文件读取并留证。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rclpy
import yaml
from da_common.ipc import SCHEMA_VERSION, FrameClient, pb, stamp, to_datetime, vector
from da_common.px4 import topic
from px4_msgs.msg import VehicleAttitude, VehicleLocalPosition
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from da_local_nav.geometry import VoxelMap, depth_to_enu
from da_local_nav.planner import LocalPlanner

VERSION = "0.1.0"
STATUS = {"ok": pb.SEGMENT_STATUS_OK, "reached": pb.SEGMENT_STATUS_REACHED, "no_path": pb.SEGMENT_STATUS_NO_PATH,
          "overloaded": pb.SEGMENT_STATUS_OVERLOADED}


class LocalNavNode(Node):
    def __init__(self, args):
        super().__init__("da_local_nav")
        self.args = args
        scene = yaml.safe_load(Path(args.scene).read_text(encoding="utf-8"))
        self.frame = scene["frame"]
        self.budget_ms = float(scene["autonomy"]["planner_cycle_budget_ms"])
        self.overload_factor = float(scene["autonomy"]["overload_cycle_factor"])
        self.planner = LocalPlanner(priors=scene.get("obstacles", {}))
        self.map = VoxelMap()
        self.task = None
        self.pose = None
        self.attitude = None
        self.eph = 1.0
        self.pose_time = 0.0
        self.segment_seq = 0
        self.last_cycle = None
        self.fault_seen = None
        self.log = Path(args.evidence).open("a", buffering=1)
        self.link = FrameClient(args.socket, self.on_frame, name="local_nav").start()
        qos = qos_profile_sensor_data
        self.create_subscription(VehicleLocalPosition, topic("/fmu/out/vehicle_local_position", VehicleLocalPosition),
                                 self.on_position, qos)
        self.create_subscription(VehicleAttitude, topic("/fmu/out/vehicle_attitude", VehicleAttitude),
                                 self.on_attitude, qos)
        self.create_subscription(Image, args.depth_topic, self.on_depth, qos)
        self.create_timer(0.2, self.cycle)

    def record(self, **row):
        row["wall_time"] = time.time()
        self.log.write(json.dumps(row) + "\n")

    def fault(self) -> str | None:
        path = Path(self.args.fault)
        try:
            value = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, ValueError):
            value = {}
        kind = value.get("kind")
        if kind and value.get("id") != self.fault_seen:
            self.fault_seen = value.get("id")
            self.record(event="fault_injected", injection=value, boundary="autonomy_node_input")
        return kind

    def on_frame(self, frame):
        if frame.WhichOneof("body") != "local_task":
            return
        task = frame.local_task
        if task.robot_id != self.args.robot_id or task.schema_version != SCHEMA_VERSION:
            return
        self.task = task if task.active else None
        self.record(event="task", task_id=task.task_id, active=task.active)

    def on_position(self, message):
        if message.xy_valid and message.z_valid:
            self.pose = (float(message.x), float(message.y), float(message.z))
            self.eph = float(message.eph) if math.isfinite(message.eph) else 5.0
            self.pose_time = time.monotonic()

    def on_attitude(self, message):
        self.attitude = tuple(float(v) for v in message.q)

    def on_depth(self, message):
        if self.pose is None or self.attitude is None or time.monotonic() - self.pose_time > 0.5:
            return
        if message.encoding != "32FC1":
            return
        depth = np.frombuffer(message.data, dtype=np.float32).reshape(message.height, message.width)
        self.map.integrate(depth_to_enu(depth, self.args.hfov, self.pose, self.attitude))

    def position_enu(self):
        return (self.pose[1], self.pose[0], -self.pose[2])

    def cycle(self):
        now = time.monotonic()
        period_ms = (now - self.last_cycle) * 1000 if self.last_cycle else 0.0
        self.last_cycle = now
        if self.pose is None or now - self.pose_time > 0.5:
            return
        fault = self.fault()
        position = self.position_enu()
        if fault != "map_freeze":
            self.publish_obstacles(position)
        task = self.task
        if task is None or fault == "planner_freeze":
            return
        goal = (task.goal.x, task.goal.y, task.goal.z)
        if fault == "planner_stall":
            # Fresh segments that never move: the guardian must detect the stall itself (D042).
            # 新鲜但从不移动的片段：guardian 必须自行识别停滞（D042）。
            plan_status, points, compute_ms, notes = "ok", [(t, position) for t in (0.5, 1.0, 1.5)], 0.0, {}
        elif fault == "planner_ignore_obstacles":
            # A planner that aims straight at the goal through everything: only the guardian's CBF stands between it
            # and the wall. / 直接穿过一切瞄向目标的规划器：只有 guardian 的 CBF 挡在它与墙之间。
            dx, dy = goal[0] - position[0], goal[1] - position[1]
            norm = max(math.hypot(dx, dy), 1e-6)
            step = min(task.max_speed_mps, norm)
            points = [(t, (position[0] + dx / norm * step * t, position[1] + dy / norm * step * t, goal[2]))
                      for t in (0.5, 1.0, 1.5)]
            plan_status, compute_ms, notes = "ok", 0.0, {"fault": "planner_ignore_obstacles"}
        else:
            plan = self.planner.plan(position, goal, speed=task.max_speed_mps, tolerance=task.goal_tolerance_m,
                                     scope_low=(task.scope_min.x, task.scope_min.y, task.scope_min.z),
                                     scope_high=(task.scope_max.x, task.scope_max.y, task.scope_max.z),
                                     points=self.map.points())
            plan_status, points, compute_ms, notes = plan.status, plan.points, plan.cycle_ms, plan.notes
        cycle_ms = max(compute_ms, period_ms)
        if cycle_ms > self.budget_ms * self.overload_factor:
            plan_status = "overloaded" if plan_status == "ok" else plan_status
        self.publish_segment(task, points, plan_status, cycle_ms)
        self.record(event="segment", seq=self.segment_seq - 1, status=plan_status, cycle_ms=round(cycle_ms, 2),
                    compute_ms=round(compute_ms, 2), period_ms=round(period_ms, 2), position=position,
                    target=points[1][1] if len(points) > 1 else points[0][1], voxels=len(self.map.voxels),
                    notes=notes)

    def publish_obstacles(self, position):
        frame = pb.AutonomyFrame()
        body = frame.obstacle_set
        now = datetime.now(timezone.utc)
        body.schema_version = SCHEMA_VERSION
        body.robot_id, body.frame_id, body.map_version = self.args.robot_id, self.frame["frame_id"], self.frame["map_version"]
        stamp(body.stamp, now)
        stamp(body.valid_until, now + timedelta(milliseconds=500))
        body.source_id, body.source_version = "da_local_nav.voxel_map", VERSION
        for point in self.map.nearest(position, 12.0, 256):
            vector(body.points.add(), point)
        body.point_radius_m = self.map.resolution * math.sqrt(3) / 2
        # 1-sigma: depth noise, estimator error and voxel quantization combined. / 1σ：深度噪声、估计误差与体素量化合成。
        body.position_std_m = math.sqrt(0.05 ** 2 + self.eph ** 2 + (self.map.resolution / math.sqrt(12)) ** 2)
        body.confidence, body.map_seq, body.coverage_radius_m = 0.9, self.map.seq, 12.0
        self.link.send(frame)

    def publish_segment(self, task, points, status, cycle_ms):
        frame = pb.AutonomyFrame()
        body = frame.trajectory_segment
        now = datetime.now(timezone.utc)
        body.schema_version = SCHEMA_VERSION
        body.robot_id, body.task_id, body.lease_epoch = self.args.robot_id, task.task_id, task.lease_epoch
        body.segment_seq = self.segment_seq
        self.segment_seq += 1
        body.source_id, body.source_version = "da_local_nav.astar", VERSION
        body.implementation = pb.IMPLEMENTATION_DETERMINISTIC
        stamp(body.created_at, now)
        stamp(body.valid_until, now + timedelta(milliseconds=400))
        body.frame_id, body.map_version = self.frame["frame_id"], self.frame["map_version"]
        for offset, point in points:
            item = body.points.add()
            item.schema_version = SCHEMA_VERSION
            item.t_offset_s = offset
            vector(item.position, point)
        body.max_speed_mps = task.max_speed_mps
        body.status = STATUS[status]
        body.cycle_ms = cycle_ms
        deadline = to_datetime(task.deadline)
        if now > deadline:
            body.status = pb.SEGMENT_STATUS_NO_PATH
        self.link.send(frame)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/autonomy/autonomy.sock")
    parser.add_argument("--robot-id", default="uav_01")
    parser.add_argument("--scene", default="/workspace/configs/scenarios/m3_campus_v3.yaml")
    parser.add_argument("--depth-topic", default="/uav_01/depth_0/image")
    parser.add_argument("--hfov", type=float, default=1.5)
    parser.add_argument("--fault", default="/fault/autonomy.json")
    parser.add_argument("--evidence", default="/artifacts/local_nav.jsonl")
    args, ros_args = parser.parse_known_args()
    rclpy.init(args=ros_args)
    node = LocalNavNode(args)
    try:
        rclpy.spin(node)
    finally:
        node.link.close()
        node.log.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
