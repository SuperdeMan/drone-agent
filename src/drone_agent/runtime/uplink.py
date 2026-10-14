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

X1 (D088): a ground robot answers handoff offers here, deterministically and from what it knows itself: its own map
from its parking spot, whether a mission it accepted may still run, and its last known rest state and energy. The
answer is stored before it is sent, so a lost reply is resent unchanged; the hub closes the offer's delivery when the
reply settles it, so an offer is never acknowledged. An answer only accepts a task: the package, its approval and the
mission's own preconditions still decide whether anything moves.

机载 uplink（D031，WP-M2-17）：机载唯一联网的进程；只转发，从不下令。

下行：已签名任务包在这里验证（签名、陈述、机器人、有效期、不回退）并原子写入私有 inbox，executive 与
guardian 起飞前会再次各自验证；操作请求经检查（机器人、有效期、已接受版本）后一次一个写入 executive
早已校验的 M1 操作者信箱。uplink 既不挂载 guardian 套接字，也不以可写方式挂载飞行产物，因此既不能提交
控制意图，也不能改动证据。

上行：完整的账本行作为保留整行（哈希链完整）的 ExecutionEvent 转发；证据契约补全任务、版本与机器人后
随媒体一起发送；guardian 的最新观测变为约 1 Hz 的 RobotStatus。一切至少一次，由服务去重。传输失败
从不让 uplink 停下，也从不影响飞行。

