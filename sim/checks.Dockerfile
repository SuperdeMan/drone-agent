ARG SITL_IMAGE=drone-agent-sitl:px4-1.17.0-m0
FROM ${SITL_IMAGE}
USER root
WORKDIR /workspace
COPY control/requirements.txt /opt/drone-requirements.txt
# Isolate tests from the image's ROS Python packages and from the host Python.
# 将测试与镜像内的 ROS Python 包及宿主机 Python 完全隔离。
RUN python3 -m venv --without-pip /opt/drone-venv \
    && python3 -m pip --python /opt/drone-venv install --no-cache-dir --only-binary=:all: --require-hashes \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    -r /opt/drone-requirements.txt \
    && python3 -m pip --python /opt/drone-venv check
COPY source/ /workspace/
ENV PATH=/opt/drone-venv/bin:${PATH}
ENV PYTHONPATH=/workspace/src
ENTRYPOINT []
CMD ["python3", "-m", "pytest", "-q", "--junitxml=/artifacts/contracts.xml"]
