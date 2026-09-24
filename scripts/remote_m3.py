"""Run M3-SITL scenarios in the owned cloud workspace (WP-M3-20, D038).

Builds the M3 images of the deployed revision (the ROS 2 base is cached across revisions), then for each scenario and
seed starts a fresh simulator, the truth collector, the camera relay, the XRCE agent, the egress and autonomy nodes
(unless the scenario runs without them), the guardian and the executive; injects the scenario's fault at its declared
boundary when the flight reaches the stated condition; lets the simulation operator land a surrendered flight; stops
everything; extracts the ULog; and runs the independent judge online and from the MCAP replay. The first failing case
stops the batch unless the request keeps going (diagnosis and measurement batches). The idle M0 simulator is restored afterwards and foreign containers are compared before and after.

在本项目云端工作区运行 M3-SITL 场景（WP-M3-20，D038）。

为已部署版本构建 M3 镜像（ROS 2 基础镜像跨版本缓存）；每个场景 × 种子都启动全新的仿真器、真值采集、相机转接、
XRCE agent、出口与自主层节点（场景不需要时不启动）、guardian 与 executive；飞行到达规定条件时在声明的边界注入
故障；由仿真操作员降落已交出控制权的飞行；全部停止后提取 ULog，并在线与从 MCAP 回放各运行一次独立裁判。首个失败
用例即停止本批，除非请求要求继续（诊断与测量批次）。结束后恢复 M0 空闲仿真器，并比较前后的其他容器。
"""

from __future__ import annotations

import json
import os
import random
import runpy
import subprocess
import time
from pathlib import Path

HELPERS = runpy.run_path(str(Path(__file__).with_name("remote_dev_stack.py")))
SERVICES = ("executive", "guardian", "autonomy", "egress", "edge", "xrce-agent", "relay", "collector")
FOLDERS = ("input", "aircraft", "truth", "sensor", "ipc", "ipc-egress", "ipc-autonomy", "ipc-belief", "egress",
           "autonomy", "edge", "judge", "ulog")
AUTONOMY_FAULTS = {"map_freeze", "planner_freeze", "planner_stall", "planner_ignore_obstacles"}


def cgroup_dir(container_id: str) -> Path | None:
    for candidate in (Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope"),
                      Path(f"/sys/fs/cgroup/docker/{container_id}")):
        if (candidate / "cpu.stat").is_file():
            return candidate
    return None


def sample_resources(path: Path, groups: dict) -> None:
    """One cgroup sample per container plus host load and CPU pressure (D040 receipts); file reads only.

    每个容器一条 cgroup 采样，另加主机负载与 CPU 压力（D040 回执）；只读文件。
    """
    now, rows = time.time(), []
    for service, directory in groups.items():
        try:
            stat = dict(line.split() for line in (directory / "cpu.stat").read_text().splitlines())
            memory = int((directory / "memory.current").read_text())
        except (OSError, ValueError):
            continue
        rows.append({"wall": now, "service": service, "usage_usec": int(stat["usage_usec"]),
                     "nr_periods": int(stat.get("nr_periods", 0)), "nr_throttled": int(stat.get("nr_throttled", 0)),
                     "throttled_usec": int(stat.get("throttled_usec", 0)), "memory_bytes": memory})
    try:
        pressure = Path("/proc/pressure/cpu").read_text().splitlines()[0].split()[1]
        rows.append({"wall": now, "service": "_host", "load1": float(Path("/proc/loadavg").read_text().split()[0]),
                     "cpu_some_avg10": float(pressure.split("=")[1])})
    except (OSError, ValueError, IndexError):
        pass
    with path.open("a") as output:
        output.writelines(json.dumps(row) + "\n" for row in rows)


def injection_due(scenario, state) -> bool:
    if state.get("active_step") != scenario.get("inject_at"):
        return False
    when = scenario.get("when")
    if when == "external_active":
        external = state.get("external") or {}
        return bool(external.get("active")) and not state.get("command_pending", False)
    if when == "capture_phase":
        return state.get("phase") == "inspect" and bool((state.get("external") or {}).get("active"))
    return False


def build(source: Path, base: Path, images: dict, checks: str) -> None:
    plan = [
        ("ground", "m1.Dockerfile", "ground", {"CHECKS_IMAGE": checks}),
        ("sim2", "m1.Dockerfile", "sim2", {"CHECKS_IMAGE": checks}),
        ("aircraft3", "m3.Dockerfile", "aircraft3", {"CHECKS_IMAGE": checks}),
        ("sim3", "m3.Dockerfile", "sim3", {"CHECKS_IMAGE": checks, "SIM2_IMAGE": images["sim2"]}),
    ]
    for role, dockerfile, target, arguments in plan:
        if HELPERS["inspect_image"](images[role]):
            continue
        command = ["docker", "build", "--pull=false", "--target", target, "-f", str(source / "sim" / dockerfile),
                   "-t", images[role]]
        for key, value in arguments.items():
            command += ["--build-arg", f"{key}={value}"]
        with (base / f"build-{role}.log").open("wb") as log:
            result = subprocess.run([*command, str(source)], stdout=log, stderr=subprocess.STDOUT, timeout=5400)
        if result.returncode:
            raise RuntimeError(f"M3 {role} build failed: {base}")


