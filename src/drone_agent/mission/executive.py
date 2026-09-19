"""Mission DAG and skill lifecycles with evidence-gated successors.

任务 DAG 与技能生命周期；证据门控后继步骤。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta

import grpc

from drone_agent.contracts import (
    ControlCommandEnvelope,
    EffectVerdict,
    ExecutionStatus,
    IdempotencyKey,
    MissionAction,
    MissionOperation,
    SafetyVerdict,
    SkillInstanceState,
    StepOutcome,
    TaskLease,
    can_transition,
    may_run_successor,
    utcnow,
)
from drone_agent.mission.verify import EffectVerifier, verify_image
from drone_agent.runtime.ledger import canonical


class Executive:
    def __init__(self, *, client, registry, package, journal, recorder, artifacts, executive_id, epoch=1):
        self.client, self.registry, self.package = client, registry, package
        self.journal, self.recorder, self.artifacts = journal, recorder, artifacts
        self.executive_id, self.epoch = executive_id, epoch
        self.outcomes, self.states = {}, {}
        self.seq = self.pulse_seq = 0
        self.lease = None
        self.status = {"safety_verdict": "hold", "reason": "initializing"}
        self.last_pulse = 0
        self.control_path = artifacts / "operator.json"
        self.operator_ids = set()
        self.node = None
        self.aborted = False
        self.registry.validate_package(package)
        # A restarted executive never silently re-runs an accepted mission. / executive 重启后不静默重跑已接受任务。
        if journal.rows:
            raise ValueError("existing mission journal requires reconciliation")

    def event(self, kind, **data):
        row = self.journal.append(kind, {"mission_id": self.package.mission_id, **data})
        self.recorder.write("mission/events", row)

    def transition(self, node, target):
        previous = self.states.get(node.task_id, SkillInstanceState.ACCEPTED)
        if not can_transition(previous, target):
            raise ValueError(f"illegal lifecycle transition: {previous} -> {target}")
        self.states[node.task_id] = target
        self.event("skill_state", step_id=node.task_id, previous=previous.value, state=target.value)

    async def start(self):
        now = utcnow()
        self.lease = TaskLease(
            robot_id=self.registry.capability.robot_id,
            mission_id=self.package.mission_id,
            mission_version=self.package.mission_version,
            lease_epoch=self.epoch,
            holder=self.executive_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=10),
            resources=sorted({r.resource_id for n in self.package.nodes for r in n.resources}),
        )
        result = await self.client.install(self.lease)
        if not result.get("accepted"):
            raise ValueError("lease rejected: " + result.get("reason", "unknown"))
        self.event("mission_accepted", package_hash=self.package.package_hash, registry_hash=self.registry.sha256)

    async def pump(self):
        now = utcnow()
        if (self.lease.expires_at - now).total_seconds() < 5:
            self.lease.expires_at = now + timedelta(seconds=10)
            result = await self.client.install(self.lease)
            if not result.get("accepted"):
                self.aborted = True
                self.status = {"safety_verdict": "recover", "reason": "lease_rejected"}
                return await self.client.observation(self.lease.robot_id)
        if time.monotonic() - self.last_pulse >= 0.2:
            self.pulse_seq += 1
            try:
                self.status = await self.client.heartbeat(
                    {
                        "schema_version": "0.1.0",
                        "robot_id": self.lease.robot_id,
                        "executive_instance": self.executive_id,
                        "mission_id": self.package.mission_id,
                        "mission_version": self.package.mission_version,
                        "lease_epoch": self.epoch,
                        "heartbeat_seq": self.pulse_seq,
                        "progress_seq": self.pulse_seq,
                        "timestamp": now,
                        "valid_until": now + timedelta(seconds=1),
                        "skill_instance_id": self.node.task_id if self.node else "idle",
                        "skill_state": self.states.get(self.node.task_id, SkillInstanceState.ACCEPTED)
                        if self.node
                        else SkillInstanceState.ACCEPTED,
                    }
                )
            except grpc.aio.AioRpcError:
                self.aborted = True
                self.status = {"safety_verdict": "recover", "reason": "guardian_authority_unavailable"}
            self.last_pulse = time.monotonic()
        obs = await self.client.observation(self.lease.robot_id)
        self.recorder.write("flight/observation", obs.model_dump(mode="json"))
        if self.control_path.exists():
            control = json.loads(self.control_path.read_text())
            request_id = control["request_id"]
            if request_id not in self.operator_ids:
                action = MissionAction(control["action"])
                if action == MissionAction.CANCEL and self.node:
                    self.transition(self.node, SkillInstanceState.CANCEL_REQUESTED)
                if action == MissionAction.PAUSE and self.node:
                    self.transition(self.node, SkillInstanceState.PAUSE_REQUESTED)
                operation = MissionOperation(
                    robot_id=self.lease.robot_id,
                    mission_id=self.package.mission_id,
                    mission_version=self.package.mission_version,
                    lease_epoch=self.epoch,
                    executive_instance=self.executive_id,
                    action=action,
                    request_id=request_id,
                )
                self.status = await self.client.operate(operation)
                self.operator_ids.add(request_id)
                self.event("operator_request", **control)
                if action == MissionAction.RESUME and self.node:
                    self.transition(self.node, SkillInstanceState.RUNNING)
        return obs

    async def send(self, envelope):
        request = asyncio.create_task(self.client.submit(envelope))
        while not request.done():
            await self.pump()
            await asyncio.sleep(0.1)
        try:
            result = request.result()
            if result.get("accepted"):
                return True
            self.event("command_rejected", reason=result.get("reason", "unknown"))
            return False
        except grpc.aio.AioRpcError as error:
            if error.code() not in {grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE}:
                raise
        # Query receipts, never turn a transport timeout into a successful effect. / 查询回执；永不把传输超时转成效果成功。
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            receipt = await self.client.reconcile(envelope.key)
            self.event(
                "command_reconciled", key=envelope.key.model_dump(mode="json"), receipt=receipt["receipt_status"]
            )
            if receipt["receipt_status"] == "recorded":
                return True
            await self.pump()
            await asyncio.sleep(0.1)
        return False

    async def execute_node(self, node):
        self.node = node
        self.transition(node, SkillInstanceState.PREPARING)
        await self.pump()
        self.transition(node, SkillInstanceState.RUNNING)
        now = utcnow()
        envelope = ControlCommandEnvelope(
            key=IdempotencyKey(
                mission_id=self.package.mission_id,
                mission_version=self.package.mission_version,
                step_id=node.task_id,
                command_id=uuid.uuid4().hex,
                robot_id=node.robot_id,
                lease_epoch=self.epoch,
            ),
            command_seq=self.seq,
            issued_at=now,
            valid_until=now + timedelta(seconds=2),
            intent_kind="skill",
            payload={"skill_id": node.skill_id, "params": node.params},
        )
        self.seq += 1
        self.event("command_submitted", envelope=envelope.model_dump(mode="json"))
        accepted = await self.send(envelope)
        verdict, execution = EffectVerdict.UNKNOWN, ExecutionStatus.UNKNOWN
        verifier = EffectVerifier(self.registry, node)
        deadline = time.monotonic() + node.timeout_s
        captures = 1
        last_capture = time.monotonic()
        while accepted and time.monotonic() < deadline:
            obs = await self.pump()
            state = self.states[node.task_id]
            safety = self.status["safety_verdict"]
            if state == SkillInstanceState.PAUSE_REQUESTED and obs.flight_mode == "HOLD":
                self.transition(node, SkillInstanceState.PAUSED)
            if safety != "proceed":
                if self.status.get("reason") == "user_pause":
                    await asyncio.sleep(0.1)
                    continue
                self.aborted = True
                if state == SkillInstanceState.CANCEL_REQUESTED:
                    self.transition(node, SkillInstanceState.RECOVERING)
                break
            if verifier.action == "capture_image":
                path = self.artifacts / f"evidence-{node.task_id}.json"
                verdict = (
                    verify_image(json.loads(path.read_text()), self.artifacts, node, self.registry)
                    if path.exists()
                    else EffectVerdict.UNKNOWN
                )
                if verdict == EffectVerdict.UNVERIFIED and captures < 3 and time.monotonic() - last_capture >= 1:
                    now = utcnow()
                    envelope = envelope.model_copy(deep=True)
                    envelope.key.command_id = uuid.uuid4().hex
                    envelope.command_seq = self.seq
                    envelope.valid_until = now + timedelta(seconds=2)
                    envelope.issued_at = now
                    self.seq += 1
                    captures += 1
                    self.event("command_submitted", envelope=envelope.model_dump(mode="json"), capture_attempt=captures)
                    accepted = await self.send(envelope)
                    last_capture = time.monotonic()
                    continue
                if verdict == EffectVerdict.UNVERIFIED and captures == 3:
                    execution = ExecutionStatus.FAILED
                    break
            else:
                verdict = verifier.observe(obs, time.monotonic())
            if verdict == EffectVerdict.VERIFIED:
                execution = ExecutionStatus.SUCCEEDED
                break
            if verdict == EffectVerdict.REFUTED:
                execution = ExecutionStatus.FAILED
                break
            await asyncio.sleep(0.1)
        if execution != ExecutionStatus.SUCCEEDED:
            self.aborted = True
            if self.status["safety_verdict"] == "proceed":
                await self.client.operate(
                    MissionOperation(
                        robot_id=node.robot_id,
                        mission_id=self.package.mission_id,
                        mission_version=self.package.mission_version,
                        lease_epoch=self.epoch,
                        executive_instance=self.executive_id,
                        action=MissionAction.CANCEL,
                        request_id=uuid.uuid4().hex,
                    )
                )
            target = (
                SkillInstanceState.OUTCOME_UNKNOWN if verdict == EffectVerdict.UNKNOWN else SkillInstanceState.FAILED
            )
        else:
            target = SkillInstanceState.COMPLETED
        current = self.states[node.task_id]
        if target == SkillInstanceState.COMPLETED and current == SkillInstanceState.RUNNING:
            self.transition(node, SkillInstanceState.VERIFYING)
        if can_transition(self.states[node.task_id], target):
            self.transition(node, target)
        outcome = StepOutcome(
            mission_id=self.package.mission_id,
            mission_version=self.package.mission_version,
            step_id=node.task_id,
            robot_id=node.robot_id,
            execution_status=execution,
            effect_verdict=verdict,
            safety_verdict=SafetyVerdict(self.status["safety_verdict"]),
        )
        self.outcomes[node.task_id] = outcome
        self.recorder.flush()
        self.event("step_outcome", outcome=outcome.model_dump(mode="json"), waypoints_verified=verifier.waypoint)

    async def run(self):
        await self.start()
        pending = list(self.package.nodes)
        while pending and not self.aborted:
            ready = [
                node
                for node in pending
                if all(
                    dep in self.outcomes
                    and may_run_successor(
                        self.outcomes[dep],
                        current_safety=SafetyVerdict(self.status["safety_verdict"]),
                        edge_allows_unverified=dep in node.allow_unverified_from,
                    )
                    for dep in node.depends_on
                )
            ]
            if not ready:
                break
            # Serial scheduling is a valid conflict-free subset of the DAG. / 串行调度是无资源冲突的 DAG 合法子集。
            node = ready[0]
            await self.execute_node(node)
            pending.remove(node)
        result = {
            "mission_id": self.package.mission_id,
            "completed": not pending and all(o.counts_as_completed for o in self.outcomes.values()),
            "outcomes": {key: value.model_dump(mode="json") for key, value in self.outcomes.items()},
            "not_run": [n.task_id for n in pending],
        }
        self.event("mission_result", **result)
        (self.artifacts / "result.json").write_bytes(canonical(result))
        return result
