# X1 air-ground simulation image (D088). Built on the aircraft stage of sim/m1.Dockerfile (the pinned PX4 v1.17.0
# SITL build plus the checked source), so the M1, M2 and P3 simulation images and worlds stay exactly as recorded. The
# setup adds the aircraft's downward camera, the P3 markers, the building, the rover's forward camera and battery
# drain, and the per-instance API link partner; the entrypoint starts the x500 and the differential rover in one
# headless Gazebo world.
#
# X1 空地仿真镜像（D088）。基于 sim/m1.Dockerfile 的 aircraft 阶段（固定的 PX4 v1.17.0 SITL 构建加已校验源码），因此 M1、
# M2 与 P3 的仿真镜像与世界保持原样。设置加入飞行器下视相机、P3 标记、楼、地面车前视相机与放电时间，以及按实例的 API 链路
# 对端；入口在一个无界面 Gazebo 世界中启动 x500 与差速地面车。
ARG AIRCRAFT_IMAGE=drone-agent-m1-aircraft:required-explicit-revision
FROM ${AIRCRAFT_IMAGE}
# bc serves PX4's accelerated-clock startup script, as in sim/p3.Dockerfile; the package index goes in the same
# step, so no layer keeps it. / bc 供 PX4 加速时钟启动脚本使用，与 sim/p3.Dockerfile 相同；软件包索引在同一步删除，
# 不留在任何层中。
RUN no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get \
    -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources -o Dir::Etc::sourceparts=- \
    -o Acquire::Retries=1 -o Acquire::https::Timeout=20 update \
    && no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get install -y --no-install-recommends bc \
    && rm -rf /var/lib/apt/lists/*
RUN /usr/bin/python3 /workspace/sim/x1_setup.py
LABEL org.drone-agent.role=sim-x1
ENV HEADLESS=1
ENV PX4_SIM_SPEED_FACTOR=1
ENV PX4_GZ_HEADLESS_RENDERING=1
ENTRYPOINT ["bash", "/workspace/sim/x1_sitl.sh"]
