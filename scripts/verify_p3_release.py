"""P3 gate: multi-site, multi-robot scheduling on one immutable candidate (WP-P3-06, D059, D060, D061).

Criteria, all bound to the same full commit:
  scope             — changes since the P2 candidate stay in P3 paths; if the onboard runtime, wire, skills, platform
                      or recovery policies changed, the M1 regression becomes required and is not waived here
  checks            — the cloud deployment receipt: the whole suite on Linux with 0 failures, errors and skips;
                      contract fields and the v1 wire freeze re-checked
  adversarial       — the M2 corpora still authorize nothing and the workflow-draft corpus produces no active,
                      escaping or review-skipping draft
  s0_matrix         — run here, on the clean candidate: P3-F01..F15 x 3 seeds in the S0 world with the independent
                      judge online and from the recordings; double ownership, duplicate execution, lost tasks,
                      conflicting reservations, false success, stale-epoch effects, decision mismatches, project
                      escapes, wrong dispatch, duplicate dispatch and wrong release all 0
  s0_ladder         — run here: 10 / 30 / 100 logical nodes (never aircraft), every rung judged with the same counts at
                      0; latency, wait and rejection distributions reported per rung, never pooled
  p1_regression     — run here: the P1 S0 matrix, since the service, dispatch and resources changed
  p2_regression     — run here: the P2 S0 matrix, since the workflow engine gained the assignment mode
  capacity          — D061: the S1 receipts ran under the frozen budget, the idle probe was taken, and every case kept
                      the real-time factor >= 0.5 and both guardians' supervision p99 <= 120 ms
  s1                — remote_p3 receipts: every S1 case x seed on two PX4 SITL aircraft in one world passed its judge
                      online and from the recordings with the same counts at 0, both robots' certificates recorded and
                      foreign containers unchanged
  p2_s1_regression  — remote_p2 receipts on this candidate, since the service and the workflow engine changed
  m2_regression     — the M2 end-to-end suite (18 cases) passed on this candidate
  desk              — the resident desk activated on this candidate with the P1 catalog, the P2 workflow catalog and
                      the P3 desk scheduling catalog, three drills passed and three migrations happened; page, script
                      and health match; the scheduling entry shows the queue, each robot's verdict with its reasons and
                      the offered assets, and refuses injection, control and assignment frames
  desk_session      — one task through the desk was assigned by the scheduler, approved by a person, claimed through the
                      gate, released by reconciliation, completed and independently judged
  historical        — the M3, P0, P1 and P2 release records are byte-identical to their closing commits
A criterion without evidence is `missing`, never `passed`; S0, the ladder and S1 are counted separately; the ladder's
nodes are logical and never physical aircraft, and S1 proves two simulated aircraft, not a fleet.

P3 门禁：在同一不可变候选上核对多站多机调度（WP-P3-06，D059、D060、D061）。判据全部绑定同一完整提交：scope（相对 P2 候选
的改动留在 P3 路径内；若机载运行时、wire、技能、平台或恢复策略改变则必须补 M1 回归，这里不豁免）、checks（云端全量 0 失败
0 错误 0 跳过，复核契约字段与 v1 wire 冻结）、adversarial（M2 语料不授权任何任务包，工作流草案语料没有生效、越权或跳过复核的
草案）、s0_matrix（在干净候选上当场运行 P3-F01..F15 × 3 种子，独立裁判在线与按录制各判一次，各项计数全部为 0）、s0_ladder
（当场运行 10 / 30 / 100 逻辑节点阶梯，从不算飞行器；每档同样计数为 0，按档分开报告延迟、等待与拒绝分布）、p1_regression
与 p2_regression（当场重跑 P1 / P2 S0 矩阵）、capacity（D061：S1 回执在冻结预算下运行、做过空闲探针，每个用例实时因子 ≥ 0.5、
两个 guardian 监督周期 p99 ≤ 120 ms）、s1（remote_p3 回执：同一世界两架 PX4 SITL 上每个用例 × 种子在线与回放通过、计数为 0、
两台机器人的证书已记录、其他容器不变）、p2_s1_regression（本候选上的 remote_p2 回执）、m2_regression（M2 端到端 18 例）、desk
（常驻任务台以 P1 目录、P2 工作流目录与 P3 任务台调度目录在本候选激活，三次演练通过、三次迁移发生；页面、脚本与健康一致；调度
入口显示队列、各机器人判定及其原因与可选资产，并拒绝注入、控制与分配帧）、desk_session（一个经任务台的任务单由调度器分配、
由人审批、经领取闸门、对账释放、完成并有独立裁判）、historical（M3、P0、P1 与 P2 的发布记录与其关闭提交逐字节一致）。没有证据
的判据是 `missing`，绝不是 `passed`；S0、阶梯与 S1 分开计数；阶梯节点是逻辑节点，从不算物理飞行器，S1 证明的是两架仿真飞行器，
不是机队。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

P2_SHA = "3bdbd503325d292efa298f28b6292dd2bf341cc0"
P2_CLOSE = "8e9645f"
M3_PATH = "docs/verification/m3-2026-09-25/release.json"
P0_PATH = "docs/verification/p0-2026-09-25-r2/release.json"
P1_PATH = "docs/verification/p1-2026-09-26/release.json"
P2_PATH = "docs/verification/p2-2026-09-26/release.json"
M2 = runpy.run_path(str(ROOT / "scripts/verify_m2_release.py"))
P2 = runpy.run_path(str(ROOT / "scripts/verify_p2_release.py"))
DOCS = P2["DOCS"]
ONBOARD = P2["ONBOARD"]
# sim/collect.py only gained an optional camera topic (default unchanged); the M2 regression exercises it.
# sim/collect.py 只增加可选相机话题（默认不变）；M2 回归覆盖它。
P3_PATHS = ("src/drone_agent/fleet/", "src/drone_agent/console/", "src/drone_agent/eval/",
            "src/drone_agent/admission/models.py", "src/drone_agent/runtime/issues.py", "configs/scheduling/",
            "configs/sites/p3_", "configs/scenarios/p3_", "configs/workflows/p3_", "scripts/", "sim/compose.p3.yaml",
            "sim/compose.desk.yaml", "sim/p3.Dockerfile", "sim/p3_setup.py", "sim/p3_sitl.sh", "sim/collect.py",
            "tests/", "pyproject.toml", "uv.lock")
COUNTS = ("double_ownership", "duplicate_execution", "lost_task", "conflicting_reservation", "false_success",
          "stale_epoch_effect", "decision_mismatch", "project_escape", "wrong_dispatch", "duplicate_dispatch",
          "wrong_release")
# D061's frozen budget for the two-aircraft SITL container; the receipts must have run under it.
# D061 为双机 SITL 容器冻结的预算；回执必须在此预算下运行。
FROZEN_BUDGET = {"sitl_cpus": "2.5", "sitl_mem": "3g"}
FINGERPRINTS = {"ca", "service", "robot", "robot-tls-uav_02"}
TASK_REFUSALS = 8


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def scope(sha: str, m1: list[dict]) -> dict:
    if subprocess.run(["git", "merge-base", "--is-ancestor", P2_SHA, sha], cwd=ROOT).returncode:
        return {"status": "failed", "reason": "candidate does not descend from the P2 candidate"}
    changed = git("diff", "--name-only", P2_SHA, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *P3_PATHS, *ONBOARD))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": P2_SHA, "changed": len(changed), "outside_p3_scope": outside, "onboard_changed": onboard}
    if outside:
        return {"status": "failed", **result}
    if onboard:
        regression = M2["m1_regression"](m1, sha)
        return {"status": regression["status"], "m1_regression": regression, **result}
    return {"status": "passed", "m1_regression": "not_required_onboard_runtime_wire_and_policies_unchanged", **result}


def summarize(summary: dict, expected: set, path: Path) -> dict:
    seen = {(r["scenario"], r["seed"]) for r in summary["results"]}
    problems = [] if seen == expected else ["case_coverage_mismatch"]
    problems += [f"{r['scenario']}-{r['seed']}:{r['classification']}" for r in summary["results"] if not r["passed"]]
    counts = summary["counts"]
    if set(counts) != set(COUNTS) or any(counts.values()):
        problems.append("nonzero_or_missing_safety_counts")
    return {"status": "passed" if not problems and summary["status"] == "passed" else "failed",
            "cases": summary["cases"], "expected": len(expected), "passed": summary["passed"], "counts": counts,
            "voided_attempts": len(summary["voided_attempts"]), "problems": problems,
            "summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def s0_matrix(sha: str, output: Path) -> dict:
    """Run the P3 S0 matrix here on the clean candidate. / 在干净候选上当场运行 P3 S0 矩阵。"""
    from drone_agent.eval.p3_world import run_suite, suite

    definition = suite(ROOT)
    expected = {(case["id"], seed) for case in definition["s0"] for seed in definition["seeds"]}
    summary = asyncio.run(run_suite(output, ROOT, sha=sha, part="s0"))
    return summarize(summary, expected, output / "suite-s0.json")


def s0_ladder(sha: str, output: Path) -> dict:
    """Run the scale ladder here and report each rung's distributions apart. / 当场运行规模阶梯并按档分开报告分布。"""
    from drone_agent.eval.p3_world import run_suite, suite

    definition = suite(ROOT)
    expected = {(rung["id"], seed) for rung in definition["ladder"] for seed in rung["seeds"]}
    summary = asyncio.run(run_suite(output, ROOT, sha=sha, part="ladder"))
    result = summarize(summary, expected, output / "suite-ladder.json")
    result["rungs"] = {r["scenario"]: {k: (r.get("metrics") or {}).get(k) for k in (
        "tasks", "decisions", "decision_ms", "assignment_latency_s", "completion_s", "wait_reasons", "reject_reasons",
        "live_tasks_max", "airborne_max", "flights", "ladder")} for r in summary["results"]}
    result["physical_aircraft"] = 0
    return result


