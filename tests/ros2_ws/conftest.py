"""Put the ROS 2 node packages' pure modules on the path; nothing here imports rclpy or px4_msgs.

把 ROS 2 节点包的纯逻辑模块加入路径；这里不导入 rclpy 或 px4_msgs。
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ros2_ws" / "src"
for package in ("da_common", "da_localization", "da_local_nav", "da_perception"):
    path = str(SRC / package)
    if path not in sys.path:
        sys.path.insert(0, path)
