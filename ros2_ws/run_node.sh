#!/usr/bin/env bash
# Start one drone-agent ROS 2 node on the system Python with only the ROS, px4_msgs and generated-proto paths; the
# drone_agent package and its venv stubs are deliberately not on the path (D014).
# 以系统 Python 启动一个 drone-agent ROS 2 节点，只带 ROS、px4_msgs 与生成 proto 的路径；drone_agent 包及其 venv
# 生成代码刻意不在路径上（D014）。
set -eo pipefail
unset PYTHONPATH VIRTUAL_ENV
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
source /opt/ros/jazzy/setup.bash
source /opt/px4_ros2_ws/install/setup.bash
if [ -f /opt/da_ros2/install/setup.bash ]; then
  source /opt/da_ros2/install/setup.bash
fi
set -u
node="$1"
shift
case "$node" in
  egress) exec /opt/da_ros2/install/da_egress_ext/lib/da_egress_ext/egress_node --ros-args "$@" ;;
  localization|local_nav|perception) ;;
  *) echo "unknown node $node" >&2; exit 2 ;;
esac
src=/workspace/ros2_ws/src
export PYTHONPATH="/opt/da_ros2/gen:${src}/da_common:${src}/da_localization:${src}/da_local_nav:${src}/da_perception:${PYTHONPATH}"
exec /usr/bin/python3 -m "da_${node}.node" "$@"