def capacity(receipts: list[dict]) -> dict:
    """D061's limits in every S1 case under the frozen budget, with the idle probe taken. / D061 限值与冻结预算。"""
    rows = [row for receipt in receipts for row in receipt.get("results", [])]
    if not rows:
        return {"status": "missing"}
    problems = []
    if any(receipt.get("budget") != FROZEN_BUDGET for receipt in receipts):
        problems.append("receipt_not_under_the_frozen_budget")
    if not any((row.get("capacity") or {}).get("idle") for row in rows):
        problems.append("idle_probe_missing")
    verdicts = {f"{row.get('scenario')}-{row.get('seed')}": (row.get("capacity") or {}).get("verdict") or {}
                for row in rows}
    for case, verdict in verdicts.items():
        if verdict.get("sufficient") is not True:
            problems.append(f"{case}:capacity_insufficient_or_unmeasured")
    factors = [v["real_time_factor_min"] for v in verdicts.values() if v.get("real_time_factor_min") is not None]
    periods = [v["supervision_p99_max_s"] for v in verdicts.values() if v.get("supervision_p99_max_s") is not None]
    return {"status": "failed" if problems else "passed", "problems": problems, "budget": FROZEN_BUDGET,
            "cases": len(verdicts), "real_time_factor_min": min(factors, default=None),
            "supervision_p99_max_s": max(periods, default=None)}


