# ros2_ws — M3 局部自主层节点

[架构总览](../docs/architecture/00-overview.md) · [D039 出口节点与本地协议](../docs/decisions.md) · [M3 实施计划](../docs/m3-implementation.md)

这里是在机载侧运行的 ROS 2 Jazzy 节点。它们运行在系统 Python 3.12（rclpy）或 C++ 上，**不导入 `drone_agent` 包**，与 guardian / executive 之间只通过 `proto/drone/autonomy/v1/autonomy.proto` 定义的私有 Unix 套接字帧交互（D014、D039）。

| 包 | 语言 | 职责 | 连接 |
|---|---|---|---|
| `da_egress_ext` | C++（`px4_ros2_cpp` release/1.17） | guardian 控制出口的物理延伸：注册外部模式 `DroneAgent Local`，只转发 TTL 内的 guardian 授权；授权过期即停并请求 PX4 Hold，1 s 后报告不可运行 | guardian 出口套接字；uXRCE-DDS |
| `da_localization` | Python | PX4 估计器输出 → `LocalizationReport`（GNSS / 视觉 / 本地位置健康） | guardian 自主层套接字 |
| `da_local_nav` | Python（numpy） | 深度 → 体素地图 → 邻近障碍集合；局部任务 → A* 短时域候选片段（包含计划中的局部地图与局部规划两部分） | guardian 自主层套接字 |
| `da_perception` | Python（numpy） | 下视 RGB 颜色特征检测 → 带协方差的 `BeliefFact` | executive 信念套接字 |
| `da_edge_inference` | Python（numpy + ONNX Runtime） | 下视 RGB → CLIP 零样本场景类别 → 模型来源的候选 `BeliefFact`（最多 1 Hz、丢帧不排队；提示嵌入在镜像构建时计算，D044） | executive 信念套接字 |
| `da_common` | Python | 帧客户端、PX4 话题版本与坐标转换 | — |

构建：`da_egress_ext` 与 `px4_msgs`、`px4_ros2_cpp` 一起用 colcon 在 `sim/m3.Dockerfile` 中构建，proto 用系统 `protoc` 生成。Python 节点是普通模块，由 `ros2_ws/run_node.sh` 以 `python3 -m <包>.node` 启动。纯逻辑（`health.py`、`geometry.py`、`planner.py`、`detector.py`、`clip.py`）不导入 ROS，由仓库 `tests/ros2_ws/` 在 uv 环境中测试。

纪律：节点只提交候选与报告，从不直接写飞控（出口节点除外，且只转发 guardian 授权）；不订阅 Gazebo 话题、不读取真值；仿真专用故障只从 `/fault/autonomy.json` 读取并记录在各自的证据日志中。
