"""Run owned cloud P3 S1 cases: two PX4 SITL aircraft in one Gazebo world under the formal service and scheduler.

Each case: prepare inputs; pick a free private subnet for the simulation network; stop this project's idle SITL; start
the two-instance world and both truth / camera relays and wait for both aircraft's home position and first frames;
start the mission service (catalogs, workflow and scheduling catalogs, ledger in the case directory), both logical dock
backends and both uplinks; wait for both dock sessions. Then act as the people would until every task of the case is
final: submit the case's workflow or tasks as the harness operator, approve each assigned mission's exact package hash
(withholding it where the case says), apply the case's dock fault, and fly every package a claim gate hands out once
with a fresh guardian and executive of that aircraft; both aircraft fly independently and at the same time. Resource
samples of every container and of the host are taken throughout. Afterwards: wait for both reconciled releases, export
every mission view, task view, resources, the ledger tables, the API transcript and both aircraft's flight windows, and
run the P3 S1 judge online and from the MCAP recordings. Nothing here reaches a guardian except starting and stopping it.

在云端运行 P3 S1 用例：同一 Gazebo 世界中的两架 PX4 SITL 飞行器，在正式服务与调度器之下。每个用例：准备输入；为仿真网络
选一个空闲私网段；停止本项目的空闲 SITL；启动双实例世界与两路真值 / 相机转接，等待两架飞行器的 home 位置与首帧；启动任务
服务（运营、工作流与调度目录，账本在用例目录内）、两个逻辑机场后端与两个 uplink；等待两个机场会话。随后像人一样行动，直到
用例的每个任务单都终结：以编排操作者提交用例的工作流或任务单，按确切任务包哈希审批每个已分配任务（用例要求时暂缓），施加
用例的机场故障，并用该飞行器新的 guardian 与 executive 把领取闸门交付的每个任务包飞一次；两架飞行器各自独立、同时飞行。
全程采样每个容器与主机的资源。之后等待两次对账释放，导出每个任务视图、任务单视图、资源、账本表、API 记录与两架飞行器的
飞行窗口，并在线与按 MCAP 录制各运行一次 P3 S1 裁判。除启停 guardian 外，这里没有任何东西触达它。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import runpy
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
M2 = runpy.run_path(str(HERE / "remote_m2.py"))
M3 = runpy.run_path(str(HERE / "remote_m3.py"))
PROJECT = "campus_p3"
OPERATOR, JUDGE = "harness:p3-operator", "harness:p3-judge"
AIRCRAFT = {"a": {"robot": "uav_01", "instance": 0, "dock": "dock_s1a"},
            "b": {"robot": "uav_02", "instance": 1, "dock": "dock_s1b"}}
FOLDERS = ("input", "service", "api", "sitl", "ulog_a", "ulog_b", "sensor_a", "sensor_b", "truth_a", "truth_b",
           "inbox_a", "inbox_b", "mailbox_a", "mailbox_b", "uplink_a", "uplink_b", "robot_a", "robot_b", "aircraft_a",
           "aircraft_b", "ipc_a", "ipc_b", "dock", "control", "world", "service-export", "judge", "idle-flight")
SERVICES = ("sitl-p3", "collector-a", "collector-b", "mission-service", "dock-sim-a", "dock-sim-b", "uplink-a",
            "uplink-b", "guardian-a", "guardian-b", "executive-a", "executive-b")
TASK_FINAL = ("completed", "failed", "outcome_unknown", "rejected", "cancelled")
RUN_FINAL = ("completed", "failed", "outcome_unknown", "cancelled")
SUBNETS = ("10.213.47.0/24", "10.213.48.0/24", "172.29.213.0/24")
CASE_LIMIT_S = 1800
# D061: below either limit the shared host cannot carry two aircraft and the matrix stops. / 低于任一限值即容量不足。
MIN_REAL_TIME_FACTOR, MAX_SUPERVISION_P99_S = 0.5, 0.120
IDLE_PROBE_S = 60
# The frozen two-aircraft budget (D061): the SITL container's CPU and memory caps. / 冻结的双机预算。
SITL_CPUS, SITL_MEM = os.environ.get("DRONE_P3_SITL_CPUS", "2.5"), os.environ.get("DRONE_P3_SITL_MEM", "3g")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_images(source: Path, base: Path, tag: str, checks: str) -> dict:
    """The M2 aircraft and ground images plus the P3 two-aircraft simulation image. / M2 机载与地面镜像加 P3 双机仿真镜像。"""
    images = M2["build_images"](source, base, tag, checks)
    images["sim"] = f"drone-agent-p3-sim:{tag}"
    if not HELPERS["inspect_image"](images["sim"]):
        with (base / "build-p3-sim.log").open("wb") as log:
            result = subprocess.run(["docker", "build", "--pull=false", "--build-arg",
                                     f"AIRCRAFT_IMAGE={images['aircraft']}", "-f", str(source / "sim/p3.Dockerfile"),
                                     "-t", images["sim"], str(source)], stdout=log, stderr=subprocess.STDOUT,
                                    timeout=1800)
        if result.returncode:
            raise RuntimeError(f"P3 simulation image build failed: {base}")
    return images


def provision(root: Path, image: str) -> dict:
    """The M2 secrets plus a client certificate for uav_02 in `robot-tls-uav_02/`. / M2 密钥加 uav_02 的客户端证书。"""
    secrets = root / "secrets" / "m2"
    secrets.mkdir(parents=True, exist_ok=True)
    os.chmod(root / "secrets", 0o700)
    output = HELPERS["run"](["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                             "-v", f"{secrets}:/secrets", image, "python3", "-m", "drone_agent.fleet.provision",
                             "--output", "/secrets", "--also-robot", "uav_02"])
    return json.loads(output.strip().splitlines()[-1])


def free_subnet() -> str:
    """A private /24 no Docker network on the host uses yet (the P3 network keeps its own once created).

    宿主上尚无 Docker 网络使用的私有 /24（P3 网络一经创建就保持自己的网段）。
    """
    names = HELPERS["run"](["docker", "network", "ls", "--format", "{{.Name}}"]).split()
    used = []
    for name in names:
        details = json.loads(HELPERS["run"](["docker", "network", "inspect", name]))[0]
        for config in (details.get("IPAM") or {}).get("Config") or []:
            if config.get("Subnet"):
                used.append((name, ipaddress.ip_network(config["Subnet"], strict=False)))
    own = [str(net) for name, net in used if name == "drone-agent-cloud_sim_p3"]
    if own:
        return own[0]
    for candidate in SUBNETS:
        network = ipaddress.ip_network(candidate)
        if not any(network.overlaps(net) for _, net in used):
            return candidate
    raise RuntimeError("no free private subnet for the P3 simulation network")


def platform_for(source: Path, robot: str) -> bytes:
    """The platform descriptor of another aircraft: only its robot id and sensor frames change (D061).

    另一架飞行器的平台描述：只改机器人 ID 与传感器坐标系（D061）。
    """
    data = yaml.safe_load((source / "configs/platforms/px4_sitl_multirotor.yaml").read_text(encoding="utf-8"))
    base = data["capability"]["robot_id"]
    data["capability"]["robot_id"] = robot
    for sensor in data["capability"].get("sensors", []):
        if sensor.get("frame_id", "").startswith(base + "/"):
            sensor["frame_id"] = robot + sensor["frame_id"][len(base):]
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()


class Sampler(threading.Thread):
    """cgroup samples of this project's containers and the host every two seconds (D061 capacity evidence).

    File reads only; the container map is refreshed with one `docker ps` every ten seconds, since guardians and
    executives are recreated for every flight.

    每两秒采样本项目容器与主机的 cgroup（D061 容量证据）。只读文件；容器映射每十秒用一次 `docker ps` 刷新，因为每次飞行都会
    重建 guardian 与 executive。
    """

    def __init__(self, path: Path):
        super().__init__(daemon=True)
        self.path, self.stop_event = path, threading.Event()

    def groups(self) -> dict:
        rows = HELPERS["run"](["docker", "ps", "--no-trunc", "--filter",
                               "label=com.docker.compose.project=drone-agent-cloud", "--format",
                               "{{.ID}} {{.Label \"com.docker.compose.service\"}}"], timeout=30)
        found = {}
        for line in rows.splitlines():
            container, _, service = line.partition(" ")
            directory = M3["cgroup_dir"](container) if service in SERVICES else None
            if directory is not None:
                found[service] = directory
        return found

    def run(self) -> None:
        groups, refreshed = {}, 0.0
        while not self.stop_event.wait(2.0):
            try:
                if time.monotonic() - refreshed >= 10.0:
                    groups, refreshed = self.groups(), time.monotonic()
                M3["sample_resources"](self.path, groups)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
                continue


def resource_summary(path: Path, window: tuple[float, float] | None = None) -> dict:
    """Per-service CPU (cores), throttling and peak memory, and the host's load and pressure, optionally in a wall
    window. / 各服务 CPU（核）、节流与峰值内存，以及主机负载与压力，可限定在一个墙钟窗口内。"""
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if window is not None:
        rows = [row for row in rows if window[0] <= row["wall"] <= window[1]]
    by_service: dict[str, list[dict]] = {}
    for row in rows:
        by_service.setdefault(row["service"], []).append(row)
    summary = {}
    for service, samples in by_service.items():
        if service == "_host":
            loads = sorted(s["load1"] for s in samples)
            pressure = sorted(s["cpu_some_avg10"] for s in samples)
            summary[service] = {"samples": len(samples), "load1_max": loads[-1], "load1_median": loads[len(loads) // 2],
                                "cpu_some_avg10_max": pressure[-1],
                                "cpu_some_avg10_median": pressure[len(pressure) // 2]}
            continue
        cores, periods, throttled = [], 0, 0
        # A recreated container restarts its counters; only forward steps count. / 重建的容器计数器归零；只计正向增量。
        for earlier, later in zip(samples, samples[1:], strict=False):
            if later["wall"] > earlier["wall"] and later["usage_usec"] >= earlier["usage_usec"]:
                cores.append((later["usage_usec"] - earlier["usage_usec"]) / 1e6 / (later["wall"] - earlier["wall"]))
            if later["nr_periods"] >= earlier["nr_periods"] and later["nr_throttled"] >= earlier["nr_throttled"]:
                periods += later["nr_periods"] - earlier["nr_periods"]
                throttled += later["nr_throttled"] - earlier["nr_throttled"]
        cores.sort()
        summary[service] = {"samples": len(samples), "cpu_cores_mean": round(sum(cores) / len(cores), 3) if cores
                            else None, "cpu_cores_p95": round(cores[min(len(cores) - 1, int(len(cores) * 0.95))], 3)
                            if cores else None, "throttled_fraction": round(throttled / periods, 4) if periods > 0
                            else 0.0, "memory_max_mib": round(max(s["memory_bytes"] for s in samples) / 2**20, 1)}
    return summary


def real_time_factor(path: Path, window: tuple[str, str] | None = None) -> dict | None:
    """Simulated seconds per wall second from the truth samples, per 10 s window, optionally inside [start, end].

    按 10 s 窗口由真值样本计算的实时因子，可限定在 [start, end] 之内。
    """
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    if window is not None:
        start, end = (datetime.fromisoformat(value) for value in window)
        rows = [row for row in rows if start <= datetime.fromisoformat(row["timestamp"]) <= end]
    if len(rows) < 2:
        return None
    factors, start = [], rows[0]
    for row in rows[1:]:
        wall = (datetime.fromisoformat(row["timestamp"]) - datetime.fromisoformat(start["timestamp"])).total_seconds()
        if wall >= 10.0:
            factors.append((row["sim_time"] - start["sim_time"]) / wall)
            start = row
    if not factors:
        return None
    factors.sort()
    return {"windows": len(factors), "min": round(factors[0], 3), "median": round(factors[len(factors) // 2], 3)}


def capacity_verdict(factors: dict, supervision: dict) -> dict:
    """D061's two limits over the flight windows. / 飞行窗口内的 D061 两项限值。"""
    lowest = min((f["min"] for f in factors.values() if f), default=None)
    worst = max((s["p99_s"] for s in supervision.values() if s and s.get("p99_s") is not None), default=None)
    sufficient = lowest is not None and lowest >= MIN_REAL_TIME_FACTOR and worst is not None \
        and worst <= MAX_SUPERVISION_P99_S
    return {"sufficient": sufficient, "real_time_factor_min": lowest, "supervision_p99_max_s": worst,
            "limits": {"real_time_factor_min": MIN_REAL_TIME_FACTOR, "supervision_p99_max_s": MAX_SUPERVISION_P99_S}}


