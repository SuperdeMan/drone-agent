"""Run owned cloud P4 work: the S1 business loop on PX4 SITL, the frozen S2 dataset install and the S2 evaluation.

S1 (`run_p4`): each case prepares its inputs, starts SITL and the truth collector, the mission service (catalog
p1_s1_v1, workflow catalog p4_s1_v1, business catalog p4_s1_v1, no vision model), the dock backend and the uplink,
places a damage patch on the red marker through the Gazebo world's own create service, and starts the case's
`appearance_watch` runs as the harness operator. Then it acts as the people would until the order is closed or its
planned rounds are spent: the approver approves each mission's exact package hash, the reviewer confirms the
inspection's finding and each reinspection's unsuspected capture, and the operator reports the repair (removing the
patch first when the case says the repair really happened). Every package the claim gate hands out is flown once with a
fresh guardian and executive; a battery swap restarts the world, so the harness restores the patch when it should still
be there. Afterwards it exports the mission, workflow and business records and runs the P4 S1 judge online and from the
MCAP recordings. Nothing here reaches the guardian except starting and stopping it.

S2 (`install_data`, `run_s2`): the frozen VisA subset arrives as an uploaded archive, is extracted without links or
traversal and verified file by file against the committed manifest before it is installed read-only. An evaluation run
analyses one split in one mode: `live` reaches the model endpoint only through the allowlisted proxy and records every
exchange, `replay` and `scripted` run without any network. Calibration runs report the threshold table, test runs the
frozen metrics, and a replay is compared sample by sample with the live run it replays.

在云端运行 P4 工作：PX4 SITL 上的 S1 业务闭环、冻结 S2 数据集的安装与 S2 评测。

S1（`run_p4`）：每个用例准备输入，启动 SITL 与真值采集、任务服务（运营目录 p1_s1_v1、工作流目录 p4_s1_v1、业务目录
p4_s1_v1，无视觉模型）、机场后端与 uplink，经 Gazebo 世界自身的 create 服务在红色标记上放置损伤贴片，并以编排操作者启动
用例的 `appearance_watch` 运行。随后像人一样行动，直到工单关闭或计划轮次用尽：审批人按确切任务包哈希审批每个任务，
reviewer 确认巡检的发现与每次复检的未疑似采集，操作者报告维修（用例说明确实修复时先移除贴片）。领取闸门交付的每个任务包都
用新的 guardian 与 executive 飞一次；换电会重启世界，所以编排在贴片仍应存在时将其恢复。之后导出任务、工作流与业务记录，并在线
与按 MCAP 录制各运行一次 P4 S1 裁判。除启停容器外，这里没有任何东西触达 guardian。

S2（`install_data`、`run_s2`）：冻结的 VisA 子集以上传归档到达，解包时拒绝链接与目录穿越，按已提交的清单逐文件核对后以只读
方式安装。一次评测运行以一种模式分析一个拆分：`live` 只经白名单代理到达模型端点并录制每次交互，`replay` 与 `scripted`
在无网络下运行。校准运行报告阈值表，测试运行报告冻结指标，回放与其所回放的实调运行逐样本比较。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
M2 = runpy.run_path(str(HERE / "remote_m2.py"))
P2 = runpy.run_path(str(HERE / "remote_p2.py"))
FOLDERS = P2["FOLDERS"]
PROJECT, ROBOT, DOCK = "campus_s1", "uav_01", "dock_s1"
OPERATOR, REVIEWER, JUDGE = "harness:p4-operator", "harness:p4-reviewer", "harness:p4-judge"
RUN_FINAL = P2["RUN_FINAL"]
REPAIRABLE = ("open", "reinspection_failed", "reinspection_unknown")
NODE_FINAL = ("completed", "skipped", "failed", "outcome_unknown", "cancelled")
CASE_LIMIT_S = 2700
DATASET = "visa_pcb_v1"
ARCHIVE = f"{DATASET}.tar"
MANIFEST = f"eval/s2/{DATASET}/manifest.json"
PROTOCOL = f"eval/s2/{DATASET}/protocol.yaml"
PROFILE = "configs/analysis/vlm_change_v3.yaml"
PROFILES = re.compile(r"^configs/analysis/vlm_change_v[0-9]{1,2}\.yaml$")
REPLAY_SOURCE = re.compile(r"^([0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})/(p4-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def revision(deployment: Path) -> tuple[str, str, str]:
    manifest = json.loads((deployment / "manifest.json").read_text())
    tag = manifest["source_sha"] + "-" + manifest["control_sha256"][:12]
    return manifest["source_sha"], tag, f"drone-agent-checks:{tag}"


# ── S1 / PX4 SITL ──


def run_p4(root: Path, deployment: Path, request: dict) -> dict:
    sha, tag, checks = revision(deployment)
    source = deployment / "source"
    if not (source / "scripts/remote_p4.py").is_file() or not (source / "sim/compose.p4.yaml").is_file():
        raise ValueError("deploy a version with the P4 S1 runner first")
    base = root / "artifacts" / deployment.name / ("p4-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    before = HELPERS["foreign_identity"]()
    if HELPERS["capacity"]()["memory"]["MemAvailable"] < 3 * 1024**3:
        raise RuntimeError("insufficient shared-server memory for P4 S1")
    images = M2["build_images"](source, base, tag, checks)
    keys = M2["provision"](root, images["ground"])
    suite = json.loads(HELPERS["run"](["docker", "run", "--rm", "--network", "none", images["ground"], "python3", "-m",
                                       "drone_agent.eval.p4_prepare"]))
    cases = suite["s1"]
    if request["scenario"] != "all":
        wanted = request["scenario"].split(",")
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(set(wanted)):
            raise ValueError("unknown P4 S1 case in selection")
    seeds = set(request.get("seeds") or [])
    selection = [(case, seed) for case in cases for seed in case["seeds"] if not seeds or seed in seeds]
    if not selection:
        raise ValueError("empty P4 S1 selection")
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
              "-f", str(source / "sim/compose.p1.yaml"), "-f", str(source / "sim/compose.p4.yaml")]
    transcript, injections, snapshots, flights, appearance = [], [], [], [], []

    def compose(*args, timeout=180, check=True, extra=None, quiet=False):
        result = subprocess.run([*prefix, *args], env={**env, **(extra or {})}, capture_output=True, timeout=timeout)
        with (case / "compose.log").open("ab") as output:
            output.write((b"" if quiet else result.stdout) + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"P4 compose failed ({result.returncode}); artifacts: {case}")
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

    world = {"patched": False}

    def gz(service: str, reqtype: str, request: str) -> bool:
        """One request to the Gazebo world's own service inside the SITL container. / 对 SITL 容器内 Gazebo 世界服务的一次请求。"""
        result = compose("exec", "-T", "sitl", "gz", "service", "-s", f"/world/default/{service}", "--reqtype",
                         reqtype, "--reptype", "gz.msgs.Boolean", "--timeout", "5000", "--req", request,
                         timeout=60, check=False)
        return result.returncode == 0 and b"data: true" in result.stdout

    def place_patch(reason: str) -> None:
        if not gz("create", "gz.msgs.EntityFactory", f"sdf: '{metadata['patch_sdf']}'"):
            raise RuntimeError("the damage patch could not be placed in the Gazebo world")
        world["patched"] = True
        appearance.append({"at": now(), "asset_id": metadata["asset"], "state": "damaged", "reason": reason})
        inject("patch_placed", asset_id=metadata["asset"], reason=reason)

    def remove_patch(reason: str) -> None:
        if not gz("remove", "gz.msgs.Entity", 'name: "damage_patch" type: MODEL'):
            raise RuntimeError("the damage patch could not be removed from the Gazebo world")
        world["patched"] = False
        appearance.append({"at": now(), "asset_id": metadata["asset"], "state": "normal", "reason": reason})
        inject("patch_removed", asset_id=metadata["asset"], reason=reason)

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
                        images["ground"], "python3", "-m", "drone_agent.eval.p4_prepare", "--output", "/input",
                        "--scenario", scenario["id"], "--seed", str(seed), "--sha", sha])
        metadata = json.loads((case / "input/scenario.json").read_text())
        metadata["asset"] = metadata["inputs"]["asset"]
        (case / "control/dock.json").write_text("{}")
        compose("stop", "executive", "guardian", "uplink", "dock-sim", "mission-service", "model-proxy", "collector",
                check=False)
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "sitl")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "collector")
        wait(lambda: (case / "sensor/latest.json").exists(), 100, "Gazebo RGB frame")
        place_patch("case start")
        service_up()
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "dock-sim")
        compose("up", "-d", "--no-build", "--pull", "never", "--force-recreate", "uplink")
        wait(lambda: (dock().get("status") or {}).get("session") == "active", 60, "an active dock session")
        snapshot("before")
        roots: list[str] = []

        def start_root() -> None:
            number = len(roots) + 1
            started = api("workflows.start", project_id=PROJECT, workflow_id=metadata["workflow_id"],
                          request_id=f"{scenario['id']}-{seed}-{run_id}-{number}"[:120], inputs=metadata["inputs"])
            if not started.get("ok"):
                raise RuntimeError(f"workflow start refused: {started.get('issue')}")
            roots.append(started["result"]["run"]["run_id"])
            inject("workflow_started", run_id=roots[-1], workflow_id=metadata["workflow_id"], inspection=number)

        start_root()
        flown: set[str] = set()
        reported: set[int] = set()
        deadline = time.monotonic() + CASE_LIMIT_S
        while time.monotonic() < deadline:
            listed = orders()
            reinspections = [r["reinspection_run"] for o in listed for r in order_detail(o["order_id"]).get("rounds", [])
                             if r.get("reinspection_run")]
            views = views_of(roots + reinspections)
            settled = bool(views) and all(v["run"]["state"] in RUN_FINAL for v in views) and \
                len(roots) == metadata["inspections"]
            if settled:
                # Read again after the runs are final: every order they opened or settled is visible now.
                # 运行终结后再读一次：它们开启或结算的每张工单此时都可见。
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
            # Further inspections of the same damage start while the first review still waits (deduplication).
            # 同一损伤的后续巡检在第一次复核仍在等待时启动（去重）。
            first = next((v for v in views if v["run"]["run_id"] == roots[0]), None)
            if len(roots) < metadata["inspections"] and first and node(first, "review").get("state") == "waiting" \
                    and all(node(v, "analyze").get("state") in NODE_FINAL for v in views if v["run"]["run_id"] in roots):
                start_root()
                continue
            analysed = len(roots) == metadata["inspections"] and all(
                node(v, "analyze").get("state") in NODE_FINAL for v in views if v["run"]["run_id"] in roots)
            for view in views:
                run = view["run"]["run_id"]
                review = node(view, "review")
                if review.get("state") != "waiting" or review.get("activity") != "human_review":
                    continue
                # Only the first inspection's review decides the shared finding; the others follow it.
                # 只有第一次巡检的复核决定共享的发现；其余随之结算。
                if run in roots and (run != roots[0] or not analysed):
                    continue
                api("workflows.review", actor=REVIEWER, project_id=PROJECT, run_id=run, node_id="review",
                    decision="confirmed", request_id=f"review-{run[-10:]}-{seed}", note="S1 harness review")
            if order is not None and order["state"] in REPAIRABLE and not rounds_spent and analysed and settled:
                number = order["round"] + 1
                if number not in reported:
                    if number == metadata["remove_at_round"] and world["patched"]:
                        remove_patch(f"repair before round {number}")
                    response = api("orders.repair", project_id=PROJECT, order_id=order["order_id"],
                                   request_id=f"repair-{order['order_id'][-10:]}-r{number}",
                                   note=f"S1 harness repair feedback, round {number}")
                    if not response.get("ok"):
                        raise RuntimeError(f"repair feedback refused: {response.get('issue')}")
                    reported.add(number)
                    inject("repair_feedback", order_id=order["order_id"], round=number,
                           patched=world["patched"])
                    continue
            history = case / "inbox/history"
            pending = sorted((p for p in history.glob("m-*-v*.json") if p.name not in flown),
                             key=lambda p: p.stat().st_mtime) if history.is_dir() else []
            if pending:
                package = pending[0]
                flown.add(package.name)
                if flights:
                    M2["battery_swap"](case, compose, wait, len(flights) + 1)
                    # The swap restarts the world from its file; the damage stays until the case removes it.
                    # 换电使世界按文件重启；损伤保持到用例移除它为止。
                    if world["patched"]:
                        place_patch("restored after the battery swap")
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
            {"history": appearance, "patch_sdf_sha256": hashlib.sha256(metadata["patch_sdf"].encode()).hexdigest()},
            indent=2))
        M2["save_sitl_log"](case, compose, "sitl.log")
        compose("stop", "-t", "5", "uplink", "dock-sim", "mission-service", "model-proxy", "collector")
        compose("stop", "-t", "10", "sitl")
        for name, rows in (("api.jsonl", transcript), ("injections.jsonl", injections)):
            (case / "world" / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        (case / "world/uav_01-flights.json").write_text(json.dumps(flights, indent=2))
        HELPERS["run"](["docker", "run", "--rm", "--network", "none", "-v", f"{case}:/run", images["ground"], "python3",
                        "-m", "drone_agent.eval.p4_prepare", "--export", "/run/service/ledger.sqlite3",
                        "--output", "/run/service-export/workflows.json"])
        shutil.copyfile(case / "service-export/workflows.json", case / "service-export/business.json")
        judged = compose("run", "-T", "--rm", "--no-deps", "judge", timeout=300, check=False)
        path = case / "judge/result.json"
        result = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "judge_failed"}
        result["judge_exit_code"] = judged.returncode
        replayed = compose("run", "-T", "--rm", "--no-deps", "judge", "python3", "-m", "drone_agent.eval.judge_p4",
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


# ── S2 / frozen dataset and model evaluation / 冻结数据集与模型评测 ──


def verify_data(data: Path, manifest: Path) -> dict:
    """Re-hash every sample file against the committed manifest. / 按已提交清单重新核对每个样本文件的摘要。"""
    body = json.loads(manifest.read_text())
    bad = []
    for sample in body["samples"]:
        path = data / sample["file"]
        if not path.is_file() or HELPERS["digest"](path) != sample["sha256"]:
            bad.append(sample["sample_id"])
    return {"samples": len(body["samples"]), "mismatched": bad[:20], "status": "passed" if not bad else "failed",
            "manifest_sha256": HELPERS["digest"](manifest)}


def install_data(root: Path, deployment: Path, request: dict) -> dict:
    """Install the uploaded frozen S2 dataset read-only after a file-by-file check; never replaces an installed one.

    逐文件核对后以只读方式安装上传的冻结 S2 数据集；从不替换已安装的数据集。
    """
    manifest = deployment / "source" / MANIFEST
    if not manifest.is_file():
        raise ValueError("deploy a version with the S2 manifest first")
    target = root / "data" / DATASET
    if target.exists():
        checked = verify_data(target, manifest)
        return {"status": "installed" if checked["status"] == "passed" else "failed", "already_installed": True,
                "path": str(target), "verification": checked}
    archive = root / "incoming" / request["run_id"] / ARCHIVE
    if not archive.is_file():
        raise ValueError("upload the dataset archive first")
    if HELPERS["digest"](archive) != request.get("sha256"):
        raise ValueError("dataset archive digest mismatch")
    staging = root / "data" / f".{DATASET}-{request['run_id']}"
    staging.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("unsafe dataset archive member")
        stream.extractall(staging, filter="data")
    checked = verify_data(staging, manifest)
    if checked["status"] != "passed":
        raise RuntimeError(f"dataset verification failed; the staging copy stays at {staging}")
    for path in sorted(staging.rglob("*"), reverse=True):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    staging.rename(target)
    os.chmod(target, 0o555)
    return {"status": "installed", "already_installed": False, "path": str(target), "verification": checked,
            "archive_sha256": request["sha256"]}


def run_s2(root: Path, deployment: Path, request: dict) -> dict:
    """One S2 run of one split in one mode, then its calibration table, metrics or replay comparison.

    一个拆分以一种模式的一次 S2 运行，随后给出校准表、指标或回放比较。
    """
    sha, tag, checks = revision(deployment)
    source = deployment / "source"
    if not (source / "sim/compose.p4.yaml").is_file() or not (source / MANIFEST).is_file():
        raise ValueError("deploy a version with the P4 S2 evaluation first")
    split, mode = request.get("split"), request.get("mode")
    if split not in ("calibration", "test") or mode not in ("live", "replay", "scripted", "retrieval"):
        raise ValueError("S2 needs split calibration|test and mode live|replay|scripted|retrieval")
    if mode == "retrieval":
        if split != "test":
            raise ValueError("the retrieval report reads the test split")
        return run_retrieval(root, deployment, request)
    threshold = request.get("threshold")
    if threshold is not None and not (isinstance(threshold, (int, float)) and 0.0 < float(threshold) < 1.0):
        raise ValueError("threshold must be in (0, 1)")
    profile = request.get("profile") or PROFILE
    if not PROFILES.fullmatch(profile) or not (source / profile).is_file():
        raise ValueError("profile must be a committed configs/analysis/vlm_change_v<N>.yaml")
    data = root / "data" / DATASET
    if not data.is_dir():
        raise ValueError("install the S2 dataset first (dev_stack.py p4-data)")
    model = root / "secrets" / "m2-model"
    if mode == "live" and not (model / "minimax.key").is_file():
        raise ValueError("store the model key first (dev_stack.py m2-key)")
    replay_of = None
    if mode == "replay":
        found = REPLAY_SOURCE.fullmatch(request.get("replay_of") or "")
        if not found:
            raise ValueError("replay needs replay_of=<deployment id>/p4-<run id> of a live run")
        replay_of = root / "artifacts" / found.group(1) / found.group(2) / "s2"
        header = json.loads((replay_of / "run/run.json").read_text())
        if header["mode"] != "live" or header["split"] != split:
            raise ValueError("the replayed run must be a live run of the same split")
    base = root / "artifacts" / deployment.name / ("p4-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    output = base / "s2"
    output.mkdir()
    images = M2["build_images"](source, base, tag, checks)
    checked = verify_data(data, source / MANIFEST)
    if checked["status"] != "passed":
        raise RuntimeError("the installed S2 dataset no longer matches the manifest")
    if replay_of is not None:
        shutil.copytree(replay_of / "run", output / "source")
    arguments = ["python3", "-m", "drone_agent.eval.p4_s2", "--root", "/workspace", "run", "--manifest",
                 f"/workspace/{MANIFEST}", "--data", "/data", "--split", split, "--mode", mode, "--output",
                 "/output/run", "--concurrency", str(int(request.get("concurrency") or 3)), "--profile", profile]
    if threshold is not None:
        arguments += ["--threshold", str(float(threshold))]
    if request.get("limit"):
        arguments += ["--limit", str(int(request["limit"]))]
    if replay_of is not None:
        arguments += ["--recordings", "/output/source/recordings"]
    if request.get("price_source"):
        arguments += ["--currency", str(request.get("currency") or "CNY"), "--price-input",
                      str(float(request["price_input"])), "--price-output", str(float(request["price_output"])),
                      "--price-source", str(request["price_source"])[:200]]
    started = time.monotonic()
    log = base / "s2-run.log"
    if mode == "live":
        # The only route to the model endpoint is the allowlisted proxy on the internal network (D036).
        # 到模型端点的唯一路径是内部网络上的白名单代理（D036）。
        env = dict(os.environ, DRONE_SOURCE_SHA=sha, DRONE_M2_RUN=str(base / "idle"), DRONE_M2_SECRETS=str(
            root / "secrets" / "m2"), DRONE_M2_MODEL=str(model), DRONE_M2_PLANNER="scripted",
            DRONE_M2_SIM_IMAGE=images["sim"], DRONE_M2_AIRCRAFT_IMAGE=images["aircraft"],
            DRONE_M2_GROUND_IMAGE=images["ground"], DRONE_M2_PACKAGE="none.json", DRONE_M2_VERSION="0",
            DRONE_M2_EPOCH="0", DRONE_M2_FLIGHT=str(base / "idle"), DRONE_P4_DATA=str(data),
            DRONE_P4_OUTPUT=str(output), DRONE_P4_UID=str(os.getuid()), DRONE_P4_GID=str(os.getgid()))
        prefix = ["docker", "compose", "-p", "drone-agent-cloud", "-f", str(source / "sim/compose.m2.yaml"),
                  "-f", str(source / "sim/compose.p1.yaml"), "-f", str(source / "sim/compose.p4.yaml"),
                  "--profile", "s2"]
        with log.open("wb") as stream:
            result = subprocess.run([*prefix, "run", "-T", "--rm", "s2-eval", *arguments], env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=4 * 3600)
        subprocess.run([*prefix, "stop", "-t", "5", "model-proxy"], env=env, capture_output=True, timeout=120)
    else:
        with log.open("wb") as stream:
            result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs",
                                     "/tmp:size=64m,mode=1777", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                                     "--user", f"{os.getuid()}:{os.getgid()}",
                                     "--memory", "1g", "--cpus", "1.0", "-e", f"DRONE_SOURCE_SHA={sha}",
                                     "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", f"{data}:/data:ro",
                                     "-v", f"{output}:/output", images["ground"], *arguments],
                                    stdout=stream, stderr=subprocess.STDOUT, timeout=4 * 3600)
    summary = {"layer": "S2", "split": split, "mode": mode, "source_sha": sha, "deployment_id": deployment.name,
               "artifact_directory": str(base), "run_exit_code": result.returncode,
               "duration_s": round(time.monotonic() - started, 1), "data": checked,
               "replay_of": request.get("replay_of"),
               "images": {"ground": HELPERS["inspect_image"](images["ground"])["Id"]}}
    header = output / "run/run.json"
    if result.returncode or not header.is_file():
        summary["status"] = "failed"
        summary["error"] = "the S2 run did not finish; see s2-run.log"
        (base / "s2.json").write_text(json.dumps(summary, indent=2))
        return summary
    summary["run"] = json.loads(header.read_text())

    def evaluate(*command: str) -> dict:
        found = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                                "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{output}:/output:ro",
                                images["ground"], "python3", "-m", "drone_agent.eval.p4_s2",
                                "--root", "/workspace", *command], capture_output=True, timeout=600)
        if found.returncode:
            raise RuntimeError(f"S2 {command[0]} failed: {found.stderr.decode(errors='replace')[-800:]}")
        return json.loads(found.stdout)

    protocol = f"/workspace/{PROTOCOL}"
    if split == "calibration":
        summary["calibration"] = evaluate("calibrate", "--results", "/output/run", "--protocol", protocol)
        summary["status"] = "completed"
    else:
        summary["metrics"] = evaluate("metrics", "--results", "/output/run", "--protocol", protocol)
        summary["status"] = summary["metrics"]["status"]
    if replay_of is not None:
        summary["replay"] = evaluate("compare", "--live", "/output/source", "--replay", "/output/run")
        if summary["replay"]["status"] != "passed":
            summary["status"] = "failed"
    for name in ("calibration", "metrics", "replay"):
        if name in summary:
            (base / f"s2-{name}.json").write_text(json.dumps(summary[name], indent=1))
    (base / "s2.json").write_text(json.dumps(summary, indent=2))
    return summary