X1（D088）：地面车在这里回答交接提议，确定性地、只依据它自己知道的事：从停车位出发的自身地图、它接受的任务是否仍可能
执行，以及最后已知的停放状态与电量。回答先保存再发送，丢失的回复原样重发；回复了结提议时由 hub 关闭其投递，因此提议从不
单独确认。回答只表示接受任务：任务包、审批与任务自身的前置条件仍决定车是否移动。
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
    WorldFact,
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
                 mailbox: Path, aircraft: Path, state_path: Path, clock=utcnow, ground=None):
        self.client, self.robot_id, self.trust, self.capability = client, robot_id, trust, capability
        self.inbox, self.mailbox, self.aircraft, self.state_path, self.clock = inbox, mailbox, aircraft, state_path, clock
        # X1 (D088): a ground robot's own site map, which answers handoff offers; None on an aircraft.
        # X1（D088）：地面车自己的站点图，用来回答交接提议；飞行器上为 None。
        self.ground = ground
        self.state = {"format": STATE_FORMAT, "accepted": {}, "journals": {}, "evidence": {}, "handoffs": {}}
        if state_path.exists():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if loaded.get("format") != STATE_FORMAT:
                raise ValueError("unsupported uplink state")
            self.state = loaded
            self.state.setdefault("handoffs", {})
        self.notes: list[dict] = []
        self.comms_ok = False
        self.capability_sent = False
        self.last_status = 0.0
        # Flights already bound to their accepted package; the first mission_accepted row never changes.
        # 已绑定到所接受任务包的飞行；第一条 mission_accepted 行不会再变。
        self.bound: set[tuple[str, int]] = set()

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
            elif item["kind"] == "handoff_offer":
                # Settled by the reply, never acknowledged (D088). / 由回复了结，从不单独确认（D088）。
                await self.answer_offer(item["offer"])
                continue
            else:
                accepted, reason = False, "service.invalid_request"
            await self.client.acknowledge(item["delivery_id"], accepted, reason)

    # ── handoff answers (X1, D088) / 交接作答（X1，D088） ──

    def _busy(self) -> bool:
        """A mission version this robot accepted may still run: no result yet and its window still open.

        本机接受的某个任务版本仍可能执行：尚无结果，且时间窗仍开着。
        """
        now = self.clock()
        for mission_id, known in self.state["accepted"].items():
            version = known["version"]
            if (self.aircraft / mission_id / f"v{version}" / "result.json").exists():
                continue
            try:
                package = MissionPackage.model_validate_json(
                    (self.inbox / "history" / f"{mission_id}-v{version}.json").read_bytes())
            except (OSError, ValueError):
                return True
            window = package.temporal_window
            if window is None or window.not_after > now:
                return True
        return False

    def offer_reply(self, offer: dict) -> dict:
        """The answer to one offer, from the robot's own map and state; deterministic, never a model.

        Checked in order: expiry, the target's map, an unfinished mission, the last known rest state (when there is
        one), energy for the site's default budget, then reachability from the parking spot with a face leg.

        依据地面车自己的地图与状态回答一个提议；确定性，从不用模型。依次检查：过期、目标所在地图、未完成的任务、最后
        已知的停放状态（有观测时）、站点默认预算所需的电量，最后是从停车位出发、带正对段的可达性。
        """
        reply = {"handoff_id": str(offer.get("handoff_id", "")), "accepted": False}
        ground = self.ground
        if ground is None:
            return {**reply, "reason_code": "outside_map", "reason": "this robot has no ground map"}
        reply["map_version"] = ground.registry_id
        try:
            expires = datetime.fromisoformat(offer["expires_at"])
            fact = WorldFact.model_validate(offer["target"])
            kind = fact.value["kind"]
            radius = float(fact.value["radius_m"]) if kind == "search_region" else None
        except (KeyError, TypeError, ValueError):
            return {**reply, "reason_code": "alignment_insufficient", "reason": "the target is malformed"}
        if expires <= self.clock():
            return {**reply, "reason_code": "expired"}
        if (fact.position is None or fact.frame is None or fact.frame.map_version != ground.registry_id
                or fact.frame.frame_id != ground.data["frame"]["frame_id"]):
            return {**reply, "reason_code": "outside_map", "reason": "the target is on another map"}
        if kind not in ("point", "search_region"):
            return {**reply, "reason_code": "alignment_insufficient", "reason": f"unknown target kind {kind}"}
        if self._busy():
            return {**reply, "reason_code": "busy", "reason": "an accepted mission may still run"}
        latest = self._latest_status()
        if latest is not None and not ground.resting(latest[1]):
            return {**reply, "reason_code": "busy", "reason": "not resting at a parking spot"}
        budget = ground.data["mission_defaults"]["energy_budget"]
        if latest is not None and (latest[1].battery_fraction is None or latest[1].battery_fraction
                                   < budget["max_consumption_fraction"] + budget["reserve_fraction"]):
            return {**reply, "reason_code": "energy"}
        answer = ground.answer((fact.position.x, fact.position.y), radius)
        if not answer.accepted:
            return {**reply, "reason_code": answer.reason, "reason": answer.detail[:300]}
        return {**reply, "accepted": True, "path_length_m": answer.path_m, "eta_s": answer.eta_s}

    async def answer_offer(self, offer: dict) -> None:
        """Answer once and keep the answer, so a lost reply is resent unchanged. / 只作答一次并保存回答，丢失的回复
        原样重发。"""
        handoff_id = str(offer.get("handoff_id", ""))
        reply = self.state["handoffs"].get(handoff_id)
        if reply is None:
            reply = self.offer_reply(offer)
            self.state["handoffs"][handoff_id] = reply
            self._save()
            self.note("handoff_answered", handoff_id=handoff_id, accepted=reply["accepted"],
                      reason_code=reply.get("reason_code"))
        receipt = await self.client.reply_handoff(reply)
        if not receipt.accepted:
            self.note("handoff_reply_refused", handoff_id=handoff_id, reason=receipt.reason)

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
        if (mission_id, version) in self.bound:
            return True
        path = directory / "executive.jsonl"
        history = self.inbox / "history" / f"{mission_id}-v{version}.json"
        if not path.exists() or not history.exists():
            return False
        first = next((row for row, _ in complete_rows(path, 0) if row["kind"] == "mission_accepted"), None)
        expected = json.loads(history.read_text(encoding="utf-8"))["package_hash"]
        if first is not None and first["data"].get("package_hash") == expected:
            self.bound.add((mission_id, version))
            return True
        return False

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
                body = {**record["contract"], "mission_id": mission_id,
                        "mission_version": version, "robot_id": self.robot_id}
                # Pixels may be identical across flights; capture identity includes its context and metadata.
                # 不同飞行的像素可能相同；采集身份包含上下文与元数据。
                identity = hashlib.sha256(canonical(body)).hexdigest()
                if self.state["evidence"].get(key) == identity:
                    continue
                media = (directory / record["media_ref"]).resolve()
                data = media.read_bytes() if media.is_relative_to(directory.resolve()) else b""
            except (OSError, ValueError, KeyError):
                continue  # a file being written is read again next cycle / 正在写入的文件下个周期再读
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                self.note("evidence_digest_mismatch", key=key)
                continue
            contract = Evidence.model_validate({**body, "evidence_id": "capture:" + identity})
            receipt = await self.client.publish_evidence(contract)
            if receipt.accepted:
                receipt = await self.client.publish_media({
                    "robot_id": self.robot_id, "mission_id": mission_id, "mission_version": version,
                    "evidence_id": contract.evidence_id, "sha256": record["sha256"], "media_type": RAW_RGB,
                    "width": record["width"], "height": record["height"], "data": data})
            if receipt.accepted:
                self.state["evidence"][key] = identity
                self._save()
            else:
                self.note("evidence_refused", key=key, reason=receipt.reason)

    def status(self) -> RobotStatus | None:
        """Status from the guardian's latest observation; unknown energy means no status. / 由最新观测得到状态。

        The most recently written status file is the latest observation: a robot keeps flights of many missions,
        and neither mission ids nor versions order them in time.
        最近写入的状态文件才是最新观测：机器人保留多个任务的飞行，任务 ID 与版本号都不按时间排序。
        """
        latest = self._latest_status()
        if latest is None:
            return None
        status, obs = latest
        if obs.battery_fraction is None:
            return None
        if self.ground is not None:
            # A rover reports in_air whenever armed, so flight_phase does not apply; fleet reads ground_phase (D088).
            # 地面车只要解锁就报告 in_air，flight_phase 不适用；fleet 读取 ground_phase（D088）。
            phase, ground_phase = "unknown", self.ground.ground_phase(obs)
        else:
            ground_phase = None
            phase = "grounded" if obs.in_air is False and obs.armed is False else "airborne" if obs.in_air else "unknown"
        return RobotStatus(
            robot_id=self.robot_id, timestamp=obs.timestamp, flight_phase=phase, ground_phase=ground_phase,
            energy=EnergyState(remaining_fraction=obs.battery_fraction),
            localization=LocalizationHealth(fix_type="estimated" if obs.localization_healthy else "none",
                                            healthy=obs.localization_healthy),
            comms=CommsState(uplink_ok=self.comms_ok, last_seen=self.clock()),
            active_skill_instance=status.get("active_step"), fc_failsafe_active=obs.fc_failsafe)

    def _latest_status(self) -> tuple[dict, FlightObservation] | None:
        written = []
        for _, _, directory in self._version_dirs():
            try:
                written.append(((directory / "status.json").stat().st_mtime_ns, directory / "status.json"))
            except OSError:
                continue
        if not written:
            return None
        try:
            status = json.loads(max(written)[1].read_text(encoding="utf-8"))
            obs = FlightObservation.model_validate(status["observation"])
        except (OSError, ValueError, KeyError):
            return None
        return (status, obs) if obs.robot_id == self.robot_id else None

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
    from drone_agent.mission.registry import load_registry

    registry = load_registry(args.root, args.scene, getattr(args, "platform", None))
    credentials = {"cert_pem": (args.tls / "robot.crt").read_bytes(), "key_pem": (args.tls / "robot.key").read_bytes(),
                   "ca_pem": (args.tls / "ca.crt").read_bytes()}
    if args.transport == "zenoh":
        # D046: `--service` is a Zenoh TLS endpoint such as tls/mission-service:7447. / `--service` 为 Zenoh TLS 端点。
        from drone_agent.fleet.transport_zenoh import ZenohFleetClient

        client = ZenohFleetClient(args.service, args.robot_id, **credentials)
    else:
        client = GrpcFleetClient(args.service, args.robot_id, server_name=args.server_name, **credentials)
    uplink = Uplink(client, robot_id=args.robot_id, trust=TrustStore.load(args.trust),
                    capability=registry.capability, inbox=args.inbox, mailbox=args.mailbox, aircraft=args.aircraft,
                    state_path=args.state / "uplink-state.json",
                    ground=registry if registry.kind == "ground_site" else None)
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
    parser.add_argument("--platform", type=Path, default=None,
                        help="platform file; default by the scene's kind / 平台文件；默认按场景类型取")
    parser.add_argument("--robot-id", default="uav_01")
    parser.add_argument("--service", default="mission-service:8450")
    parser.add_argument("--server-name", default="mission-service")
    parser.add_argument("--transport", choices=["grpc", "zenoh"], default="grpc")
    parser.add_argument("--tls", type=Path, default=Path("/tls"))
    parser.add_argument("--trust", type=Path, default=Path("/trust/trust.json"))
    parser.add_argument("--inbox", type=Path, default=Path("/inbox"))
    parser.add_argument("--mailbox", type=Path, default=Path("/mailbox"))
    parser.add_argument("--aircraft", type=Path, default=Path("/aircraft"))
    parser.add_argument("--state", type=Path, default=Path("/uplink"))
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