def run_p3(root: Path, deployment: Path, request: dict) -> dict:
    manifest = json.loads((deployment / "manifest.json").read_text())
    sha = manifest["source_sha"]
    tag = sha + "-" + manifest["control_sha256"][:12]
    checks = f"drone-agent-checks:{tag}"
    source = deployment / "source"
    if not (source / "sim/compose.p3.yaml").is_file():
        raise ValueError("deploy a version with the P3 S1 runner first")
    base = root / "artifacts" / deployment.name / ("p3-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 4 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for the two-aircraft P3 S1 cases")
    images = build_images(source, base, tag, checks)
    keys = provision(root, images["ground"])
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.p3_prepare"]))
    cases = suite["s1"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(set(wanted)):
            raise ValueError("unknown P3 S1 case in selection")
    seeds = set(request.get("seeds") or [])
    selection = [(case, seed) for case in cases for seed in case["seeds"] if not seeds or seed in seeds]
    if not selection:
        raise ValueError("empty P3 S1 selection")
    subnet = free_subnet()
    # The idle M0 SITL would compete for the same cores. / 空闲的 M0 SITL 会争用同一批核心。
    HELPERS["compose"](root, deployment, ["stop", "sitl"])
    results = []
    try:
        for index, (case, seed) in enumerate(selection):
            result = run_case(root, source, base, images, sha, request["run_id"], case, seed, subnet,
                              probe=index == 0)
            results.append(result)
            (base / "progress.json").write_text(json.dumps({"source_sha": sha, "results": results}))
            if not result.get("passed") and not request.get("keep_going"):
                break
    finally:
        HELPERS["compose"](root, deployment, ["up", "-d", "--no-build", "--pull", "never", "sitl"])
    after = HELPERS["foreign_identity"]()
    summary = {
        "status": "passed" if results and len(results) == len(selection) and all(r["passed"] for r in results)
        and before == after else "failed",
        "layer": "S1", "aircraft": 2, "source_sha": sha, "deployment_id": deployment.name,
        "artifact_directory": str(base), "planner": "scripted", "task_planning": "deterministic", "subnet": subnet,
        "budget": {"sitl_cpus": SITL_CPUS, "sitl_mem": SITL_MEM},
        "signer_key_id": keys["signer_key_id"], "certificate_fingerprints": keys["fingerprints"],
        "selection": [f"{case['id']}-{seed}" for case, seed in selection], "results": results,
        "other_containers_before": before, "other_containers_after": after,
        "images": {role: HELPERS["inspect_image"](image)["Id"] for role, image in images.items()},
    }
    (base / "suite.json").write_text(json.dumps(summary, indent=2))
    return summary


def run_case(root, source, base, images, sha, run_id, scenario, seed, subnet, *, probe: bool = False) -> dict:
    case = base / f"{scenario['id']}-{seed}"
    for folder in FOLDERS:
        (case / folder).mkdir(parents=True)
    # The dock backends run without capabilities, so their log directory must be writable without DAC override.
    # 机场后端不带任何 capability，其日志目录须无需越权即可写。
    os.chmod(case / "dock", 0o777)
    network = ipaddress.ip_network(subnet)
    hosts = list(network.hosts())
    platform = platform_for(source, "uav_02")
    (case / "platform-b.yaml").write_bytes(platform)
    env = dict(os.environ, DRONE_SOURCE_SHA=sha, DRONE_P3_RUN=str(case), DRONE_P3_SECRETS=str(root / "secrets" / "m2"),
               DRONE_P3_SIM_IMAGE=images["sim"], DRONE_P3_AIRCRAFT_IMAGE=images["aircraft"],
               DRONE_P3_GROUND_IMAGE=images["ground"], DRONE_P3_SUBNET=subnet, DRONE_P3_SITL_IP=str(hosts[9]),
               DRONE_P3_GUARDIAN_A_IP=str(hosts[20]), DRONE_P3_GUARDIAN_B_IP=str(hosts[21]),
               DRONE_P3_SITL_CPUS=SITL_CPUS, DRONE_P3_SITL_MEM=SITL_MEM,
               DRONE_P3_PACKAGE_A="none.json", DRONE_P3_PACKAGE_B="none.json", DRONE_P3_EPOCH_A="0",
               DRONE_P3_EPOCH_B="0", DRONE_P3_FLIGHT_A=str(case / "idle-flight"),
               DRONE_P3_FLIGHT_B=str(case / "idle-flight"))
    prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.p3.yaml")]
    transcript, injections, snapshots = [], [], []
    flights: dict[str, list[dict]] = {"a": [], "b": []}

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"P3 compose failed ({result.returncode}); artifacts: {case}")
        return result

    def api(method: str, *, actor: str = OPERATOR, record: bool = True, **params) -> dict:
        result = compose("exec", "-T", "mission-service", "python3", "-m", "drone_agent.fleet.api", "--socket",
                         "/run/mission/api.sock", "--actor", actor, method, json.dumps(params, ensure_ascii=False),
                         timeout=180, quiet=True, check=False)
        try:
            response = json.loads(result.stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            response = {"ok": False, "issue": {"code": "service.degraded"}}
        value = response.get("result")
        if record:
            transcript.append({"at": now(), "actor": actor, "trust": "first_party", "method": method, "probe": False,
                               "params": params, "ok": bool(response.get("ok")),
                               "code": (response.get("issue") or {}).get("code"),
                               "result_sha256": hashlib.sha256(json.dumps(value, sort_keys=True, default=str)
                                                               .encode()).hexdigest() if value is not None else None,
                               "mission_id": (value or {}).get("mission", {}).get("mission_id")
                               if isinstance(value, dict) else None, "result": None})
        return response

    def wait(predicate, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.5)
        raise RuntimeError(f"timed out waiting for {what}")

    def inject(kind: str, **fields) -> None:
        injections.append({"at": now(), "kind": kind, **fields})

    def service_up() -> None:
        (case / "service/ready.json").unlink(missing_ok=True)
        compose("up", "-d", "--no-build", "--pull", "never", "mission-service")
        wait(lambda: (case / "service/ready.json").exists(), 120, "mission service")

    def dock(key: str) -> dict:
        response = api("resources.get", actor=JUDGE, record=False, project_id=PROJECT,
                       resource_id=AIRCRAFT[key]["dock"])
        return response.get("result") or {} if response.get("ok") else {}

    def snapshot(label: str) -> None:
        response = api("resources.list", actor=JUDGE, record=False, project_id=PROJECT)
        snapshots.append({"label": label, "at": now(), "project": PROJECT, "resources": response.get("result")})

    def tasks() -> list[dict]:
        response = api("tasks.list", actor=JUDGE, record=False, project_id=PROJECT)
        return (response.get("result") or {}).get("tasks", []) if response.get("ok") else []

    def home_set(key: str) -> bool:
        log = case / "sitl" / f"px4-{AIRCRAFT[key]['instance']}.log"
        return log.exists() and b"home set" in log.read_bytes()

    sampler = Sampler(case / "resources.jsonl")
    try:
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case / 'input'}:/input",
                        images["ground"], "python3", "-m", "drone_agent.eval.p3_prepare", "--output", "/input",
                        "--scenario", scenario["id"], "--seed", str(seed), "--sha", sha])
        metadata = json.loads((case / "input/scenario.json").read_text())
        for key in AIRCRAFT:
            (case / "control" / f"dock_{key}.json").write_text("{}")
        compose("stop", *SERVICES, check=False)
        gate = M3["quiet_gate"]()
        inject("quiet_gate", **gate)
        sampler.start()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl-p3")
        wait(lambda: home_set("a") and home_set("b"), 240, "both PX4 instances' home position")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector-a", "collector-b")
        wait(lambda: (case / "sensor_a/latest.json").exists() and (case / "sensor_b/latest.json").exists(), 120,
             "both aircraft's first camera frames")
        # The same warm-up as the M2 cases: the estimators set home before anything connects.
        # 与 M2 用例相同的预热：估计器先设置 home 再有任何连接。
        time.sleep(5)
        idle = None
        if probe:
            # D061 capacity probe: both instances idle on their pads before anything connects.
            # D061 容量探针：任何连接之前，两个实例在机位上空闲。
            began = time.time()
            time.sleep(IDLE_PROBE_S)
            idle = (began, time.time())
            inject("idle_probe", seconds=IDLE_PROBE_S)
        service_up()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "dock-sim-a", "dock-sim-b")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "uplink-a", "uplink-b")
        wait(lambda: all((dock(k).get("status") or {}).get("session") == "active" for k in AIRCRAFT), 90,
             "both active dock sessions")
        snapshot("before")
        submitted: dict[str, str] = {}
        root_run = None
        if metadata["workflow_id"]:
            started = api("workflows.start", project_id=PROJECT, workflow_id=metadata["workflow_id"],
                          request_id=f"{scenario['id']}-{seed}-{run_id}"[:120], inputs={})
            if not started.get("ok"):
                raise RuntimeError(f"workflow start refused: {started.get('issue')}")
            root_run = started["result"]["run"]["run_id"]
            inject("workflow_started", run_id=root_run, workflow_id=metadata["workflow_id"])
        active: dict[str, dict | None] = {"a": None, "b": None}
        mirroring: list[tuple[str, dict, float]] = []
        flown, approved = set(), set()
        faulted = {"done": False}
        expected_tasks = len(metadata["tasks"]) or 2
        deadline = time.monotonic() + CASE_LIMIT_S
        while time.monotonic() < deadline:
            listed = tasks()
            by_id = {t["task_id"]: t for t in listed}
            for item in metadata["tasks"]:
                if item["key"] in submitted:
                    continue
                if item["after"]:
                    before_key, state = item["after"].split(":")
                    earlier = by_id.get(submitted.get(before_key, ""))
                    if earlier is None or earlier["state"] != state:
                        continue
                response = api("tasks.submit", project_id=PROJECT, asset_id=item["asset"],
                               volume_id="campus_training", candidates=[], priority=0,
                               idempotency_key=f"{item['key']}-{seed}-{run_id[-8:]}")
                if not response.get("ok"):
                    raise RuntimeError(f"task refused: {response.get('issue')}")
                submitted[item["key"]] = response["result"]["task"]["task_id"]
                inject("task_submitted", key=item["key"], task_id=submitted[item["key"]], asset=item["asset"])
            if metadata["fault"] == "maintenance_after_assignment" and not faulted["done"]:
                first = by_id.get(submitted.get(metadata["tasks"][0]["key"], ""))
                if first is not None and first["state"] == "assigned" and first["robot_id"] == metadata["hold_approval"]:
                    (case / "control/dock_a.json").write_text(json.dumps({"upkeep": "maintenance"}))
                    faulted["done"] = True
                    inject("upkeep", dock="dock_s1a", value="maintenance", task=first["task_id"], epoch=first["epoch"])
            for task in listed:
                mission_id = task["mission_id"]
                if task["state"] != "assigned" or not mission_id or mission_id in approved:
                    continue
                if metadata["hold_approval"] and task["robot_id"] == metadata["hold_approval"]:
                    continue
                detail = api("view", actor=JUDGE, record=False, mission_id=mission_id).get("result")
                if not detail or detail["mission"]["status"] != "awaiting_approval":
                    continue
                version = detail["mission"]["current_version"]
                record = next(v for v in detail["versions"] if v["version"] == version)
                if api("approve", mission_id=mission_id, version=version, package_hash=record["package_hash"]).get("ok"):
                    approved.add(mission_id)
            # Both aircraft fly independently; neither waits for the other. / 两架飞行器各自飞行，互不等待。
            for key in AIRCRAFT:
                if active[key] is None:
                    history = case / f"inbox_{key}/history"
                    pending = sorted((p for p in history.glob("m-*-v*.json") if p.name not in flown),
                                     key=lambda p: p.stat().st_mtime) if history.is_dir() else []
                    if pending:
                        if flights[key]:
                            raise RuntimeError(f"{AIRCRAFT[key]['robot']} got a second package; a battery swap of "
                                               "one instance is not supported in the two-aircraft world")
                        flown.add(pending[0].name)
                        active[key] = start_flight(case, compose, wait, key, pending[0])
                else:
                    finished = poll_flight(case, compose, key, active[key])
                    if finished:
                        flights[key].append(finished)
                        mirroring.append((key, finished, time.monotonic() + 120))
                        active[key] = None
            for item in list(mirroring):
                key, entry, limit = item
                if mirrored(api, entry, case / f"aircraft_{key}"):
                    mirroring.remove(item)
                elif time.monotonic() > limit:
                    raise RuntimeError(f"timed out waiting for the service mirror of {entry['mission_id']}"
                                       f"-v{entry['version']}")
            settled = len(listed) >= expected_tasks and all(t["state"] in TASK_FINAL for t in listed) \
                and len(submitted) == len(metadata["tasks"]) and not mirroring \
                and all(v is None for v in active.values())
            if settled and root_run:
                run_view = api("workflows.get", actor=JUDGE, record=False, project_id=PROJECT, run_id=root_run)
                settled = (run_view.get("result") or {}).get("run", {}).get("state") in RUN_FINAL
            if settled:
                break
            time.sleep(1.0)
        else:
            raise RuntimeError("the case's tasks did not finish in time")
        wait(lambda: all(dock(k).get("pad") == "free" for k in AIRCRAFT), 150, "both reconciled releases")
        snapshot("after")
        missions, task_views = {}, {}
        for task in tasks():
            task_views[task["task_id"]] = api("tasks.get", actor=JUDGE, record=False, project_id=PROJECT,
                                              task_id=task["task_id"]).get("result")
        for view in task_views.values():
            for assignment in (view or {}).get("assignments", []):
                if assignment["mission_id"]:
                    missions[assignment["mission_id"]] = api("view", actor=JUDGE, record=False,
                                                             mission_id=assignment["mission_id"])["result"]
        (case / "service-export/views.json").write_text(json.dumps(missions, ensure_ascii=False, indent=2))
        (case / "service-export/task-views.json").write_text(json.dumps(task_views, ensure_ascii=False, indent=2,
                                                                        default=str))
        if root_run:
            (case / "service-export/workflow-views.json").write_text(json.dumps(
                api("workflows.get", actor=JUDGE, record=False, project_id=PROJECT, run_id=root_run).get("result"),
                ensure_ascii=False, indent=2, default=str))
        (case / "service-export/resources.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
        logs = compose("logs", "--no-color", "--no-log-prefix", "sitl-p3", timeout=60, check=False, quiet=True)
        (case / "sitl.log").write_bytes(logs.stdout)
        compose("stop", "-t", "5", "uplink-a", "uplink-b", "dock-sim-a", "dock-sim-b", "mission-service",
                "collector-a", "collector-b")
        compose("stop", "-t", "10", "sitl-p3")
        sampler.stop_event.set()
        for name, rows in (("api.jsonl", transcript), ("injections.jsonl", injections)):
            (case / "world" / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        for key, info in AIRCRAFT.items():
            (case / "world" / f"{info['robot']}-flights.json").write_text(json.dumps(flights[key], indent=2))
        (case / "input/layout.json").write_text(json.dumps({
            "catalog": "configs/sites/p3_s1_v1.yaml", "members": "configs/sites/p3_members_s1.yaml",
            "scheduling": "configs/scheduling/p3_s1_v1.yaml", "workflows": "configs/workflows/p3_s1_v1.yaml",
            "platform_b_sha256": hashlib.sha256(platform).hexdigest()}, indent=2))
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"], "python3",
                        "-m", "drone_agent.eval.p3_prepare", "--export", "/run/service/ledger.sqlite3",
                        "--output", "/run/service-export/scheduling.json"])
        judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=600, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_p3",
                           "/run", "--layer", "s1", "--root", "/workspace", "--replay", "--output",
                           "/output/replay.json", timeout=600, check=False)
        replay_path = case / "judge/replay.json"
        replay = json.loads(replay_path.read_text()) if replay_path.exists() else {}
        result["replay_agrees"] = all(result.get(k) == replay.get(k) for k in
                                      ("classification", "false_success_reports", "problems", "counts"))
        result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
        result["scenario"], result["seed"] = scenario["id"], seed
        result["flown"] = {AIRCRAFT[k]["robot"]: [f"{f['mission_id']}-v{f['version']}" for f in v]
                           for k, v in flights.items()}
        windows = [(f["started_at"], f["ended_at"]) for v in flights.values() for f in v if f.get("ended_at")]
        span = (min(w[0] for w in windows), max(w[1] for w in windows)) if windows else None
        factors = {AIRCRAFT[k]["robot"]: real_time_factor(case / f"truth_{k}/truth.jsonl", span) for k in AIRCRAFT}
        supervision = {f"{f['robot_id']}:{f['mission_id']}:v{f['version']}": f.get("supervision")
                       for v in flights.values() for f in v}
        verdict = capacity_verdict(factors, supervision)
        result["capacity"] = {"verdict": verdict, "resources": resource_summary(case / "resources.jsonl"),
                              "idle": resource_summary(case / "resources.jsonl", idle) if idle else None,
                              "real_time_factor": factors, "supervision": supervision, "quiet_gate": gate,
                              "flight_span": span}
        result["passed"] = result["passed"] and verdict["sufficient"]
        return result
    except Exception as error:
        compose("logs", "--no-color", "--tail", "200", check=False)
        compose("stop", "-t", "5", *SERVICES, check=False)
        return {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}
    finally:
        sampler.stop_event.set()


