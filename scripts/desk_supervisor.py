"""Simulation-side onboard supervisor for the resident mission desk (D035).

It watches the robot inbox that the uplink writes after verifying each signed package and flies the latest
accepted version of each mission once, in the simulator, with the orchestration of the M2 end-to-end cases: a
fresh flight controller for every version (the ground crew's battery swap), the guardian and the executive
started with the package (each verifies it again) and the next epoch, a bounded watch until landed and
disarmed, and the simulation operator landing a surrendered or overrun flight. A flight holds the project's
`stack.lock`, so it never overlaps a deployment, a batch or another live run. Once a mission reaches a final
state its flights are copied into their own case and judged independently, online and from the recordings.
It reads no web or network input; its public records are for display only. A restart adopts the flight in
progress instead of flying again.

常驻任务台的仿真机载监管者（D035）。它监视 uplink 验签后写入的机器人 inbox，在仿真器中把每个任务的最新
已接受版本飞一次，编排语义与 M2 端到端用例相同：每个版本都从飞控断电重启开始（地勤换电），以任务包（二者
各自再次验签）和下一个代次启动 guardian 与 executive，有界监视到落地上锁，guardian 放弃控制权或超时时由
仿真操作员降落。飞行持有项目 `stack.lock`，不与部署、批次或其他实时运行重叠。任务到达终态后，把其各次飞行
复制到独立用例并在线、按录制各做一次独立裁判。它不读取网页或网络输入；公开记录只用于展示。重启时接管进行
中的飞行，而不是重飞。
"""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import socket
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
PACKAGE_FILE = re.compile(r"^(m-[0-9a-f]{12})-v([1-9][0-9]{0,5})\.json$")
MISSION = re.compile(r"^m-[0-9a-f]{12}$")
FLIGHT_LIMIT_S = 600
LANDING_GRACE_S = 120
WARMUP_S = 5
MIN_MEMORY = 3 * 1024**3
JUDGE_RECHECK_S = 10
# Mission states after which no further version will fly. / 之后不会再飞任何版本的任务状态。
TERMINAL = ("completed", "incomplete", "declined", "delivery_rejected", "rejected")
FLOWN = ("finished", "overrun", "failed", "interrupted")
PX4_COMMANDER = "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict | None:
    """A JSON record, or None when absent or being replaced; links are refused. / 读取 JSON 记录；拒绝链接。"""
    if path.is_symlink():
        raise ValueError("linked desk record is forbidden")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def write_json(path: Path, value) -> None:
    """Replace atomically so readers only ever see complete records. / 原子替换，读者只看到完整记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.{uuid.uuid4().hex}.pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    pending.replace(path)


def complete_lines(path: Path) -> int:
    """Complete JSONL rows; a torn tail is not a row. / 完整 JSONL 行数；不完整的尾部不算。"""
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n"))


class Desk:
    """Paths of the resident desk inside the project workspace. / 常驻任务台在项目工作区中的路径。"""

    def __init__(self, root: Path):
        self.root = root
        self.base = root / "desk"
        robot = self.base / "robot"
        self.inbox, self.history = robot / "inbox", robot / "inbox" / "history"
        self.mailbox, self.uplink, self.aircraft = robot / "mailbox", robot / "uplink", robot / "aircraft"
        self.state, self.ipc = robot / "state", robot / "ipc"
        self.flights, self.cases, self.idle = self.base / "flights", self.base / "judge", self.base / "idle"
        self.supervisor = self.base / "supervisor"
        self.public = self.supervisor / "public"
        self.service, self.api = self.base / "service", self.base / "api" / "api.sock"
        self.secrets, self.model = root / "secrets" / "desk", root / "secrets" / "m2-model"

    def folders(self) -> list[Path]:
        return [self.base, self.history, self.mailbox, self.uplink, self.aircraft, self.state, self.ipc, self.flights,
                self.cases, self.idle / "aircraft", self.idle / "case", self.public / "missions",
                self.supervisor / "docker-client", self.base / "deployments", self.base / "fixed-pages",
                self.service, self.api.parent]


class Stack:
    """`docker compose` on the desk file with the activation record's images and paths.

    按激活记录中的镜像与路径对任务台 compose 文件执行 `docker compose`。
    """

    def __init__(self, desk: Desk, record: dict, log: Path | None = None):
        self.desk, self.record, self.log = desk, record, log

    def env(self, **flight) -> dict:
        values = {
            "DRONE_DESK_ROOT": str(self.desk.base), "DRONE_DESK_SECRETS": str(self.desk.secrets),
            "DRONE_DESK_WORKSPACE": str(self.desk.root),
            "DRONE_DESK_MODEL": str(self.desk.model), "DRONE_DESK_SHA": self.record["source_sha"],
            "DRONE_DESK_ORIGIN": self.record["origin"], "DRONE_DESK_UID": str(self.record["uid"]),
            "DRONE_DESK_GID": str(self.record["gid"]), "DRONE_DESK_GROUND_IMAGE": self.record["images"]["ground"],
            "DRONE_DESK_AIRCRAFT_IMAGE": self.record["images"]["aircraft"],
            "DRONE_DESK_SIM_IMAGE": self.record["images"]["sim"],
            # Placeholders keep the whole file valid when no flight runs. / 无飞行时的占位值使整个文件仍然有效。
            "DRONE_DESK_FLIGHT": str(self.desk.idle), "DRONE_DESK_AIRCRAFT": str(self.desk.idle / "aircraft"),
            "DRONE_DESK_PACKAGE": "none.json", "DRONE_DESK_EPOCH": "0", "DRONE_DESK_CASE": str(self.desk.idle / "case"),
        }
        values.update({f"DRONE_DESK_{key.upper()}": str(value) for key, value in flight.items()})
        return {**os.environ, **values}

    def file(self) -> Path:
        return self.desk.root / "releases" / self.record["deployment_id"] / "source/sim/compose.desk.yaml"

    def __call__(self, *args: str, timeout: int = 180, check: bool = True, quiet: bool = False,
                 **flight) -> subprocess.CompletedProcess:
        argv = ["docker", "compose", "-p", HELPERS["PROJECT"], "-f", str(self.file()), "--profile", "flight", *args]
        result = subprocess.run(argv, env=self.env(**flight), capture_output=True, timeout=timeout)
        if self.log is not None:
            with self.log.open("ab") as stream:
                # Logs are read once at the end, not appended on every poll. / 日志只在结束时读取一次。
                stream.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"desk compose {' '.join(args[:2])} failed ({result.returncode})")
        return result


def api(desk: Desk, method: str, **params):
    """Read-only call to the mission-service API as an anonymous reader. / 以匿名读者身份只读调用任务服务 API。"""
    request = {"method": method, "actor": "", "trust": "anonymous", "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(str(desk.api))
        client.sendall(json.dumps(request).encode() + b"\n")
        with client.makefile("rb") as stream:
            line = stream.readline(16 * 1024 * 1024 + 1)
    if not line.endswith(b"\n"):
        raise RuntimeError("incomplete mission-service answer")
    response = json.loads(line)
    if not response.get("ok"):
        raise RuntimeError("mission service refused: " + (response.get("issue") or {}).get("code", "unknown"))
    return response["result"]


class Supervisor:
    def __init__(self, desk: Desk, record: dict):
        self.desk, self.record = desk, record
        self.published: str | None = None
        self.checked: dict[str, float] = {}

    # ── public records (display only) / 公开记录（仅展示）──

    def publish(self, state: str, **fields) -> None:
        value = {"schema_version": "0.1.0", "state": state, "mission_id": None, "version": None, "detail": None,
                 "queue": [], "source_sha": self.record["source_sha"], **fields}
        text = json.dumps(value, sort_keys=True)
        if text != self.published:
            self.published = text
            write_json(self.desk.public / "status.json", {**value, "updated_at": now()})

    def mission_record(self, mission: str) -> dict:
        return read_json(self.desk.public / "missions" / f"{mission}.json") or {
            "schema_version": "0.1.0", "mission_id": mission, "flights": [], "judge": None}

    def save_flight(self, mission: str, entry: dict, package: str) -> None:
        flight = self.desk.flights / f"{mission}-v{entry['version']}"
        write_json(flight / "flight.json", {"schema_version": "0.1.0", "mission_id": mission, "package": package,
                                            "source_sha": self.record["source_sha"], **entry})
        record = self.mission_record(mission)
        record["flights"] = sorted([f for f in record["flights"] if f["version"] != entry["version"]] + [entry],
                                   key=lambda f: f["version"])
        write_json(self.desk.public / "missions" / f"{mission}.json", record)

    # ── the queue / 队列 ──

    def pending(self) -> list[tuple[str, int, Path]]:
        """The latest accepted version of each mission without a flight record, oldest first.

        每个任务尚无飞行记录的最新已接受版本，按接受先后排列。
        """
        latest: dict[str, tuple[int, Path]] = {}
        for path in self.desk.history.glob("*.json") if self.desk.history.is_dir() else []:
            match = PACKAGE_FILE.fullmatch(path.name)
            if match is None or path.is_symlink():
                continue
            mission, version = match.group(1), int(match.group(2))
            if version > latest.get(mission, (0, path))[0]:
                latest[mission] = (version, path)
        queue = [(path.stat().st_mtime, mission, version, path) for mission, (version, path) in latest.items()
                 if not (self.desk.flights / f"{mission}-v{version}" / "flight.json").exists()]
        return [(mission, version, path) for _, mission, version, path in sorted(queue)]

    def unflyable(self, mission: str, version: int, path: Path) -> str | None:
        """Why a package must not start flying, or None. The aircraft still verifies it independently.

        任务包不能起飞的原因，可以飞时为 None。机载仍会独立验证它。
        """
        package = read_json(path)
        if not package or package.get("mission_id") != mission or package.get("mission_version") != version:
            return "package_identity_mismatch"
        try:
            expires = datetime.fromisoformat((package.get("approval") or {})["expires_at"])
        except (KeyError, TypeError, ValueError):
            return "approval_missing"
        if expires.tzinfo is None:
            return "approval_missing"
        # The guardian cancels a flight whose approval lapses in the air; do not start one that cannot finish.
        # guardian 会在空中审批失效时取消飞行；跑不完的飞行不要开始。
        if expires <= datetime.now(timezone.utc) + timedelta(seconds=FLIGHT_LIMIT_S):
            return "approval_expires_before_the_flight_could_finish"
        return None

    def skip(self, mission: str, version: int, path: Path, reason: str) -> None:
        self.save_flight(mission, {"version": version, "status": "skipped", "epoch": None, "started_at": now(),
                                   "ended_at": now(), "detail": reason, "manual_cleanup": False}, path.name)

    def try_lock(self):
        """The project lock without waiting; None while another run or deployment holds it.

        不等待地取得项目锁；其他运行或部署持有时返回 None。
        """
        import fcntl

        stream = (self.desk.root / "stack.lock").open("a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        return stream

    # ── one flight / 一次飞行 ──

    def m0(self, action: str) -> None:
        """Stop or restore the idle M0 simulator of the current deployment. / 停下或恢复当前部署的 M0 空闲仿真。"""
        deployment = HELPERS["current"](self.desk.root)
        arguments = ["stop", "sitl"] if action == "stop" else ["up", "-d", "--no-build", "--pull", "never", "sitl"]
        HELPERS["compose"](self.desk.root, deployment, arguments)

    @staticmethod
    def wait(predicate, timeout: float, what: str, period: float = 0.25) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(period)
        raise RuntimeError(f"timed out waiting for {what}")

    def next_epoch(self) -> int:
        state = read_json(self.desk.state / "authority.json")
        return int(state["epoch_watermark"]) + 1 if state else 1

    def clear_mailbox(self, mission: str, version: int) -> None:
        """Drop a request left for another flight; the executive would record it as rejected.

        清除留给其他飞行的请求；否则 executive 会把它记为被拒。
        """
        path = self.desk.mailbox / "operator.json"
        pending = read_json(path)
        if pending is not None and (pending.get("mission_id"), pending.get("mission_version")) != (mission, version):
            path.unlink(missing_ok=True)

    def running(self, stack: Stack, service: str, extra: dict) -> bool:
        return bool(stack("ps", "--status", "running", "-q", service, check=False, quiet=True, **extra).stdout.strip())

    def operator_land(self, stack: Stack, extra: dict, aircraft: Path, reason: str) -> None:
        """The simulation operator lands, as a pilot taking over would. / 仿真操作员降落，等同飞手接管。"""
        stack("exec", "-T", "desk-sitl", PX4_COMMANDER, "land", check=False, **extra)
        write_json(aircraft / "manual-cleanup.json", {"reason": reason, "at": now()})

    def observation(self, aircraft: Path) -> tuple[dict, dict]:
        status = read_json(aircraft / "status.json") or {}
        return status, status.get("observation") or {}

    def land_if_airborne(self, stack: Stack, extra: dict, aircraft: Path, reason: str) -> bool:
        _, observation = self.observation(aircraft)
        if observation.get("in_air") is not True:
            return False
        self.operator_land(stack, extra, aircraft, reason)
        try:
            self.wait(lambda: self.observation(aircraft)[1].get("in_air") is False, LANDING_GRACE_S, "landing")
        except RuntimeError:
            pass  # recorded as not grounded by the judge / 未落地由裁判记录
        return True

    def watch(self, stack: Stack, extra: dict, aircraft: Path, deadline: float) -> dict:
        """Bounded watch until landed, disarmed and a result exists. / 有界监视到落地上锁且有结果。"""
        cleanup, checked = False, time.monotonic()
        while True:
            status, observation = self.observation(aircraft)
            if status.get("safety") == "abort" and not cleanup:
                # Control was surrendered; the operator ends the flight. / 控制权已交出，由操作员收尾。
                self.operator_land(stack, extra, aircraft, str(status.get("reason")))
                cleanup = True
            if observation.get("in_air") is False and observation.get("armed") is False and \
                    (aircraft / "result.json").exists():
                time.sleep(2.2)
                return {"status": "finished", "manual_cleanup": cleanup}
            if time.monotonic() > deadline:
                self.land_if_airborne(stack, extra, aircraft, "flight_limit_exceeded")
                return {"status": "overrun", "manual_cleanup": True, "detail": f"exceeded {FLIGHT_LIMIT_S} s"}
            if time.monotonic() - checked > 5:
                checked = time.monotonic()
                if not self.running(stack, "desk-guardian", extra) and not (aircraft / "result.json").exists():
                    self.land_if_airborne(stack, extra, aircraft, "guardian_exited")
                    return {"status": "failed", "manual_cleanup": cleanup, "detail": "guardian exited before a result"}
            time.sleep(0.2)

    def finish(self, stack: Stack, extra: dict, flight: Path) -> None:
        """Stop the flight processes and the simulator, then restore the idle M0 simulator.

        停止飞行进程与仿真器，然后恢复 M0 空闲仿真。
        """
        stack("stop", "-t", "5", "desk-executive", "desk-guardian", check=False, **extra)
        log = stack("logs", "--no-color", "--no-log-prefix", "desk-sitl", check=False, quiet=True, timeout=60, **extra)
        (flight / "sitl.log").write_bytes(log.stdout)
        stack("stop", "-t", "5", "desk-collector", check=False, **extra)
        stack("stop", "-t", "10", "desk-sitl", check=False, **extra)
        self.m0("up")

    def fly(self, mission: str, version: int, package: Path) -> dict:
        """One mission version in the simulator; the caller holds the project lock. / 在仿真中飞一个任务版本；调用方持有项目锁。"""
        desk = self.desk
        flight, aircraft = desk.flights / f"{mission}-v{version}", desk.aircraft / mission / f"v{version}"
        for folder in ("sensor", "truth", "ulog"):
            (flight / folder).mkdir(parents=True, exist_ok=True)
        entry = {"version": version, "status": "preparing", "epoch": None, "started_at": now(), "ended_at": None,
                 "detail": None, "manual_cleanup": False}
        self.save_flight(mission, entry, package.name)
        write_json(desk.supervisor / "active.json", {"mission_id": mission, "version": version, "package": package.name,
                                                    "started_at": entry["started_at"]})
        stack = Stack(desk, self.record, log=flight / "compose.log")
        extra = {"flight": flight, "aircraft": aircraft, "package": package.name}
        try:
            self.publish("preparing", mission_id=mission, version=version,
                         detail="booting a fresh flight controller (battery swap) and waiting for PX4 home")
            self.m0("stop")
            for service in ("desk-sitl", "desk-collector"):
                stack("up", "-d", "--no-build", "--pull", "never", "--force-recreate", service, **extra)
            self.wait(lambda: (flight / "sensor/latest.json").exists(), 100, "Gazebo RGB frame")
            self.wait(lambda: b"home set" in stack("logs", "--no-color", "--no-log-prefix", "desk-sitl", check=False,
                                                   quiet=True, timeout=60, **extra).stdout, 90, "PX4 home", period=1.0)
            time.sleep(WARMUP_S)
            self.clear_mailbox(mission, version)
            entry["epoch"] = extra["epoch"] = self.next_epoch()
            aircraft.mkdir(parents=True, exist_ok=False)
            (desk.ipc / "guardian.sock").unlink(missing_ok=True)
            stack("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "desk-guardian", **extra)
            self.wait(lambda: (desk.ipc / "guardian.sock").exists(), 90, "guardian")
            stack("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "desk-executive", **extra)
            entry["status"] = "flying"
            self.save_flight(mission, entry, package.name)
            self.publish("flying", mission_id=mission, version=version, detail=f"epoch {entry['epoch']}")
            entry.update(self.watch(stack, extra, aircraft, time.monotonic() + FLIGHT_LIMIT_S))
        except Exception as error:
            entry.update(status="failed", detail=f"{type(error).__name__}: {error}"[:300])
            if aircraft.is_dir():
                entry["manual_cleanup"] = self.land_if_airborne(stack, extra, aircraft, "supervisor_error") or \
                    entry["manual_cleanup"]
        finally:
            self.publish("restoring", mission_id=mission, version=version)
            try:
                self.finish(stack, extra, flight)
            finally:
                entry["ended_at"] = now()
                self.save_flight(mission, entry, package.name)
                (desk.supervisor / "active.json").unlink(missing_ok=True)
        return entry

    def adopt(self) -> None:
        """Finish the flight a previous supervisor was watching; never start it again.

        收尾上一个监管者正在监视的飞行；绝不重新开始。
        """
        active = read_json(self.desk.supervisor / "active.json")
        if not active:
            return
        mission, version, package = active["mission_id"], int(active["version"]), active["package"]
        if not MISSION.fullmatch(mission) or PACKAGE_FILE.fullmatch(package) is None:
            raise ValueError("invalid active flight record")
        flight, aircraft = self.desk.flights / f"{mission}-v{version}", self.desk.aircraft / mission / f"v{version}"
        entry = {k: v for k, v in (read_json(flight / "flight.json") or {}).items()
                 if k in ("version", "status", "epoch", "started_at", "ended_at", "detail", "manual_cleanup")}
        entry.setdefault("version", version)
        stack = Stack(self.desk, self.record, log=flight / "compose.log")
        extra = {"flight": flight, "aircraft": aircraft, "package": package, "epoch": entry.get("epoch") or 0}
        while (lock := self.try_lock()) is None:
            self.publish("waiting_for_workspace", mission_id=mission, version=version,
                         detail="adopting a flight after a supervisor restart")
            time.sleep(2)
        try:
            if self.running(stack, "desk-guardian", extra):
                self.publish("flying", mission_id=mission, version=version, detail="watching again after a restart")
                started = datetime.fromisoformat(active["started_at"])
                left = FLIGHT_LIMIT_S - (datetime.now(timezone.utc) - started).total_seconds()
                entry.update(self.watch(stack, extra, aircraft, time.monotonic() + max(left, 30)))
            else:
                done = (aircraft / "result.json").exists()
                cleanup = self.land_if_airborne(stack, extra, aircraft, "supervisor_restart") if aircraft.is_dir() else False
                entry.update(status="finished" if done and not cleanup else "interrupted", manual_cleanup=cleanup)
            entry["detail"] = ((entry.get("detail") or "") + " · adopted after a supervisor restart").strip(" ·")
            self.publish("restoring", mission_id=mission, version=version)
            self.finish(stack, extra, flight)
        finally:
            entry["ended_at"] = now()
            self.save_flight(mission, entry, package)
            (self.desk.supervisor / "active.json").unlink(missing_ok=True)
            lock.close()

    # ── independent judge / 独立裁判 ──

    def mirrored(self, view: dict, mission: str, versions: list[int]) -> bool:
        """The service mirror holds every complete onboard row with an intact chain. / 服务镜像包含每一行且链完整。"""
        for version in versions:
            aircraft = self.desk.aircraft / mission / f"v{version}"
            record = next((v for v in view["versions"] if v["version"] == version), {})
            for name in ("executive", "guardian"):
                count = complete_lines(aircraft / f"{name}.jsonl")
                if count and record.get("journals", {}).get(name) != {"rows": count, "chain": "ok"}:
                    return False
        return True

    def judge_ready(self) -> None:
        for path in sorted((self.desk.public / "missions").glob("m-*.json")):
            mission = path.stem
            record = read_json(path)
            if not MISSION.fullmatch(mission) or not record:
                continue
            flights = record["flights"]
            flown = [f["version"] for f in flights if f["status"] in FLOWN
                     and (self.desk.aircraft / mission / f"v{f['version']}").is_dir()]
            if not flown or any(f["status"] in ("preparing", "flying") for f in flights):
                continue
            if (record.get("judge") or {}).get("after_version") == max(flown):
                continue
            if time.monotonic() - self.checked.get(mission, -JUDGE_RECHECK_S) < JUDGE_RECHECK_S:
                continue
            self.checked[mission] = time.monotonic()
            try:
                view = api(self.desk, "view", mission_id=mission)
            except (OSError, RuntimeError, ValueError):
                continue  # the service is down; try again later / 服务不可用，稍后再试
            if view["mission"]["status"] in TERMINAL and self.mirrored(view, mission, flown):
                self.judge(mission, view, flown)

    def judge(self, mission: str, view: dict, flown: list[int]) -> dict:
        """Copy the mission's flights into their own case and judge it online and from the recordings.

        把该任务的飞行复制到独立用例，在线与按录制各裁判一次。
        """
        desk, last = self.desk, max(flown)
        self.publish("judging", mission_id=mission, version=last)
        case = desk.cases / mission / f"after-v{last}"
        if not (case / "judge/result.json").exists():
            pending = case.with_name(f"{case.name}.{uuid.uuid4().hex}.pending")
            (pending / "judge").mkdir(parents=True)
            truth = bytearray()
            for version in sorted(flown):
                shutil.copytree(desk.aircraft / mission / f"v{version}", pending / "aircraft" / mission / f"v{version}")
                (pending / "inbox/history").mkdir(parents=True, exist_ok=True)
                name = f"{mission}-v{version}.json"
                shutil.copyfile(desk.history / name, pending / "inbox/history" / name)
                source = desk.flights / f"{mission}-v{version}" / "truth/truth.jsonl"
                truth.extend(source.read_bytes() if source.is_file() else b"")
            (pending / "truth").mkdir()
            (pending / "truth/truth.jsonl").write_bytes(bytes(truth))
            (pending / "service").mkdir()
            shutil.copyfile(desk.service / "ready.json", pending / "service/ready.json")
            write_json(pending / "service-export/view.json", view)
            write_json(pending / "input/scenario.json", {
                "scenario": "desk", "seed": 0, "source_sha": self.record["source_sha"], "mission_id": mission,
                "text": view["request"]["text"], "expected": {"classification": "any"}})
            if case.exists():
                shutil.rmtree(case)
            pending.rename(case)
        stack = Stack(desk, self.record, log=case / "compose.log")
        stack("run", "-T", "--rm", "--no-deps", "desk-judge", check=False, timeout=180, case=case)
        replayed = stack("run", "-T", "--rm", "--no-deps", "desk-judge", "python3", "-m", "drone_agent.eval.judge_m2",
                         "/run", "--replay", "--output", "/output/replay.json", check=False, timeout=180, case=case)
        online = read_json(case / "judge/result.json") or {"passed": False, "error": "judge_failed"}
        replay = read_json(case / "judge/replay.json") or {}
        agrees = all(online.get(k) == replay.get(k) for k in ("classification", "false_success_reports", "problems"))
        summary = {**{k: online.get(k) for k in ("classification", "false_success_reports", "problems", "flown_versions",
                                                 "truly_inspected", "report_targets", "judged_at", "error")},
                   "passed": bool(online.get("passed")) and replayed.returncode == 0 and agrees,
                   "replay_agrees": agrees, "after_version": last, "case": str(case.relative_to(desk.base))}
        record = self.mission_record(mission)
        record["judge"] = summary
        write_json(desk.public / "missions" / f"{mission}.json", record)
        return summary

    # ── the loop / 主循环 ──

    def step(self) -> None:
        self.judge_ready()
        queue = self.pending()
        waiting = [{"mission_id": m, "version": v} for m, v, _ in queue]
        if not queue:
            self.publish("idle")
            time.sleep(1)
            return
        mission, version, path = queue[0]
        reason = self.unflyable(mission, version, path)
        if reason:
            self.skip(mission, version, path, reason)
            return
        if HELPERS["capacity"]()["memory"]["MemAvailable"] < MIN_MEMORY:
            self.publish("waiting_for_memory", mission_id=mission, version=version, queue=waiting)
            time.sleep(5)
            return
        lock = self.try_lock()
        if lock is None:
            self.publish("waiting_for_workspace", mission_id=mission, version=version, queue=waiting,
                         detail="another simulation run or deployment holds the project lock")
            time.sleep(3)
            return
        try:
            self.fly(mission, version, path)
        finally:
            lock.close()

    def run(self) -> None:
        self.adopt()
        while True:
            try:
                self.step()
            except Exception as error:  # keep supervising; the error is public / 继续监管；错误公开可见
                self.publish("error", detail=f"{type(error).__name__}: {error}"[:300])
                time.sleep(5)


def main() -> None:
    import fcntl

    root = HELPERS["workspace"]()
    desk = Desk(root)
    record = read_json(desk.base / "current.json")
    if not record or HERE.parent.parent != root / "releases" / record["deployment_id"]:
        raise SystemExit("the supervisor must run from the activated desk deployment")
    desk.supervisor.mkdir(parents=True, exist_ok=True)
    with (desk.supervisor / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Supervisor(desk, record).run()


if __name__ == "__main__":
    main()
