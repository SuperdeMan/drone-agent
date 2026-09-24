"""Shared fakes for the M3 guardian tests: an external-mode-capable adapter and in-memory role endpoints.

M3 guardian 测试共用的替身：支持外部模式的适配器与内存中的角色端点。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

from drone_agent.autonomy.link import Received
from drone_agent.autonomy.messages import (
    EgressStatus,
    Implementation,
    LocalizationReport,
    LocalTask,
    ObstacleSet,
    SegmentStatus,
    TrajectoryPoint,
    TrajectorySegment,
    Vector3,
)
from drone_agent.contracts import (
    ControlCommandEnvelope,
    FlightObservation,
    Frame,
    IdempotencyKey,
    Pose,
    Position,
    RecoveryPolicy,
    TaskLease,
    utcnow,
)
from drone_agent.eval.m3_fixture import make_m3_package
from drone_agent.guardian.core import Guardian
from drone_agent.mission.registry import M3_SCENE, Registry
from drone_agent.runtime.ledger import Journal

ROOT = Path(__file__).resolve().parents[2]


class Endpoint:
    """In-memory stand-in for a role-bound LocalEndpoint. / 按角色端点的内存替身。"""

    def __init__(self):
        self.latest: dict[str, Received] = {}
        self.sent: list = []
        self.accepting = True
        self.on_send = None

    def put(self, kind, model, age=0.0):
        self.latest[kind] = Received(model, time.monotonic() - age)

    def fresh(self, kind, max_age_s):
        item = self.latest.get(kind)
        if item is None or time.monotonic() - item.monotonic > max_age_s:
            return None
        return item.model

    async def send(self, model):
        if not self.accepting:
            return 0
        self.sent.append(model)
        if self.on_send is not None:
            self.on_send(model)
        return 1


class Adapter:
    camera_available = True
    external_takeover = False

    def __init__(self, registry):
        self.capabilities = registry.capability.model_copy(deep=True)
        self.capabilities.recovery_behaviors = {"hold", "rtl", "land_here", "land_at", "handover_to_fc_failsafe"}
        self.frame = registry.data["frame"]
        self.position = [0.0, 0.0, 4.0]
        self.airborne = True
        self.battery = 0.9
        self.mode = "HOLD"
        self.writes = []
        self.switch_mode = True
        self.healthy = True
        self.sample = 0

    @staticmethod
    def external_mode_name(nav_state):
        return f"EXTERNAL{nav_state - 22}"

    def next_sample(self):
        self.sample += 1
        return self.sample

    def snapshot(self):
        now = utcnow()
        return FlightObservation(
            timestamp=now, valid_until=now + timedelta(seconds=0.5), sample_id=self.next_sample(),
            robot_id="uav_01",
            pose=Pose(frame=Frame(**self.frame), position=Position(
                x=self.position[0], y=self.position[1], z=self.position[2], covariance=(1, 0, 0, 0, 1, 0, 0, 0, 1))),
            velocity_enu_mps=[0, 0, 0], armed=self.airborne, in_air=self.airborne, flight_mode=self.mode,
            localization_healthy=self.healthy, home_healthy=True, battery_fraction=self.battery, fc_failsafe=False,
            source="deterministic_test",
        )

    async def execute(self, node, permitted, phase=None):
        assert permitted()
        self.writes.append(("execute", node.task_id, phase))

    async def request_external_mode(self, nav_state, permitted):
        assert permitted()
        self.writes.append(("external_mode", nav_state))
        if self.switch_mode:
            self.mode = self.external_mode_name(nav_state)

    async def recover(self, behavior, permitted, site=None):
        if permitted():
            self.writes.append(("recover", str(behavior), site))
            self.mode = {"hold": "HOLD", "rtl": "RETURN_TO_LAUNCH", "land_here": "LAND"}.get(str(behavior), self.mode)


def egress_status(**overrides):
    values = dict(robot_id="uav_01", stamp=utcnow(), node_version="0.1.0", registered=True, mode_nav_state=23,
                  mode_active=False, fmu_link_ok=True, compatibility_ok=True, highest_epoch=-1,
                  last_forwarded_seq=-1, forwarded_setpoints=0, rejected_stale=0, rejected_epoch=0,
                  rejected_seq=0, watchdog_exits=0)
    values.update(overrides)
    return EgressStatus(**values)


def localization(**overrides):
    now = utcnow()
    values = dict(robot_id="uav_01", stamp=now, valid_until=now + timedelta(milliseconds=500), gnss_ok=True,
                  gnss_fix_type=3, gnss_satellites=12, visual_ok=True, ev_position_fused=True,
                  gnss_position_fused=True, local_position_ok=True, source_version="0.1.0", px4_status_age_s=0.05,
                  estimator_flags_age_s=0.4)
    values.update(overrides)
    return LocalizationReport(**values)


def obstacles(points=(), **overrides):
    now = utcnow()
    values = dict(robot_id="uav_01", frame_id="map_enu", map_version="campus_v3", stamp=now,
                  valid_until=now + timedelta(milliseconds=500), source_id="local_map", source_version="0.1.0",
                  points=[Vector3(x=p[0], y=p[1], z=p[2]) for p in points], point_radius_m=0.1, position_std_m=0.1,
                  confidence=0.9, map_seq=1, coverage_radius_m=12)
    values.update(overrides)
    return ObstacleSet(**values)


def segment(task_id, *, seq=0, target=(0, 6, 4), status=SegmentStatus.OK, cycle_ms=40.0, epoch=1):
    now = utcnow()
    return TrajectorySegment(
        robot_id="uav_01", task_id=task_id, lease_epoch=epoch, segment_seq=seq, source_id="planner",
        source_version="0.1.0", implementation=Implementation.DETERMINISTIC, created_at=now,
        valid_until=now + timedelta(milliseconds=400), frame_id="map_enu", map_version="campus_v3",
        points=[TrajectoryPoint(t_offset_s=1.0, position=Vector3(x=target[0], y=target[1], z=target[2]))],
        max_speed_mps=2.0, status=status, cycle_ms=cycle_ms)


class Runtime:
    """A v2 guardian around an M3 package with a live lease and heartbeat. / 持有有效租约与心跳的 v2 guardian。"""

    def __init__(self, tmp_path, *, shape="inspect_external", egress_up=True):
        self.registry = Registry(ROOT, scene=ROOT / M3_SCENE)
        self.package = make_m3_package(self.registry, 7, "mission-m3", shape)
        self.adapter = Adapter(self.registry)
        self.egress, self.autonomy = Endpoint(), Endpoint()
        if egress_up:
            self.egress.put("egress_status", egress_status())
        self.autonomy.put("localization_report", localization())
        self.autonomy.put("obstacle_set", obstacles())
        # A planner that answers each new local task with a first segment at once (D047). / 对每个新局部任务立即
        # 给出首个片段的规划节点（D047）。
        self.autonomy.on_send = self.plan
        self.journal = Journal(tmp_path / "guardian.jsonl")
        self.log = []
        self.guardian = Guardian(
            adapter=self.adapter, package=self.package, registry=self.registry, journal=self.journal,
            policy=RecoveryPolicy.from_yaml(ROOT / "configs/recovery_policies/multirotor_m3_v2.yaml"),
            executive_id="executive-m3", simulation=True, egress=self.egress, autonomy=self.autonomy,
            log=lambda topic, value: self.log.append(topic),
        )
        now = utcnow()
        self.lease = TaskLease(robot_id="uav_01", mission_id=self.package.mission_id, mission_version=1,
                               lease_epoch=1, holder="executive-m3", issued_at=now,
                               expires_at=now + timedelta(seconds=10), resources=["uav_01.camera", "uav_01.motion"])
        self.adapter.airborne = False
        assert self.guardian.install_lease(self.lease).accepted
        self.adapter.airborne = True
        self.seq = 0
        self.beat()

    def beat(self):
        self.seq += 1
        now = utcnow()
        self.guardian.heartbeat({
            "schema_version": "0.1.0", "robot_id": "uav_01", "mission_id": self.package.mission_id,
            "mission_version": 1, "lease_epoch": 1, "executive_instance": "executive-m3",
            "heartbeat_seq": self.seq, "progress_seq": self.seq, "timestamp": now,
            "valid_until": now + timedelta(seconds=1), "skill_instance_id": "x", "skill_state": "running",
        })

    def plan(self, model):
        if isinstance(model, LocalTask) and model.active:
            self.autonomy.put("trajectory_segment", segment(model.task_id))

    def refresh(self):
        self.egress.put("egress_status", egress_status(mode_active=self.adapter.mode.startswith("EXTERNAL")))
        self.autonomy.put("localization_report", localization())
        self.autonomy.put("obstacle_set", obstacles())

    def envelope(self, step_id, payload, seq):
        now = utcnow()
        return ControlCommandEnvelope(
            key=IdempotencyKey(mission_id=self.package.mission_id, mission_version=1, step_id=step_id,
                               command_id=f"c{seq}", robot_id="uav_01", lease_epoch=1),
            command_seq=seq, issued_at=now, valid_until=now + timedelta(seconds=2), intent_kind="skill",
            payload=payload)

    def node(self, step_id):
        return next(n for n in self.package.nodes if n.task_id == step_id)

    async def submit(self, step_id, phase=None, seq=1):
        node = self.node(step_id)
        payload = {"skill_id": node.skill_id, "params": node.params}
        if phase is not None:
            payload["phase"] = phase
        return await self.guardian.submit(self.envelope(step_id, payload, seq))

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)
        if self.guardian.recovery_task:
            await asyncio.gather(self.guardian.recovery_task, return_exceptions=True)

    def kinds(self):
        return [row["kind"] for row in self.journal.rows]
