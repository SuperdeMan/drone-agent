"""The P5 72-hour soak harness of the resident desk (D073): a host process beside the desk supervisor.

It expands the frozen plan (`configs/soak/p5_soak_v1.yaml`) from T0 into occurrences, enables the planned service
schedules, and then, on real wall-clock time, does what the plan says: starts the S1 runs, the damage and road cycles,
the backlog bursts and the probes; acts as the people (approvals of exact package hashes, reviews decided by the world's
truth, repair feedback, a few cancellations) under its own `harness:soak-*` identities; and injects whole-container
faults: graceful restarts and kills of the mission service, network cuts of an uplink or of the model proxy's egress,
a throttle switch on the model proxy, vendor link drops and protocol faults, and resident restarts. Every action, every
fault window and every recovery it measured is appended to its own journal; resources are sampled every minute. It
reads the service only through the API socket and its ledger only read-only; it never touches a guardian or a flight
controller. A restart resumes from the journal: request identifiers are fixed per occurrence, so nothing starts twice,
and an occurrence missed while the harness itself was down is recorded as such. T0 exists only in the manifest.

P5 常驻任务台的 72 小时长稳编排（D073）：与任务台监管者并列的主机进程。

它把冻结的计划（`configs/soak/p5_soak_v1.yaml`）从 T0 展开为发生时刻，启用计划中的服务排班，随后按真实墙钟执行计划：启动
S1 运行、损伤与道路循环、积压突发与各类探测；以自己的 `harness:soak-*` 身份扮演人（按确切任务包哈希审批、按世界真值复核、
维修反馈、少量取消）；并注入整容器故障：任务服务的正常重启与强杀、uplink 或模型代理出站网络的断网、模型代理的限流开关、厂商
链路断开与协议故障，以及常驻容器重启。每个动作、每个故障窗口与它测得的每次恢复都追加到自己的日志；资源每分钟采样一次。它只经
API 套接字读取服务、只以只读方式读取账本；从不触达 guardian 或飞控。重启时从日志恢复：请求标识按发生时刻固定，因此不会重复
启动；编排自身停机期间错过的发生时刻如实记录。T0 只存在于 manifest 中。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import runpy
import signal
import socket
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
SUPERVISOR = runpy.run_path(str(HERE / "desk_supervisor.py"))
PLAN_FILE = "configs/soak/p5_soak_v1.yaml"
PROJECTS = ("campus_s1", "fleet_s0", "vendor_s3")
RUN_FINAL = ("completed", "failed", "outcome_unknown", "cancelled")
MISSION_FINAL = ("completed", "incomplete", "declined", "rejected", "refused", "planning_failed", "delivery_rejected",
                 "cancelled", "dispatch_expired")
ORDER_REPAIRABLE = ("open", "reinspection_failed", "reinspection_unknown")
# Tables that only ever grow; the judge checks their counts never fall. / 只增长的表；裁判核对其计数从不下降。
GROWING = ("missions", "events", "op_events", "op_claims", "wf_runs", "wf_triggers", "bz_jobs", "bz_findings",
           "bz_orders", "reports")
LATE_GRACE_S = 600
PEOPLE_PERIOD_S = 4.0
WORLD_SECTIONS = {"S0": "s0", "S1": "s1", "S3": "s3"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat()


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def write_json(path: Path, value) -> None:
    SUPERVISOR["write_json"](path, value)


def append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def load_plan(source: Path) -> tuple[dict, str]:
    import yaml

    text = (source / PLAN_FILE).read_bytes()
    plan = yaml.safe_load(text)
    if plan.get("format") != "drone.soak-plan/v1":
        raise ValueError("not a drone.soak-plan/v1 file")
    return plan, hashlib.sha256(text).hexdigest()


# ── the plan / 计划 ──


def times(entry: dict, t0: datetime, end: datetime) -> list[datetime]:
    """Every occurrence of one periodic entry inside [t0, end). / 一个周期条目在 [t0, end) 内的全部发生时刻。"""
    period = timedelta(hours=entry["every_h"]) if "every_h" in entry else timedelta(minutes=entry["every_min"])
    at, found = t0 + timedelta(minutes=entry.get("offset_min", 0)), []
    while at < end:
        found.append(at)
        at += period
    return found


def expand(plan: dict, t0: datetime) -> list[dict]:
    """The deterministic occurrences of the workload and the faults, in time order.

    负载与故障的确定性发生时刻，按时间排序。
    """
    end = t0 + timedelta(hours=plan["duration_h"])
    rng = random.Random(plan["seed"])
    found = []
    for kind, entry in plan["workload"].items():
        for index, at in enumerate(times(entry, t0, end)):
            item = {"id": f"{kind}-{index:03d}", "kind": kind, "at": iso(at), "layer": entry.get("layer")}
            if kind == "s1_watch":
                item["asset"] = entry["assets"][index % len(entry["assets"])]
            elif kind == "s0_damage":
                item["asset"] = entry["assets"][index % len(entry["assets"])]
            elif kind in ("s1_damage", "s1_road", "s3_damage"):
                item["asset"] = entry["asset"]
            found.append(item)
    for kind, entry in plan["faults"].items():
        for index, at in enumerate(times(entry, t0, end)):
            item = {"id": f"{kind}-{index:03d}", "kind": kind, "at": iso(at), "layer": "fault"}
            if kind == "vendor_protocol":
                item["fault"] = entry["kinds"][index % len(entry["kinds"])]
            found.append(item)
    found.sort(key=lambda item: (item["at"], item["id"]))
    # The seed only decides which scheduled S0 runs are cancelled; it is recorded with the plan.
    # 种子只决定取消哪些 S0 排班运行；它随计划一起记录。
    return [{**item, "nonce": rng.randrange(1 << 30)} for item in found]


def cancel_choice(plan: dict, run_id: str) -> str | None:
    """The stage at which this scheduled S0 run is cancelled, or None (seeded, about `permille` per thousand).

    该 S0 排班运行在哪个阶段取消，或 None（按种子，约千分之 `permille`）。
    """
    entry = plan["cancel"]
    digest = int(hashlib.sha256(f"{plan['seed']}:{run_id}".encode()).hexdigest(), 16)
    if digest % 1000 >= entry["permille"]:
        return None
    return entry["stages"][(digest // 1000) % len(entry["stages"])]


# ── the service and the containers / 服务与容器 ──


class Api:
    """Unix-socket API calls as the harness identities; refusals are answers, not errors.

    以编排身份经 Unix 套接字调用 API；拒绝是应答而不是错误。
    """

    def __init__(self, path: Path):
        self.path = path

    def call(self, actor: str, method: str, trust: str = "first_party", timeout: float = 130.0, **params) -> dict:
        request = {"method": method, "actor": actor, "trust": trust, "params": params}
        started = time.monotonic()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(self.path))
                client.sendall(json.dumps(request).encode() + b"\n")
                with client.makefile("rb") as stream:
                    line = stream.readline(16 * 1024 * 1024 + 1)
            response = json.loads(line) if line.endswith(b"\n") else {"ok": False, "issue": {"code": "harness.torn"}}
        except (OSError, ValueError) as error:
            response = {"ok": False, "issue": {"code": "harness.unreachable", "message": type(error).__name__}}
        response["latency_s"] = round(time.monotonic() - started, 4)
        return response


class Containers:
    """Whole-container operations on this project's desk residents only. / 只对本项目任务台常驻容器做整容器操作。"""

    network_prefix = HELPERS["PROJECT"] + "_"

    def __init__(self, log: Path):
        self.log = log

    def run(self, argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        append(self.log, {"at": iso(now()), "argv": argv, "code": result.returncode,
                          "stderr": result.stderr.decode("utf-8", errors="replace")[-300:]})
        return result

    def id(self, service: str) -> str | None:
        result = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={HELPERS['PROJECT']}",
                                 "--filter", f"label=com.docker.compose.service={service}"], capture_output=True,
                                timeout=60)
        ids = result.stdout.decode().split()
        return ids[0] if len(ids) == 1 else None

    def inspect(self, service: str) -> dict:
        container = self.id(service)
        if container is None:
            return {}
        result = subprocess.run(["docker", "inspect", container], capture_output=True, timeout=60)
        try:
            return json.loads(result.stdout)[0]
        except (ValueError, IndexError):
            return {}

    def restart(self, service: str) -> bool:
        container = self.id(service)
        return container is not None and self.run(["docker", "restart", "-t", "30", container], 180).returncode == 0

    def kill_main(self, service: str) -> bool:
        """SIGKILL the container's main process from the host (same owner), so Docker's restart policy restarts it
        as it would after a crash.

        从主机（同一属主）对容器主进程发送 SIGKILL，使 Docker 的重启策略像崩溃后那样重启它。
        """
        state = self.inspect(service).get("State") or {}
        init = int(state.get("Pid") or 0)
        if not init:
            return False
        children = Path(f"/proc/{init}/task/{init}/children")
        targets = [int(p) for p in children.read_text().split()] if children.is_file() else []
        for pid in targets or [init]:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                return False
        append(self.log, {"at": iso(now()), "kill": service, "pids": targets or [init]})
        return True

    def disconnect(self, service: str, network: str) -> bool:
        container = self.id(service)
        return container is not None and self.run(
            ["docker", "network", "disconnect", self.network_prefix + network, container]).returncode == 0

    def connect(self, service: str, network: str) -> bool:
        container = self.id(service)
        if container is None:
            return False
        networks = (self.inspect(service).get("NetworkSettings") or {}).get("Networks") or {}
        if self.network_prefix + network in networks:
            return True
        return self.run(["docker", "network", "connect", self.network_prefix + network, container]).returncode == 0

    def stats(self) -> dict:
        result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], capture_output=True,
                                timeout=60)
        found = {}
        for line in result.stdout.decode().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = row.get("Name", "")
            if name.startswith(HELPERS["PROJECT"] + "-desk"):
                found[name] = {"cpu": row.get("CPUPerc"), "mem": row.get("MemUsage"), "mem_pct": row.get("MemPerc"),
                               "pids": row.get("PIDs")}
        return found


