"""S0 logical ground robot for X1 (D088): the real uplink, guardian and executive around the logical ground adapter.

Like the logical aircraft, the robot pulls deliveries through its uplink, verifies every package against the service's
key and drives each newly accepted version once with a fresh guardian and executive and the next epoch. Unlike it, the
robot is one physical body across missions: a single ground adapter keeps its position, its armed state and its
battery, and the guardian supervises every mission, because a recovery ends with the guardian's stationary disarm.
Between missions the robot writes the observation the guardian's read-only mode would write, so the uplink reports a
fresh `ground_phase` and answers handoff offers from its own map. A harness-only link wrapper can drop uplink traffic;
nothing else is faked.

X1 的 S0 逻辑地面车（D088）：真实的 uplink、guardian 与 executive 围绕逻辑地面适配器。

与逻辑飞行器一样，地面车经 uplink 拉取投递，用服务密钥验证每个任务包，并用新的 guardian、executive 与下一个代次把每个
新接受的版本执行一次。与之不同的是，地面车跨任务是同一个物理车体：一个地面适配器保持其位置、解锁状态与电量；guardian 监督
每次任务，因为恢复以 guardian 的静止上锁收尾。两次任务之间，地面车写出 guardian 只读模式会写的观测，使 uplink 上报新鲜的
`ground_phase`，并依据自己的地图回答交接提议。只在编排中使用的链路包装可丢弃上行流量；除此之外没有任何伪造。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from drone_agent.contracts import MissionPackage, RecoveryPolicy, utcnow
from drone_agent.eval.logical_flight import Battery, Client
from drone_agent.eval.logical_ground import GroundFake
from drone_agent.eval.logical_uav import PACKAGE_FILE, FaultyClient, LinkFaults
from drone_agent.fleet.transport import LocalFleetClient
from drone_agent.guardian.core import Guardian
from drone_agent.mission.executive import Executive
from drone_agent.runtime.ledger import Journal, canonical
from drone_agent.runtime.recording import Recorder
from drone_agent.runtime.robot_state import RobotAuthorityState
from drone_agent.runtime.signing import SignatureRejected, TrustStore
from drone_agent.runtime.uplink import IDLE_DIR, Uplink


class LogicalRover:
    """One S0 ground robot with its own inbox, mailbox, authority state, mission directories and physical body.

    一台 S0 地面车，拥有自己的 inbox、信箱、控制权状态、任务目录与物理车体。
    """

    def __init__(self, directory: Path, registry, hub, trust: TrustStore, policy: Path, *, battery: Battery,
                 drain_s: float = 1800.0, client=None):
        self.dir, self.registry, self.trust, self.policy = directory, registry, trust, policy
        self.robot_id = registry.capability.robot_id
        self.battery = battery
        self.link = LinkFaults()
        # The one physical robot; its camera sees the harness world's blocks. / 唯一的物理车体；其相机看到编排世界的方块。
        self.body = GroundFake(registry, directory / "aircraft" / IDLE_DIR, battery=battery, drain_s=drain_s)
        inner = client if client is not None else LocalFleetClient(hub, self.robot_id)
        self.uplink = Uplink(FaultyClient(inner, self.link), robot_id=self.robot_id, trust=trust,
                             capability=registry.capability, inbox=directory / "inbox", mailbox=directory / "mailbox",
                             aircraft=directory / "aircraft", state_path=directory / "uplink/uplink-state.json",
                             ground=registry)
        self.state = RobotAuthorityState(directory / "robot/authority.json", self.robot_id)
        self.truth: list[dict] = []
        self.flights: list[dict] = []
        self.flown: set[str] = set()
        self.task: asyncio.Task | None = None
        self.idle = True  # whether the read-only observation runs (no mission) / 只读观测是否在运行（没有任务）
        self.started = time.monotonic()

    def observe(self) -> None:
        """The guardian's read-only mode between missions: one fresh observation into the idle status file.

        两次任务之间 guardian 的只读模式：把一次新鲜观测写进空闲状态文件。
        """
        observation = self.body.snapshot()
        self._truth(observation)
        directory = self.dir / "aircraft" / IDLE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        _status(directory, observation, None)

    def _truth(self, observation) -> None:
        point = observation.pose.position
        self.truth.append({"timestamp": observation.timestamp.isoformat(), "mono": time.monotonic(),
                           "sim_time": time.monotonic() - self.started, "robot_id": self.robot_id,
                           "position": [point.x, point.y, 0.0], "armed": observation.armed,
                           "speed": round(sum(v * v for v in observation.velocity_enu_mps[:2]) ** 0.5, 4)})

    def pending(self) -> Path | None:
        history = self.dir / "inbox/history"
        found = []
        for path in history.glob("*.json") if history.is_dir() else []:
            if PACKAGE_FILE.fullmatch(path.name) and path.name not in self.flown:
                found.append((path.stat().st_mtime_ns, path))
        return min(found)[1] if found else None

    async def cycle(self) -> None:
        """Observe while idle, one uplink cycle, then start the next accepted version. / 空闲时观测，一次 uplink
        周期，然后执行下一个已接受的版本。"""
        if self.idle:
            self.observe()
        self.uplink.last_status = 0
        await self.uplink.cycle()
        if self.task is not None and self.task.done():
            error = self.task.exception()
            if error is not None:
                self.flights[-1]["error"] = f"{type(error).__name__}: {error}"[:300]
            self.task = None
        if self.task is None:
            package = self.pending()
            if package is not None:
                self.flown.add(package.name)
                self.task = asyncio.create_task(self.fly(package))

    async def settle(self) -> None:
        if self.task is not None:
            await asyncio.wait({self.task})

    async def fly(self, path: Path) -> dict:
        """Drive one accepted version with a fresh guardian and executive; the guardian supervises throughout.

        用新的 guardian 与 executive 执行一个已接受的版本；guardian 全程监督。
        """
        package = MissionPackage.model_validate_json(path.read_bytes())
        epoch = self.state.epoch_watermark + 1
        artifacts = self.dir / "aircraft" / package.mission_id / f"v{package.mission_version}"
        artifacts.mkdir(parents=True, exist_ok=False)
        entry = {"robot_id": self.robot_id, "mission_id": package.mission_id, "version": package.mission_version,
                 "epoch": epoch, "started_at": utcnow().isoformat(), "ended_at": None, "result": None, "error": None,
                 "battery_at_start": round(self.battery.fraction, 4)}
        self.flights.append(entry)
        body = self.body
        body.artifacts, self.idle = artifacts, False
        snapshot, holder = body.snapshot, {"guardian": None, "last": 0.0}

        def observed():
            observation = snapshot()
            self._truth(observation)
            if time.monotonic() - holder["last"] >= 0.2:
                holder["last"] = time.monotonic()
                guardian = holder["guardian"]
                _status(artifacts, observation, guardian)
            return observation

        body.snapshot = observed
        guardian_journal = Journal(artifacts / "guardian.jsonl")
        journal, recorder = Journal(artifacts / "executive.jsonl"), Recorder(artifacts / "executive.mcap")
        supervision = None
        try:
            try:
                guardian = Guardian(adapter=body, package=package, registry=self.registry, journal=guardian_journal,
                                    policy=RecoveryPolicy.from_yaml(self.policy),
                                    executive_id=f"executive-{self.robot_id}", simulation=True, trust=self.trust,
                                    robot_state=self.state)
            except SignatureRejected as error:
                entry["error"] = error.code
                return entry
            holder["guardian"] = guardian
            supervision = asyncio.create_task(guardian.supervise())
            executive = Executive(client=Client(guardian, body), registry=self.registry, package=package,
                                  journal=journal, recorder=recorder, artifacts=artifacts,
                                  executive_id=f"executive-{self.robot_id}", epoch=epoch, trust=self.trust,
                                  mailbox=self.dir / "mailbox/operator.json")
            result = await executive.run()
            # A recovery ends with the guardian's stationary disarm and its record of the safe terminal.
            # 恢复以 guardian 的静止上锁及其对安全终态的记录结束。
            deadline = time.monotonic() + 10
            while guardian.recovery is not None and guardian.reason != self.registry.safe_terminal \
                    and not guardian.taken_over and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            entry["result"] = {k: result.get(k) for k in ("completed", "reason") if k in result}
        finally:
            if supervision is not None:
                supervision.cancel()
                await asyncio.gather(supervision, return_exceptions=True)
            body.snapshot = snapshot
            journal.close()
            recorder.close()
            guardian_journal.close()
            _status(artifacts, body.snapshot(), holder["guardian"])
            entry["ended_at"] = utcnow().isoformat()
            entry["battery_at_end"] = round(self.battery.fraction, 4)
            self.idle = True
        return entry

    def export(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.robot_id}-flights.json").write_text(json.dumps(self.flights, indent=2), encoding="utf-8")
        with (directory / f"{self.robot_id}-truth.jsonl").open("w", encoding="utf-8") as stream:
            for row in self.truth:
                stream.write(json.dumps(row) + "\n")


def _status(directory: Path, observation, guardian) -> None:
    status = {"observation": observation.model_dump(mode="json"),
              "safety": guardian.safety.value if guardian else "nominal", "reason": guardian.reason if guardian else "",
              "phase": guardian.phase if guardian else "parked",
              "active_step": guardian.active_step.task_id if guardian and guardian.active_step else None,
              "command_pending": False, "monotonic": time.monotonic()}
    pending = directory / "status.pending.json"
    pending.write_bytes(canonical(status))
    pending.replace(directory / "status.json")
