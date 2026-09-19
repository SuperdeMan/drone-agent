"""Run owned cloud SITL scenarios with separate aircraft and truth-judge processes.

在独立云端 SITL 中运行场景，机载进程与真值裁判分离。
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


def injection_due(scenario, state):
    step = scenario.get("inject_at")
    if step is None or state["active_step"] != step:
        return False
    if scenario.get("kind") in {"pause", "pause_resume", "mode_changed"}:
        if state["observation"]["flight_mode"] != "MISSION" or state.get("command_pending", False):
            return False
    if scenario.get("during_takeoff"):
        obs = state["observation"]
        return obs["in_air"] is True and obs.get("pose", {}).get("position", {}).get("z", 0) < 2
    return True


def run_m1(root: Path, deployment: Path, request: dict):
    manifest = json.loads((deployment / "manifest.json").read_text())
    sha = manifest["source_sha"]
    tag = sha + "-" + manifest["control_sha256"][:12]
    images = {role: f"drone-agent-m1-{role}:{tag}" for role in ("sim", "aircraft", "ground")}
    checks = f"drone-agent-checks:{tag}"
    source = deployment / "source"
    base = root / "artifacts" / deployment.name / ("m1-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    capacity = HELPERS["capacity"]()
    if capacity["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for M1")
    for role, image in images.items():
        if HELPERS["inspect_image"](image):
            continue
        with (base / f"build-{role}.log").open("wb") as build_log:
            result = subprocess.run(
                [
                    "docker",
                    "build",
                    "--pull=false",
                    "--build-arg",
                    f"CHECKS_IMAGE={checks}",
                    "--target",
                    role,
                    "-f",
                    str(source / "sim/m1.Dockerfile"),
                    "-t",
                    image,
                    str(source),
                ],
                stdout=build_log,
                stderr=subprocess.STDOUT,
                timeout=900,
            )
        if result.returncode:
            raise RuntimeError(f"M1 {role} build failed: {base}")
    suite = json.loads(
        HELPERS["run"](
            ["docker", "run", "--network", "none", images["ground"], "python3", "-m", "drone_agent.eval.prepare"]
        )
    )
    scenarios = suite["scenarios"]
    if request["scenario"] == "faults":
        scenarios = [case for case in scenarios if case["expected"] == "safe_abort" or case["id"] == "pause_resume"]
    elif request["scenario"] != "all":
        requested = request["scenario"].split(",")
        scenarios = [case for case in scenarios if case["id"] in requested]
        if len(scenarios) != len(set(requested)):
            raise ValueError("unknown M1 scenario in selection")
    if not scenarios or not request["seeds"]:
        raise ValueError("unknown or empty M1 scenario selection")
    results = []
    for scenario in scenarios:
        for seed in request["seeds"]:
            run = base / f"{scenario['id']}-{seed}"
            for folder in ("input", "aircraft", "truth", "sensor", "ipc", "judge", "ulog"):
                (run / folder).mkdir(parents=True)
            env = dict(
                os.environ,
                DRONE_M1_RUN=str(run),
                DRONE_SOURCE_SHA=sha,
                DRONE_M1_SPEED=str(request.get("speed_factor", 1)),
                DRONE_M1_SIM_IMAGE=images["sim"],
                DRONE_M1_GROUND_IMAGE=images["ground"],
                DRONE_M1_AIRCRAFT_IMAGE=images["aircraft"],
            )
            prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.m1.yaml")]

            def compose(*args, timeout=180, check=True):
                result = subprocess.run([*prefix, *args], env=env, capture_output=True, timeout=timeout)
                with (run / "compose.log").open("ab") as output:
                    output.write(result.stdout + result.stderr)
                if check and result.returncode:
                    raise RuntimeError(f"M1 compose failed ({result.returncode}); artifacts: {run}")
                return result

            def write_json(path, value):
                temporary = path.with_suffix(".pending.json")
                temporary.write_text(json.dumps(value))
                temporary.replace(path)

            def status():
                path = run / "aircraft/status.json"
                return json.loads(path.read_text()) if path.exists() else None

            try:
                HELPERS["run"](
                    [
                        "docker",
                        "run",
                        "--network",
                        "none",
                        "-v",
                        f"{run / 'input'}:/input",
                        images["ground"],
                        "python3",
                        "-m",
                        "drone_agent.eval.prepare",
                        "--output",
                        "/input",
                        "--scenario",
                        scenario["id"],
                        "--seed",
                        str(seed),
                        "--sha",
                        sha,
                        "--speed-factor",
                        str(request.get("speed_factor", 1)),
                    ]
                )
                if scenario.get("pre_dispatch"):
                    write_json(
                        run / "input/fault.json",
                        {"id": scenario["id"], "kind": scenario["kind"], "step_id": scenario["inject_at"]},
                    )
                compose("stop", "executive", "guardian", "collector", check=False)
                compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
                compose("up", "-d", "--no-build", "--pull", "never", "collector")
                deadline = time.monotonic() + 100
                while not (run / "sensor/latest.json").exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Gazebo RGB sensor produced no frame")
                    time.sleep(0.2)
                compose("up", "-d", "--no-build", "--pull", "never", "guardian")
                deadline = time.monotonic() + 90
                while not (run / "ipc/guardian.sock").exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("guardian did not become ready")
                    time.sleep(0.2)
                compose("up", "-d", "--no-build", "--pull", "never", "executive")
                injected = resumed = cleanup = False
                injection_time = 0
                injection_delay = random.Random(seed).uniform(0.1, 0.4)
                deadline = time.monotonic() + 360
                while time.monotonic() < deadline:
                    state = status()
                    if state is None:
                        time.sleep(0.1)
                        continue
                    obs = state["observation"]
                    should_inject = injection_due(scenario, state)
                    if should_inject and not injected:
                        time.sleep(injection_delay)
                        kind = scenario["kind"]
                        if kind in {"duplicate", "stale_epoch", "expired_intent", "offboard_rejected"}:
                            compose("exec", "-T", "executive", "python3", "-m", "drone_agent.eval.probe", kind)
                        elif kind == "heartbeat_stop":
                            compose(
                                "exec",
                                "-T",
                                "executive",
                                "pkill",
                                "-STOP",
                                "-f",
                                "^python3 -m drone_agent.runtime.launch executive$",
                            )
                        elif kind == "mode_changed":
                            compose(
                                "exec",
                                "-T",
                                "sitl",
                                "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander",
                                "land",
                            )
                        elif kind in {"cancel", "pause", "pause_resume"}:
                            write_json(
                                run / "aircraft/operator.json",
                                {
                                    "request_id": scenario["id"],
                                    "action": "pause" if kind.startswith("pause") else "cancel",
                                },
                            )
                        else:
                            write_json(run / "input/fault.json", {"id": scenario["id"], "kind": kind})
                        write_json(
                            run / "injection.json",
                            {
                                "kind": kind,
                                "delay_s": injection_delay,
                                "timestamp": HELPERS["datetime"].now(HELPERS["timezone"].utc).isoformat(),
                                "boundary": "runtime_input_or_ipc",
                            },
                        )
                        injected, injection_time = True, time.monotonic()
                    if (
                        injected
                        and scenario["kind"] == "pause_resume"
                        and not resumed
                        and time.monotonic() - injection_time > 3
                        and obs["flight_mode"] == "HOLD"
                    ):
                        write_json(run / "aircraft/operator.json", {"request_id": "resume", "action": "resume"})
                        resumed = True
                    if state["safety"] == "abort" and not cleanup:
                        # Manual simulation operator ends the surrendered flight. / 仿真人工操作员收尾已移交控制权的飞行。
                        compose(
                            "exec",
                            "-T",
                            "sitl",
                            "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander",
                            "land",
                            check=False,
                        )
                        cleanup = True
                        write_json(
                            run / "manual-cleanup.json", {"reason": state["reason"], "actor": "simulation_operator"}
                        )
                    if obs["in_air"] is False and obs["armed"] is False:
                        if (run / "aircraft/result.json").exists() or (injected and state["safety"] != "proceed"):
                            time.sleep(2.2)
                            break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("scenario exceeded its bounded flight duration")
                if scenario.get("kind") == "heartbeat_stop":
                    compose(
                        "exec",
                        "-T",
                        "executive",
                        "pkill",
                        "-CONT",
                        "-f",
                        "^python3 -m drone_agent.runtime.launch executive$",
                        check=False,
                    )
                    settle_deadline = time.monotonic() + 5
                    while not (run / "aircraft/result.json").exists() and time.monotonic() < settle_deadline:
                        time.sleep(0.1)
                compose("stop", "-t", "5", "executive", "guardian", "collector")
                compose("stop", "-t", "10", "sitl")
                compose("logs", "--no-color", "--tail", "160", timeout=30, check=False)
                compose(
                    "run",
                    "-T",
                    "--no-deps",
                    "judge",
                    "/usr/bin/python3",
                    "/workspace/sim/read_ulog.py",
                    "/run/ulog",
                    "/output/fc-events.json",
                    timeout=90,
                )
                judged = compose("run", "-T", "--no-deps", "judge", timeout=90, check=False)
                result_path = run / "judge/result.json"
                result = (
                    json.loads(result_path.read_text())
                    if result_path.exists()
                    else {"passed": False, "error": "judge_failed"}
                )
                result["judge_exit_code"] = judged.returncode
                replayed = compose(
                    "run",
                    "-T",
                    "--no-deps",
                    "judge",
                    "python3",
                    "-m",
                    "drone_agent.eval.replay",
                    "/run",
                    timeout=90,
                    check=False,
                )
                replay_path = run / "judge/replay.json"
                replay_result = json.loads(replay_path.read_text()) if replay_path.exists() else {}
                result["replay_agrees"] = all(
                    result.get(key) == replay_result.get(key)
                    for key in ("classification", "false_success_reports", "problems")
                )
                result["passed"] = result["passed"] and replayed.returncode == 0 and result["replay_agrees"]
            except Exception as error:
                result = {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}
                compose("logs", "--no-color", "--tail", "160", check=False)
                compose(
                    "exec",
                    "-T",
                    "executive",
                    "pkill",
                    "-CONT",
                    "-f",
                    "^python3 -m drone_agent.runtime.launch executive$",
                    check=False,
                )
                compose("stop", "-t", "5", "executive", "guardian", "collector", check=False)
            results.append(result)
            write_json(base / "progress.json", {"source_sha": sha, "results": results, "artifact_directory": str(base)})
            if not result["passed"]:
                break
        if results and not results[-1]["passed"]:
            break
    # Restore the idle, unarmed M0 service entry after the isolated experiment. / 隔离实验结束后恢复空闲、未解锁的 M0 服务入口。
    HELPERS["compose"](root, deployment, ["up", "-d", "--no-build", "--pull", "never", "sitl"])
    after = HELPERS["foreign_identity"]()
    result = {
        "status": "passed" if results and all(r["passed"] for r in results) and before == after else "failed",
        "source_sha": sha,
        "deployment_id": deployment.name,
        "artifact_directory": str(base),
        "results": results,
        "other_containers_before": before,
        "other_containers_after": after,
        "images": {role: HELPERS["inspect_image"](image)["Id"] for role, image in images.items()},
    }
    (base / "suite.json").write_text(json.dumps(result, indent=2))
    return result