def s1(receipts: list[dict], sha: str) -> dict:
    if not receipts:
        return {"status": "missing"}
    suite = yaml.safe_load(subprocess.check_output(["git", "show", f"{sha}:configs/scenarios/p3_suite.yaml"],
                                                   cwd=ROOT).decode("utf-8"))
    expected = {(case["id"], seed) for case in suite["s1"] for seed in case["seeds"]}
    results, problems = {}, []
    for receipt in receipts:
        if receipt.get("source_sha") != sha or receipt.get("status") != "passed" or receipt.get("layer") != "S1" \
                or receipt.get("aircraft") != 2:
            problems.append("a receipt failed, belongs to another revision or is not a two-aircraft S1 run")
        if receipt.get("other_containers_before") != receipt.get("other_containers_after"):
            problems.append("shared-server isolation not verified")
        if not receipt.get("signer_key_id") or set(receipt.get("certificate_fingerprints", {})) != FINGERPRINTS:
            problems.append("signing key id or both robots' certificate fingerprints not recorded")
        for row in receipt.get("results", []):
            key = row.get("scenario"), row.get("seed")
            if key not in expected or key in results:
                problems.append(f"unexpected or duplicate case {key}")
                continue
            counts = row.get("counts") or {}
            if not (row.get("passed") is True and row.get("replay_agrees") is True and row.get("judge_exit_code") == 0
                    and row.get("problems") == [] and row.get("source_sha") == sha and row.get("layer") == "S1"
                    and set(counts) == set(COUNTS) and not any(counts.values())):
                problems.append(f"case {key} did not meet the independent criteria")
            results[key] = row
    missing = sorted(expected - set(results))
    flown = {robot for r in results.values() for robot, flights in (r.get("flown") or {}).items() if flights}
    if results and flown != {"uav_01", "uav_02"}:
        problems.append("the matrix did not fly both aircraft")
    return {"status": "passed" if not problems and not missing else "failed", "cases": len(results),
            "expected": len(expected), "missing": missing, "problems": problems,
            "counts": {k: sum((r.get("counts") or {}).get(k, 0) for r in results.values()) for k in COUNTS},
            "flights": sum(len(f) for r in results.values() for f in (r.get("flown") or {}).values()),
            "aircraft": sorted(flown)}


