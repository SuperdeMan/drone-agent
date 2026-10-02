"""S3 vendor dock simulator (P5, D072): a vendor-managed dock and its aircraft behind the pinned task protocol.

The simulator is the vendor's side of `configs/vendors/dock_task_v1.yaml`: it answers the four whitelisted commands,
deduplicates by transaction within the profile's window, refuses to execute a flight twice, runs a prepared task over
simulated time (lid, takeoff, waypoints, the photo, return, landing, charging), and reports progress, media, return
home information and dock state. Harness-only faults duplicate or reorder what it sends, drop replies or the terminal
event, reject preparation, withhold or corrupt the media, freeze progress, or drop the link with or without replaying
the last progress on reconnect. Its truth log (every command received, every execution actually started, every
capture) is what the independent judge compares with the service; the service never reads it. Captures are logical
frames of the asset's appearance (`scripted_vision.frame`); nothing here is a real dock or a real vendor's firmware.

S3 厂商机场模拟器（P5，D072）：固定任务协议背后的厂商托管机场与其飞行器。

模拟器是 `configs/vendors/dock_task_v1.yaml` 的厂商一侧：回答四个白名单命令，在档案的窗口内按事务去重，拒绝同一飞行执行两次，
在模拟时间上运行已准备的任务（舱盖、起飞、航点、拍照、返航、降落、充电），并上报进度、媒体、返航信息与机场状态。只在编排中
使用的故障可使它重复或乱序发送、丢弃答复或终态事件、拒绝准备、扣留或损坏媒体、冻结进度，或断开链路（重连时重放或不重放最近
进度）。它的真值日志（收到的每个命令、实际开始的每次执行、每次拍摄）供独立裁判与服务比对；服务从不读取它。拍摄是资产外观的
逻辑帧（`scripted_vision.frame`）；这里没有真实机场，也没有任何真实厂商的固件。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import random
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.eval.scripted_vision import frame

READY = 0.98


@dataclass
class VendorFaults:
    """Harness-only switches; the service never reads them. / 只在编排中使用的开关；服务从不读取。"""

    duplicate: bool = False  # every reply and event is sent twice / 每个答复与事件都发送两次
    reorder: bool = False  # progress events leave in swapped pairs / 进度事件两两交换顺序发出
    drop_replies: dict[str, int] = field(default_factory=dict)  # method -> replies to drop / 方法 -> 要丢弃的答复数
    reject_prepare: int = 0  # nonzero: the result code of every prepare / 非零：每次准备的结果码
    missing_media: bool = False  # the photo is taken but never uploaded / 拍了照但从不上传
    corrupt_media: bool = False  # the uploaded digest does not match the bytes / 上传的摘要与字节不符
    drop_terminal: bool = False  # the terminal progress event is never sent / 终态进度事件从不发送
    stuck_at_step: int | None = None  # progress freezes at this step, no terminal / 进度在该步冻结，没有终态
    slow_open_s: float = 0.0  # real seconds the lid takes before any progress / 任何进度之前舱盖开启所需的真实秒数
    slow_return_s: float = 0.0  # real seconds a return home takes at least / 返航至少需要的真实秒数
    disconnect_at_step: int | None = None  # the link drops when the flight reaches this step / 飞到该步时链路断开
    down_for_s: float | None = 3.0  # how long the link stays down; None: for good / 链路断开多久；None 为永久
    replay_on_reconnect: bool = True  # the profile's reconnect behaviour / 档案声明的重连行为
    upkeep: str = "normal"
    environment: str = "permitted"
    wind_mps: float = 2.0


class VendorDockSim:
    """One vendor-managed dock with one aircraft, stepped on simulated time. / 一个厂商托管机场及其一架飞行器。"""

    def __init__(self, dock_id: str, robot_id: str, *, signatures: dict[str, str], home=(0.0, 0.0, 0.0),
                 speed: float = 1.0, seed: int = 0, protocol: str = "sim-dock-task/1.0", dedup_window_s: float = 600,
                 state_period_s: float = 1.0, prepared_ttl_s: float = 120.0, clock=utcnow):
        self.dock_id, self.robot_id, self.speed, self.clock = dock_id, robot_id, speed, clock
        self.signatures = dict(signatures)
        self.damaged: set[str] = set()
        self.home = list(home)
        self.protocol, self.dedup_window_s, self.state_period_s = protocol, dedup_window_s, state_period_s
        self.prepared_ttl_s = prepared_ttl_s
        self.random = random.Random(seed)
        self.faults = VendorFaults()
        self.boot_id = f"vendor-{seed:04d}-{uuid.uuid4().hex[:10]}"
        self.link_up, self.down_until = True, None
        self.outbox: list[dict] = []
        self.held: list[dict] = []
        self.t = 0.0
        self.last_state = -1e9
        self.state_seq = 0
        self.position = list(home)
        self.airborne = False
        self.battery = 1.0
        self.charging = False
        self.lid = "closed"
        self.in_dock = True
        self.replies: dict[str, tuple[float, dict]] = {}
        self.prepared: dict[str, dict] = {}
        self.executed: dict[str, str] = {}  # flight -> status / 飞行 -> 状态
        self.active: dict | None = None
        self.last_progress: dict | None = None
        self.captures = 0
        self.truth: dict = {"commands": [], "executions": [], "captures": [], "link": [], "terminals": []}

    # ── link / 链路 ──

    def connect(self) -> None:
        """A new session: hello, then the last progress when the profile replays it. / 新会话：hello，再按档案重放最近进度。"""
        self._raw({"kind": "hello", "boot_id": self.boot_id, "protocol": self.protocol, "gateway": self.dock_id})
        if self.faults.replay_on_reconnect and self.last_progress is not None:
            self._raw(dict(self.last_progress))
        self._state(force=True)

    def drop_link(self, reason: str) -> None:
        self.link_up = False
        self.outbox.clear()
        self.held.clear()
        down = self.faults.down_for_s
        self.down_until = None if down is None else self.t + down * self.speed
        self.truth["link"].append({"at": self.clock().isoformat(), "state": "down", "reason": reason})

    def take(self) -> list[dict]:
        items, self.outbox = self.outbox, []
        return items

    def _raw(self, message: dict) -> None:
        if self.link_up:
            self.outbox.append(message)

    def _emit(self, message: dict) -> None:
        copies = 2 if self.faults.duplicate else 1
        for _ in range(copies):
            self._raw(json.loads(json.dumps(message)))

    def _progress(self, flight: str, status: str, step: int, total: int) -> None:
        flight_state = self.active if self.active and self.active["flight_id"] == flight else None
        seq = flight_state["seq"] = flight_state["seq"] + 1 if flight_state else 0
        message = {"kind": "event", "tid": str(uuid.uuid4()), "bid": flight, "ts": self.clock().isoformat(),
                   "method": "flighttask_progress", "seq": seq, "need_reply": False,
                   "data": {"flight_id": flight, "status": status, "current_step": step,
                            "percent": round(100 * step / max(total, 1), 1)}}
        self.last_progress = message
        if status in ("ok", "failed", "canceled", "partially_done", "timeout") and self.faults.drop_terminal:
            return
        if self.faults.reorder and status not in ("ok", "failed", "canceled"):
            if self.held:
                self._emit(message)
                self._emit(self.held.pop())
            else:
                self.held.append(message)
            return
        for held in self.held:
            self._emit(held)
        self.held.clear()
        self._emit(message)

    # ── commands / 命令 ──

    def receive(self, message: dict) -> None:
        """One command from the gateway. / 网关的一个命令。"""
        method, tid, flight = message.get("method"), message.get("tid"), message.get("bid")
        self.truth["commands"].append({"at": self.clock().isoformat(), "method": method, "tid": tid, "flight_id": flight})
        cached = self.replies.get(tid)
        if cached is not None and self.t - cached[0] <= self.dedup_window_s * self.speed:
            self._reply(method, cached[1])
            return
        handler = {"flighttask_prepare": self._prepare, "flighttask_execute": self._execute,
                   "flighttask_undo": self._undo, "return_home": self._return_home}.get(method)
        result, output = (314000, {"reason": "unknown_method"}) if handler is None else handler(message)
        reply = {"kind": "service_reply", "tid": tid, "bid": flight, "ts": self.clock().isoformat(), "method": method,
                 "data": {"result": result, "output": output}}
        self.replies[tid] = (self.t, reply)
        self._reply(method, reply)

    def _reply(self, method: str, reply: dict) -> None:
        if self.faults.drop_replies.get(method, 0) > 0:
            self.faults.drop_replies[method] -= 1
            return
        self._emit({**reply, "ts": self.clock().isoformat()})

    def _prepare(self, message: dict) -> tuple[int, dict]:
        data = message.get("data") or {}
        flight = data.get("flight_id")
        if self.faults.reject_prepare:
            return self.faults.reject_prepare, {"reason": "rejected_by_fault"}
        if not flight or not isinstance(data.get("steps"), list) or len(data["steps"]) < 2:
            return 314100, {"reason": "invalid_task"}
        if flight in self.executed:
            return 314002, {"reason": "already_executed"}
        if self.active is not None or (self.prepared and flight not in self.prepared):
            return 314003, {"reason": "busy"}
        if not self.in_dock or self.battery < READY or self.charging:
            return 314004, {"reason": "not_ready"}
        self.prepared[flight] = {"task": data, "at": self.t}
        return 0, {"status": "prepared"}

    def _execute(self, message: dict) -> tuple[int, dict]:
        flight = (message.get("data") or {}).get("flight_id")
        if flight in self.executed or (self.active and self.active["flight_id"] == flight):
            return 314002, {"reason": "already_executed"}
        prepared = self.prepared.pop(flight, None)
        if prepared is None:
            return 314001, {"reason": "not_prepared"}
        task = prepared["task"]
        steps = task["steps"]
        self.active = {"flight_id": flight, "task": task, "steps": steps, "index": 0, "elapsed": 0.0,
                       "phase": "opening", "seq": -1, "returning": False, "media": 0}
        self.executed[flight] = "in_progress"
        self.truth["executions"].append({"at": self.clock().isoformat(), "flight_id": flight,
                                         "task_sha256": task.get("task_sha256")})
        self.lid = "opening"
        return 0, {"status": "executing"}

    def _undo(self, message: dict) -> tuple[int, dict]:
        flight = (message.get("data") or {}).get("flight_id")
        if self.prepared.pop(flight, None) is not None:
            self.executed[flight] = "undone"
            return 0, {"status": "undone"}
        if self.active and self.active["flight_id"] == flight:
            return 314010, {"reason": "executing_use_return_home"}
        return 314001, {"reason": "not_prepared"}

    def _return_home(self, message: dict) -> tuple[int, dict]:
        flight = (message.get("data") or {}).get("flight_id")
        if not self.active or self.active["flight_id"] != flight:
            return 314011, {"reason": "not_executing"}
        if not self.active["returning"]:
            self.active["returning"] = True
            self._emit({"kind": "event", "tid": str(uuid.uuid4()), "bid": flight, "ts": self.clock().isoformat(),
                        "method": "return_home_info", "seq": 100000 + self.active["seq"] + 1,
                        "data": {"flight_id": flight, "state": "returning"}})
        return 0, {"status": "returning"}

    # ── time / 时间 ──

    def step(self, dt: float) -> None:
        """Advance simulated time by `dt` real seconds times the speed. / 按速度推进 `dt` 真实秒的模拟时间。"""
        sim = dt * self.speed
        self.t += sim
        if not self.link_up and self.down_until is not None and self.t >= self.down_until:
            self.link_up, self.down_until = True, None
            self.truth["link"].append({"at": self.clock().isoformat(), "state": "up"})
            self.connect()
        for flight, prepared in list(self.prepared.items()):
            if self.t - prepared["at"] > self.prepared_ttl_s * self.speed:
                self.prepared.pop(flight)  # an unexecuted preparation expires / 未执行的准备过期
        if self.active is not None:
            self._fly(sim)
        elif self.in_dock:
            if self.lid in ("open", "closing"):
                self.lid = "closed"
            if self.battery < 1.0:
                self.charging = True
                self.battery = min(1.0, self.battery + 0.03 * sim)
            else:
                self.charging = False
        if self.t - self.last_state >= self.state_period_s * self.speed:
            self._state()

    def _fly(self, sim: float) -> None:
        flight = self.active
        steps, total = flight["steps"], len(flight["steps"])
        flight["elapsed"] += sim
        if flight["phase"] == "opening":
            if flight["elapsed"] >= max(2.0, self.faults.slow_open_s * self.speed):
                self.lid, flight["phase"], flight["elapsed"] = "open", "flying", 0.0
                self._progress(flight["flight_id"], "in_progress", 0, total)
            return
        if self.airborne:
            self.battery = max(0.0, self.battery - 0.004 * sim)
        if flight["returning"]:
            self._go_home(flight, sim)
            return
        index = flight["index"]
        if self.faults.stuck_at_step is not None and index >= self.faults.stuck_at_step:
            return
        if self.faults.disconnect_at_step is not None and index >= self.faults.disconnect_at_step and self.link_up:
            self.faults.disconnect_at_step = None
            self.drop_link(f"fault at step {index}")
        step = steps[index]
        target = list(step["position"])
        need = self._duration(step, target)
        if flight["elapsed"] < need:
            return
        flight["elapsed"] = 0.0
        self.position = target
        if step["kind"] == "takeoff":
            self.airborne, self.in_dock, self.charging = True, False, False
        elif step["kind"] == "photo":
            self._capture(flight, step)
        elif step["kind"] == "land":
            self.airborne, self.in_dock = False, True
            self.lid = "closing"
        flight["index"] = index + 1
        if flight["index"] >= total:
            self._finish(flight, "ok", total)
        else:
            self._progress(flight["flight_id"], "in_progress", flight["index"], total)

    def _duration(self, step: dict, target: list) -> float:
        if step["kind"] in ("takeoff", "land"):
            return 3.0
        if step["kind"] == "photo":
            return 1.0
        distance = math.dist(self.position, target)
        return max(1.0, distance / 2.0)

    def _go_home(self, flight: dict, sim: float) -> None:
        need = max(max(3.0, math.dist(self.position, self.home) / 2.0) + 3.0, self.faults.slow_return_s * self.speed)
        if flight["elapsed"] >= need:
            self.position, self.airborne, self.in_dock, self.lid = list(self.home), False, True, "closing"
            self._emit({"kind": "event", "tid": str(uuid.uuid4()), "bid": flight["flight_id"],
                        "ts": self.clock().isoformat(), "method": "return_home_info",
                        "seq": 100000 + flight["seq"] + 2, "data": {"flight_id": flight["flight_id"], "state": "landed"}})
            self._finish(flight, "canceled", flight["index"])

    def _finish(self, flight: dict, status: str, step: int) -> None:
        self.executed[flight["flight_id"]] = status
        self.truth["terminals"].append({"at": self.clock().isoformat(), "flight_id": flight["flight_id"],
                                        "status": status, "step": step})
        self._progress(flight["flight_id"], status, step, len(flight["steps"]))
        self.active = None

    def _capture(self, flight: dict, step: dict) -> None:
        asset = step.get("asset_id")
        self.captures += 1
        damaged = asset in self.damaged
        raw = frame(self.signatures.get(asset), damaged=damaged, nonce=self.captures + 4096)
        digest = hashlib.sha256(raw).hexdigest()
        self.truth["captures"].append({"at": self.clock().isoformat(), "flight_id": flight["flight_id"],
                                       "asset_id": asset, "state": "damaged" if damaged else "normal",
                                       "sha256": digest, "uploaded": not self.faults.missing_media})
        if self.faults.missing_media:
            return
        now = self.clock()
        x, y, z = step["position"]
        frame_ref = flight["task"].get("frame") or {}
        self._emit({"kind": "event", "tid": str(uuid.uuid4()), "bid": flight["flight_id"], "ts": now.isoformat(),
                    "method": "file_upload_callback", "seq": 200000 + self.captures, "need_reply": False,
                    "data": {"flight_id": flight["flight_id"], "step_index": step["index"],
                             "object_key": f"{self.dock_id}/{flight['flight_id']}/{step['index']}.rgb",
                             "media_type": "image/x-raw-rgb", "width": 160, "height": 120,
                             "sha256": ("0" * 64) if self.faults.corrupt_media else digest,
                             "media_b64": base64.b64encode(raw).decode(), "captured_at": now.isoformat(),
                             "pose": {"x": x + self.random.uniform(-0.2, 0.2), "y": y + self.random.uniform(-0.2, 0.2),
                                      "z": z, "covariance": [0.25, 0, 0, 0, 0.25, 0, 0, 0, 0.25]},
                             "frame": {"frame_id": frame_ref.get("frame_id", ""),
                                       "map_version": frame_ref.get("map_version", "")}}})

    def _state(self, force: bool = False) -> None:
        self.last_state = self.t
        self.state_seq += 1
        faults = self.faults
        self._raw({"kind": "state", "ts": self.clock().isoformat(), "seq": self.state_seq, "data": {
            "dock": {"lid": self.lid, "aircraft_in_dock": self.in_dock, "charging": self.charging,
                     "charge_fraction": round(self.battery, 4), "wind_mps": faults.wind_mps,
                     "environment": faults.environment, "upkeep": faults.upkeep},
            "aircraft": {"position": [round(v, 3) for v in self.position],
                         "flight_phase": "airborne" if self.airborne else "grounded",
                         "battery": round(self.battery, 4), "mode": "mission" if self.active else "idle"}}})

    def export(self) -> dict:
        return {"dock_id": self.dock_id, "robot_id": self.robot_id, "boot_id": self.boot_id,
                "faults": asdict(self.faults), "truth": self.truth}


class MemoryLink:
    """The gateway's link to an in-process simulator (S3 matrix). / 网关到进程内模拟器的链路（S3 矩阵）。"""

    def __init__(self, sim: VendorDockSim):
        self.sim = sim
        self.started = False

    @property
    def connected(self) -> bool:
        return self.sim.link_up

    async def ensure(self) -> bool:
        if not self.started:
            self.started = True
            self.sim.connect()
        return self.sim.link_up

    async def send(self, message: dict) -> bool:
        if not self.sim.link_up:
            return False
        self.sim.receive(json.loads(json.dumps(message)))
        return True

    def drain(self) -> list[dict]:
        return self.sim.take()


