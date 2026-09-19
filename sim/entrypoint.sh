#!/usr/bin/env bash
# Isolated, unarmed M0 simulation; preserve PX4 native defaults.
# 隔离且不解锁的 M0 仿真；保留 PX4 原生默认配置。
set -euo pipefail
cd /opt/PX4-Autopilot
test "$(git rev-parse HEAD)" = d6f12ad1c4f70ad3230afd7d86e971421e02fef4
exec make px4_sitl gz_x500