# ── the harness / 编排 ──


class Soak:
    def __init__(self, desk, base: Path, *, api: Api | None = None, containers: Containers | None = None,
                 clock=now):
        self.desk, self.base, self.clock = desk, base, clock
        self.manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        self.plan = self.manifest["plan"]
        self.ids = self.plan["identities"]
        self.t0, self.end = parse(self.manifest["t0"]), parse(self.manifest["end"])
        self.occurrences = json.loads((base / "occurrences.json").read_text(encoding="utf-8"))
        self.api = api or Api(desk.api)
        self.containers = containers or Containers(base / "docker.jsonl")
        state = base / "state.json"
        self.state = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {
            "occurrences": {}, "runs": {}, "planning": {}, "cancels": {}, "last_sample": None, "schedules": None}
        self.dirty = False
        self.last_people = 0.0

    # ── records / 记录 ──

    def note(self, kind: str, **fields) -> None:
        append(self.base / "actions.jsonl", {"at": iso(self.clock()), "kind": kind, **fields})

    def occ(self, item: dict) -> dict:
        return self.state["occurrences"].setdefault(item["id"], {"status": "pending"})

    def update(self, item_id: str, **fields) -> None:
        self.state["occurrences"].setdefault(item_id, {}).update(fields)
        self.dirty = True

    def save(self) -> None:
        if self.dirty:
            write_json(self.base / "state.json", self.state)
            self.dirty = False

    def call(self, role: str, method: str, *, trust: str = "first_party", record: bool = True, **params) -> dict:
        actor = self.ids[role]
        response = self.api.call(actor, method, trust=trust, **params)
        if record:
            result = response.get("result")
            self.note("api", actor=actor, method=method, ok=bool(response.get("ok")),
                      code=(response.get("issue") or {}).get("code"), latency_s=response.get("latency_s"),
                      params={k: v for k, v in params.items() if k != "payload"},
                      result_sha256=hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
                      if result is not None else None)
        return response

    def read(self, method: str, **params):
        response = self.call("viewer", method, record=False, **params)
        return response.get("result") if response.get("ok") else None

    # ── the world / 世界 ──

    def world(self) -> dict:
        try:
            value = json.loads(self.desk.world.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        if value.get("format") != SUPERVISOR["WORLD_FORMAT"]:
            value = {"format": SUPERVISOR["WORLD_FORMAT"], "s0": {}, "s1": {}, "s3": {}}
        return value

    def set_world(self, layer: str, asset: str, state: str, reason: str) -> None:
        value = self.world()
        section = WORLD_SECTIONS[layer]
        value.setdefault(section, {})[asset] = state
        value["updated_at"] = iso(self.clock())
        write_json(self.desk.world, value)
        record = {"at": iso(self.clock()), "section": section, "asset_id": asset, "state": state, "reason": reason,
                  "actor": "harness:soak"}
        append(self.desk.world_dir / "history.jsonl", record)
        self.note("world", **record)

    def world_state(self, project: str, asset: str) -> str:
        layer = {"campus_s1": "S1", "fleet_s0": "S0", "vendor_s3": "S3"}[project]
        return self.world().get(WORLD_SECTIONS[layer], {}).get(asset, "normal")

    # ── start and finish / 开始与结束 ──

    def begin(self) -> None:
        """Enable the planned schedules once; a restart finds them enabled. / 只启用一次计划中的排班；重启时已启用。"""
        if self.state["schedules"] is not None:
            return
        results = []
        for entry in self.plan["schedules"]:
            response = self.call("operator", "workflows.schedule", project_id=entry["project_id"],
                                 workflow_id=entry["workflow_id"], trigger_id=entry["trigger_id"], action="enable",
                                 reason=f"P5 soak {self.manifest['soak_id']}")
            results.append({**entry, "ok": bool(response.get("ok")), "at": iso(self.clock()),
                            "state": (response.get("result") or {}).get("state")})
        self.state["schedules"] = results
        self.dirty = True
        self.note("schedules_enabled", schedules=results)

    def finish(self) -> None:
        """Disable the schedules, end every fault, restore a normal world and seal the run.

        停用排班、结束每个故障、恢复正常世界并封存本次运行。
        """
        for entry in self.plan["schedules"]:
            self.call("operator", "workflows.schedule", project_id=entry["project_id"],
                      workflow_id=entry["workflow_id"], trigger_id=entry["trigger_id"], action="disable",
                      reason=f"P5 soak {self.manifest['soak_id']} ended")
        for item_id, status in list(self.state["occurrences"].items()):
            if status.get("status") == "fault_active":
                self.end_fault(next(o for o in self.occurrences if o["id"] == item_id))
        self.clear_switches()
        for section in ("s0", "s1", "s3"):
            for asset, state in self.world().get(section, {}).items():
                if state != "normal":
                    self.set_world({"s0": "S0", "s1": "S1", "s3": "S3"}[section], asset, "normal", "soak ended")
        self.sample(force=True)
        write_json(self.base / "finished.json", {"finished_at": iso(self.clock()), "t0": self.manifest["t0"],
                                                 "end": self.manifest["end"]})
        self.note("finished")

    def clear_switches(self) -> None:
        write_json(self.desk.faults / "model" / "throttle.json", {"until": iso(self.clock() - timedelta(seconds=1))})
        write_json(self.desk.faults / "vendor" / "vendor.json", {"faults": {"duplicate": False, "reorder": False,
                                                                            "drop_replies": {}}})

    # ── the loop / 主循环 ──

    def step(self) -> bool:
        """One pass; False once the soak has ended. / 一次处理；长稳结束后返回 False。"""
        current = self.clock()
        if (self.base / "stop.json").is_file() or current >= self.end:
            self.finish()
            self.save()
            return False
        self.sample()
        for item in self.occurrences:
            if parse(item["at"]) > current:
                break
            if self.occ(item)["status"] == "pending":
                late = (current - parse(item["at"])).total_seconds()
                if late > LATE_GRACE_S and item["layer"] != "fault":
                    self.update(item["id"], status="missed", reason="harness_down", late_s=round(late, 1))
                    self.note("occurrence_missed", occurrence=item["id"], late_s=round(late, 1))
                    continue
                self.start(item, late)
        for item in self.occurrences:
            status = self.state["occurrences"].get(item["id"], {}).get("status")
            if status in ("cycle", "fault_active", "recovering", "probe", "watching", "deferred"):
                self.advance(item)
        if time.monotonic() - self.last_people >= PEOPLE_PERIOD_S:
            self.last_people = time.monotonic()
            self.people()
        self.save()
        return True

    def start(self, item: dict, late: float) -> None:
        kind = item["kind"]
        self.update(item["id"], status="started", started_at=iso(self.clock()), late_s=round(late, 1))
        self.note("occurrence", occurrence=item["id"], occurrence_kind=kind, late_s=round(late, 1))
        workload = self.plan["workload"].get(kind)
        if kind == "s1_watch":
            self.start_run(item, workload["project_id"], workload["workflow_id"], {"asset": item["asset"]})
            self.update(item["id"], status="watching")
        elif kind in ("s1_damage", "s0_damage", "s3_damage"):
            self.set_world(item["layer"], item["asset"], workload["state"], f"{item['id']} damage")
            self.start_run(item, workload["project_id"], workload["workflow_id"], {"asset": item["asset"]})
            self.update(item["id"], status="cycle", stage="damaged")
        elif kind == "s1_road":
            self.set_world("S1", item["asset"], workload["state"], f"{item['id']} obstacle")
            response = self.call("roads", "workflows.event", trust="backend", project_id=workload["project_id"],
                                 workflow_id=workload["workflow_id"], trigger_id=workload["trigger_id"],
                                 event_type=workload["event_type"], event_id=f"soak-{item['id']}-{self.short()}",
                                 payload={"segment": item["asset"]})
            run_id = (response.get("result") or {}).get("run_id")
            if run_id:
                self.track(run_id, item, workload["project_id"], item["asset"])
            self.update(item["id"], status="cycle" if run_id else "failed", stage="damaged", runs=[run_id] if run_id
                        else [], error=(response.get("issue") or {}).get("code"))
        elif kind == "burst":
            runs = []
            for index, (project, workflow, asset) in enumerate(workload["runs"]):
                run_id = self.start_run(item, project, workflow, {"asset": asset}, suffix=f"-{index:02d}")
                if run_id:
                    runs.append(run_id)
            self.update(item["id"], status="watching", runs=runs, expected=len(workload["runs"]))
        elif kind == "planning":
            response = self.call("operator", "missions.submit", project_id=workload["project_id"],
                                 robot_id=workload["robot_id"], text=workload["text"], volume_id=workload["volume_id"],
                                 asset_ids=[], idempotency_key=f"soak-{self.short()}-{item['id']}")
            mission = ((response.get("result") or {}).get("mission") or {})
            mission_id = mission.get("mission_id")
            if mission_id:
                self.state["planning"][mission_id] = item["id"]
            self.update(item["id"], status="watching" if mission_id else "done", mission_id=mission_id,
                        mission_status=mission.get("status"), ok=bool(response.get("ok")),
                        code=(response.get("issue") or {}).get("code"), latency_s=response.get("latency_s"))
        elif kind == "authz":
            self.update(item["id"], status="done", **self.authz(item))
        elif kind == "s2_probe":
            self.start_probe(item, workload)
        else:
            self.start_fault(item)

    def short(self) -> str:
        return self.manifest["soak_id"].rsplit("-", 1)[-1]

    def start_run(self, item: dict, project: str, workflow: str, inputs: dict, suffix: str = "") -> str | None:
        response = self.call("operator", "workflows.start", project_id=project, workflow_id=workflow,
                             request_id=f"soak-{self.short()}-{item['id']}{suffix}", inputs=inputs)
        run_id = ((response.get("result") or {}).get("run") or {}).get("run_id")
        if run_id:
            self.track(run_id, item, project, inputs.get("asset"))
            runs = self.state["occurrences"][item["id"]].setdefault("runs", [])
            if run_id not in runs:
                runs.append(run_id)
            self.dirty = True
        else:
            self.update(item["id"], error=(response.get("issue") or {}).get("code"))
        return run_id

    def track(self, run_id: str, item: dict, project: str, asset: str | None) -> None:
        self.state["runs"].setdefault(run_id, {"occurrence": item["id"], "project_id": project, "asset": asset})
        self.dirty = True

    # ── advancing occurrences / 推进发生时刻 ──

    def advance(self, item: dict) -> None:
        status = self.state["occurrences"][item["id"]]
        kind = item["kind"]
        if status["status"] in ("fault_active", "recovering", "deferred"):
            self.advance_fault(item, status)
        elif status["status"] == "probe":
            self.advance_probe(item, status)
        elif status["status"] == "watching":
            if kind == "planning":
                return  # the people pass settles it / 由扮演人的处理收尾
            states = [self.run_state(run_id) for run_id in status.get("runs", [])]
            if states and all(state in RUN_FINAL for state in states):
                self.update(item["id"], status="done", finished_at=iso(self.clock()), run_states=states)
        elif status["status"] == "cycle":
            self.advance_cycle(item, status)

    def run_state(self, run_id: str) -> str | None:
        project = self.state["runs"].get(run_id, {}).get("project_id")
        view = self.read("workflows.get", project_id=project, run_id=run_id) if project else None
        return (view or {}).get("run", {}).get("state")

    def advance_cycle(self, item: dict, status: dict) -> None:
        """damaged -> an order for the asset -> world repaired and feedback reported -> closed (or not).

        损伤 -> 资产出现工单 -> 世界修复并报告反馈 -> 关单（或未关单）。
        """
        workload = self.plan["workload"][item["kind"]]
        project, asset = workload["project_id"], item["asset"]
        started = parse(status["started_at"])
        if (self.clock() - started).total_seconds() > self.plan["criteria"]["cycle_timeout_h"] * 3600:
            self.set_world(item["layer"], asset, "normal", f"{item['id']} timed out")
            self.update(item["id"], status="timeout", finished_at=iso(self.clock()))
            return
        runs = status.get("runs", [])
        orders = [o for o in self.read("orders.list", project_id=project) or []
                  if o["asset_key"].rsplit("/", 1)[-1] == asset and o["created_at"] >= status["started_at"]]
        if status.get("stage") == "damaged":
            if orders and orders[0]["state"] in ORDER_REPAIRABLE:
                order = orders[0]
                self.set_world(item["layer"], asset, "normal", f"{item['id']} repaired")
                response = self.call("operator", "orders.repair", project_id=project, order_id=order["order_id"],
                                     request_id=f"soak-{self.short()}-{item['id']}-r1", note="soak repair feedback")
                self.update(item["id"], stage="repair_reported", order_id=order["order_id"],
                            repaired_at=iso(self.clock()), repair_ok=bool(response.get("ok")))
                return
            states = [self.run_state(run_id) for run_id in runs]
            if runs and all(state in RUN_FINAL for state in states) and not orders:
                # The watch ended without an order: the damage was not seen, or the run failed.
                # 巡检结束而没有工单：损伤未被看到，或运行失败。
                self.set_world(item["layer"], asset, "normal", f"{item['id']} ended without an order")
                self.update(item["id"], status="no_order", finished_at=iso(self.clock()), run_states=states)
            return
        order = next((o for o in orders if o["order_id"] == status.get("order_id")), None)
        if order is not None and order["state"] == "closed":
            self.update(item["id"], status="closed", finished_at=iso(self.clock()), closed_at=order["closed_at"])
        elif order is not None and order["state"] in ("reinspection_failed", "reinspection_unknown"):
            detail = self.read("orders.get", project_id=project, order_id=order["order_id"]) or {}
            rounds = detail.get("rounds", [])
            if all(r.get("run_state") in RUN_FINAL for r in rounds):
                self.update(item["id"], status="not_closed", finished_at=iso(self.clock()), order_state=order["state"])

    # ── the people / 扮演人 ──

    def people(self) -> None:
        """Approve, review, repair and cancel as the soak's people would, from what the API shows.

        依据 API 所示，像长稳中的人那样审批、复核、维修与取消。
        """
        for project in PROJECTS:
            listing = self.read("workflows.list", project_id=project) or {}
            for run in listing.get("runs", []):
                if run["state"] in RUN_FINAL:
                    continue
                view = self.read("workflows.get", project_id=project, run_id=run["run_id"])
                if view is not None:
                    self.serve_run(project, view)
        for mission_id, item_id in list(self.state["planning"].items()):
            view = self.read("view", mission_id=mission_id)
            status = ((view or {}).get("mission") or {}).get("status")
            if status == "awaiting_approval":
                version = view["mission"]["current_version"]
                response = self.call("approver", "decline", mission_id=mission_id, version=version,
                                     reason="soak planning probe; not flown")
                self.update(item_id, planned_status="awaiting_approval", declined=bool(response.get("ok")))
            elif status in MISSION_FINAL:
                self.update(item_id, status="done", final_status=status, finished_at=iso(self.clock()),
                            issues=[i.get("code") for i in (view.get("issues") or [])][:5] if view else [])
                del self.state["planning"][mission_id]

    def serve_run(self, project: str, view: dict) -> None:
        run = view["run"]
        run_id = run["run_id"]
        inputs = run.get("inputs") or {}
        asset = inputs.get("asset") or inputs.get("segment")
        scheduled = str(run.get("trigger_source", "")).startswith("schedule:")
        if scheduled and project == "fleet_s0" and run_id not in self.state["cancels"]:
            self.state["cancels"][run_id] = {"stage": cancel_choice(self.plan, run_id), "done": False}
            self.dirty = True
        cancel = self.state["cancels"].get(run_id, {})
        stage = cancel.get("stage") if not cancel.get("done") else None
        if run["state"] in ("cancel_requested", "cancelling"):
            return
        for mission in view.get("missions", []):
            if stage == "in_flight" and mission["status"] in ("executing", "delivered", "running", "in_progress"):
                self.cancel_run(project, run_id, "in_flight")
                return
            if mission["status"] != "awaiting_approval":
                continue
            if stage == "before_approval":
                self.cancel_run(project, run_id, "before_approval")
                return
            if project == "campus_s1" and not self.simulator_free():
                # A person approves a PX4 flight when the simulator can fly it soon; an approval lapses in 30 minutes.
                # 人在仿真器能很快飞行时才审批 PX4 飞行；审批 30 分钟后失效。
                continue
            detail = self.read("view", mission_id=mission["mission_id"])
            if detail is None:
                continue
            version = detail["mission"]["current_version"]
            record = next((v for v in detail["versions"] if v["version"] == version), {})
            response = self.call("approver", "approve", mission_id=mission["mission_id"], version=version,
                                 package_hash=record.get("package_hash", ""))
            if stage == "after_approval" and response.get("ok"):
                self.cancel_run(project, run_id, "after_approval")
                return
        for node in view.get("nodes", []):
            if node["state"] != "waiting" or node["activity"] != "human_review":
                continue
            # The reviewer looks at the capture: the world's truth decides. / reviewer 查看采集：由世界真值决定。
            truth = self.world_state(project, asset) if asset else "normal"
            reinspection = run["workflow_id"].endswith("_reinspection")
            decision = ("confirmed" if truth == "normal" else "dismissed") if reinspection else \
                ("confirmed" if truth != "normal" else "dismissed")
            self.call("reviewer", "workflows.review", project_id=project, run_id=run_id, node_id=node["node_id"],
                      decision=decision, request_id=f"soak-{run_id[-12:]}-{node['node_id']}", note=f"world: {truth}")
        for order in view.get("orders", []):
            # An order no cycle owns is repaired at once, so nothing stays open by accident.
            # 没有循环拥有的工单立即维修，避免意外遗留。
            owned = any(s.get("order_id") == order["order_id"] for s in self.state["occurrences"].values())
            if not owned and order["state"] in ORDER_REPAIRABLE and asset:
                if self.world_state(project, asset) != "normal":
                    continue  # its cycle will report the repair / 由其循环报告维修
                self.call("operator", "orders.repair", project_id=project, order_id=order["order_id"],
                          request_id=f"soak-{order['order_id'][-12:]}-adhoc", note="soak repair of an unowned order")

    def simulator_free(self) -> bool:
        """Whether no flight, batch or deployment holds the project's simulator lock right now.

        此刻是否没有飞行、批次或部署持有项目的仿真器锁。
        """
        try:
            import fcntl
        except ImportError:  # not a Linux host (tests) / 非 Linux 主机（测试）
            return True
        with (self.desk.root / "stack.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(stream, fcntl.LOCK_UN)
            return True

    def cancel_run(self, project: str, run_id: str, stage: str) -> None:
        response = self.call("operator", "workflows.cancel", project_id=project, run_id=run_id,
                             request_id=f"soak-cancel-{run_id[-12:]}", reason=f"soak cancellation at {stage}")
        self.state["cancels"][run_id] = {"stage": stage, "done": True, "at": iso(self.clock()),
                                         "ok": bool(response.get("ok"))}
        self.dirty = True

    # ── probes / 探测 ──

    def authz(self, item: dict) -> dict:
        """Calls that must be refused, by identity class; an accepted one is an escape.

        按身份类别发出必须被拒绝的调用；被接受即为逃逸。
        """
        listed = self.read("list") or []
        recent = [m for m in listed if isinstance(m, dict) and m.get("project_id", "campus_s1") in PROJECTS][:1]
        mission_id = recent[0]["mission_id"] if recent else "m-000000000000"
        probes = [
            ("outsider", "view", "first_party", {"mission_id": mission_id}, "service.not_found"),
            ("outsider", "workflows.list", "first_party", {"project_id": "campus_s1"}, "service.not_found"),
            ("other", "workflows.list", "first_party", {"project_id": "fleet_s0"}, "service.not_found"),
            ("other", "audit.list", "first_party", {"project_id": "campus_s1", "before": None, "limit": 5},
             "service.not_found"),
            ("viewer", "workflows.start", "first_party", {"project_id": "fleet_s0", "workflow_id": "fleet_watch",
                                                          "request_id": f"soak-authz-{item['id']}",
                                                          "inputs": {"asset": "asset_red"}}, "auth.project_denied"),
            ("reviewer", "approve", "first_party", {"mission_id": mission_id, "version": 1, "package_hash": "0" * 64},
             None),
            ("approver", "orders.repair", "first_party", {"project_id": "vendor_s3", "order_id": "ord-unknown",
                                                          "request_id": f"soak-authz-{item['id']}", "note": "x"}, None),
            ("viewer", "audit.list", "third_party", {"project_id": "campus_s1", "before": None, "limit": 5}, None),
            ("roads", "workflows.start", "backend", {"project_id": "campus_s1", "workflow_id": "road_watch",
                                                     "request_id": f"soak-authz-{item['id']}",
                                                     "inputs": {"segment": "road_north"}}, None),
            ("viewer", "approve", "anonymous", {"mission_id": mission_id, "version": 1, "package_hash": "0" * 64},
             None),
        ]
        results, escapes, unexpected = [], 0, 0
        for role, method, trust, params, expected in probes:
            response = self.call(role, method, trust=trust, **params)
            code = (response.get("issue") or {}).get("code")
            # Accepted, or refused but carrying object data: an escape. A different refusal code is only noted.
            # 被接受，或被拒绝却带有对象数据：逃逸。拒绝码不同只作记录。
            escaped = bool(response.get("ok")) or response.get("result") is not None
            odd = not escaped and expected is not None and code != expected
            escapes += int(escaped)
            unexpected += int(odd)
            results.append({"role": role, "method": method, "trust": trust, "ok": bool(response.get("ok")),
                            "code": code, "expected": expected, "escaped": escaped, "unexpected_code": odd})
        return {"probes": results, "escapes": escapes, "unexpected_codes": unexpected,
                "finished_at": iso(self.clock())}

    def start_probe(self, item: dict, workload: dict) -> None:
        """One S2 live probe through the desk's model proxy, in the background. / 经任务台模型代理的一次 S2 实调探测（后台）。"""
        record = json.loads((self.desk.base / "current.json").read_text(encoding="utf-8"))
        data = self.desk.root / "data" / "visa_pcb_v1"
        output = self.base / "s2" / item["id"]
        output.mkdir(parents=True, exist_ok=True)
        if not data.is_dir() or not (self.desk.model / "minimax.key").is_file():
            self.update(item["id"], status="failed", reason="dataset_or_key_missing")
            return
        name = f"drone-agent-soak-s2-{self.short()}-{item['id']}"
        argv = ["docker", "run", "--rm", "--name", name, "--label", "io.drone-agent.component=soak-s2",
                "--network", f"{HELPERS['PROJECT']}_desk_model", "--read-only", "--tmpfs", "/tmp:size=64m,mode=1777",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--cpus", "1.0", "--memory", "1g",
                "--pids-limit", "64", "--user", f"{os.getuid()}:{os.getgid()}",
                "-e", "VISION_PROVIDER=minimax-vl", "-e", "MINIMAX_API_KEY_FILE=/model/minimax.key",
                "-e", "HTTPS_PROXY=http://desk-model-proxy:3128", "-e", "NO_PROXY=localhost,127.0.0.1",
                "-e", f"DRONE_SOURCE_SHA={record['source_sha']}", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-v", f"{data}:/data:ro", "-v", f"{self.desk.model}:/model:ro", "-v", f"{output}:/output",
                record["images"]["ground"], "python3", "-m", "drone_agent.eval.p4_s2", "--root", "/workspace", "run",
                "--manifest", "/workspace/eval/s2/visa_pcb_v1/manifest.json", "--data", "/data",
                "--split", workload["split"], "--mode", "live", "--output", "/output/run", "--concurrency", "2",
                "--profile", workload["profile"], "--limit", str(workload["samples"])]
        log = (output / "probe.log").open("wb")
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
        self.probes = getattr(self, "probes", {})
        self.probes[item["id"]] = process
        self.update(item["id"], status="probe", container=name, launched_at=iso(self.clock()))

    def advance_probe(self, item: dict, status: dict) -> None:
        process = getattr(self, "probes", {}).get(item["id"])
        output = self.base / "s2" / item["id"]
        overdue = (self.clock() - parse(status["launched_at"])).total_seconds() >= 3600
        if process is not None and process.poll() is None:
            if not overdue:
                return
            process.kill()
        elif process is None:
            # Adopted after a harness restart: wait while its container still runs. / 编排重启后接管：容器仍在运行时等待。
            running = subprocess.run(["docker", "ps", "-q", "--filter", f"name=^{status['container']}$"],
                                     capture_output=True, timeout=60).stdout.strip()
            if running and not overdue:
                return
            if running:
                self.containers.run(["docker", "stop", "-t", "5", status["container"]])
        code = process.returncode if process is not None else None
        summary = {}
        run = output / "run" / "run.json"
        if run.is_file():
            try:
                header = json.loads(run.read_text(encoding="utf-8"))
                summary = {k: header.get(k) for k in ("samples", "split", "mode", "threshold", "metrics", "errors",
                                                      "refusals", "latency")}
            except ValueError:
                summary = {}
        self.update(item["id"], status="done", exit_code=code, finished_at=iso(self.clock()), summary=summary,
                    adopted=process is None)

    # ── faults / 故障 ──

    def start_fault(self, item: dict) -> None:
        kind, entry = item["kind"], self.plan["faults"][item["kind"]]
        if kind in ("service_restart", "service_kill"):
            ok = self.containers.restart(entry["service"]) if entry["mode"] == "restart" else \
                self.containers.kill_main(entry["service"])
            self.update(item["id"], status="recovering" if ok else "failed", injected=ok, recovery="service",
                        recovering_since=iso(self.clock()))
        elif kind in ("fleet_uplink", "s1_uplink", "model_unreachable"):
            ok = self.containers.disconnect(entry["service"], entry["network"])
            self.update(item["id"], status="fault_active" if ok else "failed", injected=ok,
                        ends_at=iso(parse(item["at"]) + timedelta(minutes=entry["duration_min"])))
        elif kind == "vendor_link":
            write_json(self.desk.faults / "vendor" / "vendor.json",
                       {"faults": {"drop_link_now": item["id"], "down_for_s": entry["down_s"]}})
            self.update(item["id"], status="fault_active", injected=True,
                        ends_at=iso(parse(item["at"]) + timedelta(seconds=entry["down_s"] + 60)))
        elif kind == "vendor_protocol":
            faults = {"duplicate": {"duplicate": True}, "reorder": {"reorder": True},
                      "lost_ack": {"drop_replies": {"flighttask_prepare": 1, "flighttask_execute": 1}}}[item["fault"]]
            write_json(self.desk.faults / "vendor" / "vendor.json", {"faults": faults})
            self.update(item["id"], status="fault_active", injected=True, fault=item["fault"],
                        ends_at=iso(parse(item["at"]) + timedelta(minutes=entry["duration_min"])))
        elif kind == "model_throttle":
            until = parse(item["at"]) + timedelta(minutes=entry["duration_min"])
            write_json(self.desk.faults / "model" / "throttle.json", {"until": iso(until)})
            self.update(item["id"], status="fault_active", injected=True, ends_at=iso(until))
        elif kind == "residents_restart":
            self.update(item["id"], status="deferred", pending=list(entry["services"]))
        self.note("fault_started", occurrence=item["id"], fault=kind)

    def advance_fault(self, item: dict, status: dict) -> None:
        entry = self.plan["faults"][item["kind"]]
        if status["status"] == "recovering":
            self.recover(item, status)
        elif status["status"] == "deferred":
            waited = (self.clock() - parse(item["at"])).total_seconds()
            for service in list(status.get("pending", [])):
                if self.busy(service) and waited < entry["defer_max_min"] * 60:
                    continue
                ok = self.containers.restart(service)
                status.setdefault("restarted", []).append({"service": service, "at": iso(self.clock()), "ok": ok,
                                                           "waited_s": round(waited, 1)})
                status["pending"].remove(service)
                self.dirty = True
            if not status["pending"]:
                self.update(item["id"], status="recovering", recovery="docks", recovering_since=iso(self.clock()))
        elif status.get("ends_at") and self.clock() >= parse(status["ends_at"]):
            self.end_fault(item)

    def busy(self, service: str) -> bool:
        """Whether a restart would cut a flight in progress. / 重启是否会打断进行中的飞行。"""
        if service == "desk-dock":
            return (self.desk.supervisor / "active.json").is_file()
        if service == "desk-fleet":
            try:
                beat = json.loads((self.desk.fleet / "heartbeat.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            return any(robot.get("flying") for robot in beat.get("robots", {}).values())
        return False

    def end_fault(self, item: dict) -> None:
        kind, entry = item["kind"], self.plan["faults"][item["kind"]]
        if kind in ("fleet_uplink", "s1_uplink", "model_unreachable"):
            ok = self.containers.connect(entry["service"], entry["network"])
        elif kind in ("vendor_protocol",):
            write_json(self.desk.faults / "vendor" / "vendor.json",
                       {"faults": {"duplicate": False, "reorder": False, "drop_replies": {}}})
            ok = True
        else:
            ok = True
        recovery = {"fleet_uplink": "robots", "s1_uplink": "robots", "vendor_link": "docks"}.get(kind, "none")
        self.update(item["id"], status="recovering", ended_at=iso(self.clock()), restored=ok, recovery=recovery,
                    recovering_since=iso(self.clock()))
        self.note("fault_ended", occurrence=item["id"], fault=kind, restored=ok)

    def recover(self, item: dict, status: dict) -> None:
        """Measure readiness after a fault: the service answers, every dock session is active again, and the cut-off
        robots report fresh status. Each measure is the time since the fault ended (or the restart was issued).

        测量故障后的就绪：服务应答、每个机场会话重新活动、被断开的机器人重新报告新鲜状态。每个测量值都是自故障结束（或发出
        重启）起的时间。
        """
        waited = (self.clock() - parse(status["recovering_since"])).total_seconds()
        target = status.get("recovery", "none")
        if target == "service":
            health = self.api.call(self.ids["viewer"], "health")
            if health.get("ok") and (health.get("result") or {}).get("status") == "ready":
                self.update(item["id"], service_ready_s=round(waited, 1), recovery="docks")
            elif waited > 900:
                self.update(item["id"], status="done", service_ready_s=None, finished_at=iso(self.clock()))
            return
        if target == "docks":
            sessions = self.dock_sessions()
            if sessions and all(state == "active" for state in sessions.values()):
                self.update(item["id"], status="done", docks_active_s=round(waited, 1), sessions=sessions,
                            finished_at=iso(self.clock()))
            elif waited > 900:
                self.update(item["id"], status="done", docks_active_s=None, sessions=sessions,
                            finished_at=iso(self.clock()))
            return
        if target == "robots":
            robots = {"fleet_uplink": ("uav_fa", "uav_fb"), "s1_uplink": ("uav_01",)}[item["kind"]]
            listed = {r.get("robot_id"): r for r in (self.read("robots") or []) if isinstance(r, dict)}
            fresh = all((self.clock() - parse(listed[r]["status_at"])).total_seconds() < 15
                        for r in robots if r in listed and listed[r].get("status_at"))
            if fresh and all(r in listed for r in robots):
                self.update(item["id"], status="done", robots_fresh_s=round(waited, 1), finished_at=iso(self.clock()))
            elif waited > 900:
                self.update(item["id"], status="done", robots_fresh_s=None, finished_at=iso(self.clock()))
            return
        self.update(item["id"], status="done", finished_at=iso(self.clock()))

    def dock_sessions(self) -> dict:
        found = {}
        for project in PROJECTS:
            resources = self.read("resources.list", project_id=project) or {}
            for dock in resources.get("docks", []) if isinstance(resources, dict) else []:
                found[dock.get("dock_id")] = (dock.get("status") or {}).get("session")
        return found

    # ── sampling / 采样 ──

    def sample(self, force: bool = False) -> None:
        last = self.state.get("last_sample")
        if not force and last and (self.clock() - parse(last)).total_seconds() < self.plan["sampling"]["every_s"]:
            return
        self.state["last_sample"] = iso(self.clock())
        self.dirty = True
        row: dict = {"at": iso(self.clock())}
        health = self.api.call(self.ids["viewer"], "health")
        result = health.get("result") or {}
        row["service"] = {"ok": bool(health.get("ok")), "latency_s": health.get("latency_s"),
                          "source_sha": result.get("source_sha"), "status": result.get("status")}
        row["host"] = HELPERS["capacity"]()
        try:
            row["containers"] = self.containers.stats()
        except (OSError, subprocess.SubprocessError):
            row["containers"] = {}
        restarts = {}
        for service in ("desk-service", "desk-dock", "desk-fleet", "desk-vendor", "desk-uplink", "desk-model-proxy",
                        "desk"):
            details = self.containers.inspect(service)
            state = details.get("State") or {}
            restarts[service] = {"restart_count": details.get("RestartCount"), "started_at": state.get("StartedAt"),
                                 "running": state.get("Running"), "oom": state.get("OOMKilled"),
                                 "exit_code": state.get("ExitCode")}
        row["residents"] = restarts
        row["ledger"] = self.ledger_counts()
        media = self.desk.service / "media"
        row["media_bytes"] = sum(p.stat().st_size for p in media.rglob("*") if p.is_file()) if media.is_dir() else 0
        queues = {}
        for project in PROJECTS:
            listing = self.read("workflows.list", project_id=project) or {}
            runs = listing.get("runs", [])
            queues[project] = {"active_runs": sum(1 for r in runs if r["state"] not in RUN_FINAL),
                               "waiting": sorted({w for r in runs for w in r.get("waiting", [])})}
        row["queues"] = queues
        append(self.base / "samples.jsonl", row)

    def ledger_counts(self) -> dict:
        path = self.desk.service / "ledger.sqlite3"
        found = {"bytes": sum(p.stat().st_size for p in path.parent.glob("ledger.sqlite3*") if p.is_file())}
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                for table in GROWING:
                    found[table] = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                found["awaiting_approval"] = db.execute(
                    "SELECT COUNT(*) FROM missions WHERE status='awaiting_approval'").fetchone()[0]
                found["queued_jobs"] = db.execute(
                    "SELECT COUNT(*) FROM bz_jobs WHERE state IN ('queued','running')").fetchone()[0]
            finally:
                db.close()
        except sqlite3.Error as error:
            found["error"] = str(error)[:200]
        return found


# ── entry points / 入口 ──


def create(root: Path, soak_id: str, *, hours: float | None = None) -> Path:
    """Write the manifest, the plan and the expanded occurrences of a new soak; T0 is now.

    写出新长稳的 manifest、计划与展开的发生时刻；T0 即现在。
    """
    desk = SUPERVISOR["Desk"](root)
    record = json.loads((desk.base / "current.json").read_text(encoding="utf-8"))
    source = root / "releases" / record["deployment_id"] / "source"
    plan, digest = load_plan(source)
    if hours is not None:
        plan = {**plan, "duration_h": hours}
    base = desk.base / "soak" / soak_id
    base.mkdir(parents=True, exist_ok=False)
    t0 = now()
    manifest = {"schema_version": "0.1.0", "soak_id": soak_id, "t0": iso(t0),
                "end": iso(t0 + timedelta(hours=plan["duration_h"])), "source_sha": record["source_sha"],
                "deployment_id": record["deployment_id"], "control_sha256": record.get("control_sha256"),
                "plan_sha256": digest, "plan": plan, "full_length": hours is None,
                "images": record["images"], "created_at": iso(t0)}
    write_json(base / "manifest.json", manifest)
    write_json(base / "occurrences.json", expand(plan, t0))
    write_json(desk.base / "soak" / "current.json", {"soak_id": soak_id})
    for folder in (desk.faults / "model", desk.faults / "vendor", desk.world_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run (or resume) the current soak until it ends")
    parser.parse_args()
    root = HELPERS["workspace"]()
    desk = SUPERVISOR["Desk"](root)
    current = json.loads((desk.base / "soak" / "current.json").read_text(encoding="utf-8"))
    base = desk.base / "soak" / current["soak_id"]
    if (base / "finished.json").is_file():
        return
    soak = Soak(desk, base)
    soak.begin()
    soak.save()
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    while not stop["flag"]:
        try:
            if not soak.step():
                break
        except Exception as error:  # keep the soak alive; the error is recorded / 保持长稳运行；错误被记录
            soak.note("harness_error", error=f"{type(error).__name__}: {error}"[:400])
            soak.save()
            time.sleep(5)
        time.sleep(2)


if __name__ == "__main__":
    main()
