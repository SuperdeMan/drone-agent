"""Run owned cloud X1 S1 cases (D088): the x500 uav_01 and the differential rover ugv_01 in one Gazebo world under the
formal service with the scheduler and the handoff engine.

Each case: prepare inputs; pick a free private subnet for the simulation network; stop this project's idle SITL; wait
for a quiet window; start the two-instance world and both truth / camera relays and wait for both robots' home
positions and first frames; start the mission service (operations, workflow, scheduling and business catalogs with
`--air-ground`, ledger in the case directory), the aircraft's logical dock backend, both uplinks and the rover's
read-only observer; wait until both robots are eligible. Then place the case's anomaly block in Gazebo and act as the
people would until the case's workflow run is final: start the run as the harness operator, approve each mission's
exact package hash (the aircraft's inspection and the ground mission the handoff creates), confirm the review, and run
every package a claim gate hands out once with a fresh guardian and executive of that robot (the rover's observer
stops for its mission and starts again afterwards). The case's actions are applied to the world or to the rover's link:
remove the block once the offer is out, disconnect the rover's uplink from the service network for 12 s while it
drives, or cancel the ground mission while it drives and, once the rover stopped and disarmed on the way, carry it to
its parking spot as a person would (its hold and its task wait for that). Resource samples of every container and of
the host are taken throughout. Afterwards: wait for the reconciled releases, export every mission, task and workflow view, the resources, the ledger tables, the API transcript, the injections and
both robots' runs, extract the rover's armed periods from its PX4 flight log, and run the X1 S1 judge online and from
the recordings. Nothing here reaches a guardian except starting and stopping it.

在云端运行 X1 S1 用例（D088）：同一 Gazebo 世界中的 x500 uav_01 与差速地面车 ugv_01，在带调度器与交接引擎的正式服务之下。
每个用例：准备输入；为仿真网络选一个空闲私网段；停止本项目的空闲 SITL；等待安静窗口；启动双实例世界与两路真值 / 相机转接，
等待两台机器人的 home 位置与首帧；启动任务服务（带 `--air-ground` 的运营、工作流、调度与业务目录，账本在用例目录内）、飞行器
的逻辑机场后端、两个 uplink 与地面车的只读观测；等待两台机器人都可派遣。随后在 Gazebo 中放置用例的异常物方块，像人一样行动
直到用例的工作流运行终结：以编排操作者启动运行，按确切任务包哈希审批每个任务（飞行器的巡检与交接产生的地面任务），确认复核，
并用该机器人新的 guardian 与 executive 把领取闸门交付的每个任务包执行一次（地面车的观测为其任务停下，结束后重新启动）。用例
动作作用于世界或地面车的链路：提议发出后移走方块，地面车行驶中把其 uplink 从服务网络断开 12 s，或地面车行驶中取消地面任务，
并在车于途中停稳上锁后像人一样把它送回停车位（其持有与任务单都等这一步）。全程采样每个容器与主机的资源。之后等待对账释放，
导出每个任务、任务单与工作流视图、资源、
账本表、API 记录、注入与两台机器人的执行记录，从地面车的 PX4 飞行日志取出解锁区间，并在线与按录制各运行一次 X1 S1 裁判。
除启停 guardian 外，这里没有任何东西触达它。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import runpy
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
M2 = runpy.run_path(str(HERE / "remote_m2.py"))
M3 = runpy.run_path(str(HERE / "remote_m3.py"))
P3 = runpy.run_path(str(HERE / "remote_p3.py"))
PROJECT = "campus_x1"
OPERATOR, REVIEWER, JUDGE = "harness:x1-operator", "harness:x1-reviewer", "harness:x1-judge"
ROBOTS = {"a": {"robot": "uav_01", "instance": 0, "dock": "dock_s1a", "ground": False},
          "g": {"robot": "ugv_01", "instance": 1, "dock": None, "ground": True}}
FOLDERS = ("input", "service", "api", "sitl", "ulog_a", "ulog_g", "sensor_a", "sensor_g", "truth_a", "truth_g", "dock",
           "control", "world", "service-export", "judge", "idle-flight",
           *(f"robots/{robot}/{folder}" for robot in ("uav_01", "ugv_01")
             for folder in ("inbox", "mailbox", "uplink", "robot", "aircraft", "ipc")), "robots/ugv_01/aircraft/idle")
SERVICES = ("sitl-x1", "collector-a", "collector-g", "mission-service", "dock-sim-a", "uplink-a", "uplink-g",
            "guardian-a", "executive-a", "observer-g", "guardian-g", "executive-g")
TASK_FINAL = ("completed", "failed", "outcome_unknown", "rejected", "cancelled")
RUN_FINAL = ("completed", "failed", "outcome_unknown", "cancelled")
SUBNETS = ("10.213.49.0/24", "10.213.50.0/24", "172.29.214.0/24")
NETWORK = "drone-agent-cloud_sim_x1"
UPLINK_NETWORK = "drone-agent-cloud_uplink"
CASE_LIMIT_S = 1500
FLIGHT_LIMIT_S = 420
DRIVE_LIMIT_S = 300
LINK_CUT_S = 12.0
STILL_MPS = 0.05
SITL_CPUS, SITL_MEM = os.environ.get("DRONE_X1_SITL_CPUS", "2.5"), os.environ.get("DRONE_X1_SITL_MEM", "3g")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_images(source: Path, base: Path, tag: str, checks: str) -> dict:
    """The M2 aircraft and ground images plus the X1 simulation image. / M2 机载与地面镜像加 X1 仿真镜像。"""
    images = M2["build_images"](source, base, tag, checks)
    images["sim"] = f"drone-agent-x1-sim:{tag}"
    if not HELPERS["inspect_image"](images["sim"]):
        with (base / "build-x1-sim.log").open("wb") as log:
            result = subprocess.run(["docker", "build", "--pull=false", "--build-arg",
                                     f"AIRCRAFT_IMAGE={images['aircraft']}", "-f", str(source / "sim/x1.Dockerfile"),
                                     "-t", images["sim"], str(source)], stdout=log, stderr=subprocess.STDOUT,
                                    timeout=1800)
        if result.returncode:
            raise RuntimeError(f"X1 simulation image build failed: {base}")
    return images


def free_subnet() -> str:
    """A private /24 no Docker network on the host uses yet; the X1 network keeps its own once created.

    宿主上尚无 Docker 网络使用的私有 /24；X1 网络一经创建就保持自己的网段。
    """
    names = HELPERS["run"](["docker", "network", "ls", "--format", "{{.Name}}"]).split()
    used = []
    for name in names:
        details = json.loads(HELPERS["run"](["docker", "network", "inspect", name]))[0]
        for config in (details.get("IPAM") or {}).get("Config") or []:
            if config.get("Subnet"):
                used.append((name, ipaddress.ip_network(config["Subnet"], strict=False)))
    own = [str(net) for name, net in used if name == NETWORK]
    if own:
        return own[0]
    for candidate in SUBNETS:
        network = ipaddress.ip_network(candidate)
        if not any(network.overlaps(net) for _, net in used):
            return candidate
    raise RuntimeError("no free private subnet for the X1 simulation network")


class Sampler(threading.Thread):
    """cgroup samples of this case's containers and the host every two seconds (the P3 capacity evidence).

    每两秒采样本用例容器与主机的 cgroup（与 P3 相同的容量证据）。
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