def start_flight(case: Path, compose, wait, key: str, package: Path) -> dict:
    """Start one handed-out package with a fresh guardian and executive of aircraft `key`. / 用新 guardian 与 executive 起飞。"""
    mission_id, version = package.stem.rsplit("-v", 1)
    flight = case / f"aircraft_{key}" / mission_id / f"v{version}"
    flight.mkdir(parents=True)
    state = case / f"robot_{key}/authority.json"
    epoch = json.loads(state.read_text())["epoch_watermark"] + 1 if state.exists() else 1
    upper = key.upper()
    extra = {f"DRONE_P3_PACKAGE_{upper}": package.name, f"DRONE_P3_EPOCH_{upper}": str(epoch),
             f"DRONE_P3_FLIGHT_{upper}": str(flight)}
    entry = {"robot_id": AIRCRAFT[key]["robot"], "mission_id": mission_id, "version": int(version), "epoch": epoch,
             "started_at": now(), "ended_at": None}
    (case / f"ipc_{key}/guardian.sock").unlink(missing_ok=True)
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", f"guardian-{key}", extra=extra)
    wait(lambda: (case / f"ipc_{key}/guardian.sock").exists(), 90, f"guardian-{key}")
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", f"executive-{key}", extra=extra)
    return {"entry": entry, "flight": flight, "extra": extra, "deadline": time.monotonic() + 420, "cleanup": False,
            "landed_at": None}


