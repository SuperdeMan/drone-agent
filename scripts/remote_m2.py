"""Run owned cloud M2 end-to-end cases: mission service, uplink, signed packages, SITL flights, independent judge.

Each case: prepare inputs, start SITL and the truth collector, start the mission service and the uplink, submit the
natural-language request through the service API as the harness identity, approve the exact package hash, wait
for the uplink to hand the verified package to the inbox, fly each mission version with a fresh guardian and
executive (new epoch), wait for the service mirror to catch up, export the service view and run the judge online
and from the MCAP recordings. Nothing here reaches the guardian except starting and stopping its container.

在云端运行 M2 端到端用例：任务服务、uplink、签名任务包、SITL 飞行、独立裁判。每个用例：准备输入，启动 SITL 与
真值采集，启动任务服务与 uplink，以编排身份经服务 API 提交自然语言请求，按确切任务包哈希审批，等待 uplink 把
已验签任务包交给 inbox，每个任务版本用新的 guardian 与 executive（新代次）飞行，等待服务镜像追平，导出服务视图，
再在线与从 MCAP 录制各运行一次裁判。除了启停容器，这里没有任何东西触达 guardian。
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import time
from pathlib import Path

HELPERS = runpy.run_path(str(Path(__file__).with_name("remote_dev_stack.py")))
SCENE = "/workspace/configs/scenarios/m2_campus_v2.yaml"
FOLDERS = ("input", "aircraft", "truth", "sensor", "ipc", "judge", "ulog", "inbox", "mailbox", "uplink", "robot",
           "service", "api", "service-export", "idle-flight")


def build_images(source: Path, base: Path, tag: str, checks: str) -> dict:
    images = {"sim": f"drone-agent-m2-sim:{tag}", "aircraft": f"drone-agent-m1-aircraft:{tag}",
              "ground": f"drone-agent-m1-ground:{tag}"}
    targets = {"sim": "sim2", "aircraft": "aircraft", "ground": "ground"}
    for role, image in images.items():
        if HELPERS["inspect_image"](image):
            continue
        with (base / f"build-{role}.log").open("wb") as log:
            result = subprocess.run(["docker", "build", "--pull=false", "--build-arg", f"CHECKS_IMAGE={checks}",
                                     "--target", targets[role], "-f", str(source / "sim/m1.Dockerfile"), "-t", image,
                                     str(source)], stdout=log, stderr=subprocess.STDOUT, timeout=1200)
        if result.returncode:
            raise RuntimeError(f"M2 {role} build failed: {base}")
    return images


def provision(root: Path, image: str, name: str = "m2") -> dict:
    """Keys and certificates stay in this project's secrets directory; only public facts return.

    `name` separates trust roots: `m2` for the end-to-end cases, `desk` for the resident desk (D035).

    密钥与证书留在本项目的 secrets 目录；只返回公开事实。`name` 分隔信任根：端到端用例为 `m2`，常驻任务台为
    `desk`（D035）。
    """
    secrets = root / "secrets" / name
    secrets.mkdir(parents=True, exist_ok=True)
    os.chmod(root / "secrets", 0o700)
    output = HELPERS["run"](["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                             "-v", f"{secrets}:/secrets", image, "python3", "-m", "drone_agent.fleet.provision",
                             "--output", "/secrets"])
    return json.loads(output.strip().splitlines()[-1])


def rows(path: Path) -> int:
    return len(path.read_bytes().splitlines()) if path.exists() else 0


def run_m2(root: Path, deployment: Path, request: dict) -> dict:
    manifest = json.loads((deployment / "manifest.json").read_text())
    sha = manifest["source_sha"]
    tag = sha + "-" + manifest["control_sha256"][:12]
    checks = f"drone-agent-checks:{tag}"
    source = deployment / "source"
    base = root / "artifacts" / deployment.name / ("m2-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for M2")
    images = build_images(source, base, tag, checks)
    keys = provision(root, images["ground"])
    model = root / "secrets" / "m2-model"
    model.mkdir(parents=True, exist_ok=True)
    os.chmod(model, 0o700)
    planner = request.get("planner", "scripted")
    if planner not in ("scripted", "live"):
        raise ValueError("planner must be scripted or live")
    if planner == "live" and not (model / "minimax.key").is_file():
        raise ValueError("live planning needs the model key in the project secrets; none is configured")
    suite = json.loads(HELPERS["run"](["docker", "run", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.m2_prepare"]))
    scenarios = suite["scenarios"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        scenarios = [case for case in scenarios if case["id"] in wanted]
        if len(scenarios) != len(set(wanted)):
            raise ValueError("unknown M2 scenario in selection")
    if not scenarios or not request["seeds"] or not set(request["seeds"]) <= set(suite["seeds"]):
        raise ValueError("empty or unsupported M2 selection")
    results = []
    for scenario in scenarios:
        for seed in request["seeds"]:
            result = run_case(root, source, base, images, keys, sha, request["run_id"], planner, scenario, seed)
            results.append(result)
            (base / "progress.json").write_text(json.dumps({"source_sha": sha, "results": results,
                                                           "artifact_directory": str(base)}))
    # Restore the idle, unarmed M0 service entry. / 恢复空闲、未解锁的 M0 服务入口。
    HELPERS["compose"](root, deployment, ["up", "-d", "--no-build", "--pull", "never", "sitl"])
    after = HELPERS["foreign_identity"]()
    summary = {
        "status": "passed" if results and all(r["passed"] for r in results) and before == after else "failed",
        "source_sha": sha, "deployment_id": deployment.name, "artifact_directory": str(base), "planner": planner,
        "signer_key_id": keys["signer_key_id"], "certificate_fingerprints": keys["fingerprints"],
        "results": results, "other_containers_before": before, "other_containers_after": after,
        "images": {role: HELPERS["inspect_image"](image)["Id"] for role, image in images.items()},
    }
    (base / "suite.json").write_text(json.dumps(summary, indent=2))
    return summary


def run_case(root, source, base, images, keys, sha, run_id, planner, scenario, seed) -> dict:
    case = base / f"{scenario['id']}-{seed}"
    for folder in FOLDERS:
        (case / folder).mkdir(parents=True)
    secrets = root / "secrets" / "m2"
    env = dict(os.environ, DRONE_SOURCE_SHA=sha, DRONE_M2_RUN=str(case), DRONE_M2_SECRETS=str(secrets),
               DRONE_M2_MODEL=str(root / "secrets" / "m2-model"), DRONE_M2_PLANNER=planner,
               DRONE_M2_SIM_IMAGE=images["sim"], DRONE_M2_AIRCRAFT_IMAGE=images["aircraft"],
               DRONE_M2_GROUND_IMAGE=images["ground"], DRONE_M2_PACKAGE="none.json", DRONE_M2_VERSION="0",
               DRONE_M2_EPOCH="0", DRONE_M2_FLIGHT=str(case / "idle-flight"))
    prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.m2.yaml")]
    actor = f"harness:m2-{run_id}-{scenario['id']}-{seed}"

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            # API answers are exported once at the end, not logged on every poll. / API 答复只在结束时导出一次。
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"M2 compose failed ({result.returncode}); artifacts: {case}")
        return result

    def api(method: str, **params) -> dict:
        result = compose("exec", "-T", "mission-service", "python3", "-m", "drone_agent.fleet.api", "--socket",
                         "/run/mission/api.sock", "--actor", actor, method, json.dumps(params, ensure_ascii=False),
                         timeout=180, quiet=True)
        response = json.loads(result.stdout.decode().strip().splitlines()[-1])
        if not response.get("ok"):
            raise RuntimeError(f"service API {method} refused: {response.get('issue', {}).get('code')}")
        return response["result"]

    def wait(predicate, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.25)
        raise RuntimeError(f"timed out waiting for {what}")

    def service_up():
        (case / "service/ready.json").unlink(missing_ok=True)
        compose("up", "-d", "--no-build", "--pull", "never", "mission-service")
        wait(lambda: (case / "service/ready.json").exists(), 90, "mission service")

    try:
        HELPERS["run"](["docker", "run", "--network", "none", "-v", f"{case / 'input'}:/input", images["ground"],
                        "python3", "-m", "drone_agent.eval.m2_prepare", "--output", "/input", "--scenario",
                        scenario["id"], "--seed", str(seed), "--sha", sha])
        metadata = json.loads((case / "input/scenario.json").read_text())
        compose("stop", "executive", "guardian", "uplink", "mission-service", "model-proxy", "collector", check=False)
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector")
        wait(lambda: (case / "sensor/latest.json").exists(), 100, "Gazebo RGB frame")
        service_up()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "uplink")
        scope = metadata["scope"]
        view = api("submit", text=metadata["text"], volume_id=scope["volume_id"],
                   asset_ids=scope.get("asset_ids", []), idempotency_key=f"{scenario['id']}-{seed}-{run_id}")
        mission_id = view["mission"]["mission_id"]
        if metadata["expected"].get("flight") is not False:
            api("approve", mission_id=mission_id, version=1, package_hash=view["versions"][0]["package_hash"])
            fly_versions(case, compose, api, wait, service_up, mission_id, metadata, env)
        else:
            time.sleep(3)  # give the uplink cycles to prove nothing is delivered / 给 uplink 几个周期证明无投递
        view = api("view", mission_id=mission_id)
        (case / "service-export/view.json").write_text(json.dumps(view, ensure_ascii=False, indent=2))
        (case / "service-export/robots.json").write_text(json.dumps(api("robots"), indent=2))
        save_sitl_log(case, compose, "sitl.log")
        compose("stop", "-t", "5", "uplink", "mission-service", "model-proxy", "collector")
        compose("stop", "-t", "10", "sitl")
        judged = compose("run", "-T", "--no-deps", "judge", timeout=120, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_m2", "/run",
                           "--replay", "--output", "/output/replay.json", timeout=120, check=False)
        replay_path = case / "judge/replay.json"
        replay_result = json.loads(replay_path.read_text()) if replay_path.exists() else {}
        result["replay_agrees"] = all(result.get(k) == replay_result.get(k)
                                      for k in ("classification", "false_success_reports", "problems"))
        result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
        result["scenario"], result["seed"] = scenario["id"], seed
        return result
    except Exception as error:
        compose("logs", "--no-color", "--tail", "200", check=False)
        compose("stop", "-t", "5", "executive", "guardian", "uplink", "mission-service", "model-proxy", "collector", check=False)
        return {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}


def save_sitl_log(case, compose, name: str) -> None:
    """Keep the PX4 console (arming denials, failsafes) as case evidence. / 把 PX4 控制台留作用例证据。"""
    result = compose("logs", "--no-color", "--no-log-prefix", "sitl", timeout=60, check=False, quiet=True)
    (case / name).write_bytes(result.stdout)


def battery_swap(case, compose, wait, version: int) -> None:
    """Ground crew between versions: land, disarm, swap the battery, which power-cycles the flight controller.

    The authority state (epoch watermark, accepted versions) lives on the companion computer and survives;
    a new version must still start grounded with a new epoch (D032).

    版本之间的地勤操作：落地、上锁、换电池，飞控随之断电重启。控制权状态（代次水位、已接受版本）保存在
    伴飞计算机上并保留；新版本仍须在地面以新代次开始（D032）。
    """
    save_sitl_log(case, compose, f"sitl-before-v{version}.log")
    compose("stop", "-t", "10", "collector", "sitl")
    (case / "sensor/latest.json").unlink(missing_ok=True)
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector")
    wait(lambda: (case / "sensor/latest.json").exists(), 100, "Gazebo RGB frame after the battery swap")
    # Same warm-up as the first flight gets: the rebooted estimator sets home before anything connects.
    # Thresholds are untouched; a telemetry gap after this still aborts the flight.
    # 与首飞相同的预热：重启后的估计器先设置 home 再有任何连接。阈值不变；此后的遥测间隙仍会中止飞行。
    wait(lambda: b"home set" in compose("logs", "--no-color", "--no-log-prefix", "sitl", timeout=60, check=False,
                                        quiet=True).stdout, 90, "PX4 home position after the battery swap")
    time.sleep(5)
    (case / f"ground-crew-v{version}.json").write_text(json.dumps({
        "action": "battery_swap_power_cycle", "before_version": version,
        "timestamp": HELPERS["datetime"].now(HELPERS["timezone"].utc).isoformat(),
        "reason": "new mission version after landing; the flight controller reboots with a full battery",
        "warmup": "PX4 home set plus 5 s, as before the first flight"}))


def fly_versions(case, compose, api, wait, service_up, mission_id, metadata, env) -> None:
    version = 1
    outage = metadata.get("outage") or {}
    while version <= 3:
        package = case / "inbox/history" / f"{mission_id}-v{version}.json"
        try:
            wait(package.exists, 90, f"inbox package v{version}")
        except RuntimeError:
            if version == 1:
                raise
            return
        if outage and version == 1:
            wait(lambda: api("view", mission_id=mission_id)["versions"][0]["status"] == "delivered", 60,
                 "delivery acknowledgement")
            compose("stop", "-t", "5", "mission-service")
        if version > 1:
            battery_swap(case, compose, wait, version)
        flight = case / "aircraft" / mission_id / f"v{version}"
        flight.mkdir(parents=True)
        state = case / "robot/authority.json"
        epoch = json.loads(state.read_text())["epoch_watermark"] + 1 if state.exists() else 1
        extra = {"DRONE_M2_PACKAGE": package.name, "DRONE_M2_VERSION": str(version), "DRONE_M2_EPOCH": str(epoch),
                 "DRONE_M2_FLIGHT": str(flight)}
        (case / "ipc/guardian.sock").unlink(missing_ok=True)
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "guardian", extra=extra)
        wait(lambda: (case / "ipc/guardian.sock").exists(), 90, "guardian")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "executive", extra=extra)
        deadline, cleanup = time.monotonic() + 420, False
        while time.monotonic() < deadline:
            path = flight / "status.json"
            status = json.loads(path.read_text()) if path.exists() else None
            if status is None:
                time.sleep(0.1)
                continue
            obs = status["observation"]
            if status["safety"] == "abort" and not cleanup:
                # Simulation operator ends a surrendered flight. / 仿真操作员收尾已移交控制权的飞行。
                compose("exec", "-T", "sitl", "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander", "land",
                        check=False)
                cleanup = True
                (flight / "manual-cleanup.json").write_text(json.dumps({"reason": status["reason"]}))
            if obs.get("in_air") is False and obs.get("armed") is False and (flight / "result.json").exists():
                time.sleep(2.2)
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"v{version} exceeded its bounded flight duration")
        compose("stop", "-t", "5", "executive", "guardian", extra=extra)
        if outage and version == 1:
            service_up()
        onboard = {name: rows(flight / f"{name}.jsonl") for name in ("executive", "guardian")}

        def mirrored():
            view = api("view", mission_id=mission_id)
            record = next((v for v in view["versions"] if v["version"] == version), {})
            journals = record.get("journals", {})
            synced = all(journals.get(name) == {"rows": count, "chain": "ok"} for name, count in onboard.items())
            return view if synced and record.get("status") == "finished" else None

        view = wait(mirrored, 90, f"service mirror of v{version}")
        if view["mission"]["status"] in ("completed", "incomplete", "declined", "delivery_rejected", "rejected"):
            return
        version = max(view["mission"]["current_version"], version + 1)
