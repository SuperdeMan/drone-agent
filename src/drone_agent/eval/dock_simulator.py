"""Logical dock simulator for S0 and the S1 virtual dock (P1, WP-P1-04): ACKs apart from physical results.

The simulator belongs to the simulated world, never to the mission service. Its pad sensor reads the world's truth
of where the aircraft is (the logical flight in S0, Gazebo truth in S1) the way a real dock's sensor sees the real
aircraft; it charges the aircraft's one battery; its lid moves over time. Requested actions are acknowledged at
once, but only the physical result that follows (lid open, charge grown) is ever reported as completed, so a
jammed lid or a stalled charger shows up as such. Faults are harness-only switches the service never reads; the
simulator's own truth log (what the lid and charger really did) is what the independent judge compares with the
service's decisions. The backend loop talks to the service API only, as the bound `dock:` identity.

S0 与 S1 虚拟机场的逻辑模拟器（P1，WP-P1-04）：ACK 与物理结果分离。

模拟器属于模拟世界，从不属于任务服务。机位传感器读取世界中飞行器位置的真值（S0 为逻辑飞行，S1 为 Gazebo
真值），如同真实机场传感器观测真实飞行器；它给飞行器唯一的电池充电；舱盖随时间移动。请求的动作立即确认，
但只有随后的物理结果（舱盖打开、电量增长）才会报告为完成，因此卡滞的舱盖或停滞的充电会如实显示。故障是
只在编排中使用的开关，服务从不读取；模拟器自己的真值日志（舱盖与充电实际做了什么）供独立裁判与服务决策比对。
后端循环只以绑定的 `dock:` 身份与服务 API 通信。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.eval.logical_flight import Battery


@dataclass
class DockFaults:
    """Harness-only switches; the service never reads them. / 只在编排中使用的开关；服务从不读取。"""

    lid_jam: bool = False  # open ACK accepted, the lid jams half-way / 开盖 ACK 受理后舱盖半途卡滞
    charge_stall: bool = False  # charge ACK accepted, the measured charge never grows / 充电 ACK 受理后实测电量不增长
    offline: bool = False  # the backend stops reporting / 后端停止报告
    stale_by_s: float = 0.0  # observation time this far in the past / 观测时间提前这么多
    future_by_s: float = 0.0  # observation time this far in the future / 观测时间推后这么多
    reorder: bool = False  # the next report reuses an older sequence number / 下一份报告复用旧序号
    replay_boot: str | None = None  # the next report claims this (replaced) boot session / 下一份报告冒用已替代的 boot
    false_presence: bool = False  # the pad sensor says present whatever it sees / 机位传感器总是报告在位
    reject_actions: bool = False  # every action is refused / 所有动作被拒绝
    upkeep: str = "normal"
    environment: str = "permitted"
    wind_mps: float = 2.0
    ready_threshold: float = 0.98  # the charge this dock calls ready / 该机场认为就绪的电量


class DockSimulator:
    """One logical dock with a deterministic clock step and a seeded boot identity. / 一个确定性步进的逻辑机场。"""

    def __init__(self, dock_id: str, *, battery: Battery, presence: Callable[[], tuple[list[float], bool] | None],
                 seed: int = 0, pad_radius_m: float = 2.0, lid_time_s: float = 0.6, charge_rate: float = 0.5,
                 cooling_s: float = 0.6):
        self.dock_id, self.battery, self.presence = dock_id, battery, presence
        self.random = random.Random(f"{dock_id}:{seed}")
        self.pad_radius_m, self.lid_time_s, self.charge_rate, self.cooling_s = pad_radius_m, lid_time_s, charge_rate, cooling_s
        self.faults = DockFaults()
        self.boot_id, self.seq, self.boots = self._boot(), 0, []
        self.lid, self.lid_target, self.lid_progress = "closed", None, 0.0
        self.charging, self.cooling_left, self.charged = False, 0.0, False
        self.actions: dict[str, dict] = {}
        self.truth: list[dict] = []
        self.present = self._presence()

    def _boot(self) -> str:
        return f"boot-{self.dock_id}-{self.random.getrandbits(40):010x}"

    def reboot(self) -> None:
        """A new boot session; pending action progress is lost like on a real controller. / 新 boot 会话；未完成的动作进度丢失。"""
        self.boots.append(self.boot_id)
        self.boot_id, self.seq, self.actions = self._boot(), 0, {}
        self.log("reboot")

    def _presence(self) -> str:
        seen = self.presence()
        if seen is None:
            return "unknown"
        position, in_air = seen
        on_pad = math.hypot(position[0], position[1]) <= self.pad_radius_m and position[2] < 0.3 and not in_air
        return "present" if on_pad else "absent"

    def energy(self) -> tuple[str, float | None]:
        """Phase and measured charge; an absent aircraft cannot be measured. / 补能阶段与实测电量；不在位的飞行器无法测量。"""
        if self.present != "present":
            return "unknown", None
        if self.charging:
            return "charging", self.battery.fraction
        if self.cooling_left > 0:
            return "cooling", self.battery.fraction
        return ("ready" if self.battery.fraction >= self.faults.ready_threshold else "idle"), self.battery.fraction

    # ── actions / 动作 ──

    def handle(self, action: dict) -> tuple[bool, str]:
        """Acknowledge a requested action; the physical result comes later or never. / 确认请求的动作；物理结果之后才有或没有。"""
        action_id, kind = action["action_id"], action["kind"]
        if action_id in self.actions:
            return True, "duplicate"
        if self.faults.reject_actions:
            self.log("action_refused", action_id=action_id, action=kind)
            return False, "refused_by_dock"
        record = {"kind": kind, "result": "in_progress"}
        if kind in ("open_lid", "close_lid"):
            target = "open" if kind == "open_lid" else "closed"
            if self.lid == target:
                record["result"] = "completed"
            elif self.lid == "jammed":
                record["result"] = "failed"
            else:
                self.lid_target, self.lid_progress = target, 0.0
                self.lid = "opening" if target == "open" else "closing"
        elif kind == "start_charge":
            if self.present != "present":
                self.log("action_refused", action_id=action_id, action=kind, reason="no_aircraft")
                return False, "no_aircraft"
            self.charging, self.cooling_left = True, 0.0
        else:
            return False, "unsupported_action"
        self.actions[action_id] = record
        self.log("action_acked", action_id=action_id, action=kind)
        return True, ""

    def _finish(self, kind: str, result: str) -> None:
        for record in self.actions.values():
            if record["kind"] == kind and record["result"] == "in_progress":
                record["result"] = result

    # ── physics / 物理过程 ──

    def advance(self, dt: float) -> None:
        """Move the lid, charge or cool the battery and read the pad sensor. / 移动舱盖、充电或冷却并读取机位传感器。"""
        before = self.present
        self.present = self._presence()
        if before == "present" and self.present == "absent":
            self.charging, self.cooling_left = False, 0.0
        if self.lid in ("opening", "closing"):
            self.lid_progress += dt / self.lid_time_s
            if self.faults.lid_jam and self.lid == "opening" and self.lid_progress >= 0.5:
                self.lid = "jammed"
                self._finish("open_lid", "failed")
                self.log("lid_jammed")
            elif self.lid_progress >= 1.0:
                self.lid = self.lid_target
                self._finish("open_lid" if self.lid == "open" else "close_lid", "completed")
                self.log("lid_" + self.lid)
        if self.charging:
            if not self.faults.charge_stall:
                self.battery.charge(self.charge_rate * dt)
            if self.battery.fraction >= 0.999:
                self.charging, self.cooling_left = False, self.cooling_s
                self._finish("start_charge", "completed")
                self.log("charged")
        elif self.cooling_left > 0:
            self.cooling_left = max(0.0, self.cooling_left - dt)

    def log(self, kind: str, **fields) -> None:
        phase, charge = self.energy()
        self.truth.append({"at": utcnow().isoformat(), "kind": kind, "dock_id": self.dock_id, "boot_id": self.boot_id,
                           "lid": self.lid, "present": self.present, "energy": phase, "charge": charge,
                           "upkeep": self.faults.upkeep, "offline": self.faults.offline,
                           "environment": self.faults.environment, "wind_mps": self.faults.wind_mps, **fields})

    def report(self, now: datetime) -> dict | None:
        """One status report, or None while offline; faults shape what is sent, never the truth log.

        一份状态报告，离线时为 None；故障只影响发送内容，从不影响真值日志。
        """
        if self.faults.offline:
            return None
        self.seq += 1
        seq = self.seq
        if self.faults.reorder:
            self.faults.reorder, seq = False, max(0, self.seq - 2)
        boot = self.boot_id
        if self.faults.replay_boot:
            boot, self.faults.replay_boot = self.faults.replay_boot, None
        observed = now - timedelta(seconds=self.faults.stale_by_s) + timedelta(seconds=self.faults.future_by_s)
        phase, charge = self.energy()
        report = {"schema_version": "0.1.0", "dock_id": self.dock_id, "boot_id": boot, "seq": seq,
                  "observed_at": observed.isoformat(), "link": "online", "lid": self.lid,
                  "aircraft": "present" if self.faults.false_presence else self.present,
                  "energy": {"state": phase, "charge_fraction": None if charge is None else round(charge, 4)},
                  "environment": {"state": self.faults.environment, "wind_mps": self.faults.wind_mps},
                  "upkeep": self.faults.upkeep,
                  "actions": [{"action_id": action_id, "result": record["result"]}
                              for action_id, record in list(self.actions.items())[-10:]]}
        self.log("report", seq=seq, reported_boot=boot, sent=report)
        return report


Call = Callable[..., Awaitable[dict]]


class DockBackend:
    """Pulls requested actions, acknowledges them and reports status through the service API only.

    只经服务 API 拉取请求的动作、确认并报告状态。
    """

    def __init__(self, principal: str, docks: dict[str, DockSimulator], call: Call):
        self.principal, self.docks, self.call = principal, docks, call
        self.transcript: list[dict] = []

    async def _call(self, method: str, **params) -> dict:
        response = await self.call(method, **params)
        self.transcript.append({"at": utcnow().isoformat(), "method": method,
                                "ok": response.get("ok"), "code": (response.get("issue") or {}).get("code"),
                                "result": response.get("result") if method != "docks.report" else
                                (response.get("result") or {})})
        return response

    async def cycle(self, now: datetime | None = None) -> None:
        for dock in self.docks.values():
            if dock.faults.offline:
                continue
            listed = await self._call("docks.actions", dock_id=dock.dock_id)
            for action in listed.get("result") or []:
                accepted, reason = dock.handle(action)
                await self._call("docks.ack", action_id=action["action_id"], accepted=accepted, reason=reason)
            report = dock.report(now or utcnow())
            if report is not None:
                await self._call("docks.report", report=report)


# ── S1: the virtual dock beside a PX4 SITL flight / S1：PX4 SITL 飞行旁的虚拟机场 ──


class TruthPresence:
    """Pad sensor over Gazebo truth: the newest sample of the newest truth file, latched while the collector is idle.

    基于 Gazebo 真值的机位传感器：最新真值文件的最新样本；采集器空闲时保持最后已知值。
    """

    def __init__(self, pattern: str, root: Path, initial: tuple[list[float], bool] = ([0.0, 0.0, 0.0], False)):
        self.pattern, self.root, self.last = pattern, root, initial

    def __call__(self) -> tuple[list[float], bool]:
        files = sorted(self.root.glob(self.pattern), key=lambda p: p.stat().st_mtime) if self.root.is_dir() else []
        for path in reversed(files[-1:]):
            try:
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 4096))
                    lines = [line for line in stream.read().splitlines() if line.strip()]
                sample = json.loads(lines[-1]) if lines else None
            except (OSError, ValueError):
                sample = None
            if sample and "position" in sample:
                position = [float(v) for v in sample["position"]]
                self.last = (position, position[2] > 0.3)
        return self.last


async def serve(args) -> None:
    from drone_agent.fleet.api import ApiClient

    battery = Battery(1.0)
    presence = TruthPresence(args.truth_glob, args.truth_root)
    dock = DockSimulator(args.dock, battery=battery, presence=presence, seed=args.seed, lid_time_s=args.lid_time,
                         charge_rate=args.charge_rate, cooling_s=args.cooling)
    client = ApiClient(args.api, actor=args.principal, trust="backend")

    async def call(method: str, **params) -> dict:
        try:
            return await client.call(method, **params)
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            return {"ok": False, "issue": {"code": "service.degraded", "message": type(error).__name__}}

    backend = DockBackend(args.principal, {args.dock: dock}, call)
    previous, departed = utcnow(), False
    while True:
        now = utcnow()
        if args.control is not None and args.control.is_file():
            try:
                wanted = json.loads(args.control.read_text(encoding="utf-8"))
                for key, value in wanted.items():
                    if hasattr(dock.faults, key):
                        setattr(dock.faults, key, value)
                if wanted.get("reboot_token") and wanted["reboot_token"] != getattr(dock, "reboot_token", None):
                    dock.reboot_token = wanted["reboot_token"]
                    dock.reboot()
            except (OSError, ValueError):
                pass
        dock.advance((now - previous).total_seconds())
        # The logical battery drains while the aircraft is away; the SITL battery itself is PX4's (logical model).
        # 飞行器离开期间逻辑电池放电；SITL 电池本身由 PX4 模拟（逻辑模型）。
        if dock.present == "absent":
            departed = True
        elif dock.present == "present" and departed:
            departed = False
            battery.fraction = min(battery.fraction, args.return_charge)
        previous = now
        await backend.cycle(now)
        if args.log is not None:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            with args.log.open("a", encoding="utf-8") as stream:
                for entry in dock.truth:
                    stream.write(json.dumps(entry) + "\n")
            dock.truth.clear()
        await asyncio.sleep(args.period)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="run as the virtual dock backend of one dock")
    parser.add_argument("--api", type=Path, default=Path("/run/mission/api.sock"))
    parser.add_argument("--principal", required=True)
    parser.add_argument("--dock", required=True)
    parser.add_argument("--truth-root", type=Path, default=Path("/truth"))
    parser.add_argument("--truth-glob", default="truth.jsonl")
    parser.add_argument("--control", type=Path, help="harness-only fault switches (JSON), never a service input")
    parser.add_argument("--log", type=Path, help="append the dock's own truth log here")
    parser.add_argument("--period", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--lid-time", type=float, default=3.0)
    parser.add_argument("--charge-rate", type=float, default=0.05)
    parser.add_argument("--cooling", type=float, default=5.0)
    parser.add_argument("--return-charge", type=float, default=0.6)
    args = parser.parse_args()
    if not args.serve:
        parser.error("only --serve is supported from the command line")
    print(json.dumps({"dock": args.dock, "principal": args.principal, "faults": asdict(DockFaults())}), flush=True)
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
