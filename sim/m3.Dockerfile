# M3 images (D038, D039, D041, D044). Built on the cloud host by scripts/remote_m3.py for one deployed revision.
#   ros2base  — pinned px4_msgs (packaging from PX4/px4_msgs, messages from the pinned PX4 v1.17.0 source) and
#               px4_ros2_cpp release/1.17; depends only on pinned inputs, so Docker reuses it across revisions.
#   edgeruntime — ONNX Runtime for the system Python and the pinned CLIP vision encoder (D044); pinned inputs only.
#   edgeprompts — throwaway: the pinned CLIP text encoder turns the versioned prompt set into class embeddings.
#   aircraft3 — the M1/M2 aircraft runtime (venv + source of the revision) plus the egress node, the ROS-side
#               protobuf bindings of drone.autonomy.v1 generated with the system protoc, and the event detector.
#   sim3      — the M2 simulation image of the same revision plus the M3 world, depth camera and sim airframe.
# arm64 (D038): pass BASE_IMAGE for a JetPack 6 / ROS 2 Jazzy base; not built or verified in M3-SITL.
#
# M3 镜像（D038、D039、D041、D044），由 scripts/remote_m3.py 在云主机上为某个已部署版本构建。
#   ros2base  — 固定的 px4_msgs（打包文件来自 PX4/px4_msgs，消息来自固定的 PX4 v1.17.0 源码）与 px4_ros2_cpp
#               release/1.17；只依赖固定输入，Docker 可跨版本复用。
#   edgeruntime — 系统 Python 的 ONNX Runtime 与固定的 CLIP 视觉编码器（D044）；只依赖固定输入。
#   edgeprompts — 一次性阶段：用固定的 CLIP 文本编码器把版本化提示集转成类别嵌入。
#   aircraft3 — M1/M2 机载运行时（该版本的 venv 与源码）加出口节点、用系统 protoc 生成的 drone.autonomy.v1 ROS 侧
#               绑定，以及事件检测节点。
#   sim3      — 同一版本的 M2 仿真镜像加 M3 世界、深度相机与仿真机架。
# arm64（D038）：传入 JetPack 6 / ROS 2 Jazzy 的 BASE_IMAGE；M3-SITL 中未构建也未验证。
ARG BASE_IMAGE=drone-agent-sitl:px4-1.17.0-m0
ARG CHECKS_IMAGE=drone-agent-checks:required-explicit-revision
ARG SIM2_IMAGE=drone-agent-m2-sim:required-explicit-revision
# Event-detection pins (D044); configs/perception/event_prompts_v1.yaml carries the same values and a test compares them.
# 事件检测固定值（D044）；configs/perception/event_prompts_v1.yaml 带有相同的值，由测试比对。
ARG CLIP_REVISION=d15189d7028b43f1d3e65039190477f6af591c2a
ARG CLIP_VISION_SHA256=583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299
ARG CLIP_TEXT_SHA256=3f6571f5bad13a97c469c1622e1cfc4d9aef78b79fdbfcff804ca357bfada8cc
ARG CLIP_VOCAB_SHA256=5047b556ce86ccaf6aa22b3ffccfc52d391ea4accdab9c2f2407da5b742d4363
ARG CLIP_MERGES_SHA256=9fd691f7c8039210e0fced15865466c65820d09b63988b0174bfe25de299051a
ARG ORT_WHEEL_AMD64=34/35/e7f862dbacbc99fadd9b14a614e49c99bf0f35fd9927a82f096e3de33531/onnxruntime-1.30.0-cp312-cp312-manylinux_2_28_x86_64.whl
ARG ORT_SHA256_AMD64=fa688e7891a6aa206636fe7372e27ee75fd17713289f6b4fc7b190e0a7de9328
ARG ORT_WHEEL_ARM64=16/bd/cbc5b8f91963689fdd622f463508c01d0aa95d3f944747b1e0b1eb2160b8/onnxruntime-1.30.0-cp312-cp312-manylinux_2_28_aarch64.whl
ARG ORT_SHA256_ARM64=6c32a000d5139a38ba9349030b0032e3331acb559d596b22738d9d2b343a2b83

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