def desk(activation: dict | None, http: dict | None, probe: dict | None, sha: str) -> dict:
    if activation is None or http is None or probe is None:
        return {"status": "missing", "reason": "desk activation, HTTP and task probe receipts are required"}
    problems = []
    if activation.get("status") != "verified" or activation.get("source_sha") != sha:
        problems.append("desk_not_activated_on_candidate")
    for name, catalog in (("operations", "p1_s1_v1"), ("workflows", "p2_s1_v1"), ("scheduling", "p3_desk_v1")):
        part = activation.get(name) or {}
        if part.get("catalog_id") != catalog:
            problems.append(f"desk_without_{name}_catalog")
        if (part.get("migration") or {}).get("status") not in ("migrated", "current"):
            problems.append(f"desk_without_{name}_migration")
        if (part.get("drill") or {}).get("status") not in ("passed", "not_applicable"):
            problems.append(f"{name}_migration_drill_not_passed")
    containers = activation.get("containers") or {}
    if "desk-dock" not in containers or any(c.get("docker_access") for c in containers.values()):
        problems.append("dock_backend_missing_or_privileged")
    health = http.get("health") or {}
    if not ((http.get("page") or {}).get("status") == 200 and (http.get("script") or {}).get("matches_checkout")
            and health.get("ok") and health.get("console_source_sha") == sha == health.get("service_source_sha")):
        problems.append("page_script_or_health_mismatch")
    projects = {p.get("project_id"): p for p in probe.get("projects") or []}
    if (projects.get(probe.get("project")) or {}).get("scheduling") is not True:
        problems.append("project_not_offered_for_scheduling")
    queue = probe.get("queue") or {}
    if queue.get("error") or (queue.get("catalog") or {}).get("catalog_id") != "p3_desk_v1":
        problems.append("queue_missing")
    if not {"operator", "approver"} <= set(queue.get("roles") or []):
        problems.append("operator_approver_roles_missing")
    robots = queue.get("robots") or []
    if not robots or any("verdict" not in (r.get("eligibility") or {}) or "reasons" not in (r.get("eligibility") or {})
                         for r in robots):
        problems.append("robot_verdicts_or_reasons_missing")
    if not queue.get("assets"):
        problems.append("offered_assets_missing")
    refused = probe.get("refused") or []
    if len(refused) < TASK_REFUSALS or any("unknown type" not in (r.get("error") or "") for r in refused):
        problems.append("injection_control_or_assignment_frames_not_refused")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "catalogs": [(activation.get(n) or {}).get("catalog_id") for n in ("operations", "workflows", "scheduling")],
            "migrations": [((activation.get(n) or {}).get("migration") or {}).get("status")
                           for n in ("operations", "workflows", "scheduling")],
            "robots": [r.get("robot_id") for r in robots]}