def run_x1(root: Path, deployment: Path, request: dict) -> dict:
    manifest = json.loads((deployment / "manifest.json").read_text())
    sha = manifest["source_sha"]
    tag = sha + "-" + manifest["control_sha256"][:12]
    checks = f"drone-agent-checks:{tag}"
    source = deployment / "source"
    if not (source / "sim/compose.x1.yaml").is_file():
        raise ValueError("deploy a version with the X1 S1 runner first")
    base = root / "artifacts" / deployment.name / ("x1-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 4 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for the X1 S1 cases")
    images = build_images(source, base, tag, checks)
    keys = M2["provision"](root, images["ground"], "m2", ("ugv_01",))
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.x1_prepare"]))
    cases = suite["s1"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(set(wanted)):
            raise ValueError("unknown X1 S1 case in selection")
    seeds = set(request.get("seeds") or [])
    selection = [(case, seed) for case in cases for seed in case.get("seeds", suite["seeds"])
                 if not seeds or seed in seeds]
    if not selection:
        raise ValueError("empty X1 S1 selection")
    subnet = free_subnet()
    # The idle M0 SITL would compete for the same cores. / 空闲的 M0 SITL 会争用同一批核心。
    HELPERS["compose"](root, deployment, ["stop", "sitl"])
    results = []
    try:
        for case, seed in selection:
            result = run_case(root, source, base, images, sha, request["run_id"], case, seed, subnet)
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
        "layer": "S1", "robots": {"aircraft": 1, "ground": 1}, "source_sha": sha, "deployment_id": deployment.name,
        "artifact_directory": str(base), "planner": "scripted", "subnet": subnet,
        "budget": {"sitl_cpus": SITL_CPUS, "sitl_mem": SITL_MEM},
        "signer_key_id": keys["signer_key_id"], "certificate_fingerprints": keys["fingerprints"],
        "selection": [f"{case['id']}-{seed}" for case, seed in selection], "results": results,
        "other_containers_before": before, "other_containers_after": after,
        "images": {role: HELPERS["inspect_image"](image)["Id"] for role, image in images.items()},
    }
    (base / "suite.json").write_text(json.dumps(summary, indent=2))
    return summary


