ARG CHECKS_IMAGE
FROM ${CHECKS_IMAGE} AS aircraft
RUN python3 scripts/generate_proto.py
ENV PYTHONPATH=/workspace/src:/workspace/gen
LABEL org.drone-agent.role=aircraft
ENTRYPOINT []
CMD ["python3", "-m", "drone_agent.runtime.launch", "guardian"]

FROM aircraft AS ground
LABEL org.drone-agent.role=ground
CMD ["python3", "-m", "drone_agent.eval.judge"]

FROM aircraft AS sim
RUN /usr/bin/python3 /workspace/sim/m1_setup.py
LABEL org.drone-agent.role=sim
ENV HEADLESS=1
ENV PX4_SIM_SPEED_FACTOR=2
ENV PX4_GZ_HEADLESS_RENDERING=1
ENTRYPOINT ["bash", "/opt/drone-sim/entrypoint.sh"]