def run_retrieval(root: Path, deployment: Path, request: dict) -> dict:
    """The S2 retrieval report (D065 §7) in its own no-network image: the M3 detector's ONNX Runtime and pinned CLIP.

    在独立的无网络镜像中生成 S2 检索报告（D065 §7）：M3 检测的 ONNX Runtime 与固定 CLIP。
    """
    sha, tag, checks = revision(deployment)
    source = deployment / "source"
    data = root / "data" / DATASET
    if not data.is_dir():
        raise ValueError("install the S2 dataset first (dev_stack.py p4-data)")
    base = root / "artifacts" / deployment.name / ("p4-" + request["run_id"])
    base.mkdir(parents=True, exist_ok=False)
    output = base / "s2"
    output.mkdir()
    checked = verify_data(data, source / MANIFEST)
    if checked["status"] != "passed":
        raise RuntimeError("the installed S2 dataset no longer matches the manifest")
    image = f"drone-agent-p4-retrieval:{tag}"
    if not HELPERS["inspect_image"](image):
        with (base / "build-retrieval.log").open("wb") as log:
            built = subprocess.run(["docker", "build", "--pull=false", "--target", "retrieval", "-f",
                                    str(source / "sim/m3.Dockerfile"), "--build-arg", f"CHECKS_IMAGE={checks}",
                                    "-t", image, str(source)], stdout=log, stderr=subprocess.STDOUT, timeout=5400)
        if built.returncode:
            raise RuntimeError(f"the retrieval image build failed: {base}")
    started = time.monotonic()
    with (base / "s2-run.log").open("wb") as log:
        result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs",
                                 "/tmp:size=64m,mode=1777", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                                 "--user", f"{os.getuid()}:{os.getgid()}",
                                 "--memory", "2g", "--cpus", "2.0", "-e", "PYTHONDONTWRITEBYTECODE=1",
                                 "-v", f"{data}:/data:ro", "-v", f"{output}:/output", image, "python3", "-m",
                                 "drone_agent.eval.p4_retrieval", "--manifest", f"/workspace/{MANIFEST}",
                                 "--data", "/data", "--config", f"/workspace/eval/s2/{DATASET}/retrieval_queries_v1.yaml",
                                 "--queries", "/opt/da_edge/retrieval_queries.json", "--model",
                                 "/opt/da_edge/model/vision_model_quantized.onnx", "--output", "/output/retrieval.json"],
                                stdout=log, stderr=subprocess.STDOUT, timeout=3600)
    summary = {"layer": "S2", "split": "test", "mode": "retrieval", "source_sha": sha,
               "deployment_id": deployment.name, "artifact_directory": str(base), "run_exit_code": result.returncode,
               "duration_s": round(time.monotonic() - started, 1), "data": checked,
               "images": {"retrieval": HELPERS["inspect_image"](image)["Id"]}}
    report = output / "retrieval.json"
    if result.returncode or not report.is_file():
        summary.update(status="failed", error="the retrieval report did not finish; see s2-run.log")
    else:
        value = json.loads(report.read_text())
        summary.update(status="completed", retrieval={k: value[k] for k in (
            "corpus", "queries", "text_to_image", "image_to_reference", "threshold", "query_set", "query_set_sha256",
            "manifest_sha256", "model", "onnxruntime")})
    (base / "s2.json").write_text(json.dumps(summary, indent=2))
    return summary