def desk_session(receipt: dict | None, flights: list[dict] | None, sha: str) -> dict:
    if receipt is None or flights is None:
        return {"status": "missing", "reason": "a desk task run and the supervisor's flight.json records"}
    problems = []
    if receipt.get("errors"):
        problems.append("desk_probe_reported_errors")
    task = (receipt.get("task") or {}).get("task") or {}
    decisions = (receipt.get("task") or {}).get("decisions") or []
    if task.get("state") != "completed" or task.get("robot_id") != "uav_01" or task.get("source") != "operator":
        problems.append("task_not_completed_by_the_desk_robot")
    if not any(d.get("verdict") == "assign" and d.get("robot_id") == "uav_01" for d in decisions):
        problems.append("no_scheduler_assignment_recorded")
    approvals = [s for s in receipt.get("sent") or [] if s.get("action") == "approve"]
    if not approvals:
        problems.append("no_human_approval")
    missions = receipt.get("missions") or {}
    recorded = {(f.get("mission_id"), f.get("version")): f for f in flights}
    public_flights, completed = 0, 0
    for mission_id, mission in missions.items():
        binding = mission.get("binding") or {}
        dispatch = mission.get("dispatch") or {}
        judge = (mission.get("cloud") or {}).get("judge") or {}
        if (binding.get("project_id"), binding.get("robot_id"), binding.get("dock_id")) != (
                "campus_s1", "uav_01", "dock_s1"):
            problems.append(f"{mission_id}:not_bound_to_the_desk_site")
        if (mission.get("request") or {}).get("channel") != "scheduler":
            problems.append(f"{mission_id}:not_a_scheduler_mission")
        if mission.get("status") != "completed":
            continue
        completed += 1
        if not dispatch.get("claims") or any(c.get("state") != "claimed" for c in dispatch["claims"]):
            problems.append(f"{mission_id}:not_claimed_through_the_gate")
        if not dispatch.get("reservations") or any(r.get("state") != "released" or r.get("reason") != "reconciled"
                                                   for r in dispatch["reservations"]):
            problems.append(f"{mission_id}:hold_not_released_by_reconciliation")
        if not (judge.get("passed") is True and judge.get("replay_agrees") is True
                and judge.get("false_success_reports") == 0 and judge.get("problems") == []):
            problems.append(f"{mission_id}:not_independently_verified")
        for flight in (mission.get("cloud") or {}).get("flights") or []:
            public_flights += 1
            own = recorded.get((mission_id, flight.get("version")))
            if own is None or own.get("source_sha") != sha or any(own.get(k) != flight.get(k) for k in (
                    "version", "status", "epoch", "started_at", "ended_at", "manual_cleanup")):
                problems.append(f"{mission_id}:authoritative_flight_binding_mismatch")
    if completed != 1 or public_flights < 1:
        problems.append("the_task_did_not_complete_exactly_one_judged_mission")
    return {"status": "failed" if problems else "passed", "problems": problems, "missions": len(missions),
            "flights": public_flights, "task_state": task.get("state"), "epoch": task.get("epoch")}


def historical() -> dict:
    """Closed milestones' release records are inputs, never current results. / 已关闭里程碑的记录是输入，不是当前结果。"""
    result, problems = {}, []
    for label, path, commit in (("m3", M3_PATH, "9223d74"), ("p0", P0_PATH, "6d60f67"), ("p1", P1_PATH, "a9de358"),
                                ("p2", P2_PATH, P2_CLOSE)):
        original = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT)
        current = (ROOT / path).read_bytes()
        value = json.loads(original)
        result[label] = {"sha256": hashlib.sha256(original).hexdigest(), "status": value.get("status"),
                         "source_sha": value.get("source_sha"), "counts_as_candidate_validation": False}
        if current != original:
            problems.append(f"{label}_record_changed")
    m3 = json.loads((ROOT / M3_PATH).read_bytes())
    if m3.get("m3_sitl") != "passed" or m3.get("status") != "not_passed" or \
            m3.get("criteria", {}).get("jil", {}).get("status") != "missing":
        problems.append("m3_boundaries_changed")
    if any(result[label]["status"] != "passed" for label in ("p0", "p1", "p2")):
        problems.append("p0_p1_or_p2_not_closed")
    return {"status": "failed" if problems else "passed", "problems": problems, **result}


