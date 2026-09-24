"""Guardian-side control of one PX4 external-mode activation (D039, D042).

The guardian stays the only process that decides whether anything is written. For an external-mode step it
publishes the local task to the planner, keeps a fresh "hold here" authorization in the egress node while it
asks PX4 (over MAVLink) to enter the registered external mode, and then, once per supervision tick, turns the
planner's latest candidate into a CBF-filtered goto target that it authorizes for a short time-to-live. Each
tick also derives the v2 triggers the external mode introduces: stale local map, stale or foreign trajectory,
missing progress, compute overload and unavailable autonomy. Stopping never sends a stale setpoint: the
authorization stream ends first and the flight controller is then commanded over MAVLink by the caller.

Per-tick details go to a non-durable recorder; the durable journal gets starts, stops and interventions only,
so the 10 Hz authorization stream never adds an fsync to the supervision period.

一次 PX4 外部模式激活在 guardian 一侧的控制（D039、D042）。

guardian 仍是唯一决定是否写入的进程。对外部模式步骤，它把局部任务发给规划节点，在经 MAVLink 请求 PX4 进入
已注册外部模式期间，让出口节点始终持有新鲜的「原地悬停」授权；之后每个监督周期把规划节点最新的候选变成经 CBF
过滤的 goto 目标，并以很短的存活时间授权。每个周期还推导外部模式带来的 v2 触发条件：局部地图过期、轨迹过期或
不属于当前任务、没有进展、计算过载、自主层不可用。停止时绝不发送过期设定值：先结束授权流，再由调用方经
MAVLink 指挥飞控。

逐周期细节写入非持久记录器；持久账本只记录开始、停止与干预，10 Hz 授权流不会给监督周期增加 fsync。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from datetime import timedelta

from drone_agent.autonomy.messages import (
    AuthorizationRevoked,
    AuthorizedSetpoint,
    EgressStatus,
    LocalizationReport,
    LocalTask,
    ObstacleSet,
    SegmentStatus,
    TrajectorySegment,
    Vector3,
)
from drone_agent.contracts import RecoveryTrigger, utcnow
from drone_agent.guardian.constraint_filter import FilterSettings, filter_target
from drone_agent.mission.registry import coordinates

EXTERNAL_SKILLS = {"skill.flight.goto_local": None, "skill.inspect.asset_local": "approach"}


def enu_to_ned(point) -> Vector3:
    return Vector3(x=point[1], y=point[0], z=-point[2])


def external_phase(node, phase) -> bool:
    """Whether this intent of this node is flown in the external mode. / 该节点的这个意图是否在外部模式下飞行。"""
    if node.skill_id not in EXTERNAL_SKILLS:
        return False
    wanted = EXTERNAL_SKILLS[node.skill_id]
    return wanted is None or phase == wanted


def goal_of(registry, node) -> list[float]:
    goal_id = node.params.get("goal_id", node.params.get("approach_goal_id"))
    return list(registry.local_goal(goal_id))


class ExternalControl:
    def __init__(self, guardian, *, egress, autonomy, log=None, clock=time.monotonic):
        self.guardian, self.egress, self.autonomy = guardian, egress, autonomy
        self.log = log or (lambda topic, value: None)
        self.clock = clock
        data = guardian.registry.data
        self.settings = data["autonomy"]
        self.cbf = FilterSettings(**data["autonomy"]["cbf"])
        self.active = False
        self.entering = False
        self.node = None
        self.task: LocalTask | None = None
        self.goal: list[float] | None = None
        self.speed = 0.0
        self.nav_state: int | None = None
        self.epoch = -1
        self.seq = -1
        self.task_count = 0
        self.progress: deque[tuple[float, float]] = deque()
        self.no_path_since: float | None = None
        self.slow_segments = 0
        self.last_segment_seq = self.latency_seq = -1
        self.started = 0.0
        self.counters = {"authorized": 0, "cbf_modified": 0, "cbf_rejected": 0, "hold_authorizations": 0,
                         "segments_used": 0, "segments_refused": 0, "revocations": 0}
        self.latency_ms: list[float] = []
        self.last_reason = ""
        self.watchdog_base = 0
        self.hold_since: float | None = None

    # ── availability / 可用性 ──

    def egress_status(self) -> EgressStatus | None:
        status = self.egress.fresh("egress_status", self.settings["egress_status_timeout_s"]) if self.egress else None
        robot = self.guardian.registry.capability.robot_id
        return status if status is not None and status.robot_id == robot else None

    def available(self) -> bool:
        """External mode is offered only while the egress node is registered, compatible and linked (D039).

        只有出口节点已注册、兼容且链路正常时才提供外部模式（D039）。
        """
        status = self.egress_status()
        return bool(status and status.external_nav_state is not None and status.compatibility_ok and status.fmu_link_ok)

    def localization(self) -> LocalizationReport | None:
        """A fresh report whose own PX4 inputs and estimator flags are fresh; anything else counts as no report.

        A report built from stale PX4 inputs (the DDS link is gone or not yet up) says nothing about GNSS, so it must
        not read as a GNSS loss (D045); nor may one whose estimator flags are stale or never arrived, since unknown
        fusion is not lost fusion (D048). See 02-contracts §11.

        自身新鲜、其 PX4 输入与估计器标志也新鲜的报告；其他情况都视为没有报告。

        由过期 PX4 输入生成的报告（DDS 链路已断或尚未建立）不能说明 GNSS 状况，因此不能被解读为 GNSS 失效（D045）；
        估计器标志过期或从未到达的报告同样如此，因为融合状态未知不等于融合丢失（D048）。见 02-contracts §11。
        """
        if self.autonomy is None:
            return None
        report = self.autonomy.fresh("localization_report", self.settings["localization_timeout_s"])
        if report is None or report.robot_id != self.guardian.registry.capability.robot_id:
            return None
        if report.px4_status_age_s > self.settings["localization_input_max_age_s"]:
            return None
        if report.estimator_flags_age_s > self.settings["localization_flags_max_age_s"]:
            return None
        return report if report.stamp <= utcnow() < report.valid_until else None

    def obstacles(self) -> ObstacleSet | None:
        if self.autonomy is None:
            return None
        found = self.autonomy.fresh("obstacle_set", self.settings["obstacle_timeout_s"])
        frame = self.guardian.registry.data["frame"]
        if (
            found is None
            or found.robot_id != self.guardian.registry.capability.robot_id
            or (found.frame_id, found.map_version) != (frame["frame_id"], frame["map_version"])
            or not found.stamp <= utcnow() < found.valid_until
        ):
            return None
        return found

    def autonomy_fresh(self) -> bool:
        return self.localization() is not None and self.obstacles() is not None

    # ── authorizations / 授权 ──

    def _next_seq(self) -> int:
        epoch = self.guardian.ledger.highest_epoch
        if epoch != self.epoch:
            self.epoch, self.seq = epoch, -1
        self.seq += 1
        return self.seq

    async def authorize(self, target_enu, speed_mps: float, *, hold: bool) -> bool:
        guardian = self.guardian
        lease = guardian.gate.lease
        if lease is None or self.node is None:
            return False
        vertical = min(self.settings["max_vertical_speed_mps"], max(speed_mps, 0.1))
        setpoint = AuthorizedSetpoint(
            robot_id=lease.robot_id,
            mission_id=lease.mission_id,
            mission_version=lease.mission_version,
            step_id=self.node.task_id,
            lease_epoch=lease.lease_epoch,
            command_seq=self._next_seq(),
            issued_at=utcnow(),
            ttl_ms=int(self.settings["setpoint_ttl_ms"]),
            position_ned=enu_to_ned(target_enu),
            max_horizontal_speed_mps=min(max(speed_mps, 0.1), self.settings["max_speed_mps"]),
            max_vertical_speed_mps=vertical,
        )
        written = await self.egress.send(setpoint)
        if written:
            self.counters["authorized"] += 1
            if hold:
                self.counters["hold_authorizations"] += 1
        self.log("autonomy/authorization", {**setpoint.model_dump(mode="json"), "hold": hold, "written": written})
        return bool(written)

    async def revoke(self, reason: str) -> bool:
        """Tell the egress node that the guardian itself ends the authorization and commands PX4 next (D047).

        Without it the node would only see its authorization lapse and, judging the mode from DDS notices that may
        lag the guardian's MAVLink command, could switch PX4 back to Hold out of a landing or a return.

        告知出口节点：guardian 自己结束授权，接下来由它指挥 PX4（D047）。

        否则节点只会看到授权过期，并依据可能滞后于 guardian MAVLink 命令的 DDS 通知判断模式，可能把 PX4 从降落或返航
        切回 Hold。
        """
        if self.egress is None:
            return False
        seq = self._next_seq()
        revocation = AuthorizationRevoked(
            robot_id=self.guardian.registry.capability.robot_id,
            lease_epoch=max(self.epoch, 0),
            command_seq=seq,
            issued_at=utcnow(),
            reason=reason[:120] or "unspecified",
        )
        try:
            written = await self.egress.send(revocation)
        except Exception:  # noqa: BLE001 - a lost revocation leaves the node's own watchdog in place / 撤销丢失时节点自身的看门狗仍在
            written = 0
        if written:
            self.counters["revocations"] += 1
        self.log("autonomy/revocation", {**revocation.model_dump(mode="json"), "written": written})
        return bool(written)

    # ── lifecycle / 生命周期 ──

    async def start(self, node, phase, permitted) -> None:
        """Publish the task, wait for its first valid segment, enter the external mode behind a hold authorization.

        The mode is requested only once the new task has a valid segment, so an active external mode always has a
        trajectory (D047); PX4 keeps its own Hold while the planner catches up. Any failure withdraws the task and
        revokes the authorization before raising.

        发布任务，等到它的首个有效片段，再在悬停授权保护下进入外部模式。

        新任务拿到有效片段后才请求模式，因此外部模式一旦激活总有轨迹（D047）；规划节点跟上之前 PX4 保持自己的 Hold。
        任何失败都先撤回任务并撤销授权，再抛出异常。
        """
        guardian, adapter = self.guardian, self.guardian.adapter
        status = self.egress_status()
        if not self.available() or status is None:
            raise PermissionError("external mode not available")
        if not self.autonomy_fresh():
            raise PermissionError("local autonomy not fresh")
        obs = adapter.snapshot()
        point = coordinates(obs)
        if point is None or not permitted():
            raise PermissionError("no fresh position or authority for external mode")
        lease = guardian.gate.lease
        registry = guardian.registry
        self.node, self.goal = node, goal_of(registry, node)
        self.speed = float(node.params["speed_mps"])
        self.task_count += 1
        now = utcnow()
        low, high = registry.task_scope()
        self.task = LocalTask(
            robot_id=lease.robot_id,
            mission_id=lease.mission_id,
            mission_version=lease.mission_version,
            step_id=node.task_id,
            lease_epoch=lease.lease_epoch,
            task_id=f"{node.task_id}.{lease.lease_epoch}.{self.task_count}",
            frame_id=registry.data["frame"]["frame_id"],
            map_version=registry.data["frame"]["map_version"],
            goal=Vector3(x=self.goal[0], y=self.goal[1], z=self.goal[2]),
            goal_tolerance_m=registry.data["thresholds"]["goto_local"]["tolerance_m"],
            max_speed_mps=self.speed,
            scope_min=Vector3(x=low[0], y=low[1], z=low[2]),
            scope_max=Vector3(x=high[0], y=high[1], z=high[2]),
            issued_at=now,
            deadline=now + timedelta(seconds=node.timeout_s),
            active=True,
        )
        self.entering, self.active = True, False
        self.progress.clear()
        self.no_path_since, self.slow_segments, self.last_segment_seq, self.latency_seq = None, 0, -1, -1
        self.nav_state = status.external_nav_state
        self.watchdog_base, self.hold_since = status.watchdog_exits, None
        guardian.record("external_start", step_id=node.task_id, task_id=self.task.task_id, nav_state=self.nav_state,
                        goal=self.goal, epoch=lease.lease_epoch)
        await self.autonomy.send(self.task)
        if not await self.authorize(point, 0.1, hold=True):
            await self.stop("start_failed:hold_authorization_refused")
            raise PermissionError("egress node did not accept the hold authorization")
        # Each renewal follows the permission check with no await in between, so a recovery that revoked meanwhile
        # always holds the higher sequence number. / 每次续发都紧跟许可检查、中间没有 await，因此期间发生的恢复所作
        # 撤销总是持有更高的序号。
        deadline = self.clock() + self.settings["first_segment_timeout_s"]
        while self.segment() is None:
            await asyncio.sleep(0.1)
            here = coordinates(adapter.snapshot())
            if self.clock() >= deadline or here is None or not permitted():
                await self.stop("start_failed:no_first_segment")
                raise PermissionError("no trajectory segment for the new task")
            await self.authorize(here, 0.1, hold=True)
        guardian.record("external_first_segment", step_id=node.task_id, task_id=self.task.task_id,
                        segment_seq=self.segment().segment_seq)
        await adapter.request_external_mode(self.nav_state, permitted)
        deadline = self.clock() + self.settings["mode_confirm_timeout_s"]
        wanted = adapter.external_mode_name(self.nav_state)
        while self.clock() < deadline:
            current = adapter.snapshot()
            here = coordinates(current)
            if here is None or not permitted():
                # A recovery or takeover that arrived meanwhile owns the flight; the mode never counts as ours.
                # 期间到来的恢复或接管拥有飞行；该模式永不算作我们的。
                break
            if current.flight_mode == wanted:
                self.entering, self.active = False, True
                self.started = self.clock()
                guardian.record("external_active", step_id=node.task_id, nav_state=self.nav_state)
                return
            await self.authorize(here, 0.1, hold=True)
            await asyncio.sleep(0.1)
        await self.stop("start_failed:mode_not_confirmed")
        raise TimeoutError("external mode was not confirmed")

    async def stop(self, reason: str) -> None:
        """Revoke the authorization and withdraw the task; the caller then commands PX4 over MAVLink.

        撤销授权并撤回任务；随后由调用方经 MAVLink 指挥 PX4。
        """
        if not (self.active or self.entering) or self.task is None:
            return
        self.active = self.entering = False
        self.last_reason = reason
        await self.revoke(reason)
        withdrawn = self.task.model_copy(update={"active": False, "issued_at": utcnow()})
        try:
            await self.autonomy.send(LocalTask.model_validate(withdrawn.model_dump()))
        except Exception:  # noqa: BLE001 - a withdrawal failure never blocks the recovery / 撤回失败不能阻塞恢复
            pass
        self.guardian.record("external_stop", step_id=self.node.task_id if self.node else None, reason=reason,
                             counters=dict(self.counters), latency_p99_ms=self.latency_p99())

    def latency_p99(self) -> float | None:
        values = sorted(self.latency_ms)
        return values[min(int(len(values) * 0.99), len(values) - 1)] if values else None

    # ── per-tick supervision / 每周期监督 ──

    def segment(self) -> TrajectorySegment | None:
        found = self.autonomy.fresh("trajectory_segment", 1.0) if self.autonomy else None
        frame = self.guardian.registry.data["frame"]
        task = self.task
        if (
            found is None
            or task is None
            or found.task_id != task.task_id
            or found.lease_epoch != task.lease_epoch
            or found.robot_id != task.robot_id
            or (found.frame_id, found.map_version) != (frame["frame_id"], frame["map_version"])
            or not found.may_execute
            or not found.created_at <= utcnow() < found.valid_until
        ):
            return None
        return found

    def _progress_stalled(self, point, now) -> bool:
        distance = math.dist(point, self.goal)
        self.progress.append((now, distance))
        window = self.settings["progress_window_s"]
        while self.progress and now - self.progress[0][0] > window:
            self.progress.popleft()
        if distance <= self.task.goal_tolerance_m or now - self.started < window:
            return False
        oldest = self.progress[0][1]
        return oldest - distance < self.settings["progress_min_m"]

    async def tick(self, obs) -> list[tuple[RecoveryTrigger | None, str]]:
        """Supervise and authorize once; returns (trigger, detail) pairs; a None trigger means an external takeover.

        监督并授权一次；返回 (触发条件, 细节)；触发条件为 None 表示外部接管。
        """
        if not self.active:
            return []
        now = self.clock()
        status = self.egress_status()
        if status is None or not status.fmu_link_ok or status.external_nav_state != self.nav_state:
            return [(RecoveryTrigger.AUTONOMY_UNAVAILABLE, "egress_unavailable")]
        if status.watchdog_exits > self.watchdog_base:
            return [(RecoveryTrigger.TRAJECTORY_STALE, "egress_watchdog_exit")]
        if obs.flight_mode == "HOLD":
            # HOLD is expected only from the egress watchdog, whose report may lag the mode by one status period;
            # a HOLD nobody here requested is someone else's takeover. / 只有出口看门狗会切到 HOLD，其报告可能晚一个
            # 状态周期；这里无人请求的 HOLD 是他人接管。
            self.hold_since = self.hold_since or now
            if now - self.hold_since > self.settings["egress_status_timeout_s"] + 0.1:
                # No recovery edge: an unrequested mode change revokes business authority (M1 rule). / 不走恢复边：
                # 未请求的模式变化撤销业务控制权（M1 规则）。
                return [(None, "unrequested_hold_during_external_mode")]
            return []
        self.hold_since = None
        if self.localization() is None:
            return [(RecoveryTrigger.AUTONOMY_UNAVAILABLE, "localization_report_stale")]
        belief = self.obstacles()
        if belief is None:
            return [(RecoveryTrigger.OBSERVATION_STALE, "local_map_stale")]
        point = coordinates(obs)
        if point is None or not obs.timestamp <= utcnow() < obs.valid_until:
            return []  # The guardian's own observation check owns this case. / 由 guardian 自身的观测检查处理。
        segment = self.segment()
        if segment is None:
            self.counters["segments_refused"] += 1
            return [(RecoveryTrigger.TRAJECTORY_STALE, "no_valid_segment")]
        budget = self.settings["planner_cycle_budget_ms"] * self.settings["overload_cycle_factor"]
        if segment.segment_seq != self.last_segment_seq:
            self.last_segment_seq = segment.segment_seq
            slow = segment.status is SegmentStatus.OVERLOADED or segment.cycle_ms > budget
            self.slow_segments = self.slow_segments + 1 if slow else 0
        if self.slow_segments >= 2:
            return [(RecoveryTrigger.COMPUTE_OVERLOADED, f"planner_cycle_ms={segment.cycle_ms:.0f}")]
        if segment.status is SegmentStatus.NO_PATH:
            self.no_path_since = self.no_path_since or now
            if now - self.no_path_since >= self.settings["no_path_timeout_s"]:
                return [(RecoveryTrigger.PROGRESS_STALLED, "planner_no_path")]
        else:
            self.no_path_since = None
        if self._progress_stalled(point, now):
            return [(RecoveryTrigger.PROGRESS_STALLED, "no_progress_toward_goal")]
        target = self._carrot(segment, point)
        low, high = self.guardian.registry.task_scope()
        result = filter_target(
            tuple(point),
            tuple(target),
            v_max=min(self.speed, segment.max_speed_mps, self.settings["max_speed_mps"]),
            bounds={"x": (low[0], high[0]), "y": (low[1], high[1]), "z": (low[2], high[2])},
            obstacles=self.guardian.registry.data.get("obstacles", {}),
            belief={"points": [p.as_list() for p in belief.points], "point_radius_m": belief.point_radius_m,
                    "position_std_m": belief.position_std_m},
            settings=self.cbf,
        )
        self.log("autonomy/filter", {"segment_seq": segment.segment_seq, "accepted": result.accepted,
                                     "reason": result.reason, "modified": result.modified, "min_h": result.min_h
                                     if math.isfinite(result.min_h) else None, "binding": result.binding[:8],
                                     "target": list(result.target) if result.target else None,
                                     "desired": list(target), "position": list(point)})
        if not result.accepted:
            self.counters["cbf_rejected"] += 1
            return [(RecoveryTrigger.TRAJECTORY_STALE, "cbf_rejected:" + result.reason)]
        if result.modified:
            self.counters["cbf_modified"] += 1
        self.counters["segments_used"] += 1
        if await self.authorize(result.target, result.speed_mps, hold=False) and segment.segment_seq != self.latency_seq:
            # D040 intent latency: a segment's creation to its first authorized setpoint; later reuses within its
            # validity are not new intents. / D040 意图延迟：片段生成到其首个授权设定值；有效期内的复用不是新意图。
            self.latency_seq = segment.segment_seq
            self.latency_ms.append((utcnow() - segment.created_at).total_seconds() * 1000)
        return []

    def _carrot(self, segment: TrajectorySegment, point) -> list[float]:
        """The segment point closest to the filter horizon, clamped to the flight band. / 最接近过滤时域的片段点，限制在飞行高度带内。"""
        horizon = self.cbf.horizon_s
        chosen = min(segment.points, key=lambda p: abs(p.t_offset_s - horizon)).position.as_list()
        band = self.settings["flight_altitude_band_m"]
        chosen[2] = min(max(chosen[2], band[0]), band[1])
        return chosen
