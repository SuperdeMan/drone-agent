FROM px4io/px4-dev-ros2@sha256:558f27556d5ddb09a5082ec5a4407e4497b3d5bed646278242488010f267ae23

USER root

# Use the local Ubuntu mirror while retaining Ubuntu archive signatures.
# 使用国内 Ubuntu 镜像，仍校验 Ubuntu 官方仓库签名。
RUN sed -i \
    -e 's|http://archive.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
    -e 's|http://security.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
    /etc/apt/sources.list.d/ubuntu.sources

# The ROS toolchain omits simulator packages; install Harmonic inside this image.
# ROS 工具链未带仿真器；只在此镜像内安装 Harmonic。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    curl -fsSL https://packages.osrfoundation.org/gazebo.gpg \
        -o /usr/share/keyrings/px4-gazebo.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/px4-gazebo.gpg] https://packages.osrfoundation.org/gazebo/ubuntu-stable noble main" \
        > /etc/apt/sources.list.d/px4-gazebo.list \
    && no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get update \
    && no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get install -y --no-install-recommends \
        gz-harmonic libunwind-dev cppzmq-dev libeigen3-dev pkg-config

WORKDIR /opt/PX4-Autopilot
ARG PX4_COMMIT=d6f12ad1c4f70ad3230afd7d86e971421e02fef4

# Pin SITL source and submodules; documentation media and NuttX are not build inputs.
# 固定 SITL 源码与子模块；文档媒体和 NuttX 不是本次构建输入。
RUN git -c http.version=HTTP/1.1 clone --filter=blob:none --no-checkout --depth 1 \
        --branch v1.17.0 https://github.com/PX4/PX4-Autopilot.git . \
    && git sparse-checkout set --no-cone '/*' '!/docs/' \
    && git checkout --detach ${PX4_COMMIT} \
    && test "$(git rev-parse HEAD)" = "${PX4_COMMIT}"

RUN git -c http.version=HTTP/1.1 submodule update --init --recursive --depth 1 --jobs 6 -- \
    Tools/simulation/gz src/drivers/gps/devices src/lib/events/libevents \
    src/lib/heatshrink/heatshrink src/modules/mavlink/mavlink \
    src/modules/uxrce_dds_client/Micro-XRCE-DDS-Client

# PX4 builds the camera/optical-flow plugins even for the unarmed x500 smoke.
# 即使只做未解锁的 x500 冒烟，PX4 仍会编译相机与光流插件。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get update \
    && no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get install -y --no-install-recommends \
        libopencv-dev libxml2-utils libgstreamer-plugins-base1.0-dev protobuf-compiler

# Build only: the runtime entrypoint starts headless Gazebo later.
# 这里只构建；运行入口稍后启动无界面 Gazebo。
RUN --mount=type=cache,target=/root/.cache/ccache make -j2 px4_sitl_default

COPY entrypoint.sh smoke.py /opt/drone-sim/
ENV HEADLESS=1
ENV PX4_SIM_MODEL=gz_x500
ENV PX4_GZ_WORLD=default
ENV PYTHONUNBUFFERED=1
WORKDIR /opt/drone-sim
ENTRYPOINT ["bash", "/opt/drone-sim/entrypoint.sh"]
