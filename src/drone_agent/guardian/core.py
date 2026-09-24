"""Mission-bound egress with durable receipts and preemptive recovery.

M2 adds, in signed mode only, onboard signature verification and a robot-level epoch watermark that
survives mission versions (D030), and binds each intent of a multi-phase skill to a declared phase whose
preconditions are checked separately (D034). Without a trust store the M1 local-trust behaviour is
unchanged.

M3 (D039–D042) adds, only for packages under recovery policy v2: validation against the live capability (the
external mode exists only while the egress node is healthy), the external-mode controller for goto and approach
intents with CBF filtering and short-lived authorizations, the v2 recovery context (control mode, visual
localization, energy reachability) and the v2 triggers (stale local map or trajectory, stalled progress, compute
overload including the guardian's own period, unavailable autonomy, GNSS loss). Every intervention ends the
authorization stream before the MAVLink recovery command. Policy v1 missions keep the M1/M2 behaviour.

绑定任务的控制出口，具备持久化回执与抢占式恢复。

M2 仅在签名模式下增加机载验签与跨任务版本保留的机器人级代次水位（D030），并把多相位技能的每个意图
绑定到声明的相位、分别检查其前置条件（D034）。没有信任存储时，M1 本地信任行为不变。

M3（D039–D042）只对恢复策略 v2 的任务包增加：按实时能力校验（外部模式只在出口节点健康时存在）；goto 与接近意图的
外部模式控制器，带 CBF 过滤与短时授权；v2 恢复上下文（控制模式、视觉定位、能源可达性）；v2 触发条件（局部地图或
轨迹过期、进展停滞、计算过载（含 guardian 自身周期）、自主层不可用、GNSS 丢失）。每次干预都先结束授权流，再发
MAVLink 恢复命令。策略 v1 的任务保持 M1/M2 行为。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from drone_agent.contracts import (
    ControlCommandEnvelope,
    ControlMode,
    EgressDecision,
    EgressGate,
    MissionAction,
    RecoveryBehavior,
    RecoveryPolicy,
    RecoveryTrigger,
    SafetyVerdict,
    TaskLease,
    utcnow,
)
from drone_agent.guardian.energy import EnergyModel, EnergySettings
from drone_agent.guardian.external import ExternalControl, external_phase
from drone_agent.mission.registry import coordinates
from drone_agent.runtime.ledger import CommandLedger, content_hash
from drone_agent.runtime.signing import verify_package

PHASE_OF_ACTION = {"takeoff": "takeoff", "capture_image": "inspect", "land": "landing"}
PHASE_OF_INTENT = {"approach": "cruise", "capture": "inspect"}


def flight_phase(node, intent_phase=None):
    """Recovery-policy flight phase for a node and optional intent phase.

    节点（及可选意图相位）对应的恢复策略飞行阶段。
    """
    if intent_phase is not None:
        return PHASE_OF_INTENT.get(intent_phase, "cruise")
    return PHASE_OF_ACTION.get(node.skill_id.rsplit(".", 1)[1], "cruise")


class Guardian:
    def __init__(
        self, *, adapter, package, registry, journal, policy: RecoveryPolicy, executive_id: str, simulation=False,
        trust=None, robot_state=None, egress=None, autonomy=None, log=None,
    ):
        # Signed mode (M2): verify before trusting any package content. / 签名模式（M2）：信任任何任务包内容之前先验签。
        self.signer_key_id = (
            verify_package(package, trust, robot_id=registry.capability.robot_id, now=utcnow()) if trust else None
        )
        if not simulation:
            policy.require_verified()
        self.adapter, self.package, self.registry = adapter, package, registry
        self.journal, self.ledger = journal, CommandLedger(journal)
        self.policy, self.executive_id = policy, executive_id
        self.gate = EgressGate()
        self.safety = SafetyVerdict.HOLD
        self.reason = "awaiting_lease"
        self.last_heartbeat = None
        self.heartbeat_seq = -1
        self.progress_seq = -1
        self.last_progress = None
        self.active_step = None
        self.phase = "ground"
        self.recovery = None
        self.recovery_started = None
        self.recovery_followed = False
        self.recovery_task = None
        self.dispatch_task = None
        self.taken_over = False
        self.generation = 0
        self.periods = []
        self.last_tick = None
        self.operation_ids = set()
        self.initial_energy = None
        self.lease_deadline = 0.0
        self.uplink_ok = True
        self.authorized_to_continue = True
        # M3 (D042): policy v2 features; None/empty under v1. / M3（D042）：策略 v2 功能；v1 下为空。
        self.v2 = policy.version == "v2"
        self.external = ExternalControl(self, egress=egress, autonomy=autonomy, log=log) if self.v2 else None
        self.energy = EnergyModel(EnergySettings.from_registry(registry.data)) if self.v2 else None
        self.recent_periods: deque[tuple[float, float]] = deque()
        self.recovery_site = None
        self.land_at_landing = False
        self.energy_context: dict = {}
        for row in reversed(journal.rows):
            if row["kind"] == "intent":
                prior_key = row["data"]["envelope"]["key"]
                if (prior_key["mission_id"], prior_key["mission_version"]) != (
                    package.mission_id,
                    package.mission_version,
                ):
                    raise ValueError("authority journal belongs to a different mission")
                self.active_step = next((node for node in package.nodes if node.task_id == prior_key["step_id"]), None)
                if self.active_step is None:
                    raise ValueError("persisted command is absent from the approved package")
                self.phase = flight_phase(self.active_step, row["data"]["envelope"]["payload"].get("phase"))
                break
        self.registry.validate_package(
            package, camera_available=adapter.camera_available, control_modes=self.live_capabilities().control_modes
        )
        if self.policy.validate_against(adapter.capabilities):
            raise ValueError("recovery policy exceeds actual adapter capabilities")
        self.robot_state = robot_state
        self.epoch_floor = 0
        if robot_state is not None:
            robot_state.accept_version(package.mission_id, package.mission_version, package.package_hash)
            self.epoch_floor = robot_state.minimum_epoch()
        if self.signer_key_id is not None:
            self.record("package_verified", package_hash=package.package_hash, signer_key_id=self.signer_key_id,
                        epoch_floor=self.epoch_floor)

    def record(self, kind, **data):
        return self.journal.append(kind, {"mission_id": self.package.mission_id, **data})

    def live_capabilities(self):
        """The adapter capability with `external_mode` only while the egress node is healthy (D039).

        适配器能力；只有出口节点健康时才含 `external_mode`（D039）。
        """
        caps = self.adapter.capabilities.model_copy(deep=True)
        if self.external is not None and self.external.available():
            caps.control_modes = caps.control_modes | {ControlMode.EXTERNAL_MODE}
        return caps

    def predicate_overrides(self, obs):
        """Facts only the guardian owns: external-mode liveness and, under v2, the energy reachability.

        只有 guardian 掌握的事实：外部模式在线性，以及 v2 下的能源可达性。
        """
        if not self.v2:
            return None
        overrides = {
            "external_mode_available": self.external.available(),
            "local_autonomy_fresh": self.external.autonomy_fresh(),
        }
        context = self.energy_context or {}
        if context:
            # v2 replaces the static plan sum with in-flight reachability (D042). / v2 用飞行中可达性取代静态求和（D042）。
            overrides["energy_remaining_plan_feasible"] = bool(context.get("rtl_reachable")) and not context.get(
                "return_due"
            )
        return overrides

    def install_lease(self, lease: TaskLease):
        lease = TaskLease.model_validate(lease.model_dump())
        package = self.package
        if (
            self.taken_over
            or not lease.is_valid_at(utcnow())
            or lease.holder != self.executive_id
            or lease.robot_id != self.registry.capability.robot_id
            or (lease.mission_id, lease.mission_version) != (package.mission_id, package.mission_version)
            or not package.is_authorized(robot_id=lease.robot_id, now=utcnow())
        ):
            return EgressDecision(accepted=False, reason="lease_authority_mismatch")
        resources = {r.resource_id for n in package.nodes for r in n.resources}
        if set(lease.resources) != resources:
            return EgressDecision(accepted=False, reason="lease_resource_mismatch")
        if self.ledger.lease is None and lease.lease_epoch <= self.ledger.highest_epoch:
            return EgressDecision(accepted=False, reason="restart_or_revocation_requires_new_epoch")
        if lease.lease_epoch < self.epoch_floor:
            # A new mission version never reuses an epoch granted to an earlier one. / 新任务版本从不复用旧版本的代次。
            return EgressDecision(accepted=False, reason="epoch_below_robot_watermark")
        new_epoch = lease.lease_epoch > self.ledger.highest_epoch
        if new_epoch:
            obs = self.adapter.snapshot()
            if (
                obs.in_air is not False
                or obs.armed is not False
                or (self.dispatch_task and not self.dispatch_task.done())
            ):
                return EgressDecision(accepted=False, reason="new_authority_requires_grounded_reconciliation")
        try:
            self.gate.grant(lease)
        except ValueError as error:
            return EgressDecision(accepted=False, reason=str(error))
        self.ledger.record("lease", lease.model_dump(mode="json"))
        if self.robot_state is not None:
            self.robot_state.record_epoch(lease.lease_epoch)
        self.lease_deadline = time.monotonic() + (lease.expires_at - utcnow()).total_seconds()
        if new_epoch:
            self.generation += 1
            self.heartbeat_seq = self.progress_seq = -1
            self.last_progress = self.last_heartbeat = None
        return EgressDecision(accepted=True)

    def lease_valid(self):
        lease = self.gate.lease
        return bool(lease and lease.is_valid_at(utcnow()) and time.monotonic() < self.lease_deadline)

    def heartbeat(self, pulse: dict):
        lease = self.gate.lease
        now = utcnow()
        if not lease or not self.lease_valid():
            raise ValueError("heartbeat without live lease")
        expected = {
            "robot_id": lease.robot_id,
            "mission_id": lease.mission_id,
            "mission_version": lease.mission_version,
            "lease_epoch": lease.lease_epoch,
            "executive_instance": self.executive_id,
        }
        if any(pulse.get(k) != v for k, v in expected.items()):
            raise ValueError("heartbeat identity mismatch")
        if not pulse["timestamp"] <= now < pulse["valid_until"] or (now - pulse["timestamp"]).total_seconds() > 2:
            raise ValueError("expired or future heartbeat")
        if pulse["heartbeat_seq"] <= self.heartbeat_seq or pulse.get("progress_seq", -1) < self.progress_seq:
            raise ValueError("replayed heartbeat")
        self.heartbeat_seq = pulse["heartbeat_seq"]
        self.last_heartbeat = time.monotonic()
        if pulse["progress_seq"] > self.progress_seq:
            self.progress_seq = pulse["progress_seq"]
            self.last_progress = self.last_heartbeat
        if self.recovery is None and not self.taken_over:
            self.safety, self.reason = SafetyVerdict.PROCEED, ""
        return self.status()

    def heartbeat_healthy(self):
        return self.last_progress is not None and time.monotonic() - self.last_progress < 2

    def status(self):
        return {
            "schema_version": "0.1.0",
            "robot_id": self.registry.capability.robot_id,
            "timestamp": utcnow(),
            "safety_verdict": self.safety,
            "lease_epoch": self.ledger.highest_epoch,
            "reason": self.reason,
        }

    def accepted_phases(self, step_id):
        """Intent phases already accepted for a step, in order. / 某步骤已被接受的意图相位，按顺序。"""
        return [
            command["envelope"]["payload"].get("phase")
            for command in self.ledger.commands.values()
            if command["envelope"]["key"]["step_id"] == step_id and command.get("receipt") == "accepted"
        ]

    def bind_intent(self, node, envelope):
        """The declared phase an intent belongs to, or a rejection reason. / 意图所属的声明相位，或拒绝原因。"""
        manifest = self.registry.manifests[node.skill_id]
        payload = envelope.payload
        phase = payload.get("phase") if manifest.intent_phases else None
        expected = {"skill_id": node.skill_id, "params": node.params}
        if manifest.intent_phases:
            expected["phase"] = phase
        if envelope.intent_kind != "skill" or payload != expected:
            return None, "intent_not_bound_to_package"
        if manifest.intent_phases:
            declared = [p.phase for p in manifest.intent_phases]
            if phase not in declared:
                return None, "intent_not_bound_to_package"
            done = self.accepted_phases(node.task_id)
            index = declared.index(phase)
            if (index == 0 and done) or (index > 0 and declared[index - 1] not in done):
                return None, "phase_out_of_order"
        return phase, ""

    def reject(self, envelope, reason):
        self.record("command_rejected", key=envelope.key.model_dump(mode="json"), reason=reason)
        return EgressDecision(accepted=False, reason=reason)

    async def submit(self, envelope: ControlCommandEnvelope):
        envelope = ControlCommandEnvelope.model_validate(envelope.model_dump())
        key, digest = envelope.key.as_string(), content_hash(envelope.model_dump(mode="json"))
        lease = self.gate.lease
        if lease is None or envelope.key.lease_epoch != lease.lease_epoch:
            return self.reject(envelope, "stale_epoch_or_no_lease")
        prior = self.ledger.reconcile(key)
        if prior["receipt"] != "not_received":
            if prior["digest"] != digest:
                return self.reject(envelope, "idempotency_payload_conflict")
            return EgressDecision(accepted=prior["receipt"] == "accepted", reason="duplicate:" + prior["receipt"])
        if self.safety != SafetyVerdict.PROCEED or not self.heartbeat_healthy() or not self.lease_valid():
            return self.reject(envelope, "guardian_not_proceeding")
        if self.dispatch_task and not self.dispatch_task.done():
            return self.reject(envelope, "egress_busy")
        node = next((n for n in self.package.nodes if n.task_id == envelope.key.step_id), None)
        if node is None:
            return self.reject(envelope, "intent_not_bound_to_package")
        phase, problem = self.bind_intent(node, envelope)
        if problem:
            return self.reject(envelope, problem)
        if not {r.resource_id for r in node.resources} <= set(lease.resources):
            return self.reject(envelope, "resource_outside_lease")
        if self.external is not None and (self.external.active or self.external.entering) and not (
            node is self.external.node and not external_phase(node, phase)
        ):
            # A new step ends the external stream first: stop authorizing, then PX4 Hold over MAVLink (D039).
            # 新步骤先结束外部模式：停止授权，再经 MAVLink 让 PX4 悬停（D039）。
            await self.end_external("step_handover")
        obs = self.adapter.snapshot()
        try:
            self.registry.require_preconditions(
                node,
                obs,
                self.package,
                phase=phase,
                lease_valid=lease.is_valid_at(utcnow()),
                heartbeat_ok=self.heartbeat_healthy(),
                camera_available=self.adapter.camera_available,
                overrides=self.predicate_overrides(obs),
            )
        except ValueError as error:
            return self.reject(envelope, str(error))
        decision = self.gate.check(envelope, now=utcnow())
        if not decision.accepted:
            return self.reject(envelope, decision.reason)
        self.ledger.record("intent", {"key": key, "digest": digest, "envelope": envelope.model_dump(mode="json")})
        self.active_step = node
        self.adapter.control_context = {
            "key": envelope.key.model_dump(mode="json"),
            "command_seq": envelope.command_seq,
        }
        self.phase = flight_phase(node, phase)
        if self.initial_energy is None:
            self.initial_energy = obs.battery_fraction
        generation = self.generation

        def permitted():
            if (
                self.generation != generation
                or self.safety != SafetyVerdict.PROCEED
                or self.taken_over
                or self.adapter.external_takeover
            ):
                return False
            snapshot = self.adapter.snapshot()
            predicates = self.registry.predicates(
                node,
                snapshot,
                self.package,
                lease_valid=self.lease_valid(),
                heartbeat_ok=self.heartbeat_healthy(),
                camera_available=self.adapter.camera_available,
                overrides=self.predicate_overrides(snapshot),
            )
            return all(predicates.get(name, False) for name in self.registry.manifests[node.skill_id].invariants)

        if self.external is not None and external_phase(node, phase):
            # External-mode intents enter the registered mode behind a hold authorization (D039).
            # 外部模式意图在悬停授权保护下进入已注册模式（D039）。
            dispatch = self.external.start(node, phase, permitted)
        elif phase:
            dispatch = self.adapter.execute(node, permitted, phase=phase)
        else:
            # Single-phase M1 skills keep the original adapter call. / 单相位的 M1 技能保持原有适配器调用。
            dispatch = self.adapter.execute(node, permitted)
        self.dispatch_task = asyncio.create_task(dispatch)
        receipt, reason, detail = "unknown", "dispatch_interrupted", None
        try:
            await asyncio.wait_for(
                asyncio.shield(self.dispatch_task), self.registry.data["supervision"]["command_timeout_s"]
            )
            if permitted():
                receipt, reason = "accepted", ""
        except TimeoutError:
            self.dispatch_task.cancel()
            await asyncio.gather(self.dispatch_task, return_exceptions=True)
            reason = "adapter_timeout_reconcile_required"
        except asyncio.CancelledError:
            reason = "preempted_by_recovery"
        except Exception as error:
            # The reason stays a stable class name for recovery matching; the vendor result is evidence only.
            # 原因保持稳定的类名以供恢复匹配；厂商返回的结果只作为证据记录。
            reason, detail = "adapter_error:" + type(error).__name__, str(error)[:300]
        self.ledger.record("receipt", {"key": key, "receipt": receipt, "reason": reason,
                                       **({"detail": detail} if detail else {})})
        if receipt == "unknown" and self.recovery is None:
            await self.intervene(RecoveryTrigger.USER_CANCEL, reason)
        return EgressDecision(accepted=receipt == "accepted", reason=reason)

    async def end_external(self, reason):
        """End external control for a handover: stop authorizing, then hold over MAVLink and confirm.

        为交接结束外部控制：停止授权，再经 MAVLink 悬停并确认。
        """
        if self.external is None or not (self.external.active or self.external.entering):
            return
        await self.external.stop(reason)
        try:
            await asyncio.wait_for(self.adapter.recover(RecoveryBehavior.HOLD, lambda: not self.taken_over), 5)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.adapter.snapshot().flight_mode != "HOLD":
                await asyncio.sleep(0.1)
        except Exception as error:  # noqa: BLE001 - an unknown handover outcome is a recovery, not a success / 交接结果未知即恢复
            self.record("external_handover_unknown", reason=type(error).__name__)
            await self.intervene(RecoveryTrigger.USER_CANCEL, "external_handover_unknown")

    def context(self, obs):
        if self.v2:
            return self.context_v2(obs)
        battery = obs.battery_fraction
        return {
            "flight_phase": self.phase,
            "localization_healthy": obs.localization_healthy,
            "rtl_reachable": bool(
                obs.home_healthy
                and obs.localization_healthy
                and battery is not None
                and battery > self.package.energy_budget.reserve_fraction
            ),
            "authorized_to_continue": self.authorized_to_continue,
        }

    def context_v2(self, obs):
        """v2 context (D042): control mode, visual localization when reported, energy reachability.

        v2 上下文（D042）：控制模式、有报告时的视觉定位、能源可达性。
        """
        context = {
            "flight_phase": self.phase,
            "localization_healthy": obs.localization_healthy,
            "authorized_to_continue": self.authorized_to_continue,
            "control_mode": "external_mode" if self.external.active or self.external.entering else "mission_upload",
        }
        report = self.external.localization()
        if report is not None:
            # A missing report leaves the key absent, so the visual-ok edge cannot match on unknown data.
            # 没有报告时该键缺席，视觉可用边不会在未知数据上匹配。
            context["visual_localization_ok"] = report.visual_ok
        energy = self.energy_context or {}
        context["rtl_reachable"] = bool(obs.home_healthy and obs.localization_healthy and energy.get("rtl_reachable"))
        context["nearest_site_reachable"] = bool(obs.localization_healthy and energy.get("nearest_site_reachable"))
        return context

    def update_energy(self, obs):
        if self.energy is None:
            return
        self.energy.observe(time.monotonic(), obs.battery_fraction)
        sites = {
            name: site["position"]
            for name, site in self.registry.data["landing_sites"].items()
            if site.get("reserved_for") == self.registry.capability.robot_id and name != "home_pad"
        }
        home = self.registry.data["landing_sites"]["home_pad"]["position"]
        self.energy_context = self.energy.context(
            coordinates(obs), obs.battery_fraction, self.package.energy_budget.reserve_fraction, home, sites
        )

    def own_overload(self, now):
        """The guardian's own period statistics exceed the D040 budget. / guardian 自身周期统计越过 D040 预算。"""
        limits = self.registry.data["autonomy"]["guardian_overload"]
        while self.recent_periods and now - self.recent_periods[0][0] > limits["window_s"]:
            self.recent_periods.popleft()
        periods = [value for _, value in self.recent_periods]
        if len(periods) < 5:
            return None
        slow = sum(value > limits["slow_period_s"] for value in periods) / len(periods)
        if max(periods) > limits["max_period_s"] or slow > limits["slow_fraction"]:
            return f"guardian_period_max={max(periods) * 1000:.0f}ms_slow={slow:.2f}"
        return None

    async def relinquish(self, reason):
        if self.taken_over:
            return
        self.taken_over, self.safety, self.reason = True, SafetyVerdict.ABORT, reason
        self.generation += 1
        if self.external is not None:
            await self.external.stop("relinquish:" + reason)
        self.gate.revoke()
        self.ledger.record("revoked", {"reason": reason})
        if self.dispatch_task:
            self.dispatch_task.cancel()
            await asyncio.gather(self.dispatch_task, return_exceptions=True)
        if self.recovery_task and self.recovery_task is not asyncio.current_task():
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
        self.record("safety_intervention", reason=reason, behavior="handover_to_fc_failsafe")

    async def intervene(self, trigger, reason=None, detail=None):
        if self.taken_over:
            return
        obs = self.adapter.snapshot()
        if obs.fc_failsafe or self.adapter.external_takeover:
            await self.relinquish("fc_failsafe_active" if obs.fc_failsafe else "external_mode_takeover")
            return
        edge = self.policy.select(trigger, self.context(obs))
        if edge is None or edge.target == RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE:
            await self.relinquish(reason or trigger.value)
            return
        priority = {RecoveryTrigger.USER_PAUSE: 1, RecoveryTrigger.USER_CANCEL: 2, RecoveryTrigger.ENERGY_LOW: 4}
        if self.recovery is not None:
            current_behavior = self.recovery.then if self.recovery_followed else self.recovery.target
            if (
                current_behavior in {RecoveryBehavior.RTL, RecoveryBehavior.LAND_HERE}
                and edge.target == RecoveryBehavior.HOLD
            ):
                return
        if self.recovery is not None and priority.get(trigger, 3) <= priority.get(self.recovery.trigger, 3):
            return
        context = self.context(obs)
        self.generation += 1
        self.recovery, self.recovery_started, self.recovery_followed = edge, time.monotonic(), False
        self.safety = SafetyVerdict.HOLD if edge.target == RecoveryBehavior.HOLD else SafetyVerdict.RECOVER
        self.reason = reason or trigger.value
        if self.external is not None:
            # The authorization stream ends before any MAVLink recovery command (D039). / 任何 MAVLink 恢复命令之前先结束授权流（D039）。
            await self.external.stop("intervention:" + trigger.value)
        self.recovery_site = None
        self.land_at_landing = False
        if edge.target == RecoveryBehavior.LAND_AT:
            name = (self.energy_context or {}).get("nearest_site")
            self.recovery_site = self.registry.data["landing_sites"].get(name, {}).get("position") if name else None
        extra = {"detail": detail} if detail else {}
        if self.v2:
            extra["context"] = {k: v for k, v in context.items() if isinstance(v, (bool, str))}
            if self.recovery_site is not None:
                extra["site"] = self.recovery_site
        self.record(
            "safety_intervention",
            reason=self.reason,
            behavior=edge.target.value,
            edge_hash=edge.validation_hash(),
            epoch=self.ledger.highest_epoch,
            **extra,
        )
        if self.dispatch_task and not self.dispatch_task.done():
            self.dispatch_task.cancel()
            await asyncio.gather(self.dispatch_task, return_exceptions=True)
        if self.recovery_task and not self.recovery_task.done():
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
        self.recovery_task = asyncio.create_task(self._recover(edge.target))

    async def _recover(self, behavior):
        if self.taken_over:
            return
        obs = self.adapter.snapshot()
        if obs.in_air is False and obs.armed is False:
            self.record("recovered_to", state="landed_disarmed")
            return
        self.adapter.control_context = {"lease_epoch": self.ledger.highest_epoch, "command_seq": len(self.journal.rows)}
        self.record(
            "recovery_intent",
            behavior=behavior.value,
            lease_epoch=self.ledger.highest_epoch,
            command_seq=len(self.journal.rows),
        )
        try:
            permitted = lambda: (  # noqa: E731
                not self.taken_over
                and not self.adapter.external_takeover
                and not self.adapter.snapshot().fc_failsafe
            )
            if behavior == RecoveryBehavior.LAND_AT:
                if self.recovery_site is None:
                    raise ValueError("land_at without a reachable registered site")
                await asyncio.wait_for(self.adapter.recover(behavior, permitted, site=self.recovery_site), 5)
            else:
                await asyncio.wait_for(self.adapter.recover(behavior, permitted), 5)
            self.record("recovery_receipt", behavior=behavior.value, status="accepted")
        except Exception as error:
            self.record("recovery_receipt", behavior=behavior.value, status="unknown", reason=type(error).__name__)
            await self.relinquish("recovery_outcome_unknown")

    async def operate(self, operation):
        lease = self.gate.lease
        if (
            not lease
            or not self.lease_valid()
            or any(
                [
                    operation.robot_id != lease.robot_id,
                    operation.mission_id != lease.mission_id,
                    operation.mission_version != lease.mission_version,
                    operation.lease_epoch != lease.lease_epoch,
                    operation.executive_instance != self.executive_id,
                ]
            )
        ):
            raise ValueError("operation identity mismatch")
        if operation.request_id in self.operation_ids:
            return self.status()
        if operation.action == MissionAction.RESUME:
            obs = self.adapter.snapshot()
            if (
                self.taken_over
                or self.recovery is None
                or self.recovery.trigger != RecoveryTrigger.USER_PAUSE
                or self.recovery_followed
                or not self.heartbeat_healthy()
                or utcnow() >= obs.valid_until
                or not obs.localization_healthy
                or not obs.battery_fraction
                or obs.battery_fraction <= self.package.energy_budget.reserve_fraction + 0.1
                or obs.flight_mode != "HOLD"
            ):
                raise ValueError("resume conditions not satisfied")
            if self.recovery_task and not self.recovery_task.done():
                raise ValueError("hold receipt pending")
            self.adapter.control_context = {
                "lease_epoch": self.ledger.highest_epoch,
                "command_seq": len(self.journal.rows),
            }
            self.record("resume_authorized", request_id=operation.request_id, **self.adapter.control_context)
            self.operation_ids.add(operation.request_id)
            generation = self.generation

            def permitted():
                return (
                    generation == self.generation
                    and not self.taken_over
                    and self.lease_valid()
                    and self.heartbeat_healthy()
                    and not self.recovery_followed
                )

            # Resume only the already uploaded route; do not upload it again. / 仅恢复已上传航线，不重新上传。
            try:
                await asyncio.wait_for(self.adapter.resume(permitted), 5)
            except Exception:
                await self.intervene(RecoveryTrigger.USER_CANCEL, "resume_outcome_unknown")
            else:
                if permitted():
                    self.recovery, self.safety, self.reason = None, SafetyVerdict.PROCEED, ""
        elif operation.action == MissionAction.PAUSE:
            if not self.active_step or not self.registry.manifests[self.active_step.skill_id].pause.pausable:
                raise ValueError("active skill is not pausable")
            await self.intervene(RecoveryTrigger.USER_PAUSE)
        else:
            self.record("cancel_requested", request_id=operation.request_id)
            await self.intervene(RecoveryTrigger.USER_CANCEL)
        self.operation_ids.add(operation.request_id)
        return self.status()

    async def tick(self):
        now = time.monotonic()
        if self.last_tick is not None:
            self.periods.append(now - self.last_tick)
            self.recent_periods.append((now, now - self.last_tick))
        self.last_tick = now
        obs = self.adapter.snapshot()
        if self.taken_over or self.ledger.highest_epoch < 0:
            return
        if obs.fc_failsafe:
            await self.relinquish("fc_failsafe_active")
            return
        if self.adapter.external_takeover:
            await self.relinquish("external_mode_takeover")
            return
        if self.recovery and self.recovery.then and not self.recovery_followed:
            if now - self.recovery_started >= self.recovery.after_s:
                self.recovery_followed = True
                self.safety = SafetyVerdict.RECOVER
                self.recovery_task = asyncio.create_task(self._recover(self.recovery.then))
        if obs.in_air is False and obs.armed is False and self.recovery:
            if self.reason != "landed_disarmed":
                self.record("recovered_to", state="landed_disarmed")
                self.reason = "landed_disarmed"
            return
        self.update_energy(obs)
        if (
            self.recovery is not None
            and self.recovery.target == RecoveryBehavior.LAND_AT
            and not self.recovery_followed
            and not self.land_at_landing
            and self.recovery_site is not None
        ):
            point = coordinates(obs)
            site = next(
                (s for s in self.registry.data["landing_sites"].values() if s["position"] == self.recovery_site), None
            )
            if point and site and ((point[0] - site["position"][0]) ** 2 + (point[1] - site["position"][1]) ** 2) ** 0.5 <= site["radius_m"] / 2:
                # Above the registered site: complete land_at with a native landing. / 到达登记降落点上方：以原生降落完成 land_at。
                self.land_at_landing = True
                self.record("land_at_site_reached", site=self.recovery_site)
                self.recovery_task = asyncio.create_task(self._recover(RecoveryBehavior.LAND_HERE))
        if self.active_step is None:
            return
        if not self.package.is_authorized(robot_id=self.registry.capability.robot_id, now=utcnow()):
            await self.intervene(RecoveryTrigger.USER_CANCEL, "mission_authorization_expired")
        if self.phase == "takeoff" and coordinates(obs) and coordinates(obs)[2] >= 3:
            self.phase = "hover"
        checks = []
        if not obs.timestamp <= utcnow() < obs.valid_until:
            checks.append(RecoveryTrigger.OBSERVATION_STALE)
        elif not obs.localization_healthy:
            checks.append(RecoveryTrigger.LOCALIZATION_DEGRADED)
        if not self.heartbeat_healthy():
            checks.append(RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST)
        if not self.lease_valid():
            checks.append(RecoveryTrigger.LEASE_EXPIRED)
        if not self.uplink_ok and not self.authorized_to_continue:
            checks.append(RecoveryTrigger.UPLINK_LOST)
        details = {}
        if self.v2:
            report = self.external.localization()
            if (
                report is not None
                and not report.gnss_ok
                and RecoveryTrigger.LOCALIZATION_DEGRADED not in checks
                and RecoveryTrigger.OBSERVATION_STALE not in checks
            ):
                # v2: GNSS loss degrades localization even while the estimator copes on vision (D042).
                # v2：即使估计器靠视觉维持，GNSS 丢失也属于定位退化（D042）。
                checks.append(RecoveryTrigger.LOCALIZATION_DEGRADED)
                details[RecoveryTrigger.LOCALIZATION_DEGRADED] = "gnss_lost"
            overload = self.own_overload(now)
            if overload:
                checks.append(RecoveryTrigger.COMPUTE_OVERLOADED)
                details[RecoveryTrigger.COMPUTE_OVERLOADED] = overload
            if self.external.active and RecoveryTrigger.OBSERVATION_STALE not in checks:
                for trigger, detail in await self.external.tick(obs):
                    if trigger is None:
                        self.adapter.external_takeover = True
                        await self.relinquish("external_mode_takeover")
                        return
                    checks.append(trigger)
                    details[trigger] = detail
        energy = obs.battery_fraction
        return_due = bool(self.v2 and (self.energy_context or {}).get("return_due"))
        if (
            energy is None
            or energy
            <= self.package.energy_budget.reserve_fraction + self.registry.data["supervision"]["return_margin_fraction"]
            or return_due
        ):
            checks.insert(0, RecoveryTrigger.ENERGY_LOW)
        elif (
            self.initial_energy is not None
            and self.initial_energy - energy > self.package.energy_budget.max_consumption_fraction
        ):
            checks.insert(0, RecoveryTrigger.ENERGY_LOW)
        point = coordinates(obs)
        if point and obs.velocity_enu_mps and not self.recovery:
            predicted = [
                p + v * self.registry.data["supervision"]["prediction_s"]
                for p, v in zip(point, obs.velocity_enu_mps, strict=True)
            ]
            if self.active_step.skill_id == "skill.flight.land" and obs.flight_mode == "LAND":
                site = self.registry.data["landing_sites"].get(self.active_step.params["landing_site_id"])
                if site and site["reserved_for"] == self.registry.capability.robot_id:
                    from math import dist

                    if dist(point[:2], site["position"][:2]) <= site["radius_m"]:
                        predicted[2] = max(site["position"][2], predicted[2])
            if not self.registry.inside(point) or not self.registry.inside(predicted):
                checks.insert(0, RecoveryTrigger.GEOFENCE_PREDICTED_BREACH)
        for trigger in checks:
            await self.intervene(trigger, detail=details.get(trigger))

    async def supervise(self):
        deadline = time.monotonic()
        period = self.registry.data["supervision"]["period_s"]
        while True:
            await self.tick()
            deadline += period
            if deadline < time.monotonic():
                deadline = time.monotonic()
            await asyncio.sleep(max(0, deadline - time.monotonic()))
