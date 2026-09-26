"""Run owned cloud P1 S1 cases: the formal mission service with its catalog, the logical dock backend and PX4 SITL.

Each case: prepare inputs, start SITL and the truth collector, the mission service (catalog p1_s1_v1, ledger schema v2
in the case directory), the dock backend and the uplink; wait for an active dock session; submit into the project for
`uav_01` and approve the exact package hash as harness identities; then either let the claim gate hand the package
out and fly it with a fresh guardian and executive (optionally restarting the service and the dock backend while
airborne), or put the dock into maintenance after approval and prove nothing is handed out before a cancel voids the
delivery. Afterwards: wait for the reconciled release, export the service view, resources, the operations tables,
the API transcript and the flight window, and run the P1 S1 judge online and from the MCAP recordings. Faults reach
only the dock's harness-only control file; nothing here reaches the guardian except starting and stopping it.

在云端运行 P1 S1 用例：带目录的正式任务服务、逻辑机场后端与 PX4 SITL。每个用例：准备输入，启动 SITL 与真值采集、
任务服务（目录 p1_s1_v1，账本在用例目录内为 schema v2）、机场后端与 uplink；等待机场会话有效；以编排身份为 `uav_01`
向项目提交并按确切任务包哈希审批；然后要么由领取闸门交付任务包，用新的 guardian 与 executive 飞行（可在空中重启
服务与机场后端），要么在审批后使机场进入维护，证明取消使投递作废之前没有任何交付。之后等待对账释放，导出服务视图、
资源、运营表、API 记录与飞行窗口，并在线与按 MCAP 录制各运行一次 P1 S1 裁判。故障只写入机场的编排专用控制文件；
除启停容器外，这里没有任何东西触达 guardian。
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
M2 = runpy.run_path(str(HERE / "remote_m2.py"))
FOLDERS = (*M2["FOLDERS"], "dock", "control", "world")
PROJECT, ROBOT, DOCK = "campus_s1", "uav_01", "dock_s1"
OPERATOR, JUDGE = "harness:p1-operator", "harness:p1-judge"
TERMINAL = ("completed", "incomplete", "declined", "delivery_rejected", "rejected", "cancelled", "dispatch_expired")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_p1(root: Path, deployment: Path, request: dict) -> dict:
    manifest = json.loads((deployment / "manifest.json").read_text())
    sha = manifest["source_sha"]
    tag = sha + "-" + manifest["control_sha256"][:12]
    checks = f"drone-agent-checks:{tag}"
    source = deployment / "source"
    if not (source / "sim/compose.p1.yaml").is_file():
        raise ValueError("deploy a version with the P1 S1 runner first")
    base = root / "artifacts" / deployment.name / ("p1-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for P1 S1")
    images = M2["build_images"](source, base, tag, checks)
    keys = M2["provision"](root, images["ground"])
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.p1_prepare"]))
    cases = suite["s1"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(set(wanted)):
            raise ValueError("unknown P1 S1 case in selection")
    seeds = set(request.get("seeds") or [])
    selection = [(case, seed) for case in cases for seed in case["seeds"] if not seeds or seed in seeds]
    if not selection:
        raise ValueError("empty P1 S1 selection")
    results = []
    for case, seed in selection:
        result = run_case(root, source, base, images, sha, request["run_id"], case, seed)
        results.append(result)
        (base / "progress.json").write_text(json.dumps({"source_sha": sha, "results": results}))
        if not result.get("passed") and not request.get("keep_going"):
            break
    HELPERS["compose"](root, deployment, ["up", "-d", "--no-build", "--pull", "never", "sitl"])
    after = HELPERS["foreign_identity"]()
    summary = {
        "status": "passed" if results and len(results) == len(selection) and all(r["passed"] for r in results)
        and before == after else "failed",
        "layer": "S1", "source_sha": sha, "deployment_id": deployment.name, "artifact_directory": str(base),
        "planner": "scripted", "signer_key_id": keys["signer_key_id"], "certificate_fingerprints": keys["fingerprints"],
        "selection": [f"{case['id']}-{seed}" for case, seed in selection], "results": results,
        "other_containers_before": before, "other_containers_after": after,
        "images": {role: HELPERS["inspect_image"](image)["Id"] for role, image in images.items()},
    }
    (base / "suite.json").write_text(json.dumps(summary, indent=2))
    return summary


def run_case(root, source, base, images, sha, run_id, scenario, seed) -> dict:
    case = base / f"{scenario['id']}-{seed}"
    for folder in FOLDERS:
        (case / folder).mkdir(parents=True)
    # The dock backend runs without capabilities, so its log directory must be writable without DAC override; the
    # workspace itself stays 0700. / 机场后端不带任何 capability，其日志目录须无需越权即可写；工作区本身仍为 0700。
    os.chmod(case / "dock", 0o777)
    secrets = root / "secrets" / "m2"
    env = dict(os.environ, DRONE_SOURCE_SHA=sha, DRONE_M2_RUN=str(case), DRONE_M2_SECRETS=str(secrets),
               DRONE_M2_MODEL=str(root / "secrets" / "m2-model"), DRONE_M2_PLANNER="scripted",
               DRONE_M2_SIM_IMAGE=images["sim"], DRONE_M2_AIRCRAFT_IMAGE=images["aircraft"],
               DRONE_M2_GROUND_IMAGE=images["ground"], DRONE_M2_PACKAGE="none.json", DRONE_M2_VERSION="0",
               DRONE_M2_EPOCH="0", DRONE_M2_FLIGHT=str(case / "idle-flight"), DRONE_M2_TRANSPORT="grpc",
               DRONE_M2_FLEET_TARGET="mission-service:8450")
    prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.m2.yaml"),
              "-f", str(source / "sim/compose.p1.yaml")]
    transcript, injections, snapshots, flights = [], [], [], []

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"P1 compose failed ({result.returncode}); artifacts: {case}")
        return result

    def api(method: str, *, actor: str = OPERATOR, **params) -> dict:
        result = compose("exec", "-T", "mission-service", "python3", "-m", "drone_agent.fleet.api", "--socket",
                         "/run/mission/api.sock", "--actor", actor, method, json.dumps(params, ensure_ascii=False),
                         timeout=180, quiet=True, check=False)
        try:
            response = json.loads(result.stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            response = {"ok": False, "issue": {"code": "service.degraded"}}
        value = response.get("result")
        transcript.append({"at": now(), "actor": actor, "trust": "first_party", "method": method, "probe": False,
                           "params": params, "ok": bool(response.get("ok")),
                           "code": (response.get("issue") or {}).get("code"),
                           "result_sha256": hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode())
                           .hexdigest() if value is not None else None,
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

    def service_up(restart: bool = False) -> None:
        (case / "service/ready.json").unlink(missing_ok=True)
        if restart:
            compose("restart", "-t", "5", "mission-service")
        else:
            compose("up", "-d", "--no-build", "--pull", "never", "mission-service")
        wait(lambda: (case / "service/ready.json").exists(), 90, "mission service")

    def dock() -> dict:
        response = api("resources.get", actor=JUDGE, project_id=PROJECT, resource_id=DOCK)
        return response.get("result") or {} if response.get("ok") else {}

    def snapshot(label: str) -> None:
        response = api("resources.list", actor=JUDGE, project_id=PROJECT)
        snapshots.append({"label": label, "at": now(), "project": PROJECT, "resources": response.get("result")})

    def status(mission_id: str) -> str:
        response = api("view", actor=JUDGE, mission_id=mission_id)
        return ((response.get("result") or {}).get("mission") or {}).get("status", "")

    try:
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case / 'input'}:/input",
                        images["ground"], "python3", "-m", "drone_agent.eval.p1_prepare", "--output", "/input",
                        "--scenario", scenario["id"], "--seed", str(seed), "--sha", sha])
        metadata = json.loads((case / "input/scenario.json").read_text())
        (case / "control/dock.json").write_text("{}")
        compose("stop", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy", "collector",
                check=False)
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector")
        wait(lambda: (case / "sensor/latest.json").exists(), 100, "Gazebo RGB frame")
        service_up()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "dock-sim")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "uplink")
        wait(lambda: (dock().get("status") or {}).get("session") == "active", 60, "an active dock session")
        snapshot("before")
        submitted = api("missions.submit", project_id=PROJECT, robot_id=ROBOT, text=metadata["text"],
                        volume_id=metadata["scope"]["volume_id"], asset_ids=[],
                        idempotency_key=f"{scenario['id']}-{seed}-{run_id}")
        if not submitted.get("ok"):
            raise RuntimeError(f"submit refused: {submitted.get('issue')}")
        view = submitted["result"]
        mission_id = view["mission"]["mission_id"]
        approved = api("approve", mission_id=mission_id, version=1, package_hash=view["versions"][0]["package_hash"])
        if not approved.get("ok"):
            raise RuntimeError(f"approve refused: {approved.get('issue')}")
        if scenario["id"] == "p1_s1_blocked_before_claim":
            (case / "control/dock.json").write_text(json.dumps({"upkeep": "maintenance"}))
            inject("upkeep", value="maintenance")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if any((case / "inbox/history").glob(f"{mission_id}-v*.json")):
                    raise RuntimeError("a package reached the aircraft while the dock was in maintenance")
                time.sleep(1)
            snapshot("blocked")
            api("operate", mission_id=mission_id, action="cancel", request_id=f"cancel-{run_id[-8:]}-{seed}")
            wait(lambda: status(mission_id) == "cancelled", 30, "the cancelled mission")
        else:
            flights.append(fly_once(case, compose, api, wait, service_up, mission_id,
                                    restart=scenario["id"] == "p1_s1_restart", inject=inject))
            wait(lambda: status(mission_id) in TERMINAL, 60, "the mission's final status")
            wait(lambda: dock().get("pad") == "free", 90, "the reconciled release")
        snapshot("after")
        exported = api("view", actor=JUDGE, mission_id=mission_id)["result"]
        (case / "service-export/view.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2))
        (case / "service-export/robots.json").write_text(json.dumps(api("robots", actor=JUDGE).get("result"), indent=2))
        (case / "service-export/resources.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
        M2["save_sitl_log"](case, compose, "sitl.log")
        compose("stop", "-t", "5", "uplink", "dock-sim", "mission-service", "model-proxy", "collector")
        compose("stop", "-t", "10", "sitl")
        for name, rows in (("api.jsonl", transcript), ("injections.jsonl", injections)):
            (case / "world" / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        (case / "world/uav_01-flights.json").write_text(json.dumps(flights, indent=2))
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"], "python3",
                        "-m", "drone_agent.eval.p1_prepare", "--export-ops", "/run/service/ledger.sqlite3",
                        "--output", "/run/service-export/operations.json"])
        judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=180, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_p1",
                           "/run", "--layer", "s1", "--root", "/workspace", "--replay", "--output",
                           "/output/replay.json", timeout=180, check=False)
        replay_path = case / "judge/replay.json"
        replay = json.loads(replay_path.read_text()) if replay_path.exists() else {}
        result["replay_agrees"] = all(result.get(k) == replay.get(k) for k in
                                      ("classification", "false_success_reports", "problems", "counts"))
        result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
        result["scenario"], result["seed"] = scenario["id"], seed
        return result
    except Exception as error:
        compose("logs", "--no-color", "--tail", "200", check=False)
        compose("stop", "-t", "5", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy",
                "collector", check=False)
        return {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}


def fly_once(case, compose, api, wait, service_up, mission_id, *, restart: bool, inject) -> dict:
    """Fly v1 once the claim gate handed it out; optionally restart the service and dock backend while airborne.

    领取闸门交付后飞 v1；可在空中重启服务与机场后端。
    """
    package = case / "inbox/history" / f"{mission_id}-v1.json"
    wait(package.exists, 120, "the claimed package in the inbox")
    flight = case / "aircraft" / mission_id / "v1"
    flight.mkdir(parents=True)
    state = case / "robot/authority.json"
    epoch = json.loads(state.read_text())["epoch_watermark"] + 1 if state.exists() else 1
    extra = {"DRONE_M2_PACKAGE": package.name, "DRONE_M2_VERSION": "1", "DRONE_M2_EPOCH": str(epoch),
             "DRONE_M2_FLIGHT": str(flight)}
    entry = {"robot_id": ROBOT, "mission_id": mission_id, "version": 1, "epoch": epoch, "started_at": now(),
             "ended_at": None}
    (case / "ipc/guardian.sock").unlink(missing_ok=True)
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "guardian", extra=extra)
    wait(lambda: (case / "ipc/guardian.sock").exists(), 90, "guardian")
    compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "executive", extra=extra)
    deadline, cleanup, restarted = time.monotonic() + 420, False, False
    while time.monotonic() < deadline:
        path = flight / "status.json"
        status = json.loads(path.read_text()) if path.exists() else None
        if status is None:
            time.sleep(0.1)
            continue
        observation = status["observation"]
        if status["safety"] == "abort" and not cleanup:
            compose("exec", "-T", "sitl", "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander", "land",
                    check=False)
            cleanup = True
            (flight / "manual-cleanup.json").write_text(json.dumps({"reason": status["reason"]}))
        if restart and not restarted and observation.get("in_air") is True:
            restarted = True
            inject("service_restart")
            service_up(restart=True)
            inject("dock_restart")
            compose("restart", "-t", "2", "dock-sim")
        if observation.get("in_air") is False and observation.get("armed") is False and (flight / "result.json").exists():
            entry["ended_at"] = now()
            time.sleep(2.2)
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("v1 exceeded its bounded flight duration")
    compose("stop", "-t", "5", "executive", "guardian", extra=extra)
    onboard = {name: M2["rows"](flight / f"{name}.jsonl") for name in ("executive", "guardian")}

    def mirrored():
        response = api("view", actor=JUDGE, mission_id=mission_id)
        record = next((v for v in (response.get("result") or {}).get("versions", []) if v["version"] == 1), {})
        journals = record.get("journals", {})
        return all(journals.get(name) == {"rows": count, "chain": "ok"} for name, count in onboard.items()) \
            and record.get("status") == "finished"

    wait(mirrored, 90, "the service mirror of v1")
    return entry
