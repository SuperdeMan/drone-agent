#!/usr/bin/env bash
# P3 two-aircraft SITL (D061): one headless Gazebo world with PX4 instance 0 (uav_01, pad at the origin) and instance 1
# (uav_02, pad 12 m east, standalone on the same world). Each instance sends its API/offboard link to its guardian's
# fixed address (DRONE_P3_PARTNER_0 / _1) as MAVLink system 1 and logs its console to /sitl-logs/px4-<i>.log; the ULog
# files land in rootfs/<i>/log. The container stops when either instance exits. PX4 native failsafes are untouched.
#
# P3 双机 SITL（D061）：一个无界面 Gazebo 世界，PX4 实例 0（uav_01，机位在原点）与实例 1（uav_02，机位在东 12 m，以
# standalone 方式接入同一世界）。每个实例以 MAVLink 系统 1 把 API / offboard 链路发往其 guardian 的固定地址
# （DRONE_P3_PARTNER_0 / _1），控制台写入 /sitl-logs/px4-<i>.log；ULog 位于 rootfs/<i>/log。任一实例退出时容器停止。PX4 原生
# 失效保护不变。
set -euo pipefail
cd /opt/PX4-Autopilot
test "$(git rev-parse HEAD)" = d6f12ad1c4f70ad3230afd7d86e971421e02fef4
: "${DRONE_P3_PARTNER_0:?}" "${DRONE_P3_PARTNER_1:?}"
BUILD=/opt/PX4-Autopilot/build/px4_sitl_default
export HEADLESS=1 PX4_GZ_WORLD=default PX4_SIM_MODEL=gz_x500 PX4_SIM_SPEED_FACTOR="${PX4_SIM_SPEED_FACTOR:-1}"
# Every aircraft is system 1 on its own link, as the onboard adapter expects (sim/p3_setup.py).
# 每架飞行器在自己的链路上都是系统 1，与机载适配器的预期一致（sim/p3_setup.py）。
export PX4_SYS_ID_OVERRIDE=1
mkdir -p /sitl-logs "$BUILD/rootfs/0" "$BUILD/rootfs/1"
# PX4 loads gz_env.sh only when it starts the world itself, so the standalone second instance would spawn its model
# from an empty model path; both instances get the same model, world and plugin paths here.
# PX4 只在自己启动世界时加载 gz_env.sh，standalone 的第二个实例会从空的模型路径生成模型；此处给两个实例相同的模型、
# 世界与插件路径。
set +u
. "$BUILD/rootfs/gz_env.sh"
set -u

PIDS=()
start() {
    local instance=$1 pose=$2 partner=$3 standalone=$4
    (
        cd "$BUILD/rootfs/$instance"
        if [ "$standalone" = yes ]; then export PX4_GZ_STANDALONE=1; fi
        PX4_GZ_MODEL_POSE="$pose" PX4_OFFBOARD_PARTNER="$partner" exec "$BUILD/bin/px4" -i "$instance" -d
    ) > "/sitl-logs/px4-$instance.log" 2>&1 &
    PIDS+=("$!")
}

start 0 "0,0,0,0,0,0" "$DRONE_P3_PARTNER_0" no
for _ in $(seq 1 120); do
    if grep -q "Spawning Gazebo model" /sitl-logs/px4-0.log 2>/dev/null; then
        break
    fi
    sleep 1
done
sleep 3
start 1 "12,0,0,0,0,0" "$DRONE_P3_PARTNER_1" yes
echo "p3 sitl: instance pids ${PIDS[*]}"
wait -n "${PIDS[@]}"
echo "p3 sitl: an instance exited" >&2
exit 1