def run_case(root, source, base, images, sha, run_id, scenario, seed, subnet) -> dict:
    case = base / f"{scenario['id']}-{seed}"
    for folder in FOLDERS:
        (case / folder).mkdir(parents=True, exist_ok=True)
    # The dock backend runs without capabilities, so its log directory must be writable without DAC override.
    # 机场后端不带任何 capability，其日志目录须无需越权即可写。
    os.chmod(case / "dock", 0o777)
    hosts = list(ipaddress.ip_network(subnet).hosts())
    env = dict(os.environ, DRONE_SOURCE_SHA=sha, DRONE_X1_RUN=str(case), DRONE_X1_SECRETS=str(root / "secrets" / "m2"),
               DRONE_X1_SIM_IMAGE=images["sim"], DRONE_X1_AIRCRAFT_IMAGE=images["aircraft"],
               DRONE_X1_GROUND_IMAGE=images["ground"], DRONE_X1_SUBNET=subnet, DRONE_X1_SITL_IP=str(hosts[9]),
               DRONE_X1_GUARDIAN_A_IP=str(hosts[20]), DRONE_X1_GUARDIAN_G_IP=str(hosts[21]),
               DRONE_X1_SITL_CPUS=SITL_CPUS, DRONE_X1_SITL_MEM=SITL_MEM,
               DRONE_X1_PACKAGE_A="none.json", DRONE_X1_PACKAGE_G="none.json", DRONE_X1_EPOCH_A="0",
               DRONE_X1_EPOCH_G="0", DRONE_X1_FLIGHT_A=str(case / "idle-flight"),
               DRONE_X1_FLIGHT_G=str(case / "idle-flight"))
    prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.x1.yaml")]
    transcript, injections, snapshots = [], [], []
    runs: dict[str, list[dict]] = {"a": [], "g": []}

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"X1 compose failed ({result.returncode}); artifacts: {case}")
        return result

    relay = P3["Relay"]([*prefix, "exec", "-T", "mission-service", "python3", "-m", "drone_agent.fleet.api",
                         "--socket", "/run/mission/api.sock", "--relay"], env)

    def api(method: str, *, actor: str = OPERATOR, record: bool = True, **params) -> dict:
        response = relay.call({"actor": actor, "method": method, "params": params}) \
            or {"ok": False, "issue": {"code": "service.degraded"}}
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

    def gz(service: str, reqtype: str, request: str) -> bool:
        """One request to the Gazebo world's own service. / 对 Gazebo 世界服务的一次请求。"""
        result = compose("exec", "-T", "sitl-x1", "gz", "service", "-s", f"/world/default/{service}", "--reqtype",
                         reqtype, "--reptype", "gz.msgs.Boolean", "--timeout", "5000", "--req", request,
                         timeout=60, check=False)
        return result.returncode == 0 and b"data: true" in result.stdout

    def view(mission_id: str) -> dict | None:
        response = api("view", actor=JUDGE, record=False, mission_id=mission_id)
        return response.get("result") if response.get("ok") else None

    def listing() -> dict:
        response = api("tasks.list", actor=JUDGE, record=False, project_id=PROJECT)
        return response.get("result") or {} if response.get("ok") else {}

    def workflow(run: str) -> dict:
        response = api("workflows.get", actor=JUDGE, record=False, project_id=PROJECT, run_id=run)
        return response.get("result") or {} if response.get("ok") else {}

    def snapshot(label: str) -> None:
        response = api("resources.list", actor=JUDGE, record=False, project_id=PROJECT)
        snapshots.append({"label": label, "at": now(), "project": PROJECT, "resources": response.get("result")})

    def approve(mission_id: str) -> bool:
        detail = view(mission_id)
        if not detail or detail["mission"]["status"] != "awaiting_approval":
            return False
        version = detail["mission"]["current_version"]
        record = next(v for v in detail["versions"] if v["version"] == version)
        return bool(api("approve", mission_id=mission_id, version=version,
                        package_hash=record["package_hash"]).get("ok"))

    def home_set(instance: int) -> bool:
        log = case / "sitl" / f"px4-{instance}.log"
        return log.exists() and b"home set" in log.read_bytes()

    def uplink_container() -> str:
        return compose("ps", "-q", "uplink-g", quiet=True).stdout.decode().strip()

    def observer(up: bool) -> None:
        if up:
            compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "observer-g")
        else:
            compose("stop", "-t", "5", "observer-g")

    def released(mission_id: str) -> bool:
        detail = view(mission_id) or {}
        reservations = ((detail.get("dispatch") or {}).get("reservations")) or []
        return bool(reservations) and all(r["state"] not in ("reserved", "occupied", "uncertain") for r in reservations)

    sampler = Sampler(case / "resources.jsonl")
    try:
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case / 'input'}:/input",
                        images["ground"], "python3", "-m", "drone_agent.eval.x1_prepare", "--output", "/input",
                        "--scenario", scenario["id"], "--seed", str(seed), "--sha", sha])
        metadata = json.loads((case / "input/scenario.json").read_text())
        (case / "control/dock_a.json").write_text("{}")
        compose("stop", *SERVICES, check=False)
        gate = M3["quiet_gate"]()
        inject("quiet_gate", **gate)
        sampler.start()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl-x1")
        wait(lambda: home_set(0) and home_set(1), 240, "both PX4 instances' home position")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector-a", "collector-g")
        wait(lambda: (case / "sensor_a/latest.json").exists() and (case / "sensor_g/latest.json").exists(), 120,
             "both robots' first camera frames")
        # The same warm-up as the M2 cases: the estimators set home before anything connects.
        # 与 M2 用例相同的预热：估计器先设置 home 再有任何连接。
        time.sleep(5)
        (case / "service/ready.json").unlink(missing_ok=True)
        compose("up", "-d", "--no-build", "--pull", "never", "mission-service")
        wait(lambda: (case / "service/ready.json").exists(), 120, "mission service")
        observer(True)
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "dock-sim-a", "uplink-a", "uplink-g")
        wait(lambda: {r["robot_id"]: r["eligibility"]["verdict"] for r in listing().get("robots", [])}
             == {"uav_01": "eligible", "ugv_01": "eligible"}, 150, "both robots eligible")
        snapshot("before")
        block = metadata["block"]
        if not gz("create", "gz.msgs.EntityFactory", f"sdf: '{block['sdf']}'"):
            raise RuntimeError("the anomaly block could not be placed in the Gazebo world")
        inject("anomaly_placed", x=block["x"], y=block["y"], size_m=block["size_m"], colour=block["colour"],
               from_air=True, from_ground=True)
        started = api("workflows.start", project_id=PROJECT, workflow_id=metadata["workflow_id"],
                      request_id=f"{scenario['id']}-{seed}-{run_id}"[:120], inputs=metadata["inputs"])
        if not started.get("ok"):
            raise RuntimeError(f"workflow start refused: {started.get('issue')}")
        root_run = started["result"]["run"]["run_id"]
        inject("workflow_started", run_id=root_run, workflow_id=metadata["workflow_id"])
        active: dict[str, dict | None] = {"a": None, "g": None}
        mirroring: list[tuple[str, dict, float]] = []
        flown, approved = set(), set()
        state = {"removed": False, "cut_at": None, "restored": False, "cancelled": None, "carry_at": None,
                 "carried": False}
        deadline = time.monotonic() + CASE_LIMIT_S
        while time.monotonic() < deadline:
            listed = listing()
            tasks = listed.get("tasks", [])
            run_view = workflow(root_run)
            missions = {m["mission_id"] for m in run_view.get("missions", [])} | \
                {t["mission_id"] for t in tasks if t.get("mission_id")}
            for mission_id in sorted(missions - approved):
                if approve(mission_id):
                    approved.add(mission_id)
            review = next((n for n in run_view.get("nodes", []) if n["node_id"] == "review"), {})
            if review.get("state") == "waiting" and review.get("activity") == "human_review":
                api("workflows.review", actor=REVIEWER, project_id=PROJECT, run_id=root_run, node_id="review",
                    decision="confirmed", request_id=f"review-{root_run[-10:]}-{seed}", note="S1 harness review")
            handoff = next((t for t in tasks if t.get("source") == "handoff"), None)
            if "remove_block_after_offer" in metadata["actions"] and handoff and not state["removed"]:
                if not gz("remove", "gz.msgs.Entity", f'name: "{block["model"]}" type: MODEL'):
                    raise RuntimeError("the anomaly block could not be removed from the Gazebo world")
                state["removed"] = True
                inject("anomaly_removed", x=block["x"], y=block["y"], colour=block["colour"])
            ground = active["g"]
            if ground is not None:
                status = read_status(ground["flight"])
                observation = (status or {}).get("observation") or {}
                speed = math.hypot(*(observation.get("velocity_enu_mps") or [0.0, 0.0])[:2])
                position = ((observation.get("pose") or {}).get("position") or {})
                if "cut_link_while_driving" in metadata["actions"] and state["cut_at"] is None and speed > 0.3:
                    HELPERS["run"](["docker", "network", "disconnect", UPLINK_NETWORK, uplink_container()])
                    state["cut_at"] = time.monotonic()
                    inject("link_lost", robot_id="ugv_01")
                if "cancel_while_driving" in metadata["actions"] and state["cancelled"] is None and speed > 0.3 \
                        and float(position.get("y", 0.0)) > 2.0:
                    mission_id = ground["entry"]["mission_id"]
                    response = api("operate", mission_id=mission_id, action="cancel",
                                   request_id=f"cancel-{mission_id[2:]}-{seed}")
                    state["cancelled"] = mission_id
                    inject("cancel_sent", mission_id=mission_id, accepted=bool(response.get("ok")),
                           position=[position.get("x"), position.get("y")])
            if state["cut_at"] is not None and not state["restored"] and \
                    time.monotonic() - state["cut_at"] >= LINK_CUT_S:
                HELPERS["run"](["docker", "network", "connect", UPLINK_NETWORK, uplink_container()])
                state["restored"] = True
                inject("link_restored", robot_id="ugv_01")
            if state["carry_at"] is not None and not state["carried"] and time.monotonic() >= state["carry_at"]:
                # A person brings the robot that stopped on the way back to its parking spot; its hold is released
                # only once it reports itself parked there. / 由人把停在途中的车送回停车位；它在那里报告已停放后持有
                # 才会释放。
                if not gz("set_pose", "gz.msgs.Pose", metadata["parking_pose"]):
                    raise RuntimeError("the rover could not be carried to its parking spot in the Gazebo world")
                state["carried"] = True
                inject("carried_home", robot_id="ugv_01")
            # Each robot runs independently; neither waits for the other. / 两台机器人各自执行，互不等待。
            for key, info in ROBOTS.items():
                if active[key] is None:
                    history = case / "robots" / info["robot"] / "inbox/history"
                    pending = sorted((p for p in history.glob("m-*-v*.json") if p.name not in flown),
                                     key=lambda p: p.stat().st_mtime) if history.is_dir() else []
                    if pending:
                        if runs[key] and not info["ground"]:
                            raise RuntimeError("uav_01 got a second package; a battery swap is not part of X1 S1")
                        flown.add(pending[0].name)
                        if info["ground"]:
                            observer(False)
                        active[key] = start_run(case, compose, wait, key, pending[0])
                else:
                    finished = poll_run(case, compose, key, active[key])
                    if finished:
                        runs[key].append(finished)
                        mirroring.append((key, finished, time.monotonic() + 120))
                        active[key] = None
                        if info["ground"]:
                            observer(True)
                            if "carry_home_after_stop" in metadata["actions"] \
                                    and finished["mission_id"] == state["cancelled"]:
                                state["carry_at"] = time.monotonic() + 3.0
            for item in list(mirroring):
                key, entry, limit = item
                if mirrored(api, entry, case / "robots" / ROBOTS[key]["robot"] / "aircraft"):
                    mirroring.remove(item)
                elif time.monotonic() > limit:
                    raise RuntimeError(f"timed out waiting for the service mirror of {entry['mission_id']}"
                                       f"-v{entry['version']}")
            settled = run_view.get("run", {}).get("state") in RUN_FINAL \
                and all(t["state"] in TASK_FINAL for t in tasks) and not mirroring \
                and all(v is None for v in active.values()) \
                and (state["cut_at"] is None or state["restored"]) \
                and (state["carry_at"] is None or state["carried"])
            if settled:
                break
            time.sleep(2.0)
        else:
            raise RuntimeError("the case's run did not finish in time")
        wait(lambda: (api("resources.get", actor=JUDGE, record=False, project_id=PROJECT, resource_id="dock_s1a")
                      .get("result") or {}).get("pad") == "free", 150, "the aircraft's reconciled release")
        for entry in runs["g"]:
            wait(lambda entry=entry: released(entry["mission_id"]), 120, "the ground robot's reconciled release")
        snapshot("after")
        task_views = {}
        listed = listing()
        for task in listed.get("tasks", []):
            task_views[task["task_id"]] = api("tasks.get", actor=JUDGE, record=False, project_id=PROJECT,
                                              task_id=task["task_id"]).get("result")
        run_view = workflow(root_run)
        mission_ids = {m["mission_id"] for m in run_view.get("missions", [])} | \
            {a["mission_id"] for v in task_views.values() for a in (v or {}).get("assignments", []) if a["mission_id"]}
        views = {m: view(m) for m in sorted(mission_ids)}
        (case / "service-export/views.json").write_text(json.dumps(views, ensure_ascii=False, indent=2))
        (case / "service-export/task-views.json").write_text(json.dumps(task_views, ensure_ascii=False, indent=2,
                                                                        default=str))
        (case / "service-export/workflow-views.json").write_text(json.dumps({root_run: run_view}, ensure_ascii=False,
                                                                            indent=2, default=str))
        (case / "service-export/resources.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
        logs = compose("logs", "--no-color", "--no-log-prefix", "sitl-x1", timeout=60, check=False, quiet=True)
        (case / "sitl.log").write_bytes(logs.stdout)
        relay.close()
        compose("stop", "-t", "5", "uplink-a", "uplink-g", "observer-g", "dock-sim-a", "mission-service",
                "collector-a", "collector-g")
        # A longer stop lets PX4 close its flight logs. / 停止时间放长，让 PX4 关闭飞行日志。
        compose("stop", "-t", "20", "sitl-x1", timeout=120)
        sampler.stop_event.set()
        for name, rows in (("api.jsonl", transcript), ("injections.jsonl", injections)):
            (case / "world" / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        for key, info in ROBOTS.items():
            (case / "world" / f"{info['robot']}-flights.json").write_text(json.dumps(runs[key], indent=2))
        scene = {"origins": {"site_s1a": [0.0, 0.0], "site_s1g": [2.0, -4.0]},
                 "anomalies": [{"x": block["x"], "y": block["y"], "size_m": block["size_m"],
                                "colour": block["colour"], "from_air": True, "from_ground": True,
                                "removed": state["removed"]}]}
        (case / "world/scene.json").write_text(json.dumps(scene, indent=2))
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", "--entrypoint",
                        "/usr/bin/python3", images["ground"], "/workspace/sim/read_arming.py", "/run/ulog_g",
                        "/run/world/ugv_01-arming.json"], timeout=300)
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"], "python3",
                        "-m", "drone_agent.eval.x1_prepare", "--export", "/run/service/ledger.sqlite3",
                        "--output", "/run/service-export/scheduling.json"])
        judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=600, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_x1",
                           "/run", "--layer", "s1", "--root", "/workspace", "--replay", "--output",
                           "/output/replay.json", timeout=600, check=False)
        replay_path = case / "judge/replay.json"
        replay = json.loads(replay_path.read_text()) if replay_path.exists() else {}
        result["replay_agrees"] = all(result.get(k) == replay.get(k) for k in
                                      ("classification", "problems", "counts"))
        result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
        result["scenario"], result["seed"] = scenario["id"], seed
        result["flown"] = {ROBOTS[k]["robot"]: [f"{f['mission_id']}-v{f['version']}" for f in v]
                           for k, v in runs.items()}
        windows = [(f["started_at"], f["ended_at"]) for v in runs.values() for f in v if f.get("ended_at")]
        span = (min(w[0] for w in windows), max(w[1] for w in windows)) if windows else None
        factors = {ROBOTS[k]["robot"]: P3["real_time_factor"](case / f"truth_{k}/truth.jsonl", span) for k in ROBOTS}
        supervision = {f"{f['robot_id']}:{f['mission_id']}:v{f['version']}": f.get("supervision")
                       for v in runs.values() for f in v}
        verdict = P3["capacity_verdict"](factors, supervision)
        result["capacity"] = {"verdict": verdict, "resources": P3["resource_summary"](case / "resources.jsonl"),
                              "real_time_factor": factors, "supervision": supervision, "quiet_gate": gate,
                              "flight_span": span}
        result["passed"] = result["passed"] and verdict["sufficient"]
        return result
    except Exception as error:
        relay.close()
        compose("logs", "--no-color", "--tail", "200", check=False)
        compose("stop", "-t", "5", *SERVICES, check=False)
        return {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}
    finally:
        relay.close()
        sampler.stop_event.set()