def capabilities(sha: str, criteria: dict) -> list[dict]:
    passed = all(item["status"] == "passed" for item in criteria.values())
    return [
        {"capability": "multi_site_scheduling_v1", "milestone": "P3", "implemented": True,
         "software_validation": "passed" if passed else "not_passed", "source_sha": sha,
         "layers": {"S0": {"scenarios": 15, "seeds": 3, "logical_uavs": 5, "physical_flight": False},
                    "ladder": {"logical_nodes": [10, 30, 100], "physical_aircraft": 0},
                    "S1": {"px4_sitl_aircraft": 2, "same_world": True, "logical_docks": 2}},
         "airspace": "simulated grid cells and lost-contact envelopes; no real airspace filing",
         "coordination": "deconfliction by exclusive cells; close-proximity cooperation is not claimed",
         "hardware_validation": "not_applicable"},
        {"milestone": "P2", "software_validation": "historical_passed", "source_sha": P2_SHA,
         "counts_as_candidate_validation": False},
        {"milestone": "M3", "status": "not_passed", "reason": "JIL missing; not a P3 dependency"},
        *({"milestone": name, "status": "missing", "hardware_validation": "missing"} for name in ("H1", "H2", "H3")),
        *({"milestone": name, "status": "planned", "software_validation": "missing"}
          for name in ("P4", "P5", "X1", "X2", "X3")),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--s1", type=Path, nargs="*", default=[])
    parser.add_argument("--p2-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--m2", type=Path, nargs="*", default=[])
    parser.add_argument("--m1", type=Path, nargs="*", default=[], help="only needed if onboard code changed")
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-tasks", type=Path, help="desk_probe.py tasks receipt (queue, reasons, refusals)")
    parser.add_argument("--desk-session", type=Path, help="desk_probe.py tasks --start receipt")
    parser.add_argument("--desk-flights-dir", type=Path, help="the supervisor's flight.json records of that run")
    parser.add_argument("--s0-output", type=Path, default=ROOT / "outputs" / "p3-s0")
    parser.add_argument("--ladder-output", type=Path, default=ROOT / "outputs" / "p3-ladder-gate")
    parser.add_argument("--p1-output", type=Path, default=ROOT / "outputs" / "p3-p1-regression")
    parser.add_argument("--p2-output", type=Path, default=ROOT / "outputs" / "p3-p2-regression")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    head = git("rev-parse", "HEAD")
    if subprocess.run(["git", "merge-base", "--is-ancestor", args.sha, head], cwd=ROOT).returncode:
        parser.error("HEAD must be the candidate or its documentation-only descendant")
    newer = git("diff", "--name-only", args.sha, head).splitlines()
    if any(not p.startswith(DOCS) for p in newer) or git(
            "status", "--porcelain", "--", "src", "tests", "scripts", "configs", "proto", "sim", "ros2_ws",
            "pyproject.toml", "uv.lock"):
        parser.error("gate implementation and runtime must match the clean candidate")
    inputs = [p for p in [args.deployment, args.desk, args.desk_http, args.desk_tasks, args.desk_session,
                          *args.s1, *args.p2_s1, *args.m2, *args.m1] if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    flights = [load(p) for p in sorted(args.desk_flights_dir.glob("*.json"))] if args.desk_flights_dir else None
    s1_receipts = [load(p) for p in args.s1]
    criteria = {
        "scope": scope(args.sha, [load(p) for p in args.m1]),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "adversarial": P2["adversarial"](),
        "s0_matrix": s0_matrix(args.sha, args.s0_output),
        "s0_ladder": s0_ladder(args.sha, args.ladder_output),
        "p1_regression": P2["p1_regression"](args.sha, args.p1_output),
        "p2_regression": P2["s0_matrix"](args.sha, args.p2_output),
        "capacity": capacity(s1_receipts),
        "s1": s1(s1_receipts, args.sha),
        "p2_s1_regression": P2["s1"]([load(p) for p in args.p2_s1], args.sha),
        "m2_regression": M2["e2e"]([load(p) for p in args.m2], args.sha),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_tasks), args.sha),
        "desk_session": desk_session(load(args.desk_session), flights, args.sha),
        "historical": historical(),
    }
    paths = inputs + ([*sorted(args.desk_flights_dir.glob("*.json"))] if args.desk_flights_dir else [])
    digests = {p.relative_to(args.output.parent).as_posix() if p.is_relative_to(args.output.parent) else p.name:
               hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {"schema_version": "0.1.0", "milestone": "P3", "source_sha": args.sha, "records_sha": head,
              "status": "passed" if all(v["status"] == "passed" for v in criteria.values()) else "not_passed",
              "criteria": criteria, "capabilities": capabilities(args.sha, criteria), "inputs": digests}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