def run_m3(root: Path, deployment: Path, request: dict):
    manifest = json.loads((deployment / "manifest.json").read_text())
    sha = manifest["source_sha"]
    tag = sha + "-" + manifest["control_sha256"][:12]
    images = {"ground": f"drone-agent-m1-ground:{tag}", "sim2": f"drone-agent-m2-sim:{tag}",
              "aircraft3": f"drone-agent-m3-aircraft:{tag}", "sim3": f"drone-agent-m3-sim:{tag}"}
    checks = f"drone-agent-checks:{tag}"
    source = deployment / "source"
    base = root / "artifacts" / deployment.name / ("m3-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for M3")
    build(source, base, images, checks)
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.m3_prepare"]))
    scenarios = suite["scenarios"]
    selection = request["scenario"]
    if selection.startswith("class:"):
        scenarios = [case for case in scenarios if case["class"] == selection.removeprefix("class:")]
    elif selection != "all":
        wanted = selection.split(",")
        scenarios = [case for case in scenarios if case["id"] in wanted]
        if len(scenarios) != len(set(wanted)):
            raise ValueError("unknown M3 scenario in selection")
    if not scenarios or not request["seeds"]:
        raise ValueError("unknown or empty M3 scenario selection")
    results = []
    for scenario in scenarios:
        for seed in request["seeds"]:
            run = base / f"{scenario['id']}-{seed}"
            for folder in FOLDERS:
                (run / folder).mkdir(parents=True)
            env = dict(os.environ, DRONE_M3_RUN=str(run), DRONE_SOURCE_SHA=sha, DRONE_M3_SPEED="1",
                       DRONE_M3_SIM_IMAGE=images["sim3"], DRONE_M3_GROUND_IMAGE=images["ground"],
                       DRONE_M3_AIRCRAFT_IMAGE=images["aircraft3"],
                       DRONE_EDGE_PERIOD_S=str(float(scenario.get("edge_period_s", 1.0))))
            prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.m3.yaml")]

            def compose(*args, timeout=180, check=True):
                result = subprocess.run([*prefix, *args], env=env, capture_output=True, timeout=timeout)
                with (run / "compose.log").open("ab") as output:
                    output.write(result.stdout + result.stderr)
                if check and result.returncode:
                    raise RuntimeError(f"M3 compose failed ({result.returncode}); artifacts: {run}")
                return result

            def write_json(path, value):
                temporary = path.with_suffix(".pending.json")
                temporary.write_text(json.dumps(value))
                temporary.replace(path)

            def status():
                path = run / "aircraft/status.json"
                try:
                    return json.loads(path.read_text()) if path.exists() else None
                except ValueError:
                    return None

            frozen_at = None
            try:
                HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{run / 'input'}:/input",
                                images["ground"], "python3", "-m", "drone_agent.eval.m3_prepare", "--output", "/input",
                                "--scenario", scenario["id"], "--seed", str(seed), "--sha", sha])
                compose("stop", *SERVICES, check=False)
                compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
                compose("up", "-d", "--no-build", "--pull", "never", "collector")
                deadline = time.monotonic() + 120
                while not (run / "sensor/latest.json").exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Gazebo RGB sensor produced no frame")
                    time.sleep(0.2)
                compose("up", "-d", "--no-build", "--pull", "never", "relay", "xrce-agent")
                if scenario.get("nodes", True):
                    compose("up", "-d", "--no-build", "--pull", "never", "egress", "autonomy", "edge")
                compose("up", "-d", "--no-build", "--pull", "never", "guardian")
                deadline = time.monotonic() + 150
                while not (run / "ipc/guardian.sock").exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("guardian did not become ready")
                    time.sleep(0.2)
                compose("up", "-d", "--no-build", "--pull", "never", "executive")
                groups = {}
                for service in ("sitl", *SERVICES):
                    found = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", f"drone-agent-cloud-{service}-1"],
                                           capture_output=True, text=True, timeout=30)
                    directory = cgroup_dir(found.stdout.strip()) if found.returncode == 0 else None
                    if directory is not None:
                        groups[service] = directory
                next_sample = 0.0
                injected = cleanup = False
                injection_delay = random.Random(seed).uniform(0.5, 2.0)
                due_since = None
                deadline = time.monotonic() + 480
                while time.monotonic() < deadline:
                    if time.monotonic() >= next_sample:
                        sample_resources(run / "resources.jsonl", groups)
                        next_sample = time.monotonic() + 1.0
                    state = status()
                    if state is None:
                        time.sleep(0.1)
                        continue
                    obs = state["observation"]
                    kind = scenario.get("kind")
                    if kind and not injected and injection_due(scenario, state):
                        due_since = due_since or time.monotonic()
                        if time.monotonic() - due_since >= injection_delay:
                            record = {"id": f"{scenario['id']}-{seed}", "kind": kind}
                            if kind in AUTONOMY_FAULTS:
                                write_json(run / "input/autonomy.json", record)
                            elif kind == "battery":
                                write_json(run / "input/fault.json", {**record, "battery": scenario["battery"]})
                            elif kind == "guardian_freeze":
                                compose("exec", "-T", "guardian", "pkill", "-STOP", "-f",
                                        "^python3 -m drone_agent.runtime.launch guardian")
                                frozen_at = time.monotonic()
                            elif kind == "xrce_agent_stop":
                                compose("kill", "xrce-agent")
                            elif kind == "egress_stop":
                                compose("kill", "egress")
                            elif kind == "edge_kill":
                                compose("kill", "edge")
                            elif kind == "cpu_hog":
                                for _ in range(3):
                                    compose("exec", "-T", "-d", "autonomy", "/usr/bin/python3", "-c", "while True: pass")
                            elif kind == "gnss_off":
                                compose("exec", "-T", "sitl",
                                        "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-failure", "gps", "off")
                            else:
                                raise ValueError(f"unknown M3 injection {kind}")
                            write_json(run / "injection.json", {
                                "kind": kind, "delay_s": injection_delay, "boundary": scenario.get("boundary"),
                                "timestamp": HELPERS["datetime"].now(HELPERS["timezone"].utc).isoformat()})
                            injected = True
                    if frozen_at is not None and time.monotonic() - frozen_at >= 3.0:
                        compose("exec", "-T", "guardian", "pkill", "-CONT", "-f",
                                "^python3 -m drone_agent.runtime.launch guardian", check=False)
                        frozen_at = None
                    if state["safety"] == "abort" and not cleanup:
                        # The simulation operator ends a surrendered flight. / 仿真操作员收尾已交出控制权的飞行。
                        compose("exec", "-T", "sitl", "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander",
                                "land", check=False)
                        cleanup = True
                        write_json(run / "manual-cleanup.json", {"reason": state["reason"], "actor": "simulation_operator"})
                    if obs["in_air"] is False and obs["armed"] is False:
                        if (run / "aircraft/result.json").exists() or (injected and state["safety"] != "proceed"):
                            time.sleep(2.2)
                            break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("scenario exceeded its bounded flight duration")
                if frozen_at is not None:
                    compose("exec", "-T", "guardian", "pkill", "-CONT", "-f",
                            "^python3 -m drone_agent.runtime.launch guardian", check=False)
                settle = time.monotonic() + 8
                while not (run / "aircraft/result.json").exists() and time.monotonic() < settle:
                    time.sleep(0.2)
                compose("stop", "-t", "5", *SERVICES)
                compose("stop", "-t", "10", "sitl")
                compose("logs", "--no-color", "--tail", "200", timeout=30, check=False)
                compose("run", "-T", "--rm", "--no-deps", "judge", "/usr/bin/python3", "/workspace/sim/read_ulog.py",
                        "/run/ulog", "/output/fc-events.json", timeout=120)
                judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=180, check=False)
                result_path = run / "judge/result.json"
                result = json.loads(result_path.read_text()) if result_path.exists() else {
                    "passed": False, "error": "judge_failed"}
                result["judge_exit_code"] = judged.returncode
                replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m",
                                   "drone_agent.eval.judge_m3", "/run", "--replay", "--output", "/output/replay.json",
                                   timeout=180, check=False)
                replay_path = run / "judge/replay.json"
                replay_result = json.loads(replay_path.read_text()) if replay_path.exists() else {}
                result["replay_agrees"] = all(result.get(key) == replay_result.get(key)
                                              for key in ("classification", "false_success_reports", "problems"))
                result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
            except Exception as error:
                result = {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}
                compose("logs", "--no-color", "--tail", "200", check=False)
                compose("exec", "-T", "guardian", "pkill", "-CONT", "-f",
                        "^python3 -m drone_agent.runtime.launch guardian", check=False)
                compose("stop", "-t", "5", *SERVICES, check=False)
                compose("stop", "-t", "10", "sitl", check=False)
            results.append(result)
            write_json(base / "progress.json", {"source_sha": sha, "results": results, "artifact_directory": str(base)})
            if not result["passed"] and not request.get("keep_going"):
                break
        if results and not results[-1]["passed"] and not request.get("keep_going"):
            break
    # Restore the idle, unarmed M0 simulator. / 恢复空闲、未解锁的 M0 仿真器。
    HELPERS["compose"](root, deployment, ["up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl"])
    after = HELPERS["foreign_identity"]()
    result = {
        "status": "passed" if results and all(r["passed"] for r in results) and before == after else "failed",
        "milestone": "M3",
        "source_sha": sha,
        "deployment_id": deployment.name,
        "artifact_directory": str(base),
        "results": results,
        "other_containers_before": before,
        "other_containers_after": after,
        "images": {role: (HELPERS["inspect_image"](image) or {}).get("Id") for role, image in images.items()},
    }
    (base / "suite.json").write_text(json.dumps(result, indent=2))
    return result
