"""PX4 conventions shared by the Python nodes: versioned topic names and NED/FRD to map ENU/FLU conversion.

Python 节点共用的 PX4 约定：带版本的话题名，以及 NED/FRD 到地图 ENU/FLU 的转换。
"""

from __future__ import annotations

import math


def topic(base: str, message_type) -> str:
    """PX4 message versioning: version 0 has no suffix, version n adds `_vn`. / 版本 0 无后缀，版本 n 加 `_vn`。"""
    version = int(getattr(message_type, "MESSAGE_VERSION", 0))
    return base + (f"_v{version}" if version else "")


def ned_to_enu(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (y, x, -z)


def enu_to_ned(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (y, x, -z)


def rotate(q_wxyz, v) -> tuple[float, float, float]:
    """Rotate vector `v` by the unit quaternion (w, x, y, z). / 用单位四元数 (w, x, y, z) 旋转向量 `v`。"""
    w, x, y, z = q_wxyz
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v' = v + w * t + cross(q.xyz, t)
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def frd_to_ned(q_wxyz, v_frd) -> tuple[float, float, float]:
    """PX4 attitude q rotates body FRD into local NED. / PX4 姿态 q 把机体 FRD 旋转到本地 NED。"""
    return rotate(q_wxyz, v_frd)


def yaw_from_quaternion(q_wxyz) -> float:
    w, x, y, z = q_wxyz
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
