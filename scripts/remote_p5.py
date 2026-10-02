"""Run owned cloud P5 S1 work: the road obstacle template (the second business template) on PX4 SITL (D071).

Each case prepares its inputs, starts the P5 simulator (the M2 world plus the road marking) and the truth collector, the
mission service (catalogs p5_s1_v1, no vision model), the dock backend and the uplink, places an obstacle model on the
road marking through the Gazebo world's own create service, and raises a road report as the bound `event:` source.
Then it acts as the people would until the clearance order is closed or its planned rounds are spent: the approver
approves each mission's exact package hash, the reviewer confirms the candidate obstacle and each reinspection's
unobstructed capture, and the operator reports the clearance (removing the obstacle first when the case says the
clearance really happened). Every package the claim gate hands out is flown once with a fresh guardian and executive; a
battery swap restarts the world, so the harness restores the obstacle when it should still be there. Afterwards it
exports the records and runs the P5 S1 judge online and from the MCAP recordings. Nothing here reaches the guardian
except starting and stopping it.

在云端运行 P5 S1 工作：PX4 SITL 上的道路障碍模板（第二业务模板，D071）。

每个用例准备输入，启动 P5 仿真器（M2 世界加道路标线）与真值采集、任务服务（p5_s1_v1 目录，无视觉模型）、机场后端与 uplink，
经 Gazebo 世界自身的 create 服务在道路标线上放置障碍物模型，并以绑定的 `event:` 来源发出道路报告。随后像人一样行动，直到
清障工单关闭或计划轮次用尽：审批人按确切任务包哈希审批每个任务，reviewer 确认候选障碍与每次复检的无遮挡采集，操作者报告清障
（用例说明确实清障时先移除障碍物）。领取闸门交付的每个任务包都用新的 guardian 与 executive 飞一次；换电会重启世界，所以编排在
障碍物仍应存在时将其恢复。之后导出记录，并在线与按 MCAP 录制各运行一次 P5 S1 裁判。除启停容器外，这里没有任何东西触达 guardian。
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
OPERATOR, REVIEWER, JUDGE = "harness:p5-operator", "harness:p5-reviewer", "harness:p5-judge"
ROADS = "event:road-reports"
RUN_FINAL = P2["RUN_FINAL"]
REPAIRABLE = ("open", "reinspection_failed", "reinspection_unknown")
NODE_FINAL = ("completed", "skipped", "failed", "outcome_unknown", "cancelled")
CASE_LIMIT_S = 2700


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def revision(deployment: Path) -> tuple[str, str, str]:
    manifest = json.loads((deployment / "manifest.json").read_text())
    tag = manifest["source_sha"] + "-" + manifest["control_sha256"][:12]
    return manifest["source_sha"], tag, f"drone-agent-checks:{tag}"


def build_sim(source: Path, base: Path, tag: str, sim2_image: str) -> str:
    """The P5 simulator built on the M2 one (also used by the resident desk). / 基于 M2 仿真镜像构建 P5 仿真器（常驻任务台也用）。"""
    p5 = f"drone-agent-p5-sim:{tag}"
    if not HELPERS["inspect_image"](p5):
        with (base / "build-p5-sim.log").open("wb") as log:
            result = subprocess.run(["docker", "build", "--pull=false", "--build-arg", f"SIM2_IMAGE={sim2_image}",
                                     "-f", str(source / "sim/p5.Dockerfile"), "-t", p5, str(source)],
                                    stdout=log, stderr=subprocess.STDOUT, timeout=1200)
        if result.returncode:
            raise RuntimeError(f"P5 simulator build failed: {base}")
    return p5


def build_images(source: Path, base: Path, tag: str, checks: str) -> dict:
    """The M2 images, plus the P5 simulator built on the M2 one. / M2 镜像，加上基于 M2 仿真镜像构建的 P5 仿真器。"""
    images = M2["build_images"](source, base, tag, checks)
    return {**images, "sim": build_sim(source, base, tag, images["sim"])}


def run_p5(root: Path, deployment: Path, request: dict) -> dict:
    sha, tag, checks = revision(deployment)
    source = deployment / "source"
    if not (source / "scripts/remote_p5.py").is_file() or not (source / "sim/compose.p5.yaml").is_file():
        raise ValueError("deploy a version with the P5 S1 runner first")
    base = root / "artifacts" / deployment.name / ("p5-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for P5 S1")
    images = build_images(source, base, tag, checks)
    keys = M2["provision"](root, images["ground"])
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.p5_prepare"]))
    cases = suite["s1"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(set(wanted)):
            raise ValueError("unknown P5 S1 case in selection")
    seeds = set(request.get("seeds") or [])
    selection = [(case, seed) for case in cases for seed in case["seeds"] if not seeds or seed in seeds]
    if not selection:
        raise ValueError("empty P5 S1 selection")
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
              "-f", str(source / "sim/compose.p5.yaml")]
    transcript, injections, snapshots, flights, appearance = [], [], [], [], []

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"P5 compose failed ({result.returncode}); artifacts: {case}")
        return result

    def api(method: str, *, actor: str = OPERATOR, trust: str = "first_party", record: bool = True,
            **params) -> dict:
        result = compose("exec", "-T", "mission-service", "python3", "-m", "drone_agent.fleet.api", "--socket",
                         "/run/mission/api.sock", "--actor", actor, "--trust", trust, method,
                         json.dumps(params, ensure_ascii=False), timeout=180, quiet=True, check=False)
        try:
            response = json.loads(result.stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            response = {"ok": False, "issue": {"code": "service.degraded"}}
        value = response.get("result")
        if record:
            transcript.append({"at": now(), "actor": actor, "trust": trust, "method": method, "probe": False,
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

    world = {"obstacle": False}

    def gz(service: str, reqtype: str, request: str) -> bool:
        """One request to the Gazebo world's own service inside the SITL container. / 对 SITL 容器内 Gazebo 世界服务的一次请求。"""
        result = compose("exec", "-T", "sitl", "gz", "service", "-s", f"/world/default/{service}", "--reqtype",
                         reqtype, "--reptype", "gz.msgs.Boolean", "--timeout", "5000", "--req", request,
                         timeout=60, check=False)
        return result.returncode == 0 and b"data: true" in result.stdout

    def place_obstacle(reason: str) -> None:
        if not gz("create", "gz.msgs.EntityFactory", f"sdf: '{metadata['obstacle_sdf']}'"):
            raise RuntimeError("the obstacle could not be placed in the Gazebo world")
        world["obstacle"] = True
        # The P4 judge's world check reads a damaged / normal history of the asset. / P4 裁判的世界核对读取资产的损伤 / 正常历史。
        appearance.append({"at": now(), "asset_id": metadata["segment"], "state": "damaged", "reason": reason})
        inject("obstacle_placed", asset_id=metadata["segment"], reason=reason)

    def remove_obstacle(reason: str) -> None:
        if not gz("remove", "gz.msgs.Entity", 'name: "road_obstacle" type: MODEL'):
            raise RuntimeError("the obstacle could not be removed from the Gazebo world")
        world["obstacle"] = False
        appearance.append({"at": now(), "asset_id": metadata["segment"], "state": "normal", "reason": reason})
        inject("obstacle_removed", asset_id=metadata["segment"], reason=reason)

    def views_of(run_ids: list[str]) -> list[dict]:
        found = []
        for run_id in run_ids:
            response = api("workflows.get", actor=JUDGE, record=False, project_id=PROJECT, run_id=run_id)
            if response.get("result") is not None:
                found.append(response["result"])
        return found

    def orders() -> list[dict]:
        response = api("orders.list", actor=JUDGE, record=False, project_id=PROJECT)
        return response.get("result") or [] if response.get("ok") else []

    def order_detail(order_id: str) -> dict:
        response = api("orders.get", actor=JUDGE, record=False, project_id=PROJECT, order_id=order_id)
        return response.get("result") or {} if response.get("ok") else {}

    def node(view: dict, node_id: str) -> dict:
        return next((n for n in view["nodes"] if n["node_id"] == node_id), {})

    try:
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case / 'input'}:/input",
                        images["ground"], "python3", "-m", "drone_agent.eval.p5_prepare", "--output", "/input",
                        "--scenario", scenario["id"], "--seed", str(seed), "--sha", sha])
        metadata = json.loads((case / "input/scenario.json").read_text())
        (case / "control/dock.json").write_text("{}")
        compose("stop", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy", "collector",
                check=False)
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector")
        wait(lambda: (case / "sensor/latest.json").exists(), 100, "Gazebo RGB frame")
        place_obstacle("case start")
        service_up()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "dock-sim")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "uplink")
        wait(lambda: (dock().get("status") or {}).get("session") == "active", 60, "an active dock session")
        snapshot("before")
        reported_event = api("workflows.event", actor=ROADS, trust="backend", project_id=PROJECT,
                             workflow_id=metadata["workflow_id"], trigger_id=metadata["trigger_id"],
                             event_type=metadata["event_type"], event_id=f"road-{scenario['id']}-{seed}-{run_id}"[:120],
                             payload={"segment": metadata["segment"]})
        if not reported_event.get("ok"):
            raise RuntimeError(f"road report refused: {reported_event.get('issue')}")
        roots = [reported_event["result"]["run_id"]]
        inject("road_report", run_id=roots[0], segment=metadata["segment"])
        flown: set[str] = set()
        reported: set[int] = set()
        deadline = time.monotonic() + CASE_LIMIT_S
        while time.monotonic() < deadline:
            listed = orders()
            reinspections = [r["reinspection_run"] for o in listed for r in order_detail(o["order_id"]).get("rounds", [])
                             if r.get("reinspection_run")]
            views = views_of(roots + reinspections)
            settled = bool(views) and all(v["run"]["state"] in RUN_FINAL for v in views)
            if settled:
                listed = orders()
            order = listed[0] if len(listed) == 1 else None
            rounds_spent = len(reported) >= metadata["rounds"]
            if settled and (order is None or order["state"] == "closed"
                            or (rounds_spent and order["state"] in REPAIRABLE)):
                break
            for view in views:
                for mission in view["missions"]:
                    if mission["status"] == "awaiting_approval":
                        detail = api("view", actor=JUDGE, record=False, mission_id=mission["mission_id"]).get("result")
                        version = detail["mission"]["current_version"] if detail else 1
                        record = next(v for v in detail["versions"] if v["version"] == version) if detail else {}
                        api("approve", mission_id=mission["mission_id"], version=version,
                            package_hash=record.get("package_hash", ""))
            analysed = all(node(v, "analyze").get("state") in NODE_FINAL for v in views if v["run"]["run_id"] in roots)
            for view in views:
                review = node(view, "review")
                if review.get("state") != "waiting" or review.get("activity") != "human_review":
                    continue
                run = view["run"]["run_id"]
                api("workflows.review", actor=REVIEWER, project_id=PROJECT, run_id=run, node_id="review",
                    decision="confirmed", request_id=f"review-{run[-10:]}-{seed}", note="S1 harness review")
            if order is not None and order["state"] in REPAIRABLE and not rounds_spent and analysed and settled:
                number = order["round"] + 1
                if number not in reported:
                    if number == metadata["remove_at_round"] and world["obstacle"]:
                        remove_obstacle(f"clearance before round {number}")
                    response = api("orders.repair", project_id=PROJECT, order_id=order["order_id"],
                                   request_id=f"clear-{order['order_id'][-10:]}-r{number}",
                                   note=f"S1 harness clearance feedback, round {number}")
                    if not response.get("ok"):
                        raise RuntimeError(f"clearance feedback refused: {response.get('issue')}")
                    reported.add(number)
                    inject("clearance_feedback", order_id=order["order_id"], round=number,
                           obstacle=world["obstacle"])
                    continue
            history = case / "inbox/history"
            pending = sorted((p for p in history.glob("m-*-v*.json") if p.name not in flown),
                             key=lambda p: p.stat().st_mtime) if history.is_dir() else []
            if pending:
                package = pending[0]
                flown.add(package.name)
                if flights:
                    M2["battery_swap"](case, compose, wait, len(flights) + 1)
                    # The swap restarts the world from its file; the obstacle stays until the case removes it.
                    # 换电使世界按文件重启；障碍物保持到用例移除它为止。
                    if world["obstacle"]:
                        place_obstacle("restored after the battery swap")
                flights.append(P2["fly"](case, compose, api, wait, package, lambda observation: None, judge=JUDGE))
                continue
            time.sleep(1.0)
        else:
            raise RuntimeError("the case's runs and order did not settle in time")
        wait(lambda: dock().get("pad") == "free", 120, "the reconciled release")
        snapshot("after")
        listed = orders()
        details = {o["order_id"]: order_detail(o["order_id"]) for o in listed}
        reinspections = [r["reinspection_run"] for d in details.values() for r in d.get("rounds", [])
                         if r.get("reinspection_run")]
        views = views_of(roots + reinspections)
        missions = {}
        for view in views:
            for mission in view["missions"]:
                missions[mission["mission_id"]] = api("view", actor=JUDGE, record=False,
                                                      mission_id=mission["mission_id"])["result"]
        (case / "service-export/views.json").write_text(json.dumps(missions, ensure_ascii=False, indent=2))
        (case / "service-export/workflow-views.json").write_text(json.dumps(
            {v["run"]["run_id"]: v for v in views}, ensure_ascii=False, indent=2, default=str))
        (case / "service-export/order-views.json").write_text(json.dumps(details, ensure_ascii=False, indent=2,
                                                                         default=str))
        (case / "service-export/robots.json").write_text(json.dumps(api("robots", actor=JUDGE, record=False)
                                                                    .get("result"), indent=2))
        (case / "service-export/resources.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
        (case / "service-export/appearance.json").write_text(json.dumps(
            {"history": appearance,
             "obstacle_sdf_sha256": hashlib.sha256(metadata["obstacle_sdf"].encode()).hexdigest()}, indent=2))
        M2["save_sitl_log"](case, compose, "sitl.log")
        compose("stop", "-t", "5", "uplink", "dock-sim", "mission-service", "model-proxy", "collector")
        compose("stop", "-t", "10", "sitl")
        for name, rows in (("api.jsonl", transcript), ("injections.jsonl", injections)):
            (case / "world" / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        (case / "world/uav_01-flights.json").write_text(json.dumps(flights, indent=2))
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"], "python3",
                        "-m", "drone_agent.eval.p5_prepare", "--export", "/run/service/ledger.sqlite3",
                        "--output", "/run/service-export/workflows.json"])
        shutil.copyfile(case / "service-export/workflows.json", case / "service-export/business.json")
        judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=300, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_p5",
                           "/run", "--layer", "s1", "--root", "/workspace", "--replay", "--output",
                           "/output/replay.json", timeout=300, check=False)
        replay_path = case / "judge/replay.json"
        replay = json.loads(replay_path.read_text()) if replay_path.exists() else {}
        result["replay_agrees"] = all(result.get(k) == replay.get(k) for k in
                                      ("classification", "false_success_reports", "problems", "counts"))
        result["passed"] = bool(result.get("passed")) and replayed.returncode == 0 and result["replay_agrees"]
        result["scenario"], result["seed"] = scenario["id"], seed
        result["flown_versions"] = [f"{f['mission_id']}-v{f['version']}" for f in flights]
        result["signature_fractions"] = road_fractions(case)
        return result
    except Exception as error:
        compose("logs", "--no-color", "--tail", "200", check=False)
        compose("stop", "-t", "5", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy",
                "collector", check=False)
        if (case / "service/ledger.sqlite3").is_file() and not (case / "service-export/business.json").is_file():
            # Best effort, for diagnosis and the band record only; a failed case stays failed.
            # 尽力导出，只用于诊断与区间记录；失败的用例仍是失败。
            try:
                HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"],
                                "python3", "-m", "drone_agent.eval.p5_prepare", "--export",
                                "/run/service/ledger.sqlite3", "--output", "/run/service-export/business.json"])
            except Exception:  # noqa: BLE001 - diagnosis must not mask the case's own error / 诊断不能掩盖用例自身的错误
                pass
        return {"passed": False, "error": str(error), "scenario": scenario["id"], "seed": seed,
                "signature_fractions": road_fractions(case)}


def road_fractions(case: Path) -> list[dict]:
    """The road marking's measured signature fraction per analysis, for the band calibration record.

    每次分析测得的道路标线特征占比，供区间标定记录使用。
    """
    path = case / "service-export/business.json"
    if not path.is_file():
        return []
    try:
        tables = json.loads(path.read_text())
    except ValueError:
        return []
    found = []
    for job in tables.get("bz_jobs", []):
        result = json.loads(job["result"]) if isinstance(job.get("result"), str) else (job.get("result") or {})
        found.append({"job_id": job["job_id"], "purpose": job["purpose"], "verdict": job.get("verdict"),
                      "fraction": (result.get("measures") or {}).get("signature_fraction")})
    return found
