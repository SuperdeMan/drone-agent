"""Run owned cloud P6 work: the S1 recapture loop on PX4 SITL (D078).

Each case prepares its inputs, starts SITL and the truth collector, puts the glare disc and/or the P4 damage patch
into the Gazebo world through the world's own create service, starts the mission service (catalog p1_s1_v1, workflow
catalog p6_s1_v1, business catalog p6_s1_v1, no vision model), the dock backend and the uplink, and starts one
`recapture_watch` run as the harness operator. Then it acts as the people would until the run ends: the approver
approves each mission's exact package hash; before approving the recapture, the harness removes the glare disc when
the case says the condition passed; the reviewer confirms a suspected finding. Every package the claim gate hands out
is flown once with a fresh guardian and executive; a battery swap restarts the world, so the harness restores what
should still be there. Afterwards it exports the mission, workflow and business records and runs the P6 S1 judge
online and from the MCAP recordings. Nothing here reaches the guardian except starting and stopping it.

在云端运行 P6 工作：PX4 SITL 上的 S1 补拍闭环（D078）。

每个用例准备输入，启动 SITL 与真值采集，经 Gazebo 世界自身的 create 服务放置反光圆片和 / 或 P4 损伤贴片，启动任务服务
（运营目录 p1_s1_v1、工作流目录 p6_s1_v1、业务目录 p6_s1_v1，无视觉模型）、机场后端与 uplink，并以编排操作者启动一个
`recapture_watch` 运行。随后像人一样行动直到运行结束：审批人按确切任务包哈希审批每个任务；审批补拍之前，若用例说明条件
已过去，编排移除反光圆片；复核人确认疑似发现。领取闸门交付的每个任务包都用新的 guardian 与 executive 飞一次；换电会重启
世界，所以编排恢复仍应存在的物体。之后导出任务、工作流与业务记录，并在线与按 MCAP 录制各运行一次 P6 S1 裁判。除启停
容器外，这里没有任何东西触达 guardian。
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
M2 = runpy.run_path(str(HERE / "remote_m2.py"))
P2 = runpy.run_path(str(HERE / "remote_p2.py"))
FOLDERS = P2["FOLDERS"]
PROJECT, ROBOT, DOCK = "campus_s1", "uav_01", "dock_s1"
OPERATOR, REVIEWER, JUDGE = "harness:p4-operator", "harness:p4-reviewer", "harness:p4-judge"
RUN_FINAL = P2["RUN_FINAL"]
CASE_LIMIT_S = 2400
MODELS = {"glare": "glare_disc", "patch": "damage_patch"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def revision(deployment: Path) -> tuple[str, str, str]:
    manifest = json.loads((deployment / "manifest.json").read_text())
    tag = manifest["source_sha"] + "-" + manifest["control_sha256"][:12]
    return manifest["source_sha"], tag, f"drone-agent-checks:{tag}"


def run_p6(root: Path, deployment: Path, request: dict) -> dict:
    sha, tag, checks = revision(deployment)
    source = deployment / "source"
    if not (source / "scripts/remote_p6.py").is_file() or not (source / "sim/compose.p6.yaml").is_file():
        raise ValueError("deploy a version with the P6 S1 runner first")
    base = root / "artifacts" / deployment.name / ("p6-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for P6 S1")
    images = M2["build_images"](source, base, tag, checks)
    keys = M2["provision"](root, images["ground"])
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.p6_prepare"]))
    cases = suite["s1"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(set(wanted)):
            raise ValueError("unknown P6 S1 case in selection")
    seeds = set(request.get("seeds") or [])
    selection = [(case, seed) for case in cases for seed in case["seeds"] if not seeds or seed in seeds]
    if not selection:
        raise ValueError("empty P6 S1 selection")
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
        "planner": "scripted", "workflow_planning": "deterministic", "analysis": "deterministic",
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
               DRONE_M2_FLEET_TARGET="mission-service:8450", DRONE_P4_DATA=str(case / "input"),
               DRONE_P4_OUTPUT=str(case / "judge"), DRONE_P4_UID=str(os.getuid()), DRONE_P4_GID=str(os.getgid()))
    prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.m2.yaml"),
              "-f", str(source / "sim/compose.p1.yaml"), "-f", str(source / "sim/compose.p4.yaml"),
              "-f", str(source / "sim/compose.p6.yaml")]
    transcript, injections, snapshots, flights, appearance = [], [], [], [], []

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"P6 compose failed ({result.returncode}); artifacts: {case}")
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
        wait(lambda: (case / "service/ready.json").exists(), 90, "mission service")

    def dock() -> dict:
        response = api("resources.get", actor=JUDGE, record=False, project_id=PROJECT, resource_id=DOCK)
        return response.get("result") or {} if response.get("ok") else {}

    def snapshot(label: str) -> None:
        response = api("resources.list", actor=JUDGE, record=False, project_id=PROJECT)
        snapshots.append({"label": label, "at": now(), "project": PROJECT, "resources": response.get("result")})

    def gz(service: str, reqtype: str, request: str) -> bool:
        """One request to the Gazebo world's own service inside the SITL container. / 对 SITL 容器内 Gazebo 世界服务的一次请求。"""
        result = compose("exec", "-T", "sitl", "gz", "service", "-s", f"/world/default/{service}", "--reqtype",
                         reqtype, "--reptype", "gz.msgs.Boolean", "--timeout", "5000", "--req", request,
                         timeout=60, check=False)
        return result.returncode == 0 and b"data: true" in result.stdout

    world = {"glare": False, "patch": False}

    def record(reason: str) -> None:
        appearance.append({"at": now(), "asset_id": metadata["inputs"]["asset"],
                           "state": "damaged" if world["patch"] else "normal",
                           "overlay": "glare" if world["glare"] else None, "reason": reason})

    def place(kind: str, reason: str) -> None:
        if not gz("create", "gz.msgs.EntityFactory", f"sdf: '{metadata[kind + '_sdf']}'"):
            raise RuntimeError(f"the {kind} model could not be placed in the Gazebo world")
        world[kind] = True
        record(reason)
        inject(f"{kind}_placed", reason=reason)

    def remove(kind: str, reason: str) -> None:
        if not gz("remove", "gz.msgs.Entity", f'name: "{MODELS[kind]}" type: MODEL'):
            raise RuntimeError(f"the {kind} model could not be removed from the Gazebo world")
        world[kind] = False
        record(reason)
        inject(f"{kind}_removed", reason=reason)

    try:
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case / 'input'}:/input",
                        images["ground"], "python3", "-m", "drone_agent.eval.p6_prepare", "--output", "/input",
                        "--scenario", scenario["id"], "--seed", str(seed), "--sha", sha])
        metadata = json.loads((case / "input/scenario.json").read_text())
        plan = metadata["world"]
        (case / "control/dock.json").write_text("{}")
        compose("stop", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy", "collector",
                check=False)
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector")
        wait(lambda: (case / "sensor/latest.json").exists(), 100, "Gazebo RGB frame")
        record("case start, before any model")
        for kind in ("patch", "glare"):
            if plan.get(kind):
                place(kind, "case start")
        service_up()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "dock-sim")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "uplink")
        wait(lambda: (dock().get("status") or {}).get("session") == "active", 60, "an active dock session")
        snapshot("before")
        started = api("workflows.start", project_id=PROJECT, workflow_id=metadata["workflow_id"],
                      request_id=f"{scenario['id']}-{seed}-{run_id}"[:120], inputs=metadata["inputs"])
        if not started.get("ok"):
            raise RuntimeError(f"workflow start refused: {started.get('issue')}")
        run = started["result"]["run"]["run_id"]
        inject("workflow_started", run_id=run, workflow_id=metadata["workflow_id"])
        flown: set[str] = set()
        view: dict = {}
        deadline = time.monotonic() + CASE_LIMIT_S
        while time.monotonic() < deadline:
            response = api("workflows.get", actor=JUDGE, record=False, project_id=PROJECT, run_id=run)
            view = response.get("result") or view
            if view and view["run"]["state"] in RUN_FINAL:
                break
            for mission in view.get("missions", []):
                if mission["status"] != "awaiting_approval":
                    continue
                if mission.get("node_id") == "recapture" and world["glare"] and plan.get("remove_glare_before_recapture"):
                    remove("glare", "the condition passed before the recapture was approved")
                detail = api("view", actor=JUDGE, record=False, mission_id=mission["mission_id"]).get("result")
                version = detail["mission"]["current_version"] if detail else 1
                versions = detail["versions"] if detail else []
                package = next((v["package_hash"] for v in versions if v["version"] == version), "")
                api("approve", mission_id=mission["mission_id"], version=version, package_hash=package)
            review = next((n for n in view.get("nodes", []) if n["node_id"] == "review"), {})
            if review.get("state") == "waiting":
                api("workflows.review", actor=REVIEWER, project_id=PROJECT, run_id=run, node_id="review",
                    decision="confirmed", request_id=f"review-{run[-10:]}-{seed}", note="S1 harness review")
            history = case / "inbox/history"
            pending = sorted((p for p in history.glob("m-*-v*.json") if p.name not in flown),
                             key=lambda p: p.stat().st_mtime) if history.is_dir() else []
            if pending:
                package_file = pending[0]
                flown.add(package_file.name)
                if flights:
                    M2["battery_swap"](case, compose, wait, len(flights) + 1)
                    # The swap restarts the world from its file; what is still there is placed again.
                    # 换电使世界按文件重启；仍应存在的物体重新放置。
                    for kind in ("patch", "glare"):
                        if world[kind]:
                            place(kind, "restored after the battery swap")
                flights.append(P2["fly"](case, compose, api, wait, package_file, lambda observation: None,
                                         judge=JUDGE))
                continue
            time.sleep(1.0)
        else:
            raise RuntimeError("the case's run did not end in time")
        wait(lambda: dock().get("pad") == "free", 120, "the reconciled release")
        snapshot("after")
        orders = api("orders.list", actor=JUDGE, record=False, project_id=PROJECT).get("result") or []
        missions = {m["mission_id"]: api("view", actor=JUDGE, record=False, mission_id=m["mission_id"])["result"]
                    for m in view.get("missions", [])}
        (case / "service-export/views.json").write_text(json.dumps(missions, ensure_ascii=False, indent=2))
        (case / "service-export/workflow-views.json").write_text(json.dumps({run: view}, ensure_ascii=False, indent=2,
                                                                            default=str))
        (case / "service-export/order-views.json").write_text(json.dumps(
            {o["order_id"]: o for o in orders}, ensure_ascii=False, indent=2, default=str))
        (case / "service-export/robots.json").write_text(json.dumps(api("robots", actor=JUDGE, record=False)
                                                                    .get("result"), indent=2))
        (case / "service-export/resources.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
        (case / "service-export/appearance.json").write_text(json.dumps(
            {"history": appearance, "glare_sdf_sha256": hashlib.sha256(metadata["glare_sdf"].encode()).hexdigest(),
             "patch_sdf_sha256": hashlib.sha256(metadata["patch_sdf"].encode()).hexdigest()}, indent=2))
        M2["save_sitl_log"](case, compose, "sitl.log")
        compose("stop", "-t", "5", "uplink", "dock-sim", "mission-service", "model-proxy", "collector")
        compose("stop", "-t", "10", "sitl")
        for name, rows in (("api.jsonl", transcript), ("injections.jsonl", injections)):
            (case / "world" / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        (case / "world/uav_01-flights.json").write_text(json.dumps(flights, indent=2))
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"], "python3",
                        "-m", "drone_agent.eval.p6_prepare", "--export", "/run/service/ledger.sqlite3",
                        "--output", "/run/service-export/workflows.json"])
        shutil.copyfile(case / "service-export/workflows.json", case / "service-export/business.json")
        judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=300, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_p6",
                           "/run", "--layer", "s1", "--root", "/workspace", "--replay", "--output",
                           "/output/replay.json", timeout=300, check=False)
        replay_path = case / "judge/replay.json"
        replay = json.loads(replay_path.read_text()) if replay_path.exists() else {}
        result["replay_agrees"] = all(result.get(k) == replay.get(k) for k in
                                      ("classification", "false_success_reports", "problems", "counts", "metrics"))
        result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
        result["scenario"], result["seed"] = scenario["id"], seed
        result["flown_versions"] = [f"{f['mission_id']}-v{f['version']}" for f in flights]
        return result
    except Exception as error:
        compose("logs", "--no-color", "--tail", "200", check=False)
        compose("stop", "-t", "5", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy",
                "collector", check=False)
        return {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed}