# ── the resident simulator process (P5 desk) / 常驻模拟器进程（P5 任务台）──


def load_faults(path: Path | None) -> dict:
    """Harness fault switches; absent or unreadable keeps the defaults. A new `drop_link_now` token drops the link once.

    编排故障开关；缺失或不可读时保持默认。新的 `drop_link_now` 令牌使链路断开一次。
    """
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(data.get("faults") or {}) if isinstance(data, dict) else {}


def load_damaged(path: Path | None) -> set[str]:
    """Assets the desk world file shows damaged or obstructed at the vendor site (section `s3`).

    任务台世界文件中厂商站点（`s3` 一节）处于损伤或遮挡状态的资产。
    """
    from drone_agent.eval.s0_fleet import read_world

    return {asset for asset, state in read_world(path, "s3").items() if state != "normal"}


def flush_truth(sim: VendorDockSim, log: Path) -> None:
    """Append the new truth rows and keep a small state record. / 追加新的真值行并保留一个小的状态记录。"""
    from drone_agent.eval.s0_fleet import append_rows

    rows = [{"kind": kind, **entry} for kind, entries in sim.truth.items() for entry in entries]
    for entries in sim.truth.values():
        entries.clear()
    append_rows(log / "truth.jsonl", sorted(rows, key=lambda row: row["at"]))
    state = {"boot_id": sim.boot_id, "faults": asdict(sim.faults), "link_up": sim.link_up, "executed": sim.executed,
             "active": (sim.active or {}).get("flight_id"), "at": sim.clock().isoformat()}
    pending = log / "state.json.pending"
    pending.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    pending.replace(log / "state.json")


