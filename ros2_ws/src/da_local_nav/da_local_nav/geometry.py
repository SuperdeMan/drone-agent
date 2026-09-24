"""Depth projection and the local voxel map (WP-M3-09); pure numpy, no ROS imports.

A depth pixel becomes a point in the camera's optical frame, then body FRD, then local NED through the PX4 attitude,
then map ENU. Occupied voxels live for a bounded time: the map is a short-term, onboard-only belief (D007), never
shared and never used as truth. Points near the ground or beyond the sensor range are ignored.

深度投影与局部体素地图（WP-M3-09）；纯 numpy，不导入 ROS。

深度像素先变成相机光学坐标系中的点，再到机体 FRD，经 PX4 姿态到本地 NED，最后到地图 ENU。占据体素只存活有限
时间：地图是短期、只在机载的信念（D007），从不共享，也从不当作真值。靠近地面或超出量程的点被忽略。
"""

from __future__ import annotations

import math
import time

import numpy as np


def rotation_frd_to_ned(q_wxyz) -> np.ndarray:
    w, x, y, z = q_wxyz
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def depth_to_enu(depth: np.ndarray, hfov: float, position_ned, q_wxyz, *, mount_frd=(0.12, 0.0, 0.0),
                 max_range: float = 12.0, min_range: float = 0.3, stride: int = 1) -> np.ndarray:
    """Project a depth image (metres along the optical axis) into map ENU points, shape (N, 3).

    把深度图（沿光轴的米数）投影为地图 ENU 点，形状 (N, 3)。
    """
    height, width = depth.shape
    fx = (width / 2) / math.tan(hfov / 2)
    cx, cy = (width - 1) / 2, (height - 1) / 2
    rows, cols = np.mgrid[0:height:stride, 0:width:stride]
    d = depth[::stride, ::stride]
    valid = np.isfinite(d) & (d >= min_range) & (d <= max_range)
    d, rows, cols = d[valid], rows[valid], cols[valid]
    right, down = (cols - cx) / fx * d, (rows - cy) / fx * d
    frd = np.stack([d, right, down], axis=1) + np.asarray(mount_frd)
    ned = frd @ rotation_frd_to_ned(q_wxyz).T + np.asarray(position_ned)
    return np.stack([ned[:, 1], ned[:, 0], -ned[:, 2]], axis=1)


class VoxelMap:
    def __init__(self, resolution: float = 0.25, ttl_s: float = 8.0, ground_clearance: float = 0.3):
        self.resolution, self.ttl_s, self.ground = resolution, ttl_s, ground_clearance
        self.voxels: dict[tuple[int, int, int], float] = {}
        self.seq = 0

    def integrate(self, points_enu: np.ndarray, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        points = points_enu[points_enu[:, 2] > self.ground]
        for key in {tuple(k) for k in np.floor(points / self.resolution).astype(int).tolist()}:
            self.voxels[key] = now
        self.expire(now)
        self.seq += 1

    def expire(self, now: float) -> None:
        stale = [key for key, seen in self.voxels.items() if now - seen > self.ttl_s]
        for key in stale:
            del self.voxels[key]

    def points(self) -> np.ndarray:
        if not self.voxels:
            return np.zeros((0, 3))
        return (np.array(list(self.voxels), dtype=float) + 0.5) * self.resolution

    def nearest(self, position, radius: float, limit: int) -> np.ndarray:
        """Occupied voxel centres within `radius`, nearest first, at most `limit`. / 半径内的占据体素中心，由近到远。"""
        points = self.points()
        if not len(points):
            return points
        distance = np.linalg.norm(points - np.asarray(position), axis=1)
        order = np.argsort(distance)
        order = order[distance[order] <= radius][:limit]
        return points[order]
