"""Mission DAG and skill lifecycles with evidence-gated successors.

M2 adds signature verification in signed mode (D030) and a phased path for multi-phase skills such as
`skill.inspect.asset` (D034): the approach intent runs until its registered route is verified, then
capture intents run with at most `max_captures` attempts; running out of attempts ends the step
`unverified`, never succeeded. Single-phase M1 skills keep their original flow.

任务 DAG 与技能生命周期；证据门控后继步骤。

M2 在签名模式下增加验签（D030），并为 `skill.inspect.asset` 等多相位技能增加相位化流程（D034）：接近
意图执行到登记航线被证实为止，然后执行拍摄意图，最多 `max_captures` 次；次数用尽即以 `unverified`
结束，绝不记为成功。单相位的 M1 技能保持原流程。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta

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
from drone_agent.mission.verify import EffectVerifier, verify_asset_image, verify_image
from drone_agent.runtime.ledger import canonical
from drone_agent.runtime.signing import verify_package


class Executive:
    def __init__(self, *, client, registry, package, journal, recorder, artifacts, executive_id, epoch=1, trust=None,
                 mailbox=None):
        # Signed mode (M2): verify independently of the guardian and the uplink. / 签名模式（M2）：独立于 guardian 与 uplink 验签。
        self.signer_key_id = (
            verify_package(package, trust, robot_id=registry.capability.robot_id, now=utcnow()) if trust else None
        )
        self.client, self.registry, self.package = client, registry, package
        self.journal, self.recorder, self.artifacts = journal, recorder, artifacts
        self.executive_id, self.epoch = executive_id, epoch
        self.outcomes, self.states = {}, {}
        self.seq = self.pulse_seq = 0
        self.lease = None
        self.status = {"safety_verdict": "hold", "reason": "initializing"}
        self.last_pulse = 0
        # M2 (D031): the uplink writes the mailbox in its own volume; M1 keeps it beside the artifacts.
        # M2（D031）：uplink 在自己的卷里写信箱；M1 仍放在产物目录旁。
        self.control_path = mailbox or artifacts / "operator.json"
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
        signed = {"signer_key_id": self.signer_key_id} if self.signer_key_id else {}
        self.event("mission_accepted", package_hash=self.package.package_hash, registry_hash=self.registry.sha256,
                   **signed)

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
            await self.handle_operator(json.loads(self.control_path.read_text()))
        return obs

    async def handle_operator(self, control):
        """Reject late or invalid operator requests without disrupting the running skill.

        拒绝迟到或无效的操作者请求，不干扰正在执行的技能。
        """
        request_id = control.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in self.operator_ids:
            return
        self.operator_ids.add(request_id)
        try:
            action = MissionAction(control["action"])
            if self.node is None:
                raise ValueError("no_active_skill")
            binding = {
                "mission_id": self.package.mission_id,
                "mission_version": self.package.mission_version,
                "lease_epoch": self.epoch,
                "step_id": self.node.task_id,
            }
            if any(key in control for key in (*binding, "valid_until")):
                if any(control.get(key) != value for key, value in binding.items()):
                    raise ValueError("operator_target_changed")
                if not isinstance(control.get("valid_until"), str):
                    raise ValueError("operator_validity_missing")
                valid_until = datetime.fromisoformat(control["valid_until"].replace("Z", "+00:00"))
                if valid_until.tzinfo is None or utcnow() >= valid_until:
                    raise ValueError("operator_request_expired")
            state = self.states.get(self.node.task_id, SkillInstanceState.ACCEPTED)
            target = {
                MissionAction.PAUSE: SkillInstanceState.PAUSE_REQUESTED,
                MissionAction.RESUME: SkillInstanceState.RUNNING,
                MissionAction.CANCEL: SkillInstanceState.CANCEL_REQUESTED,
            }[action]
            if action == MissionAction.RESUME and state != SkillInstanceState.PAUSED:
                raise ValueError("skill_not_paused")
            if action == MissionAction.PAUSE and state != SkillInstanceState.RUNNING:
                raise ValueError("skill_not_running")
            if not can_transition(state, target):
                raise ValueError("operator_lifecycle_conflict")
            operation = MissionOperation(
                robot_id=self.lease.robot_id,
                mission_id=self.package.mission_id,
                mission_version=self.package.mission_version,
                lease_epoch=self.epoch,
                executive_instance=self.executive_id,
                action=action,
                request_id=request_id,
            )
            status = await self.client.operate(operation)
        except (ValueError, KeyError, TypeError) as error:
            self.event("operator_rejected", request_id=request_id, action=control.get("action"), reason=str(error))
            return
        except grpc.aio.AioRpcError as error:
            uncertain = error.code() in {grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE}
            self.event(
                "operator_rejected", request_id=request_id, action=control.get("action"),
                reason="outcome_unknown" if uncertain else "guardian_rejected",
            )
            if uncertain:
                self.aborted = True
                self.status = {"safety_verdict": "recover", "reason": "operator_outcome_unknown"}
            return
        self.status = status
        if action == MissionAction.PAUSE and (status["safety_verdict"] != "hold" or status.get("reason") != "user_pause"):
            self.event("operator_rejected", request_id=request_id, action=action.value, reason="pause_not_authorized")
            return
        if action == MissionAction.RESUME and status["safety_verdict"] != "proceed":
            self.event("operator_rejected", request_id=request_id, action=action.value, reason="resume_not_authorized")
            return
        # Change lifecycle only after the guardian accepts the request. / guardian 接受请求后才改变生命周期。
        self.transition(self.node, target)
        self.event("operator_request", **control, accepted=True)

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
        cancelled_before_dispatch = self.states[node.task_id] == SkillInstanceState.CANCEL_REQUESTED
        self.transition(
            node, SkillInstanceState.RECOVERING if cancelled_before_dispatch else SkillInstanceState.RUNNING
        )
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
        accepted = False
        if not cancelled_before_dispatch:
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
                if self.status.get("reason") == "user_pause" and safety == "hold":
                    await asyncio.sleep(0.1)
                    continue
                self.aborted = True
                if state == SkillInstanceState.CANCEL_REQUESTED:
                    self.transition(node, SkillInstanceState.RECOVERING)
                elif state in {SkillInstanceState.PAUSED, SkillInstanceState.PAUSE_REQUESTED}:
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
        await self._conclude(node, execution, verdict, verifier.waypoint)

    async def _conclude(self, node, execution, verdict, waypoints):
        """Shared end of a step: cancellation settling, abort, lifecycle and the three-way outcome.

        步骤的共同收尾：取消收敛、中止、生命周期与三元结果。
        """
        if self.states[node.task_id] == SkillInstanceState.RECOVERING and any(
            row["kind"] == "operator_request" and row["data"].get("action") == "cancel" for row in self.journal.rows
        ):
            safe_since = None
            recovery_deadline = time.monotonic() + 180
            while time.monotonic() < recovery_deadline:
                obs = await self.pump()
                if obs.timestamp <= utcnow() < obs.valid_until and obs.armed is False and obs.in_air is False:
                    safe_since = time.monotonic() if safe_since is None else safe_since
                    if time.monotonic() - safe_since >= 2:
                        execution, verdict = ExecutionStatus.CANCELLED, EffectVerdict.UNKNOWN
                        break
                else:
                    safe_since = None
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
                SkillInstanceState.CANCELLED
                if execution == ExecutionStatus.CANCELLED
                else SkillInstanceState.OUTCOME_UNKNOWN
                if verdict == EffectVerdict.UNKNOWN
                else SkillInstanceState.FAILED
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
        self.event("step_outcome", outcome=outcome.model_dump(mode="json"), waypoints_verified=waypoints)

    def _gate(self, node, obs):
        """The per-cycle safety gate of the M1 loop: None, "wait" or "stop".

        M1 循环中每个周期的安全闸门：None、"wait" 或 "stop"。
        """
        state = self.states[node.task_id]
        safety = self.status["safety_verdict"]
        if state == SkillInstanceState.PAUSE_REQUESTED and obs.flight_mode == "HOLD":
            self.transition(node, SkillInstanceState.PAUSED)
        if safety != "proceed":
            if self.status.get("reason") == "user_pause" and safety == "hold":
                return "wait"
            self.aborted = True
            if state == SkillInstanceState.CANCEL_REQUESTED:
                self.transition(node, SkillInstanceState.RECOVERING)
            elif state in {SkillInstanceState.PAUSED, SkillInstanceState.PAUSE_REQUESTED}:
                self.transition(node, SkillInstanceState.RECOVERING)
            return "stop"
        return None

    async def send_phase(self, node, phase, attempt=None):
        """Submit one phase-bound intent for a multi-phase skill. / 为多相位技能提交一个绑定相位的意图。"""
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
            payload={"skill_id": node.skill_id, "params": node.params, "phase": phase},
        )
        self.seq += 1
        extra = {"capture_attempt": attempt} if attempt is not None else {}
        self.event("command_submitted", envelope=envelope.model_dump(mode="json"), phase=phase, **extra)
        return await self.send(envelope)

    async def execute_phased(self, node):
        """Approach until the registered route is verified, then capture with bounded retakes.

        接近直到登记航线被证实，然后拍摄，补拍有上限。
        """
        self.node = node
        self.transition(node, SkillInstanceState.PREPARING)
        await self.pump()
        cancelled_before_dispatch = self.states[node.task_id] == SkillInstanceState.CANCEL_REQUESTED
        self.transition(
            node, SkillInstanceState.RECOVERING if cancelled_before_dispatch else SkillInstanceState.RUNNING
        )
        verdict, execution = EffectVerdict.UNKNOWN, ExecutionStatus.UNKNOWN
        approach = EffectVerifier(self.registry, node, phase="approach")
        deadline = time.monotonic() + node.timeout_s
        accepted = False if cancelled_before_dispatch else await self.send_phase(node, "approach")
        approached = False
        while accepted and time.monotonic() < deadline:
            obs = await self.pump()
            gate = self._gate(node, obs)
            if gate == "wait":
                await asyncio.sleep(0.1)
                continue
            if gate == "stop":
                break
            if approach.observe(obs, time.monotonic()) == EffectVerdict.VERIFIED:
                approached = True
                break
            await asyncio.sleep(0.1)
        captures, last_capture, seen = 0, 0.0, None
        limit = int(node.params["max_captures"])
        evidence = self.artifacts / f"evidence-{node.task_id}.json"
        while approached and time.monotonic() < deadline:
            retake = verdict == EffectVerdict.UNVERIFIED and time.monotonic() - last_capture >= 1
            if captures < limit and (captures == 0 or retake):
                captures += 1
                last_capture = time.monotonic()
                if not await self.send_phase(node, "capture", attempt=captures):
                    break
            obs = await self.pump()
            gate = self._gate(node, obs)
            if gate == "wait":
                await asyncio.sleep(0.1)
                continue
            if gate == "stop":
                break
            if evidence.exists():
                record = json.loads(evidence.read_text())
                # Re-verify only when a new frame was persisted. / 只有持久化了新帧才重新验证。
                if (record.get("sha256"), record.get("capture_timestamp")) != seen:
                    seen = (record.get("sha256"), record.get("capture_timestamp"))
                    verdict = verify_asset_image(record, self.artifacts, node, self.registry)
            if verdict == EffectVerdict.VERIFIED:
                execution = ExecutionStatus.SUCCEEDED
                break
            if verdict == EffectVerdict.REFUTED or (verdict == EffectVerdict.UNVERIFIED and captures >= limit):
                execution = ExecutionStatus.FAILED
                break
            await asyncio.sleep(0.1)
        await self._conclude(node, execution, verdict, approach.waypoint)

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
            if self.registry.manifests[node.skill_id].intent_phases:
                await self.execute_phased(node)
            else:
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
