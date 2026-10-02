# P5 simulation image (D071): the M2 simulation image plus the road segment marking of the second business template.
# The M1 and M2 images and worlds stay exactly as recorded; the source is the same checked revision.
#
# P5 仿真镜像（D071）：M2 仿真镜像加第二业务模板的道路段标线。M1 与 M2 的镜像与世界保持原样；源码是同一已校验版本。
ARG SIM2_IMAGE=drone-agent-m2-sim:required-explicit-revision
FROM ${SIM2_IMAGE}
RUN /usr/bin/python3 /workspace/sim/p5_setup.py
LABEL org.drone-agent.role=sim-p5
