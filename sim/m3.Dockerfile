# M3 images (D038, D039, D041). Built on the cloud host by scripts/remote_m3.py for one deployed revision.
#   ros2base  — pinned px4_msgs (packaging from PX4/px4_msgs, messages from the pinned PX4 v1.17.0 source) and
#               px4_ros2_cpp release/1.17; depends only on pinned inputs, so Docker reuses it across revisions.
#   aircraft3 — the M1/M2 aircraft runtime (venv + source of the revision) plus the egress node and the ROS-side
#               protobuf bindings of drone.autonomy.v1 generated with the system protoc.
#   sim3      — the M2 simulation image of the same revision plus the M3 world, depth camera and sim airframe.
# arm64 (D038): pass BASE_IMAGE for a JetPack 6 / ROS 2 Jazzy base; not built or verified in M3-SITL.
#
# M3 镜像（D038、D039、D041），由 scripts/remote_m3.py 在云主机上为某个已部署版本构建。
#   ros2base  — 固定的 px4_msgs（打包文件来自 PX4/px4_msgs，消息来自固定的 PX4 v1.17.0 源码）与 px4_ros2_cpp
#               release/1.17；只依赖固定输入，Docker 可跨版本复用。
#   aircraft3 — M1/M2 机载运行时（该版本的 venv 与源码）加出口节点，以及用系统 protoc 生成的 drone.autonomy.v1 ROS 侧绑定。
#   sim3      — 同一版本的 M2 仿真镜像加 M3 世界、深度相机与仿真机架。
# arm64（D038）：传入 JetPack 6 / ROS 2 Jazzy 的 BASE_IMAGE；M3-SITL 中未构建也未验证。
ARG BASE_IMAGE=drone-agent-sitl:px4-1.17.0-m0
ARG CHECKS_IMAGE=drone-agent-checks:required-explicit-revision
ARG SIM2_IMAGE=drone-agent-m2-sim:required-explicit-revision

FROM ${CHECKS_IMAGE} AS checks

FROM ${BASE_IMAGE} AS ros2base
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources \
    -o Dir::Etc::sourceparts=- -o Acquire::Retries=2 update \
 && no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources \
    -o Dir::Etc::sourceparts=- install -y --no-install-recommends libprotobuf-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/px4_ros2_ws/src
# Pinned in configs/platforms/px4_sitl_multirotor.yaml; a changed archive digest fails the build.
# 固定于 configs/platforms/px4_sitl_multirotor.yaml；归档摘要变化即构建失败。
ARG PX4_MSGS_COMMIT=86d8239e962f6939e05c3737784f60c02fa884db
ARG PX4_MSGS_SHA256=36d2f2ea48f5a367976acb070ee9da0ef4cd18e9e210ed91a4bafc23ad8893e6
ARG PX4_ROS2_COMMIT=4a3370f084ac6f1ef001a4afa2b007845ffd0837
ARG PX4_ROS2_SHA256=8cf06afdef7ef6e83c24c179728b9d78a7b4b6117ef317f2828f0b165f58d54a
RUN test "$(git -C /opt/PX4-Autopilot rev-parse HEAD)" = d6f12ad1c4f70ad3230afd7d86e971421e02fef4 \
 && curl -fsSL -m 300 -o /tmp/px4_msgs.tgz https://codeload.github.com/PX4/px4_msgs/tar.gz/${PX4_MSGS_COMMIT} \
 && echo "${PX4_MSGS_SHA256}  /tmp/px4_msgs.tgz" | sha256sum -c - \
 && mkdir px4_msgs && tar xzf /tmp/px4_msgs.tgz -C px4_msgs --strip-components=1 \
 && rm -f px4_msgs/msg/*.msg px4_msgs/msg/versioned/*.msg px4_msgs/srv/*.srv \
 && cp -a /opt/PX4-Autopilot/msg/*.msg /opt/PX4-Autopilot/msg/versioned/*.msg px4_msgs/msg/ \
 && cp -a /opt/PX4-Autopilot/srv/*.srv px4_msgs/srv/ \
 && curl -fsSL -m 300 -o /tmp/px4_ros2.tgz https://codeload.github.com/Auterion/px4-ros2-interface-lib/tar.gz/${PX4_ROS2_COMMIT} \
 && echo "${PX4_ROS2_SHA256}  /tmp/px4_ros2.tgz" | sha256sum -c - \
 && mkdir px4-ros2-interface-lib && tar xzf /tmp/px4_ros2.tgz -C px4-ros2-interface-lib --strip-components=1 \
 && rm /tmp/*.tgz
WORKDIR /opt/px4_ros2_ws
RUN source /opt/ros/jazzy/setup.bash \
 && MAKEFLAGS=-j2 colcon build --executor sequential --parallel-workers 1 \
      --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF --packages-up-to px4_ros2_cpp \
 && rm -rf build log
ENTRYPOINT []

FROM ros2base AS aircraft3
COPY --from=checks /opt/drone-venv /opt/drone-venv
COPY --from=checks /workspace /workspace
WORKDIR /workspace
ENV PATH=/opt/drone-venv/bin:${PATH}
RUN python3 scripts/generate_proto.py
ENV PYTHONPATH=/workspace/src:/workspace/gen
# ROS-side bindings with the system protoc 3.21, matching the system protobuf runtime (D039).
# ROS 侧绑定使用系统 protoc 3.21，与系统 protobuf 运行时一致（D039）。
RUN mkdir -p /opt/da_ros2/gen \
 && /usr/bin/protoc --python_out=/opt/da_ros2/gen -I/workspace/proto -I/usr/include \
      /workspace/proto/drone/autonomy/v1/autonomy.proto
RUN source /opt/ros/jazzy/setup.bash && source /opt/px4_ros2_ws/install/setup.bash \
 && MAKEFLAGS=-j2 colcon build --base-paths /workspace/ros2_ws/src --packages-select da_egress_ext \
      --build-base /opt/da_ros2/build --install-base /opt/da_ros2/install \
      --cmake-args -DCMAKE_BUILD_TYPE=Release -DDA_PROTO_ROOT=/workspace/proto \
 && rm -rf /opt/da_ros2/build
LABEL org.drone-agent.role=aircraft-m3
ENTRYPOINT []
CMD ["python3", "-m", "drone_agent.runtime.launch", "guardian"]

FROM ${SIM2_IMAGE} AS sim3
RUN /usr/bin/python3 /workspace/sim/m3_setup.py \
 && printf '%s\n' '#!/usr/bin/env bash' \
      '# M3 simulation: x500_vision with the M3 world; PX4 native defaults otherwise.' \
      '# M3 仿真：x500_vision 与 M3 世界；其余保持 PX4 原生默认。' \
      'set -euo pipefail' 'cd /opt/PX4-Autopilot' \
      'test "$(git rev-parse HEAD)" = d6f12ad1c4f70ad3230afd7d86e971421e02fef4' \
      'exec make px4_sitl gz_x500_vision' > /opt/drone-sim/entrypoint-m3.sh
LABEL org.drone-agent.role=sim-m3
ENTRYPOINT ["bash", "/opt/drone-sim/entrypoint-m3.sh"]
