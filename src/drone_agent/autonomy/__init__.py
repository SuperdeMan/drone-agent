"""M3 local-autonomy boundary: plugin interfaces, the local IPC protocol and its guardian-side link (D039).

The ROS 2 nodes live in `ros2_ws/` and run on the system Python or C++; this package never imports rclpy
(D014). Everything that crosses between them is a `drone.autonomy.v1` frame on a private Unix socket and is
re-validated here by the domain models in `messages`. Autonomy nodes and learned plugins only ever submit
candidates; the guardian filters them before anything reaches the flight controller.

M3 局部自主层边界：插件接口、本地 IPC 协议及其 guardian 一侧的链路（D039）。

ROS 2 节点在 `ros2_ws/`，运行于系统 Python 或 C++；本包从不导入 rclpy（D014）。两者之间传递的一切都是
私有 Unix 套接字上的 `drone.autonomy.v1` 帧，并在这里用 `messages` 中的领域模型重新校验。自主层节点与学习型
插件只提交候选；guardian 过滤之后才可能有内容到达飞控。
"""