def poll_flight(case: Path, compose, key: str, active: dict) -> dict | None:
    """One look at a running flight; the finished entry once grounded, disarmed and with its result. / 查看一次飞行。"""
    flight, entry = active["flight"], active["entry"]
    if time.monotonic() > active["deadline"]:
        raise RuntimeError(f"{entry['mission_id']}-v{entry['version']} exceeded its bounded flight duration")
    path = flight / "status.json"
    status = json.loads(path.read_text()) if path.exists() else None
    if status is None:
        return None
    observation = status["observation"]
    if status["safety"] == "abort" and not active["cleanup"]:
        # Simulation operator ends a surrendered flight of this instance. / 仿真操作员收尾本实例已移交控制权的飞行。
        compose("exec", "-T", "sitl-p3", "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander", "--instance",
                str(AIRCRAFT[key]["instance"]), "land", check=False)
        active["cleanup"] = True
        (flight / "manual-cleanup.json").write_text(json.dumps({"reason": status["reason"]}))
    if observation.get("in_air") is False and observation.get("armed") is False and (flight / "result.json").exists():
        if active["landed_at"] is None:
            active["landed_at"] = time.monotonic()
            entry["ended_at"] = now()
            return None
        if time.monotonic() - active["landed_at"] < 2.2:
            return None
        compose("stop", "-t", "5", f"executive-{key}", f"guardian-{key}", extra=active["extra"])
        supervision = flight / "supervision.json"
        entry["supervision"] = json.loads(supervision.read_text()) if supervision.exists() else None
        return entry
    return None


def mirrored(api, entry: dict, aircraft: Path) -> bool:
    """Whether the service mirrored both journals of this version. / 服务是否已镜像本版本的两本账本。"""
    flight = aircraft / entry["mission_id"] / f"v{entry['version']}"
    onboard = {name: M2["rows"](flight / f"{name}.jsonl") for name in ("executive", "guardian")}
    response = api("view", actor=JUDGE, record=False, mission_id=entry["mission_id"])
    record = next((v for v in (response.get("result") or {}).get("versions", [])
                   if v["version"] == entry["version"]), {})
    journals = record.get("journals", {})
    return all(journals.get(name) == {"rows": count, "chain": "ok"} for name, count in onboard.items()) \
        and record.get("status") == "finished"
