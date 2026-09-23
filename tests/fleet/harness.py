"""In-process M2 loop: mission service, uplink and the real onboard processes around a flight fake.

The service plans with a scripted provider (labelled `scripted`, never reported as model output), signs with
a fresh key and queues deliveries in an in-process hub; the uplink pulls them into a private inbox and
forwards journals and evidence; the guardian and executive fly the inbox package with the deterministic
flight fake from the onboard tests.

进程内的 M2 闭环：任务服务、uplink 以及围绕飞行替身的真实机载进程。服务用脚本 provider 规划（标注为
`scripted`，从不当作模型输出），用新密钥签名并在进程内 hub 中排队投递；uplink 把它们拉进私有 inbox 并
转发账本与证据；guardian 与 executive 使用机载测试中的确定性飞行替身执行 inbox 中的任务包。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from drone_agent.admission.models import MissionRequest, RequestChannel
from drone_agent.contracts import MissionPackage, RecoveryPolicy, utcnow
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.transport import FleetHub, LocalFleetClient
from drone_agent.guardian.core import Guardian
from drone_agent.mission.executive import Executive
from drone_agent.planner.draft import TOOL_NAME
from drone_agent.planner.engine import ModelIdentity, PlannerEngine
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.planner.tools.catalog import ToolCatalog
from drone_agent.planner.tools.client import InProcessSession
from drone_agent.providers import KeyedScriptedProvider
from drone_agent.runtime.ledger import Journal, canonical
from drone_agent.runtime.permission import TrustLevel
from drone_agent.runtime.recording import Recorder
from drone_agent.runtime.robot_state import RobotAuthorityState
from drone_agent.runtime.signing import SigningKey, TrustStore
from drone_agent.runtime.uplink import Uplink
from tests.runtime.test_m2_onboard import Client, FlightFake, fast_registry

ROOT = Path(__file__).resolve().parents[2]
SCENE = ROOT / "configs/scenarios/m2_campus_v2.yaml"
POLICY = ROOT / "configs/recovery_policies/multirotor_m1_v1.yaml"
RED_REQUEST = "Inspect the red equipment marker east of the pad and bring back a photo."


def draft(asset: str = "asset_red") -> dict:
    return {"decision": "plan", "decline_reason": "", "goal": f"Inspect {asset}", "goal_type": "inspect",
            "approved_volume_id": "campus_training",
            "tasks": [{"task_id": f"inspect_{asset}", "skill_id": "skill.inspect.asset", "asset_id": asset}],
            "notes": ""}


def scripted_planner(answers: dict[str, dict] | None = None) -> PlannerEngine:
    answers = answers or {RED_REQUEST: draft()}
    provider = KeyedScriptedProvider({text: {"tool_calls": [{"id": "c1", "name": TOOL_NAME, "arguments": value}]}
                                      for text, value in answers.items()})
    tools = InProcessSession(ToolCatalog(ROOT, SCENE)).initialize()
    return PlannerEngine(provider, ModelIdentity("scripted", "scripted-fixture"), fast_registry(), tools)


def request(text: str = RED_REQUEST, key: str = "idem-loop-0001", **overrides) -> MissionRequest:
    values = dict(request_id="req-" + key, text=text, requested_by="tailnet:operator@example.test",
                  trust_level=TrustLevel.FIRST_PARTY, channel=RequestChannel.CONSOLE,
                  approved_volume_id="campus_training", idempotency_key=key, received_at=utcnow())
    values.update(overrides)
    return MissionRequest(**values)


@dataclass
class Loop:
    root: Path
    key: SigningKey
    ledger: BusinessLedger
    hub: FleetHub
    service: MissionService
    uplink: Uplink

    @property
    def trust(self) -> TrustStore:
        return TrustStore.from_entries([self.key.trust_entry()])

    def inbox_package(self) -> MissionPackage:
        return MissionPackage.model_validate_json((self.root / "inbox/package.json").read_bytes())

    async def fly(self, package: MissionPackage, *, epoch: int, frames=(), truth: list | None = None,
                  sim_restart: bool = False) -> dict:
        """Fly `package` with a fresh guardian and executive in its own version directory.

        `truth`, when given, collects the fake's positions like the simulator's truth collector.

        以新的 guardian 与 executive 在独立版本目录中执行 `package`。给出 `truth` 时，像仿真真值采集器一样
        收集替身的位置。
        """
        artifacts = self.root / "aircraft" / package.mission_id / f"v{package.mission_version}"
        artifacts.mkdir(parents=True)
        reg = fast_registry()
        adapter = FlightFake(reg, artifacts, frames)
        if truth is not None:
            # A battery swap reboots the simulator, so its clock starts again. / 换电重启仿真器，时钟重新开始。
            start = time.monotonic() if sim_restart or not truth else truth[0]["mono"]
            adapter.sim_clock = lambda: time.monotonic() - start
            snapshot = adapter.snapshot

            def recorded():
                obs = snapshot()
                point = obs.pose.position
                truth.append({"timestamp": obs.timestamp.isoformat(), "sim_time": adapter.sim_clock(),
                              "mono": time.monotonic(), "position": [point.x, point.y, point.z]})
                return obs

            adapter.snapshot = recorded
        guardian_journal = Journal(artifacts / "guardian.jsonl")
        state = RobotAuthorityState(self.root / "robot/authority.json", "uav_01")
        guardian = Guardian(adapter=adapter, package=package, registry=reg, journal=guardian_journal,
                            policy=RecoveryPolicy.from_yaml(POLICY), executive_id="executive-test", simulation=True,
                            trust=self.trust, robot_state=state)
        journal, recorder = Journal(artifacts / "executive.jsonl"), Recorder(artifacts / "executive.mcap")
        try:
            executive = Executive(client=Client(guardian, adapter), registry=reg, package=package, journal=journal,
                                  recorder=recorder, artifacts=artifacts, executive_id="executive-test", epoch=epoch,
                                  trust=self.trust, mailbox=self.root / "mailbox/operator.json")
            await executive.run()
            if guardian.recovery_task:
                await guardian.recovery_task
        finally:
            journal.close()
            recorder.close()
            guardian_journal.close()
        # What the launcher's observe loop leaves behind. / 启动器观测循环留下的内容。
        (artifacts / "status.json").write_bytes(canonical({
            "observation": adapter.snapshot().model_dump(mode="json"), "safety": guardian.safety.value,
            "reason": guardian.reason, "phase": guardian.phase, "active_step": None, "command_pending": False}))
        return json.loads((artifacts / "result.json").read_text())

    async def sync(self, rounds: int = 2) -> dict:
        """Let the uplink run and the service refresh. / 让 uplink 运行、服务刷新。"""
        for _ in range(rounds):
            self.uplink.last_status = 0
            await self.uplink.cycle()
            for mission_id in sorted(self.service.dirty):
                self.service.dirty.discard(mission_id)
                self.service.refresh(mission_id)
            for mission in self.ledger.missions():
                self.service.refresh(mission["mission_id"])
        return {m["mission_id"]: m for m in self.ledger.missions()}


def build_loop(root: Path, planner=None) -> Loop:
    key = SigningKey.generate()
    ledger = BusinessLedger(root / "service/ledger.sqlite3")
    hub = FleetHub(ledger, root / "service/media")
    service = MissionService(root=ROOT, scene=SCENE, ledger=ledger, hub=hub, signing_key=key,
                             approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                             planner=planner or scripted_planner())
    uplink = Uplink(LocalFleetClient(hub, "uav_01"), robot_id="uav_01",
                    trust=TrustStore.from_entries([key.trust_entry()]), capability=fast_registry().capability,
                    inbox=root / "inbox", mailbox=root / "mailbox", aircraft=root / "aircraft",
                    state_path=root / "uplink/uplink-state.json")
    return Loop(root, key, ledger, hub, service, uplink)