async def serve(sim: VendorDockSim, socket_path: Path, *, control: Path | None, log: Path | None,
                world: Path | None = None) -> None:
    """Serve one gateway at a time over a Unix socket; the link faults close and refuse the connection.

    经 Unix 套接字一次服务一个网关；链路故障关闭并拒绝连接。
    """
    client: dict = {"writer": None}
    known_faults: dict = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not sim.link_up:
            writer.close()
            return
        previous = client["writer"]
        if previous is not None:
            previous.close()
        client["writer"] = writer
        sim.connect()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                with contextlib.suppress(ValueError):
                    sim.receive(json.loads(line))
        except (OSError, asyncio.LimitOverrunError):
            pass
        finally:
            if client["writer"] is writer:
                client["writer"] = None
            writer.close()

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path=str(socket_path), limit=1024 * 1024)
    socket_path.chmod(0o660)
    last = asyncio.get_running_loop().time()
    tick = 0
    while True:
        await asyncio.sleep(0.1)
        now = asyncio.get_running_loop().time()
        sim.step(now - last)
        last = now
        tick += 1
        if tick % 10 == 0:
            faults = load_faults(control)
            if faults != known_faults:
                for name, value in faults.items():
                    if hasattr(sim.faults, name):
                        setattr(sim.faults, name, value)
                if faults.get("drop_link_now") and faults.get("drop_link_now") != known_faults.get("drop_link_now")                         and sim.link_up:
                    sim.drop_link("harness")
                known_faults = faults
            sim.damaged = load_damaged(world)
            if log is not None:
                log.mkdir(parents=True, exist_ok=True)
                flush_truth(sim, log)
        writer = client["writer"]
        if writer is not None and not sim.link_up:
            writer.close()
            client["writer"] = None
            continue
        if writer is not None:
            for message in sim.take():
                writer.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            with contextlib.suppress(OSError, ConnectionError):
                await writer.drain()
        else:
            sim.take()
    server.close()


