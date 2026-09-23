"""Onboard uplink (D031, WP-M2-17): the only networked onboard process; it relays and never commands.

Down: signed packages are verified here (signature, statement, robot, validity, no rollback) and written
atomically to the private inbox, where the executive and the guardian verify them again before flying;
operator requests are checked (robot, validity, accepted version) and written one at a time into the M1
operator mailbox that the executive already validates. The uplink mounts neither the guardian socket nor
anything writable in the flight artifacts, so it cannot submit a control intent or alter evidence.

Up: complete journal rows are forwarded as ExecutionEvents that keep the whole row (hash chain intact),
evidence contracts are completed with mission, version and robot and sent with their media, and the
guardian's latest observation becomes a ~1 Hz RobotStatus. Everything is at-least-once; the service
deduplicates. A transport failure never stops the uplink and never affects the flight.

机载 uplink（D031，WP-M2-17）：机载唯一联网的进程；只转发，从不下令。

下行：已签名任务包在这里验证（签名、陈述、机器人、有效期、不回退）并原子写入私有 inbox，executive 与
guardian 起飞前会再次各自验证；操作请求经检查（机器人、有效期、已接受版本）后一次一个写入 executive
早已校验的 M1 操作者信箱。uplink 既不挂载 guardian 套接字，也不以可写方式挂载飞行产物，因此既不能提交
控制意图，也不能改动证据。

上行：完整的账本行作为保留整行（哈希链完整）的 ExecutionEvent 转发；证据契约补全任务、版本与机器人后
随媒体一起发送；guardian 的最新观测变为约 1 Hz 的 RobotStatus。一切至少一次，由服务去重。传输失败
从不让 uplink 停下，也从不影响飞行。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import time
from datetime import datetime
from pathlib import Path

from drone_agent.contracts import (
    CapabilityDescriptor,
    CommsState,
    EnergyState,
    Evidence,
    FlightObservation,
    LocalizationHealth,
    MissionPackage,
    OperatorRequest,
    RobotStatus,
    utcnow,
)
from drone_agent.fleet.events import journal_row_to_event
from drone_agent.runtime.ledger import canonical
from drone_agent.runtime.signing import SignatureRejected, TrustStore, verify_package

VERSION_DIR = re.compile(r"^v([1-9][0-9]{0,5})$")
JOURNALS = ("executive", "guardian")
RAW_RGB = "image/x-raw-rgb"
STATE_FORMAT = "drone.uplink-state/v1"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    with pending.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)


def complete_rows(path: Path, offset: int) -> list[tuple[dict, int]]:
    """Complete journal lines after `offset` with the offset after each; a torn tail waits.

    `offset` 之后的完整账本行及各自结束偏移；不完整的尾部等待下次读取。
    """
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read()
    rows = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        offset += len(line)
        rows.append((json.loads(line), offset))
    return rows


class Uplink:
    def __init__(self, client, *, robot_id: str, trust: TrustStore, capability: CapabilityDescriptor, inbox: Path,
                 mailbox: Path, aircraft: Path, state_path: Path, clock=utcnow):
        self.client, self.robot_id, self.trust, self.capability = client, robot_id, trust, capability
        self.inbox, self.mailbox, self.aircraft, self.state_path, self.clock = inbox, mailbox, aircraft, state_path, clock
        self.state = {"format": STATE_FORMAT, "accepted": {}, "journals": {}, "evidence": {}}
        if state_path.exists():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if loaded.get("format") != STATE_FORMAT:
                raise ValueError("unsupported uplink state")
            self.state = loaded
        self.notes: list[dict] = []
        self.comms_ok = False
        self.capability_sent = False
        self.last_status = 0.0

    def note(self, kind: str, **data) -> None:
        self.notes = [*self.notes[-49:], {"kind": kind, "at": self.clock().isoformat(), **data}]

    def _save(self) -> None:
        atomic_write(self.state_path, canonical(self.state))

    # ── down / 下行 ──

    def accept_package(self, package: MissionPackage | None) -> tuple[bool, str]:
        """Verify a package and hand it to the inbox. / 验证任务包并交给 inbox。"""
        try:
            if package is None:
                raise SignatureRejected("onboard.unsigned", "the delivery carries no package")
            signer = verify_package(package, self.trust, robot_id=self.robot_id, now=self.clock())
            known = self.state["accepted"].get(package.mission_id)
            if known and (package.mission_version < known["version"] or (
                    package.mission_version == known["version"] and package.package_hash != known["package_hash"])):
                raise SignatureRejected("onboard.version_rollback",
                                        f"v{package.mission_version} after accepted v{known['version']}")
        except SignatureRejected as error:
            self.note("package_rejected", code=error.code)
            return False, error.code
        data = canonical(package.model_dump(mode="json"))
        atomic_write(self.inbox / "history" / f"{package.mission_id}-v{package.mission_version}.json", data)
        atomic_write(self.inbox / "package.json", data)
        versions = sorted({*self.state["accepted"].get(package.mission_id, {}).get("versions", []),
                           package.mission_version})
        self.state["accepted"][package.mission_id] = {"version": package.mission_version, "versions": versions,
                                                      "package_hash": package.package_hash, "signer_key_id": signer}
        self._save()
        return True, "accepted"

    def _answered(self, envelope: dict) -> bool:
        directory = self.aircraft / envelope["mission_id"] / f"v{envelope['mission_version']}"
        path = directory / "executive.jsonl"
        if not path.exists():
            return False
        return any(row["kind"] in ("operator_request", "operator_rejected")
                   and row["data"].get("request_id") == envelope["request_id"] for row, _ in complete_rows(path, 0))

    def relay_operation(self, request: OperatorRequest | None) -> tuple[bool | None, str]:
        """Write one operator request into the mailbox; None means busy, retry later.

        把一个操作请求写入信箱；None 表示信箱忙，稍后重试。
        """
        now = self.clock()
        if request is None or request.robot_id != self.robot_id:
            return False, "auth.robot_mismatch"
        if request.valid_until <= now:
            return False, "operator_request_expired"
        known = self.state["accepted"].get(request.mission_id)
        if not known or known["version"] != request.mission_version:
            return False, "operator_target_changed"
        path = self.mailbox / "operator.json"
        if path.exists():
            pending = json.loads(path.read_text(encoding="utf-8"))
            if pending.get("request_id") == request.request_id:
                return True, "relayed"
            if datetime.fromisoformat(pending["valid_until"]) > now and not self._answered(pending):
                return None, "mailbox_busy"
        envelope = {"request_id": request.request_id, "action": request.action.value,
                    "mission_id": request.mission_id, "mission_version": request.mission_version,
                    "lease_epoch": request.lease_epoch, "step_id": request.step_id,
                    "valid_until": request.valid_until.isoformat(), "requested_by": request.requested_by}
        atomic_write(path, canonical(envelope))
        return True, "relayed"

    async def pull(self) -> None:
        batch = await self.client.fetch_deliveries(0, 10)
        for item in batch["items"]:
            if item["kind"] == "mission_package":
                accepted, reason = self.accept_package(item["package"])
            elif item["kind"] == "operator_request":
                accepted, reason = self.relay_operation(item["operation"])
                if accepted is None:
                    continue
            else:
                accepted, reason = False, "service.invalid_request"
            await self.client.acknowledge(item["delivery_id"], accepted, reason)

    # ── up / 上行 ──

    def _version_dirs(self):
        for mission_id, known in sorted(self.state["accepted"].items()):
            root = self.aircraft / mission_id
            if not root.is_dir():
                continue
            for directory in sorted(root.iterdir()):
                match = VERSION_DIR.fullmatch(directory.name)
                if match and directory.is_dir() and int(match.group(1)) in known.get("versions", []):
                    yield mission_id, int(match.group(1)), directory

    def _bound_to_package(self, mission_id: str, version: int, directory: Path) -> bool:
        """The flight journal must name the package this uplink accepted. / 飞行账本必须指向本 uplink 接受的任务包。"""
        path = directory / "executive.jsonl"
        history = self.inbox / "history" / f"{mission_id}-v{version}.json"
        if not path.exists() or not history.exists():
            return False
        first = next((row for row, _ in complete_rows(path, 0) if row["kind"] == "mission_accepted"), None)
        expected = json.loads(history.read_text(encoding="utf-8"))["package_hash"]
        return first is not None and first["data"].get("package_hash") == expected

    async def push(self) -> None:
        for mission_id, version, directory in self._version_dirs():
            if not self._bound_to_package(mission_id, version, directory):
                continue
            for journal in JOURNALS:
                path = directory / f"{journal}.jsonl"
                if not path.exists():
                    continue
                key = f"{mission_id}/v{version}/{journal}"
                for row, offset in complete_rows(path, self.state["journals"].get(key, 0)):
                    event = journal_row_to_event(row, journal=journal, robot_id=self.robot_id, mission_id=mission_id,
                                                 version=version)
                    receipt = await self.client.publish_event(event)
                    if not receipt.accepted:
                        self.note("event_refused", key=key, seq=row["seq"], reason=receipt.reason)
                        break
                    self.state["journals"][key] = offset
                    self._save()
            await self._push_evidence(mission_id, version, directory)

    async def _push_evidence(self, mission_id: str, version: int, directory: Path) -> None:
        for path in sorted(directory.glob("evidence-*.json")):
            key = f"{mission_id}/v{version}/{path.name}"
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if self.state["evidence"].get(key) == record["sha256"]:
                    continue
                media = (directory / record["media_ref"]).resolve()
                data = media.read_bytes() if media.is_relative_to(directory.resolve()) else b""
            except (OSError, ValueError, KeyError):
                continue  # a file being written is read again next cycle / 正在写入的文件下个周期再读
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                self.note("evidence_digest_mismatch", key=key)
                continue
            contract = Evidence.model_validate({**record["contract"], "mission_id": mission_id,
                                                "mission_version": version, "robot_id": self.robot_id})
            receipt = await self.client.publish_evidence(contract)
            if receipt.accepted:
                receipt = await self.client.publish_media({
                    "robot_id": self.robot_id, "mission_id": mission_id, "mission_version": version,
                    "evidence_id": contract.evidence_id, "sha256": record["sha256"], "media_type": RAW_RGB,
                    "width": record["width"], "height": record["height"], "data": data})
            if receipt.accepted:
                self.state["evidence"][key] = record["sha256"]
                self._save()
            else:
                self.note("evidence_refused", key=key, reason=receipt.reason)

    def status(self) -> RobotStatus | None:
        """Status from the guardian's latest observation; unknown energy means no status. / 由最新观测得到状态。"""
        latest = None
        for _, _, directory in self._version_dirs():
            if (directory / "status.json").exists():
                latest = directory / "status.json"
        if latest is None:
            return None
        try:
            status = json.loads(latest.read_text(encoding="utf-8"))
            obs = FlightObservation.model_validate(status["observation"])
        except (OSError, ValueError, KeyError):
            return None
        if obs.battery_fraction is None or obs.robot_id != self.robot_id:
            return None
        phase = "grounded" if obs.in_air is False and obs.armed is False else "airborne" if obs.in_air else "unknown"
        return RobotStatus(
            robot_id=self.robot_id, timestamp=obs.timestamp, flight_phase=phase,
            energy=EnergyState(remaining_fraction=obs.battery_fraction),
            localization=LocalizationHealth(fix_type="estimated" if obs.localization_healthy else "none",
                                            healthy=obs.localization_healthy),
            comms=CommsState(uplink_ok=self.comms_ok, last_seen=self.clock()),
            active_skill_instance=status.get("active_step"), fc_failsafe_active=obs.fc_failsafe)

    async def cycle(self) -> None:
        if not self.capability_sent:
            self.capability_sent = (await self.client.publish_capability(self.capability)).accepted
        await self.pull()
        await self.push()
        if time.monotonic() - self.last_status >= 1:
            status = self.status()
            if status is not None:
                await self.client.publish_status(status)
            self.last_status = time.monotonic()

    async def run(self, stop: asyncio.Event, period_s: float = 0.5) -> None:
        while not stop.is_set():
            try:
                await self.cycle()
                self.comms_ok = True
            except Exception as error:  # a relay never crashes the aircraft side / 转发失败绝不影响机载其他部分
                self.comms_ok = False
                self.note("cycle_error", type=type(error).__name__)
            atomic_write(self.state_path.with_name("uplink-status.json"), canonical({
                "comms_ok": self.comms_ok, "accepted": self.state["accepted"], "notes": self.notes[-10:],
                "at": self.clock().isoformat()}))
            try:
                await asyncio.wait_for(stop.wait(), period_s)
            except TimeoutError:
                pass


