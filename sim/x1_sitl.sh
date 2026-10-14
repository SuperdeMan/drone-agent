#!/usr/bin/env bash
# X1 air-ground SITL (D088): one headless Gazebo world with PX4 instance 0 (the x500 uav_01, pad at the origin) and
# instance 1 (the differential rover ugv_01 at its parking spot, 2 m east and 4 m south of the origin, facing north),
# standalone on the same world. Each instance sends its API/offboard link to its guardian's fixed address
# (DRONE_X1_PARTNER_0 / _1) as MAVLink system 1 and logs its console to /sitl-logs/px4-<i>.log. The container stops
# when either instance exits. PX4 native failsafes are untouched.
#
# X1 空地 SITL（D088）：一个无界面 Gazebo 世界，PX4 实例 0（x500 uav_01，机位在原点）与实例 1（差速地面车 ugv_01，位于其
# 停车位：原点以东 2 m、以南 4 m，朝北），后者以 standalone 方式接入同一世界。每个实例以 MAVLink 系统 1 把 API / offboard
# 链路发往其 guardian 的固定地址（DRONE_X1_PARTNER_0 / _1），控制台写入 /sitl-logs/px4-<i>.log。任一实例退出时容器停止。
# PX4 原生失效保护不变。
set -euo pipefail
cd /opt/PX4-Autopilot
test "$(git rev-parse HEAD)" = d6f12ad1c4f70ad3230afd7d86e971421e02fef4
: "${DRONE_X1_PARTNER_0:?}" "${DRONE_X1_PARTNER_1:?}"
BUILD=/opt/PX4-Autopilot/build/px4_sitl_default
export HEADLESS=1 PX4_GZ_WORLD=default PX4_SIM_SPEED_FACTOR="${PX4_SIM_SPEED_FACTOR:-1}"
export PX4_SYS_ID_OVERRIDE=1
mkdir -p /sitl-logs "$BUILD/rootfs/0" "$BUILD/rootfs/1"
set +u
. "$BUILD/rootfs/gz_env.sh"
set -u

PIDS=()
start() {
    local instance=$1 model=$2 pose=$3 partner=$4 standalone=$5
    (
        cd "$BUILD/rootfs/$instance"
        if [ "$standalone" = yes ]; then export PX4_GZ_STANDALONE=1; fi
        PX4_SIM_MODEL="$model" PX4_GZ_MODEL_POSE="$pose" PX4_OFFBOARD_PARTNER="$partner" \
            exec "$BUILD/bin/px4" -i "$instance" -d
    ) > "/sitl-logs/px4-$instance.log" 2>&1 &
    PIDS+=("$!")
}

start 0 gz_x500 "0,0,0,0,0,0" "$DRONE_X1_PARTNER_0" no
for _ in $(seq 1 120); do
    if grep -q "Spawning Gazebo model" /sitl-logs/px4-0.log 2>/dev/null; then
        break
    fi
    sleep 1
done
sleep 3
start 1 gz_rover_differential "2,-4,0,0,0,1.5707963" "$DRONE_X1_PARTNER_1" yes
echo "x1 sitl: instance pids ${PIDS[*]}"
wait -n "${PIDS[@]}"
echo "x1 sitl: an instance exited" >&2
exit 1
