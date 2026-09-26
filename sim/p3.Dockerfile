# P3 two-aircraft simulation image (D061). Built on the aircraft stage of sim/m1.Dockerfile (the pinned PX4 v1.17.0
# SITL build plus the checked source), so the M1 and M2 simulation images and worlds stay exactly as recorded. The
# setup adds the per-instance camera, the three P3 markers and the per-instance API link partner; the entrypoint starts
# both PX4 instances in one headless Gazebo world.
#
# P3 双机仿真镜像（D061）。基于 sim/m1.Dockerfile 的 aircraft 阶段（固定的 PX4 v1.17.0 SITL 构建加已校验源码），因此 M1 与
# M2 的仿真镜像与世界保持原样。设置加入按实例的相机、三个 P3 标记与按实例的 API 链路对端；入口在一个无界面 Gazebo 世界中
# 启动两个 PX4 实例。
ARG AIRCRAFT_IMAGE=drone-agent-m1-aircraft:required-explicit-revision
FROM ${AIRCRAFT_IMAGE}
RUN no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get \
    -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources -o Dir::Etc::sourceparts=- \
    -o Acquire::Retries=1 -o Acquire::https::Timeout=20 update \
    && no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get install -y --no-install-recommends bc
RUN /usr/bin/python3 /workspace/sim/p3_setup.py
LABEL org.drone-agent.role=sim-p3
ENV HEADLESS=1
ENV PX4_SIM_SPEED_FACTOR=1
ENV PX4_GZ_HEADLESS_RENDERING=1
ENTRYPOINT ["bash", "/workspace/sim/p3_sitl.sh"]