async def main_async(args) -> None:
    from drone_agent.fleet.transport import GrpcFleetClient
    from drone_agent.mission.registry import Registry

    registry = Registry(args.root, scene=args.scene)
    client = GrpcFleetClient(args.service, args.robot_id, cert_pem=(args.tls / "robot.crt").read_bytes(),
                             key_pem=(args.tls / "robot.key").read_bytes(), ca_pem=(args.tls / "ca.crt").read_bytes(),
                             server_name=args.server_name)
    uplink = Uplink(client, robot_id=args.robot_id, trust=TrustStore.load(args.trust),
                    capability=registry.capability, inbox=args.inbox, mailbox=args.mailbox, aircraft=args.aircraft,
                    state_path=args.state / "uplink-state.json")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await uplink.run(stop)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--robot-id", default="uav_01")
    parser.add_argument("--service", default="mission-service:8450")
    parser.add_argument("--server-name", default="mission-service")
    parser.add_argument("--tls", type=Path, default=Path("/tls"))
    parser.add_argument("--trust", type=Path, default=Path("/trust/trust.json"))
    parser.add_argument("--inbox", type=Path, default=Path("/inbox"))
    parser.add_argument("--mailbox", type=Path, default=Path("/mailbox"))
    parser.add_argument("--aircraft", type=Path, default=Path("/aircraft"))
    parser.add_argument("--state", type=Path, default=Path("/uplink"))
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
