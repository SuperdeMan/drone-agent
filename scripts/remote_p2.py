"""Run owned cloud P2 S1 cases: a workflow on the formal mission service, the logical dock and PX4 SITL.

Each case: prepare inputs, start SITL and the truth collector, the mission service (catalog p1_s1_v1, workflow catalog
p2_s1_v1, ledger in the case directory), the dock backend and the uplink; wait for an active dock session; start the
case's workflow as the harness operator. Then act as the people would until every run of the case is final: the
harness approver approves each child mission's exact package hash, the harness reviewer decides waiting reviews as the
case says, the operator reports repairs; every package the claim gate hands out is flown once with a fresh guardian
and executive (a battery swap before every flight after the first). A case may cancel the workflow or restart the
mission service while the aircraft is airborne. Afterwards: wait for the reconciled release, export every mission
view, the workflow views, resources, the ledger tables, the API transcript and the flight windows, and run the P2 S1
judge online and from the MCAP recordings. Nothing here reaches the guardian except starting and stopping it.

在云端运行 P2 S1 用例：正式任务服务上的工作流、逻辑机场与 PX4 SITL。每个用例：准备输入，启动 SITL 与真值采集、任务
服务（目录 p1_s1_v1、工作流目录 p2_s1_v1，账本在用例目录内）、机场后端与 uplink；等待机场会话有效；以编排操作者启动用例
的工作流。随后像人一样行动，直到用例的每个运行都终结：编排审批人按确切任务包哈希审批每个子任务，编排复核人按用例处理
等待中的复核，操作者报告维修；领取闸门交付的每个任务包都用新的 guardian 与 executive 飞一次（首飞之后每次飞行前换电）。
用例可在飞行器空中时取消工作流或重启任务服务。之后等待对账释放，导出每个任务视图、工作流视图、资源、账本表、API 记录与
飞行窗口，并在线与按 MCAP 录制各运行一次 P2 S1 裁判。除启停容器外，这里没有任何东西触达 guardian。
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
OPERATOR, REVIEWER, JUDGE = "harness:p2-operator", "harness:p2-reviewer", "harness:p2-judge"
RUN_FINAL = ("completed", "failed", "outcome_unknown", "cancelled")
MISSION_FINAL = ("completed", "incomplete", "declined", "delivery_rejected", "rejected", "refused", "planning_failed",
                 "cancelled", "dispatch_expired")
CASE_LIMIT_S = 1800


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_p2(root: Path, deployment: Path, request: dict) -> dict:
    manifest = json.loads((deployment / "manifest.json").read_text())
    sha = manifest["source_sha"]
    tag = sha + "-" + manifest["control_sha256"][:12]
    checks = f"drone-agent-checks:{tag}"
    source = deployment / "source"
    if not (source / "sim/compose.p2.yaml").is_file():
        raise ValueError("deploy a version with the P2 S1 runner first")
    base = root / "artifacts" / deployment.name / ("p2-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for P2 S1")
    images = M2["build_images"](source, base, tag, checks)
    keys = M2["provision"](root, images["ground"])
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.p2_prepare"]))
    cases = suite["s1"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(set(wanted)):
            raise ValueError("unknown P2 S1 case in selection")
    seeds = set(request.get("seeds") or [])
    selection = [(case, seed) for case in cases for seed in case["seeds"] if not seeds or seed in seeds]
    if not selection:
        raise ValueError("empty P2 S1 selection")
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
        "planner": "scripted", "workflow_planning": "deterministic", "analysis": "scripted",
        "signer_key_id": keys["signer_key_id"], "certificate_fingerprints": keys["fingerprints"],
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
    # The dock backend runs without capabilities, so its log directory must be writable without DAC override.
    # 机场后端不带任何 capability，其日志目录须无需越权即可写。
    os.chmod(case / "dock", 0o777)
    secrets = root / "secrets" / "m2"
    env = dict(os.environ, DRONE_SOURCE_SHA=sha, DRONE_M2_RUN=str(case), DRONE_M2_SECRETS=str(secrets),
               DRONE_M2_MODEL=str(root / "secrets" / "m2-model"), DRONE_M2_PLANNER="scripted",
               DRONE_M2_SIM_IMAGE=images["sim"], DRONE_M2_AIRCRAFT_IMAGE=images["aircraft"],
               DRONE_M2_GROUND_IMAGE=images["ground"], DRONE_M2_PACKAGE="none.json", DRONE_M2_VERSION="0",
               DRONE_M2_EPOCH="0", DRONE_M2_FLIGHT=str(case / "idle-flight"), DRONE_M2_TRANSPORT="grpc",
               DRONE_M2_FLEET_TARGET="mission-service:8450")
    prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.m2.yaml"),
              "-f", str(source / "sim/compose.p1.yaml"), "-f", str(source / "sim/compose.p2.yaml")]
    transcript, injections, snapshots, flights = [], [], [], []

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"P2 compose failed ({result.returncode}); artifacts: {case}")
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

    def service_up(restart: bool = False) -> None:
        (case / "service/ready.json").unlink(missing_ok=True)
        if restart:
            compose("restart", "-t", "5", "mission-service")
        else:
            compose("up", "-d", "--no-build", "--pull", "never", "mission-service")
        wait(lambda: (case / "service/ready.json").exists(), 90, "mission service")

    def dock() -> dict:
        response = api("resources.get", actor=JUDGE, record=False, project_id=PROJECT, resource_id=DOCK)
        return response.get("result") or {} if response.get("ok") else {}

    def snapshot(label: str) -> None:
        response = api("resources.list", actor=JUDGE, record=False, project_id=PROJECT)
        snapshots.append({"label": label, "at": now(), "project": PROJECT, "resources": response.get("result")})

    def runs_of(root_run: str) -> list[dict]:
        """The root run and its reinspection runs, as the judge identity reads them. / 根运行及其复检运行。"""
        found, pending = [], [root_run]
        while pending:
            response = api("workflows.get", actor=JUDGE, record=False, project_id=PROJECT, run_id=pending.pop())
            view = response.get("result")
            if view is None:
                continue
            found.append(view)
            pending += [child["run_id"] for child in view["children"]]
        return found

    try:
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case / 'input'}:/input",
                        images["ground"], "python3", "-m", "drone_agent.eval.p2_prepare", "--output", "/input",
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
        started = api("workflows.start", project_id=PROJECT, workflow_id=metadata["workflow_id"],
                      request_id=f"{scenario['id']}-{seed}-{run_id}"[:120], inputs=metadata["inputs"])
        if not started.get("ok"):
            raise RuntimeError(f"workflow start refused: {started.get('issue')}")
        root_run = started["result"]["run"]["run_id"]
        inject("workflow_started", run_id=root_run, workflow_id=metadata["workflow_id"])
        flown: set[str] = set()
        fault = {"done": False}

        def hook(observation: dict) -> None:
            if fault["done"] or observation.get("in_air") is not True or not metadata.get("fault"):
                return
            fault["done"] = True
            if metadata["fault"] == "cancel_airborne":
                inject("cancel_airborne", run_id=root_run)
                api("workflows.cancel", project_id=PROJECT, run_id=root_run, request_id=f"cancel-{run_id[-8:]}-{seed}",
                    reason="S1 cancel while airborne")
            elif metadata["fault"] == "restart_airborne":
                inject("service_restart")
                service_up(restart=True)

        deadline = time.monotonic() + CASE_LIMIT_S
        while time.monotonic() < deadline:
            views = runs_of(root_run)
            if views and all(v["run"]["state"] in RUN_FINAL for v in views):
                break
            for view in views:
                project = view["run"]["project_id"]
                for mission in view["missions"]:
                    if mission["status"] == "awaiting_approval":
                        detail = api("view", actor=JUDGE, record=False, mission_id=mission["mission_id"]).get("result")
                        version = detail["mission"]["current_version"] if detail else 1
                        record = next(v for v in detail["versions"] if v["version"] == version) if detail else {}
                        api("approve", mission_id=mission["mission_id"], version=version,
                            package_hash=record.get("package_hash", ""))
                for node in view["nodes"]:
                    if node["state"] != "waiting":
                        continue
                    decision = metadata["reviews"].get(node["node_id"])
                    if node["activity"] == "human_review" and decision:
                        api("workflows.review", actor=REVIEWER, project_id=project, run_id=view["run"]["run_id"],
                            node_id=node["node_id"], decision=decision, request_id=f"review-{node['node_id']}-{seed}",
                            note="S1 harness review")
                    if node["activity"] == "await_repair" and metadata["repair"]:
                        for order in view["orders"]:
                            if order["state"] == "open":
                                api("workflows.repair", project_id=project, order_id=order["order_id"],
                                    request_id=f"repair-{order['order_id']}", note="S1 harness repair feedback")
            history = case / "inbox/history"
            pending = sorted((p for p in history.glob("m-*-v*.json") if p.name not in flown),
                             key=lambda p: p.stat().st_mtime) if history.is_dir() else []
            if pending:
                package = pending[0]
                flown.add(package.name)
                if flights:
                    M2["battery_swap"](case, compose, wait, len(flights) + 1)
                flights.append(fly(case, compose, api, wait, package, hook))
                continue
            time.sleep(1.0)
        else:
            raise RuntimeError("the case's runs did not finish in time")
        wait(lambda: dock().get("pad") == "free", 120, "the reconciled release")
        snapshot("after")
        views = runs_of(root_run)
        missions = {}
        for view in views:
            for mission in view["missions"]:
                missions[mission["mission_id"]] = api("view", actor=JUDGE, record=False,
                                                      mission_id=mission["mission_id"])["result"]
        (case / "service-export/views.json").write_text(json.dumps(missions, ensure_ascii=False, indent=2))
        (case / "service-export/workflow-views.json").write_text(json.dumps(
            {v["run"]["run_id"]: v for v in views}, ensure_ascii=False, indent=2, default=str))
        (case / "service-export/robots.json").write_text(json.dumps(api("robots", actor=JUDGE, record=False)
                                                                    .get("result"), indent=2))
        (case / "service-export/resources.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
        M2["save_sitl_log"](case, compose, "sitl.log")
        compose("stop", "-t", "5", "uplink", "dock-sim", "mission-service", "model-proxy", "collector")
        compose("stop", "-t", "10", "sitl")
        for name, rows in (("api.jsonl", transcript), ("injections.jsonl", injections)):
            (case / "world" / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        (case / "world/uav_01-flights.json").write_text(json.dumps(flights, indent=2))
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"], "python3",
                        "-m", "drone_agent.eval.p2_prepare", "--export", "/run/service/ledger.sqlite3",
                        "--output", "/run/service-export/workflows.json"])
        judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=300, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_p2",
                           "/run", "--layer", "s1", "--root", "/workspace", "--replay", "--output",
                           "/output/replay.json", timeout=300, check=False)
        replay_path = case / "judge/replay.json"
        replay = json.loads(replay_path.read_text()) if replay_path.exists() else {}
        result["replay_agrees"] = all(result.get(k) == replay.get(k) for k in
                                      ("classification", "false_success_reports", "problems", "counts"))
        result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
        result["scenario"], result["seed"] = scenario["id"], seed
        result["flown_versions"] = [f"{f['mission_id']}-v{f['version']}" for f in flights]
        return result
    except Exception as error:
        compose("logs", "--no-color", "--tail", "200", check=False)
        compose("stop", "-t", "5", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy",
                "collector", check=False)
        return {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}


def fly(case, compose, api, wait, package: Path, hook) -> dict:
    """Fly one handed-out package once with a fresh guardian and executive; `hook` sees each observation.

    用新的 guardian 与 executive 把一个已交付任务包飞一次；`hook` 看到每个观测。
    """
    mission_id, version = package.stem.rsplit("-v", 1)
    flight = case / "aircraft" / mission_id / f"v{version}"
    flight.mkdir(parents=True)
    state = case / "robot/authority.json"
    epoch = json.loads(state.read_text())["epoch_watermark"] + 1 if state.exists() else 1
    extra = {"DRONE_M2_PACKAGE": package.name, "DRONE_M2_VERSION": version, "DRONE_M2_EPOCH": str(epoch),
             "DRONE_M2_FLIGHT": str(flight)}
    entry = {"robot_id": ROBOT, "mission_id": mission_id, "version": int(version), "epoch": epoch,
             "started_at": now(), "ended_at": None}
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
        observation = status["observation"]
        if status["safety"] == "abort" and not cleanup:
            compose("exec", "-T", "sitl", "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4-commander", "land",
                    check=False)
            cleanup = True
            (flight / "manual-cleanup.json").write_text(json.dumps({"reason": status["reason"]}))
        hook(observation)
        if observation.get("in_air") is False and observation.get("armed") is False and (flight / "result.json").exists():
            entry["ended_at"] = now()
            time.sleep(2.2)
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"{package.name} exceeded its bounded flight duration")
    compose("stop", "-t", "5", "executive", "guardian", extra=extra)
    onboard = {name: M2["rows"](flight / f"{name}.jsonl") for name in ("executive", "guardian")}

    def mirrored():
        response = api("view", actor=JUDGE, record=False, mission_id=mission_id)
        record = next((v for v in (response.get("result") or {}).get("versions", [])
                       if v["version"] == int(version)), {})
        journals = record.get("journals", {})
        return all(journals.get(name) == {"rows": count, "chain": "ok"} for name, count in onboard.items()) \
            and record.get("status") == "finished"

    wait(mirrored, 90, f"the service mirror of {package.name}")
    return entry