def read_status(flight: Path) -> dict | None:
    try:
        return json.loads((flight / "status.json").read_text())
    except (OSError, ValueError):
        return None


def start_run(case: Path, compose, wait, key: str, package: Path) -> dict:
    """Start one handed-out package with a fresh guardian and executive of robot `key`. / 用新的 guardian 与 executive
    执行一个已交付的任务包。"""
    info = ROBOTS[key]
    mission_id, version = package.stem.rsplit("-v", 1)
    robot = case / "robots" / info["robot"]
    flight = robot / "aircraft" / mission_id / f"v{version}"
    flight.mkdir(parents=True)
    state = robot / "robot/authority.json"
    epoch = json.loads(state.read_text())["epoch_watermark"] + 1 if state.exists() else 1
    upper = key.upper()
    extra = {f"DRONE_X1_PACKAGE_{upper}": package.name, f"DRONE_X1_EPOCH_{upper}": str(epoch),
             f"DRONE_X1_FLIGHT_{upper}": str(flight)}
    entry = {"robot_id": info["robot"], "mission_id": mission_id, "version": int(version), "epoch": epoch,
             "started_at": now(), "ended_at": None}
    (robot / "ipc/guardian.sock").unlink(missing_ok=True)
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", f"guardian-{key}", extra=extra)
    wait(lambda: (robot / "ipc/guardian.sock").exists(), 90, f"guardian-{key}")
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", f"executive-{key}", extra=extra)
    limit = DRIVE_LIMIT_S if info["ground"] else FLIGHT_LIMIT_S
    return {"entry": entry, "flight": flight, "extra": extra, "deadline": time.monotonic() + limit,
            "cleanup": False, "done_at": None}