def main() -> None:
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--scene", type=Path, required=True, help="the vendor site's map (assets and home)")
    parser.add_argument("--dock", required=True)
    parser.add_argument("--robot", required=True)
    parser.add_argument("--control", type=Path, help="harness-only fault switches (JSON), never a service input")
    parser.add_argument("--world", type=Path, help="the desk world file; its `s3` section damages assets")
    parser.add_argument("--log", type=Path, help="directory for the truth log and the executed flights")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    scene = yaml.safe_load(args.scene.read_text(encoding="utf-8"))
    signatures = {asset: entry["visual_signature"] for asset, entry in scene["assets"].items()}
    home = scene["landing_sites"]["home_pad"]["position"]
    sim = VendorDockSim(args.dock, args.robot, signatures=signatures, home=tuple(home), speed=args.speed,
                        seed=args.seed or int.from_bytes(os.urandom(2), "big"))
    if args.log is not None and (args.log / "state.json").is_file():
        # A restarted endpoint still refuses to execute a flight it already executed. / 重启后的端点仍拒绝重复执行。
        with contextlib.suppress(OSError, ValueError):
            sim.executed.update(json.loads((args.log / "state.json").read_text(encoding="utf-8")).get("executed") or {})
    asyncio.run(serve(sim, args.socket, control=args.control, log=args.log, world=args.world))


if __name__ == "__main__":
    main()

