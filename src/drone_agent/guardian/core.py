"""Mission-bound egress with durable receipts and preemptive recovery.

绑定任务的控制出口，具备持久化回执与抢占式恢复。
"""

from __future__ import annotations

import asyncio
import time

from drone_agent.contracts import (
    ControlCommandEnvelope,
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
from drone_agent.mission.registry import coordinates
from drone_agent.runtime.ledger import CommandLedger, content_hash


class Guardian:
    def __init__(self, *, adapter, package, registry, journal, policy: RecoveryPolicy, executive_id: str):
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
        self.uplink_ok = True
        self.authorized_to_continue = True
        self.registry.validate_package(package, camera_available=adapter.camera_available)

    def record(self, kind, **data):
        return self.journal.append(kind, {"mission_id": self.package.mission_id, **data})

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
        try:
            self.gate.grant(lease)
        except ValueError as error:
            return EgressDecision(accepted=False, reason=str(error))
        self.ledger.record("lease", lease.model_dump(mode="json"))
        return EgressDecision(accepted=True)

    def heartbeat(self, pulse: dict):
        lease = self.gate.lease
        now = utcnow()
        if not lease or not lease.is_valid_at(now):
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
        if self.safety != SafetyVerdict.PROCEED or not self.heartbeat_healthy():
            return self.reject(envelope, "guardian_not_proceeding")
        if self.dispatch_task and not self.dispatch_task.done():
            return self.reject(envelope, "egress_busy")
        node = next((n for n in self.package.nodes if n.task_id == envelope.key.step_id), None)
        if (
            node is None
            or envelope.intent_kind != "skill"
            or envelope.payload != {"skill_id": node.skill_id, "params": node.params}
        ):
            return self.reject(envelope, "intent_not_bound_to_package")
        if not {r.resource_id for r in node.resources} <= set(lease.resources):
            return self.reject(envelope, "resource_outside_lease")
        obs = self.adapter.snapshot()
        try:
            self.registry.require_preconditions(
                node,
                obs,
                self.package,
                lease_valid=lease.is_valid_at(utcnow()),
                heartbeat_ok=self.heartbeat_healthy(),
                camera_available=self.adapter.camera_available,
            )
        except ValueError as error:
            return self.reject(envelope, str(error))
        decision = self.gate.check(envelope, now=utcnow())
        if not decision.accepted:
            return self.reject(envelope, decision.reason)
        self.ledger.record("intent", {"key": key, "digest": digest, "envelope": envelope.model_dump(mode="json")})
        self.active_step = node
        self.phase = {"takeoff": "takeoff", "capture_image": "inspect", "land": "landing"}.get(
            node.skill_id.rsplit(".", 1)[1], "cruise"
        )
        if self.initial_energy is None:
            self.initial_energy = obs.battery_fraction
        generation = self.generation

        def permitted():
            return self.generation == generation and self.safety == SafetyVerdict.PROCEED and not self.taken_over

        self.dispatch_task = asyncio.create_task(self.adapter.execute(node, permitted))
        receipt, reason = "unknown", "dispatch_interrupted"
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
            reason = "adapter_error:" + type(error).__name__
        self.ledger.record("receipt", {"key": key, "receipt": receipt, "reason": reason})
        if receipt == "unknown" and self.recovery is None:
            await self.intervene(RecoveryTrigger.USER_CANCEL, reason)
        return EgressDecision(accepted=receipt == "accepted", reason=reason)

    def context(self, obs):
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

    async def relinquish(self, reason):
        if self.taken_over:
            return
        self.taken_over, self.safety, self.reason = True, SafetyVerdict.ABORT, reason
        self.generation += 1
        self.gate.revoke()
        self.ledger.record("revoked", {"reason": reason})
        if self.dispatch_task:
            self.dispatch_task.cancel()
            await asyncio.gather(self.dispatch_task, return_exceptions=True)
        if self.recovery_task and self.recovery_task is not asyncio.current_task():
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
        self.record("safety_intervention", reason=reason, behavior="handover_to_fc_failsafe")

    async def intervene(self, trigger, reason=None):
        if self.taken_over:
            return
        obs = self.adapter.snapshot()
        edge = self.policy.select(trigger, self.context(obs))
        if edge is None or edge.target == RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE:
            await self.relinquish(reason or trigger.value)
            return
        priority = {RecoveryTrigger.USER_PAUSE: 1, RecoveryTrigger.USER_CANCEL: 2, RecoveryTrigger.ENERGY_LOW: 4}
        if self.recovery is not None and priority.get(trigger, 3) <= priority.get(self.recovery.trigger, 3):
            return
        self.generation += 1
        self.recovery, self.recovery_started, self.recovery_followed = edge, time.monotonic(), False
        self.safety = SafetyVerdict.HOLD if edge.target == RecoveryBehavior.HOLD else SafetyVerdict.RECOVER
        self.reason = reason or trigger.value
        self.record(
            "safety_intervention",
            reason=self.reason,
            behavior=edge.target.value,
            edge_hash=edge.validation_hash(),
            epoch=self.ledger.highest_epoch,
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
        self.record(
            "recovery_intent",
            behavior=behavior.value,
            lease_epoch=self.ledger.highest_epoch,
            command_seq=len(self.journal.rows),
        )
        try:
            await asyncio.wait_for(self.adapter.recover(behavior, lambda: not self.taken_over), 5)
            self.record("recovery_receipt", behavior=behavior.value, status="accepted")
        except Exception as error:
            self.record("recovery_receipt", behavior=behavior.value, status="unknown", reason=type(error).__name__)
            await self.relinquish("recovery_outcome_unknown")

    async def operate(self, operation):
        lease = self.gate.lease
        if (
            not lease
            or not lease.is_valid_at(utcnow())
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
            self.record("resume_authorized", request_id=operation.request_id)
            # Resume only the already uploaded route; do not upload it again. / 仅恢复已上传航线，不重新上传。
            await self.adapter.resume(lambda: not self.taken_over)
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
        if self.active_step is None:
            return
        if self.phase == "takeoff" and coordinates(obs) and coordinates(obs)[2] >= 3:
            self.phase = "hover"
        checks = []
        if utcnow() >= obs.valid_until:
            checks.append(RecoveryTrigger.OBSERVATION_STALE)
        elif not obs.localization_healthy:
            checks.append(RecoveryTrigger.LOCALIZATION_DEGRADED)
        if not self.heartbeat_healthy():
            checks.append(RecoveryTrigger.EXECUTIVE_HEARTBEAT_LOST)
        lease = self.gate.lease
        if not lease or not lease.is_valid_at(utcnow()):
            checks.append(RecoveryTrigger.LEASE_EXPIRED)
        if not self.uplink_ok and not self.authorized_to_continue:
            checks.append(RecoveryTrigger.UPLINK_LOST)
        energy = obs.battery_fraction
        if (
            energy is None
            or energy
            <= self.package.energy_budget.reserve_fraction + self.registry.data["supervision"]["return_margin_fraction"]
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
            if not self.registry.inside(point) or not self.registry.inside(predicted):
                checks.insert(0, RecoveryTrigger.GEOFENCE_PREDICTED_BREACH)
        for trigger in checks:
            await self.intervene(trigger)

    async def supervise(self):
        while True:
            await self.tick()
            await asyncio.sleep(self.registry.data["supervision"]["period_s"])