def poll_run(case: Path, compose, key: str, active: dict) -> dict | None:
    """One look at a running mission; the finished entry once its result is written and the robot is at rest.

    The aircraft rests landed and disarmed; the rover rests disarmed and still (its guardian disarms it after a stop).

    查看一次运行中的任务；结果已写出且机器人静止后返回结束记录。飞行器的静止是落地并上锁；地面车的静止是上锁且不动（停车后
    由其 guardian 上锁）。
    """
    info, flight, entry = ROBOTS[key], active["flight"], active["entry"]
    if time.monotonic() > active["deadline"]:
        raise RuntimeError(f"{entry['mission_id']}-v{entry['version']} exceeded its bounded duration")
    status = read_status(flight)
    if status is None:
        return None
    observation = status["observation"]
    finished = (flight / "result.json").exists()
    if info["ground"]:
        velocity = observation.get("velocity_enu_mps") or []
        speed = math.hypot(*velocity[:2]) if len(velocity) >= 2 else None
        at_rest = observation.get("armed") is False and speed is not None and speed <= STILL_MPS
    else:
        if finished and observation.get("in_air") is True:
            active.setdefault("airborne_after_result", time.monotonic())
        stuck = finished and time.monotonic() - active.get("airborne_after_result", time.monotonic()) > 30
        if (status["safety"] == "abort" or stuck) and not active["cleanup"]:
            # Simulation operator ends a surrendered flight, or one still airborne 30 s after its executive ended.
            # 仿真操作员收尾已移交控制权的飞行，或执行器结束 30 s 后仍在空中的飞行。
            compose("exec", "-T", "sitl-x1", "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander",
                    "--instance", str(info["instance"]), "land", check=False)
            active["cleanup"] = True
            (flight / "manual-cleanup.json").write_text(json.dumps({
                "reason": status["reason"] if status["safety"] == "abort" else "airborne_after_result"}))
        at_rest = observation.get("in_air") is False and observation.get("armed") is False
    if not (finished and at_rest):
        active["done_at"] = None
        return None
    if active["done_at"] is None:
        active["done_at"] = time.monotonic()
        entry["ended_at"] = now()
        return None
    if time.monotonic() - active["done_at"] < 2.2:
        return None
    compose("stop", "-t", "5", f"executive-{key}", f"guardian-{key}", extra=active["extra"])
    supervision = flight / "supervision.json"
    entry["supervision"] = json.loads(supervision.read_text()) if supervision.exists() else None
    return entry


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
