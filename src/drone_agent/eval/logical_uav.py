"""S0 logical aircraft (P1, D055): the real uplink, guardian and executive around the logical flight adapter.

The aircraft pulls deliveries through its uplink exactly like the SITL robot, verifies every package against the
service's key, and flies each newly accepted version once with a fresh guardian and executive and the next epoch,
like the resident desk supervisor. Its position and battery are the world truth its dock's pad sensor reads. A
harness-only link wrapper can drop uplink traffic to reproduce lost acknowledgements; nothing else is faked, so
what reaches the service is the same hash-chained journal a PX4 flight would produce.

S0 逻辑飞行器（P1，D055）：真实的 uplink、guardian 与 executive 围绕逻辑飞行适配器。

飞行器与 SITL 机器人完全相同地经 uplink 拉取投递，用服务密钥验证每个任务包，并像常驻任务台监管者那样，用新的
guardian、executive 与下一个代次把每个新接受的版本飞一次。其位置与电池是其机场机位传感器读取的世界真值。只在
编排中使用的链路包装可丢弃 uplink 流量以复现回执丢失；除此之外没有任何伪造，送达服务的是与 PX4 飞行相同的
哈希链账本。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from drone_agent.contracts import MissionPackage, RecoveryPolicy, utcnow
from drone_agent.eval.logical_flight import Battery, Client, FlightFake
from drone_agent.fleet.transport import LocalFleetClient, Receipt
from drone_agent.guardian.core import Guardian
from drone_agent.mission.executive import Executive
from drone_agent.mission.registry import Registry
from drone_agent.runtime.ledger import Journal, canonical
from drone_agent.runtime.recording import Recorder
from drone_agent.runtime.robot_state import RobotAuthorityState
from drone_agent.runtime.signing import SignatureRejected, TrustStore
from drone_agent.runtime.uplink import Uplink

PACKAGE_FILE = re.compile(r"^(m-[0-9a-f]{12})-v([1-9][0-9]{0,5})\.json$")


def quick_registry(root: Path, scene: Path, capability) -> Registry:
    """The site map with short hold times, used only by S0 aircraft; the service keeps the original.

    保持时间缩短的站点地图，只供 S0 飞行器使用；服务保留原始地图。
    """
    registry = Registry(root, scene=scene)
    registry.data = copy.deepcopy(registry.data)
    for key in ("takeoff", "approach", "return_home", "land"):
        registry.data["thresholds"][key]["hold_duration_s"] = 0.2
    registry.capability = capability
    return registry


@dataclass
class LinkFaults:
    """Harness-only uplink faults. / 只在编排中使用的 uplink 故障。"""

    drop_events: bool = False  # journal rows are lost on the way up / 账本行在上行途中丢失
    drop_status: bool = False  # status reports are lost / 状态报告丢失


class FaultyClient:
    """A LocalFleetClient whose uplink traffic a harness may drop. / 编排可丢弃其上行流量的 LocalFleetClient。"""

    def __init__(self, inner: LocalFleetClient, faults: LinkFaults):
        self.inner, self.faults = inner, faults

    async def publish_event(self, event):
        if self.faults.drop_events:
            return Receipt(False, "link_lost")
        return await self.inner.publish_event(event)

    async def publish_status(self, status):
        if self.faults.drop_status:
            return Receipt(False, "link_lost")
        return await self.inner.publish_status(status)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class LogicalUav:
    """One S0 aircraft with its own inbox, mailbox, authority state and flight directories.

    一架 S0 飞行器，拥有自己的 inbox、信箱、控制权状态与飞行目录。
    """

    def __init__(self, directory: Path, registry: Registry, hub, trust: TrustStore, policy: Path, *,
                 battery: Battery, drain_per_sample: float = 0.002):
        self.dir, self.registry, self.trust, self.policy = directory, registry, trust, policy
        self.robot_id = registry.capability.robot_id
        self.battery, self.drain = battery, drain_per_sample
        self.link = LinkFaults()
        self.uplink = Uplink(FaultyClient(LocalFleetClient(hub, self.robot_id), self.link), robot_id=self.robot_id,
                             trust=trust, capability=registry.capability, inbox=directory / "inbox",
                             mailbox=directory / "mailbox", aircraft=directory / "aircraft",
                             state_path=directory / "uplink/uplink-state.json")
        self.state = RobotAuthorityState(directory / "robot/authority.json", self.robot_id)
        self.position, self.in_air = [0.0, 0.0, 0.0], False
        self.truth: list[dict] = []
        self.flights: list[dict] = []
        self.flown: set[str] = set()
        self.task: asyncio.Task | None = None
        self.started = time.monotonic()

    def presence(self) -> tuple[list[float], bool]:
        """What the dock's pad sensor sees of this aircraft. / 机场机位传感器看到的本飞行器。"""
        return list(self.position), self.in_air

    def pending(self) -> Path | None:
        """The oldest accepted package version not yet flown. / 最早的已接受但未飞的版本。"""
        history = self.dir / "inbox/history"
        found = []
        for path in history.glob("*.json") if history.is_dir() else []:
            if PACKAGE_FILE.fullmatch(path.name) and path.name not in self.flown:
                found.append((path.stat().st_mtime_ns, path))
        return min(found)[1] if found else None

    async def cycle(self) -> None:
        """One uplink cycle, then start the next accepted version if the aircraft is idle. / 一次 uplink 周期，空闲时起飞下一版本。"""
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

    def _status(self, artifacts: Path, observation, guardian) -> None:
        status = {"observation": observation.model_dump(mode="json"),
                  "safety": guardian.safety.value if guardian else "nominal", "reason": guardian.reason if guardian else "",
                  "phase": guardian.phase if guardian else "grounded", "active_step": None, "command_pending": False,
                  "monotonic": time.monotonic()}
        pending = artifacts / "status.pending.json"
        pending.write_bytes(canonical(status))
        pending.replace(artifacts / "status.json")

    async def fly(self, path: Path) -> dict:
        """Fly one accepted package version with a fresh guardian and executive. / 用新的 guardian 与 executive 飞一个版本。"""
        package = MissionPackage.model_validate_json(path.read_bytes())
        epoch = self.state.epoch_watermark + 1
        artifacts = self.dir / "aircraft" / package.mission_id / f"v{package.mission_version}"
        artifacts.mkdir(parents=True, exist_ok=False)
        entry = {"robot_id": self.robot_id, "mission_id": package.mission_id, "version": package.mission_version,
                 "epoch": epoch, "started_at": utcnow().isoformat(), "ended_at": None, "result": None, "error": None,
                 "battery_at_start": round(self.battery.fraction, 4)}
        self.flights.append(entry)
        adapter = FlightFake(self.registry, artifacts, battery=self.battery, drain_per_sample=self.drain)
        adapter.sim_clock = lambda: time.monotonic() - self.started
        snapshot, holder = adapter.snapshot, {"guardian": None, "last": 0.0}

        def observed():
            observation = snapshot()
            point = observation.pose.position
            self.position, self.in_air = [point.x, point.y, point.z], bool(observation.in_air)
            self.truth.append({"timestamp": observation.timestamp.isoformat(), "sim_time": adapter.sim_clock(),
                               "mono": time.monotonic(), "robot_id": self.robot_id,
                               "position": [point.x, point.y, point.z], "in_air": self.in_air})
            if time.monotonic() - holder["last"] >= 0.2:
                holder["last"] = time.monotonic()
                self._status(artifacts, observation, holder["guardian"])
            return observation

        adapter.snapshot = observed
        guardian_journal = Journal(artifacts / "guardian.jsonl")
        journal, recorder = Journal(artifacts / "executive.jsonl"), Recorder(artifacts / "executive.mcap")
        try:
            try:
                guardian = Guardian(adapter=adapter, package=package, registry=self.registry, journal=guardian_journal,
                                    policy=RecoveryPolicy.from_yaml(self.policy),
                                    executive_id=f"executive-{self.robot_id}", simulation=True, trust=self.trust,
                                    robot_state=self.state)
            except SignatureRejected as error:
                entry["error"] = error.code
                return entry
            holder["guardian"] = guardian
            executive = Executive(client=Client(guardian, adapter), registry=self.registry, package=package,
                                  journal=journal, recorder=recorder, artifacts=artifacts,
                                  executive_id=f"executive-{self.robot_id}", epoch=epoch, trust=self.trust,
                                  mailbox=self.dir / "mailbox/operator.json")
            result = await executive.run()
            if guardian.recovery_task:
                await guardian.recovery_task
            entry["result"] = {k: result.get(k) for k in ("completed", "reason") if k in result}
        finally:
            journal.close()
            recorder.close()
            guardian_journal.close()
            self._status(artifacts, adapter.snapshot(), holder["guardian"])
            entry["ended_at"] = utcnow().isoformat()
            entry["battery_at_end"] = round(self.battery.fraction, 4)
        return entry

    def export(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.robot_id}-flights.json").write_text(json.dumps(self.flights, indent=2), encoding="utf-8")
        with (directory / f"{self.robot_id}-truth.jsonl").open("w", encoding="utf-8") as stream:
            for row in self.truth:
                stream.write(json.dumps(row) + "\n")
