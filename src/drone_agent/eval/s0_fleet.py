"""The resident logical fleet of the P5 desk (D069): logical aircraft and their docks as one long-running process.

Every logical robot of the operations catalog whose dock this process's principal backs runs the real uplink, guardian
and executive around the logical flight adapter (`eval/logical_uav.py`), and reaches the mission service exactly like
the PX4 SITL aircraft: over mTLS gRPC with its own client certificate, verifying every package with the desk's trust
file. Its docks report through the API socket as the bound `dock:` identity. The camera renders the logical frame of
each asset as the desk world file says (normal, damaged or obstructed); only the host's harness writes that file, and
the mission service never reads it. A restart never flies a version again: a version with a flight directory has been
flown. Positions and flights are appended to bounded logs for the soak judge.

P5 任务台的常驻逻辑机队（D069）：逻辑飞行器及其机场作为一个常驻进程。

运营目录中、其机场由本进程主体负责的每台逻辑机器人，都运行围绕逻辑飞行适配器（`eval/logical_uav.py`）的真实 uplink、
guardian 与 executive，并与 PX4 SITL 飞行器完全相同地连接任务服务：经 mTLS gRPC、用自己的客户端证书，并用任务台的信任
文件验证每个任务包。其机场以绑定的 `dock:` 身份经 API 套接字报告。相机按任务台世界文件（正常、损伤或遮挡）渲染各资产的
逻辑帧；只有主机上的编排写该文件，任务服务从不读取。重启从不重飞某个版本：有飞行目录的版本即已飞过。位置与飞行追加到有界
日志，供长稳裁判使用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.eval.dock_simulator import DockBackend, DockSimulator
from drone_agent.eval.logical_flight import Battery
from drone_agent.eval.logical_uav import PACKAGE_FILE, LogicalUav, quick_registry
from drone_agent.eval.scripted_vision import frame
from drone_agent.fleet.dispatch import static_capability
from drone_agent.fleet.resources import load_catalog
from drone_agent.runtime.signing import TrustStore

POLICY = "configs/recovery_policies/multirotor_m1_v1.yaml"
LOG_LIMIT = 32 * 1024 * 1024
STATES = ("normal", "damaged", "obstructed")


def read_world(path: Path | None, section: str) -> dict[str, str]:
    """One section of the desk world file: asset -> state; unreadable or absent means all normal.

    任务台世界文件的一节：资产 -> 状态；不可读或缺失即全部正常。
    """
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != "drone.desk-world/v1":
        return {}
    entries = data.get(section) or {}
    return {asset: state for asset, state in entries.items() if isinstance(asset, str) and state in STATES}


def append_rows(path: Path, rows: list[dict]) -> None:
    """Append JSON lines and keep one previous file past the size limit. / 追加 JSON 行，超限时保留一个旧文件。"""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    if path.stat().st_size > LOG_LIMIT:
        path.replace(path.with_name(path.name + ".1"))


def flown_versions(directory: Path) -> set[str]:
    """Package files whose version already has a flight directory. / 已有飞行目录的任务包文件名。"""
    found = set()
    for mission in (directory / "aircraft").glob("m-*") if (directory / "aircraft").is_dir() else []:
        for version in mission.glob("v*"):
            name = f"{mission.name}-{version.name}.json"
            if PACKAGE_FILE.fullmatch(name):
                found.add(name)
    return found


class ResidentUav(LogicalUav):
    """A logical aircraft at the uplink's own status rate (1 Hz), not the stepped S0 world's every cycle.

    以 uplink 自身状态频率（1 Hz）运行的逻辑飞行器，而不是 S0 步进世界的每个周期。
    """

    async def cycle(self) -> None:
        await self.uplink.cycle()
        if self.task is not None and self.task.done():
            error = None if self.task.cancelled() else self.task.exception()
            if error is not None and self.flights:
                self.flights[-1]["error"] = f"{type(error).__name__}: {error}"[:300]
            self.task = None
        if self.task is None:
            package = self.pending()
            if package is not None:
                self.flown.add(package.name)
                self.task = asyncio.create_task(self.fly(package))


class Fleet:
    """The logical robots and docks one principal backs. / 一个主体负责的逻辑机器人与机场。"""

    def __init__(self, args):
        from drone_agent.fleet.api import ApiClient
        from drone_agent.fleet.transport import GrpcFleetClient

        self.args = args
        root = args.root
        catalog = load_catalog(root / args.catalog)
        trust = TrustStore.load(args.trust)
        self.uavs: dict[str, LogicalUav] = {}
        self.docks: dict[str, DockSimulator] = {}
        self.captures: list[dict] = []
        # Every capture carries a nonce that only grows, from a random start per process, so two captures never share
        # bytes across flushes and restarts (a repeated image would be a replay to the business rules, D063).
        # 每次拍摄带一个只增不减的 nonce，每个进程从随机值开始，因此刷新与重启前后两次拍摄的字节从不相同（重复影像在业务规则
        # 看来是重放，D063）。
        self.nonce = int.from_bytes(os.urandom(3), "big")
        seed = int.from_bytes(os.urandom(4), "big")
        for robot_id, entry in sorted(catalog.robots.items()):
            dock = catalog.docks[entry.dock_id]
            if entry.execution_backend != "logical_sim" or dock.vendor_managed or dock.backend.principal != args.principal:
                continue
            capability = static_capability(root, entry.platform, robot_id)
            registry = quick_registry(root, root / catalog.sites[entry.site_id].scene, capability)
            tls = args.tls / robot_id
            client = GrpcFleetClient(args.service, robot_id, server_name=args.server_name,
                                     cert_pem=(tls / "robot.crt").read_bytes(), key_pem=(tls / "robot.key").read_bytes(),
                                     ca_pem=(tls / "ca.crt").read_bytes())
            battery = Battery(1.0)
            uav = ResidentUav(args.state / robot_id, registry, None, trust, root / POLICY, battery=battery, client=client)
            uav.flown = flown_versions(args.state / robot_id)
            signatures = {asset: data.get("visual_signature") for asset, data in registry.data["assets"].items()}
            uav.camera = self.camera(robot_id, signatures)
            self.uavs[robot_id] = uav
            # Every process start is a new boot session of each dock. / 每次进程启动都是各机场的新会话。
            self.docks[entry.dock_id] = DockSimulator(entry.dock_id, battery=battery, presence=uav.presence,
                                                      seed=seed + len(self.docks))
        if not self.uavs:
            raise SystemExit(f"no logical robot is backed by {args.principal}")
        api = ApiClient(args.api, actor=args.principal, trust="backend")

        async def call(method: str, **params) -> dict:
            try:
                return await api.call(method, **params)
            except (OSError, RuntimeError, ValueError, TimeoutError) as error:
                return {"ok": False, "issue": {"code": "service.degraded", "message": type(error).__name__}}

        self.backend = DockBackend(args.principal, self.docks, call)
        self.logged_flights = dict.fromkeys(self.uavs, 0)
        self.sampled: dict[str, float] = {}

    def camera(self, robot_id: str, signatures: dict[str, str]):
        """The logical camera: the asset's frame as the world file says now. / 逻辑相机：按世界文件当前所述渲染资产帧。"""
        def capture(asset_id: str) -> bytes:
            state = read_world(self.args.world, "s0").get(asset_id, "normal")
            self.nonce += 1
            self.captures.append({"at": utcnow().isoformat(), "robot_id": robot_id, "asset_id": asset_id,
                                  "state": state, "nonce": self.nonce})
            return frame(signatures.get(asset_id), damaged=state != "normal", nonce=self.nonce)
        return capture

    def flush(self) -> None:
        """Positions, captures and finished flights to the logs. / 把位置、拍摄与已结束的飞行写入日志。"""
        state = self.args.state
        for robot_id, uav in self.uavs.items():
            rows, uav.truth = uav.truth, []
            kept = []
            for row in rows:
                # Two positions a second are enough for the soak judge. / 每秒两个位置足够长稳裁判使用。
                if row["mono"] - self.sampled.get(robot_id, -1.0) >= 0.5:
                    self.sampled[robot_id] = row["mono"]
                    kept.append(row)
            append_rows(state / robot_id / "truth.jsonl", kept)
            done = [f for f in uav.flights[self.logged_flights[robot_id]:] if f.get("ended_at")]
            if done and len(done) == len(uav.flights) - self.logged_flights[robot_id]:
                append_rows(state / robot_id / "flights.jsonl", done)
                self.logged_flights[robot_id] = len(uav.flights)
        captures, self.captures = self.captures, []
        append_rows(state / "captures.jsonl", captures)
        for dock in self.docks.values():
            rows, dock.truth = dock.truth, []
            append_rows(state / "docks" / f"{dock.dock_id}.jsonl", rows)

    async def run(self, stop: asyncio.Event) -> None:
        previous, reported = time.monotonic(), 0.0
        while not stop.is_set():
            now = time.monotonic()
            for dock in self.docks.values():
                dock.advance(now - previous)
            previous = now
            if now - reported >= self.args.report_period:
                reported = now
                await self.backend.cycle(utcnow())
            for uav in self.uavs.values():
                try:
                    await uav.cycle()
                except Exception as error:  # a link error never stops the fleet / 链路错误从不停止机队
                    uav.flights.append({"robot_id": uav.robot_id, "error": f"{type(error).__name__}: {error}"[:300],
                                        "started_at": utcnow().isoformat(), "ended_at": utcnow().isoformat()})
            self.flush()
            (self.args.state / "heartbeat.json").write_text(json.dumps({
                "at": utcnow().isoformat(), "robots": {r: {"flying": u.task is not None, "flown": len(u.flown)}
                                                       for r, u in self.uavs.items()}}), encoding="utf-8")
            try:
                await asyncio.wait_for(stop.wait(), self.args.period)
            except TimeoutError:
                pass
        for uav in self.uavs.values():
            if uav.task is not None:
                uav.task.cancel()
        self.flush()


async def serve(args) -> None:
    fleet = Fleet(args)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    print(json.dumps({"principal": args.principal, "robots": sorted(fleet.uavs), "docks": sorted(fleet.docks)}),
          flush=True)
    await fleet.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--catalog", default="configs/sites/p5_desk_v1.yaml")
    parser.add_argument("--principal", required=True, help="the dock backend identity whose robots this process flies")
    parser.add_argument("--api", type=Path, default=Path("/api/api.sock"))
    parser.add_argument("--service", default="desk-service:8450")
    parser.add_argument("--server-name", default="mission-service")
    parser.add_argument("--tls", type=Path, default=Path("/tls"), help="one <robot_id>/ folder of client credentials each")
    parser.add_argument("--trust", type=Path, default=Path("/trust/trust.json"))
    parser.add_argument("--state", type=Path, default=Path("/fleet"))
    parser.add_argument("--world", type=Path, help="the desk world file (harness-written, read-only here)")
    parser.add_argument("--period", type=float, default=0.25)
    parser.add_argument("--report-period", type=float, default=1.0)
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