FROM ros2base AS edgeruntime
ARG TARGETARCH
ARG CLIP_REVISION
ARG CLIP_VISION_SHA256
ARG ORT_WHEEL_AMD64
ARG ORT_SHA256_AMD64
ARG ORT_WHEEL_ARM64
ARG ORT_SHA256_ARM64
# ONNX Runtime without its optional tool dependencies (inference needs only numpy, which the system Python has), and
# the vision encoder of the pinned CLIP revision. / 不带可选工具依赖的 ONNX Runtime（推理只需系统 Python 自带的
# numpy），以及固定 CLIP 版本的视觉编码器。
RUN case "${TARGETARCH:-amd64}" in \
      amd64) wheel="${ORT_WHEEL_AMD64}"; digest="${ORT_SHA256_AMD64}" ;; \
      arm64) wheel="${ORT_WHEEL_ARM64}"; digest="${ORT_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -m 300 -o /tmp/ort.whl "https://pypi.tuna.tsinghua.edu.cn/packages/${wheel}" \
 && echo "${digest}  /tmp/ort.whl" | sha256sum -c - \
 && mkdir -p /opt/da_edge/pydeps /opt/da_edge/model \
 && /usr/bin/python3 -m zipfile -e /tmp/ort.whl /opt/da_edge/pydeps \
 && rm /tmp/ort.whl \
 && curl -fsSL -m 900 -o /opt/da_edge/model/vision_model_quantized.onnx \
      "https://hf-mirror.com/Xenova/clip-vit-base-patch32/resolve/${CLIP_REVISION}/onnx/vision_model_quantized.onnx" \
 && echo "${CLIP_VISION_SHA256}  /opt/da_edge/model/vision_model_quantized.onnx" | sha256sum -c -

FROM edgeruntime AS edgeprompts
ARG CLIP_REVISION
ARG CLIP_TEXT_SHA256
ARG CLIP_VOCAB_SHA256
ARG CLIP_MERGES_SHA256
RUN mkdir -p /tmp/clip && cd /tmp/clip \
 && base="https://hf-mirror.com/Xenova/clip-vit-base-patch32/resolve/${CLIP_REVISION}" \
 && curl -fsSL -m 900 -o text_model.onnx "${base}/onnx/text_model.onnx" \
 && curl -fsSL -m 120 -o vocab.json "${base}/vocab.json" \
 && curl -fsSL -m 120 -o merges.txt "${base}/merges.txt" \
 && echo "${CLIP_TEXT_SHA256}  text_model.onnx" | sha256sum -c - \
 && echo "${CLIP_VOCAB_SHA256}  vocab.json" | sha256sum -c - \
 && echo "${CLIP_MERGES_SHA256}  merges.txt" | sha256sum -c -
COPY --from=checks /workspace/configs/perception /tmp/prompts
COPY --from=checks /workspace/ros2_ws/src/da_edge_inference /tmp/src
RUN PYTHONPATH=/opt/da_edge/pydeps:/tmp/src /usr/bin/python3 -m da_edge_inference.build_prompts \
      --config /tmp/prompts/event_prompts_v1.yaml --model-dir /tmp/clip --output /opt/da_edge/prompts.json

FROM edgeruntime AS aircraft3
COPY --from=checks /opt/drone-venv /opt/drone-venv
COPY --from=checks /workspace /workspace
WORKDIR /workspace
# ROS-side bindings with the system protoc 3.21, matching the system protobuf runtime (D039). The ROS build runs
# before the venv joins PATH: ament's CMake must find the system python3 that carries catkin_pkg.
# ROS 侧绑定使用系统 protoc 3.21，与系统 protobuf 运行时一致（D039）。ROS 构建在 venv 进入 PATH 之前运行：
# ament 的 CMake 必须找到带 catkin_pkg 的系统 python3。
RUN mkdir -p /opt/da_ros2/gen \
 && /usr/bin/protoc --python_out=/opt/da_ros2/gen -I/workspace/proto -I/usr/include \
      /workspace/proto/drone/autonomy/v1/autonomy.proto
RUN source /opt/ros/jazzy/setup.bash && source /opt/px4_ros2_ws/install/setup.bash \
 && MAKEFLAGS=-j2 colcon build --base-paths /workspace/ros2_ws/src --packages-select da_egress_ext \
      --build-base /opt/da_ros2/build --install-base /opt/da_ros2/install \
      --cmake-args -DCMAKE_BUILD_TYPE=Release -DDA_PROTO_ROOT=/workspace/proto \
 && rm -rf /opt/da_ros2/build
ENV PATH=/opt/drone-venv/bin:${PATH}
RUN python3 scripts/generate_proto.py
ENV PYTHONPATH=/workspace/src:/workspace/gen
COPY --from=edgeprompts /opt/da_edge/prompts.json /opt/da_edge/prompts.json
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
