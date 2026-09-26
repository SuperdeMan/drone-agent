"""P2 gate: durable workflows on one immutable candidate (WP-P2-08, D057, D058).

Criteria, all bound to the same full commit:
  scope          — changes since the P1 candidate stay in P2 paths; if the onboard runtime, wire, skills, platform or
                   recovery policies changed, the M1 regression becomes required and is not waived here
  checks         — the cloud deployment receipt: the whole suite on Linux with 0 failures, errors and skips; contract
                   fields and the v1 wire freeze re-checked
  adversarial    — the M2 deterministic and scripted natural-language corpora still authorize nothing, and the
                   workflow-draft corpus produces no active, escaping or review-skipping draft
  s0_matrix      — run here, on the clean candidate: P2-F01..F15 x 3 seeds in the S0 world with the independent judge
                   online and from the recordings; duplicate dispatch, post-cancel dispatch and successors, false
                   success, false orders, project escape, lost runs, wrong dispatch and wrong release all 0
  p1_regression  — run here: the P1 S0 matrix (P1-F01..F14 x 3) still passes, since the service and dispatch changed
  s1             — remote_p2 receipts: every S1 case x seed passed its judge online and from the recordings with the
                   same counts at 0 and unchanged foreign containers
  m2_regression  — the M2 end-to-end suite (18 cases) passed on this candidate, since the service changed
  desk           — the resident desk activated on this candidate with the P1 catalog and the P2 workflow catalog, both
                   drills passed and both migrations happened; page, script and health match; the workflow entry shows
                   the templates and roles, returns an inactive draft and refuses injection, control and activation
  desk_session   — one workflow through the desk reached a simulated work order and a completed reinspection run,
                   every mission bound, claimed through the gate, released by reconciliation and judged
  historical     — the M3, P0 and P1 release records are byte-identical to their closing commits
A criterion without evidence is `missing`, never `passed`; S0 and S1 are counted separately and never as physical
flights of several aircraft; scripted analysis never counts as recognition quality.

P2 门禁：在同一不可变候选上核对持久工作流（WP-P2-08，D057，D058）。判据全部绑定同一完整提交：scope（相对 P1 候选的改动留在
P2 路径内；若机载运行时、wire、技能、平台或恢复策略改变，则必须补 M1 回归，这里不豁免）、checks（云端全量 0 失败 0 错误
0 跳过，并复核契约字段与 v1 wire 冻结）、adversarial（M2 语料仍不授权任何任务包，工作流草案语料没有生效、越权或跳过复核的
草案）、s0_matrix（在干净候选上当场运行 P2-F01..F15 × 3 种子，独立裁判在线与按录制各判一次，各项计数全部为 0）、
p1_regression（服务与派遣已改，当场重跑 P1 S0 矩阵）、s1（remote_p2 回执全部通过、计数为 0、其他容器不变）、
m2_regression（M2 端到端 18 例在本候选通过）、desk（常驻任务台以 P1 目录与 P2 工作流目录在本候选激活，两次演练通过、两次
迁移发生；页面、脚本与健康一致；工作流入口显示模板与角色、返回未生效草案并拒绝注入、控制与激活帧）、desk_session（一次经
任务台的工作流到达模拟工单与完成的复检运行，每个任务都经绑定、领取闸门、对账释放并有裁判结果）、historical（M3、P0 与 P1
的发布记录与其关闭提交逐字节一致）。没有证据的判据是 `missing`，绝不是 `passed`；S0 与 S1 分开计数；脚本分析从不计为识别质量。
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

P1_SHA = "b49701b6386ee7ea88dc05221879786f717d1947"
P1_CLOSE = "a9de358"
M3_PATH = "docs/verification/m3-2026-09-25/release.json"
P0_PATH = "docs/verification/p0-2026-09-25-r2/release.json"
P1_PATH = "docs/verification/p1-2026-09-26/release.json"
M2 = runpy.run_path(str(ROOT / "scripts/verify_m2_release.py"))
DOCS = ("docs/", "README.md", "README.zh-CN.md", "AGENTS.md", "CLAUDE.md", "eval/BASELINES.md",
        "proto/README.md", "sim/README.md")
# Onboard code and flight configuration: a change here needs the full M1 regression. / 机载代码与飞行配置：改动需完整 M1 回归。
ONBOARD = ("src/drone_agent/mission/", "src/drone_agent/guardian/", "src/drone_agent/adapters/",
           "src/drone_agent/autonomy/", "src/drone_agent/contracts/", "proto/", "ros2_ws/", "configs/skills/",
           "configs/platforms/", "configs/recovery_policies/", "sim/m1.Dockerfile", *(
               f"src/drone_agent/runtime/{name}" for name in ("launch.py", "ipc.py", "ledger.py", "wire.py",
                                                             "recording.py", "signing.py", "robot_state.py",
                                                             "uplink.py")))
P2_PATHS = ("src/drone_agent/fleet/", "src/drone_agent/console/", "src/drone_agent/eval/",
            "src/drone_agent/planner/workflow_draft.py", "src/drone_agent/planner/prompts/workflow-",
            "src/drone_agent/admission/models.py", "src/drone_agent/runtime/permission.py",
            "src/drone_agent/runtime/issues.py", "configs/workflows/", "configs/sites/p2_", "configs/scenarios/p2_",
            "eval/adversarial/workflow_", "scripts/", "sim/compose.p2.yaml", "sim/compose.desk.yaml", "tests/",
            "pyproject.toml", "uv.lock")
COUNTS = ("duplicate_dispatch", "post_cancel_dispatch", "post_cancel_successor", "false_success", "false_order",
          "project_escape", "lost_run", "wrong_dispatch", "wrong_release")
P1_COUNTS = ("wrong_dispatch", "duplicate_dispatch", "wrong_release", "project_escape", "false_success")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def scope(sha: str, m1: list[dict]) -> dict:
    if subprocess.run(["git", "merge-base", "--is-ancestor", P1_SHA, sha], cwd=ROOT).returncode:
        return {"status": "failed", "reason": "candidate does not descend from the P1 candidate"}
    changed = git("diff", "--name-only", P1_SHA, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *P2_PATHS, *ONBOARD))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": P1_SHA, "changed": len(changed), "outside_p2_scope": outside, "onboard_changed": onboard}
    if outside:
        return {"status": "failed", **result}
    if onboard:
        regression = M2["m1_regression"](m1, sha)
        return {"status": regression["status"], "m1_regression": regression, **result}
    return {"status": "passed", "m1_regression": "not_required_onboard_runtime_wire_and_policies_unchanged", **result}


def adversarial() -> dict:
    """The M2 corpora plus the workflow-draft corpus. / M2 语料加工作流草案语料。"""
    from drone_agent.eval.workflow_adversarial import run_corpus

    missions = M2["adversarial"]()
    drafts = asyncio.run(run_corpus(ROOT))
    ok = missions["status"] == "passed" and drafts["status"] == "passed" and drafts["escapes"] == 0 \
        and drafts["activatable_drafts"] == 0
    return {"status": "passed" if ok else "failed", "missions": missions,
            "workflow_drafts": {k: drafts[k] for k in ("cases", "passed", "escapes", "activatable_drafts",
                                                       "activation_path")}}


def s0_matrix(sha: str, output: Path) -> dict:
    """Run the P2 S0 matrix here on the clean candidate and summarize it. / 在干净候选上当场运行 P2 S0 矩阵并汇总。"""
    from drone_agent.eval.p2_world import run_suite, suite

    definition = suite(ROOT)
    expected = {(case["id"], seed) for case in definition["s0"] for seed in definition["seeds"]}
    summary = asyncio.run(run_suite(output, ROOT, sha=sha))
    return summarize(summary, expected, output, COUNTS)


def p1_regression(sha: str, output: Path) -> dict:
    """The P1 S0 matrix on this candidate: the service and the dock chores changed. / P1 S0 矩阵回归。"""
    from drone_agent.eval.p1_world import run_suite, suite

    definition = suite(ROOT)
    expected = {(case["id"], seed) for case in definition["s0"] for seed in definition["seeds"]}
    summary = asyncio.run(run_suite(output, ROOT, sha=sha))
    return summarize(summary, expected, output, P1_COUNTS)


def summarize(summary: dict, expected: set, output: Path, counts_named: tuple[str, ...]) -> dict:
    seen = {(r["scenario"], r["seed"]) for r in summary["results"]}
    problems = [] if seen == expected else ["case_coverage_mismatch"]
    problems += [f"{r['scenario']}-{r['seed']}:{r['classification']}" for r in summary["results"] if not r["passed"]]
    counts = summary["counts"]
    if any(counts.get(k) for k in counts_named):
        problems.append("nonzero_safety_counts")
    return {"status": "passed" if not problems and summary["status"] == "passed" else "failed",
            "cases": summary["cases"], "expected": len(expected), "passed": summary["passed"], "counts": counts,
            "voided_attempts": len(summary["voided_attempts"]), "problems": problems,
            "summary_sha256": hashlib.sha256((output / "suite.json").read_bytes()).hexdigest()}


def s1(receipts: list[dict], sha: str) -> dict:
    if not receipts:
        return {"status": "missing"}
    suite = yaml.safe_load(subprocess.check_output(["git", "show", f"{sha}:configs/scenarios/p2_suite.yaml"],
                                                   cwd=ROOT).decode("utf-8"))
    expected = {(case["id"], seed) for case in suite["s1"] for seed in case["seeds"]}
    results, problems = {}, []
    for receipt in receipts:
        if receipt.get("source_sha") != sha or receipt.get("status") != "passed" or receipt.get("layer") != "S1":
            problems.append("a receipt failed, belongs to another revision or is not S1")
        if receipt.get("other_containers_before") != receipt.get("other_containers_after"):
            problems.append("shared-server isolation not verified")
        if not receipt.get("signer_key_id") or set(receipt.get("certificate_fingerprints", {})) != {
                "ca", "service", "robot"}:
            problems.append("signing key id or certificate fingerprints not recorded")
        for row in receipt.get("results", []):
            key = row.get("scenario"), row.get("seed")
            if key not in expected or key in results:
                problems.append(f"unexpected or duplicate case {key}")
                continue
            counts = row.get("counts") or {}
            if not (row.get("passed") is True and row.get("replay_agrees") is True and row.get("judge_exit_code") == 0
                    and row.get("problems") == [] and row.get("source_sha") == sha
                    and set(counts) == set(COUNTS) and not any(counts.values())):
                problems.append(f"case {key} did not meet the independent criteria")
            results[key] = row
    missing = sorted(expected - set(results))
    return {"status": "passed" if not problems and not missing else "failed", "cases": len(results),
            "expected": len(expected), "missing": missing, "problems": problems,
            "counts": {k: sum((r.get("counts") or {}).get(k, 0) for r in results.values()) for k in COUNTS},
            "flights": sum(len(r.get("flown_versions") or []) for r in results.values())}


def desk(activation: dict | None, http: dict | None, probe: dict | None, sha: str) -> dict:
    if activation is None or http is None or probe is None:
        return {"status": "missing", "reason": "desk activation, HTTP and workflow probe receipts are required"}
    problems = []
    operations, workflows = activation.get("operations") or {}, activation.get("workflows") or {}
    if activation.get("status") != "verified" or activation.get("source_sha") != sha:
        problems.append("desk_not_activated_on_candidate")
    if operations.get("catalog_id") != "p1_s1_v1" or (operations.get("migration") or {}).get("status") not in (
            "migrated", "current"):
        problems.append("desk_without_p1_catalog_or_migration")
    if workflows.get("catalog_id") != "p2_s1_v1" or (workflows.get("migration") or {}).get("status") not in (
            "migrated", "current"):
        problems.append("desk_without_workflow_catalog_or_migration")
    for name, drill in (("operations", operations.get("drill") or {}), ("workflows", workflows.get("drill") or {})):
        if drill.get("status") not in ("passed", "not_applicable"):
            problems.append(f"{name}_migration_drill_not_passed")
    containers = activation.get("containers") or {}
    if "desk-dock" not in containers or any(c.get("docker_access") for c in containers.values()):
        problems.append("dock_backend_missing_or_privileged")
    health = http.get("health") or {}
    if not ((http.get("page") or {}).get("status") == 200 and (http.get("script") or {}).get("matches_checkout")
            and health.get("ok") and health.get("console_source_sha") == sha == health.get("service_source_sha")):
        problems.append("page_script_or_health_mismatch")
    if not {"operator", "approver", "reviewer"} <= set(probe.get("roles") or []):
        problems.append("operator_reviewer_roles_missing")
    templates = {t["workflow_id"]: t for t in probe.get("templates") or []}
    if not {"asset_check", "campus_round", "asset_reinspection"} <= set(templates) or \
            (probe.get("catalog") or {}).get("catalog_id") != "p2_s1_v1":
        problems.append("workflow_templates_missing")
    schedules = [x for t in templates.values() for x in t["triggers"] if x["kind"] == "schedule"]
    if not schedules:
        problems.append("schedule_state_missing")
    draft = probe.get("draft") or {}
    if draft.get("active") is not False or draft.get("status") not in ("planned", "refused", "failed") \
            or (draft.get("use") or {}).get("source") not in ("live_model", "scripted"):
        problems.append("draft_missing_or_active")
    if draft.get("status") == "planned" and (draft.get("robots") != ["uav_01"]
                                             or ["report", "build_report"] not in draft.get("nodes", [])):
        problems.append("draft_outside_the_project_robot")
    refused = probe.get("refused") or []
    if len(refused) < 7 or any("unknown type" not in (r.get("error") or "") for r in refused):
        problems.append("injection_control_or_activation_frames_not_refused")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "catalogs": [operations.get("catalog_id"), workflows.get("catalog_id")],
            "migrations": [(operations.get("migration") or {}).get("status"),
                           (workflows.get("migration") or {}).get("status")],
            "draft": {k: draft.get(k) for k in ("status", "active")} | {"source": (draft.get("use") or {}).get("source")}}


def desk_session(receipt: dict | None, flights: list[dict] | None, sha: str) -> dict:
    if receipt is None or flights is None:
        return {"status": "missing", "reason": "a desk workflow run and the supervisor's flight.json records"}
    problems = []
    if receipt.get("errors"):
        problems.append("desk_probe_reported_errors")
    runs = receipt.get("runs") or {}
    root = runs.get(receipt.get("root_run")) or {}
    children = [r for run_id, r in runs.items() if run_id != receipt.get("root_run")]
    if (root.get("run") or {}).get("state") != "completed" or not children or \
            any((c.get("run") or {}).get("state") != "completed" for c in children):
        problems.append("workflow_or_reinspection_not_completed")
    orders = root.get("orders") or []
    if [o.get("state") for o in orders] != ["reinspection_requested"]:
        problems.append("simulated_work_order_not_reached")
    if not any(r.get("decision") == "confirmed" for r in root.get("reviews") or []):
        problems.append("no_human_confirmation")
    analyses = [a for r in runs.values() for a in r.get("analyses") or []]
    if not analyses or any(a.get("source") not in ("scripted", "deterministic") for a in analyses):
        problems.append("analysis_source_not_labelled")
    missions = receipt.get("missions") or {}
    if len(missions) < 2:
        problems.append("fewer_than_two_desk_missions")
    recorded = {(f.get("mission_id"), f.get("version")): f for f in flights}
    public_flights = 0
    for mission_id, mission in missions.items():
        binding = mission.get("binding") or {}
        dispatch = mission.get("dispatch") or {}
        judge = (mission.get("cloud") or {}).get("judge") or {}
        if (binding.get("project_id"), binding.get("robot_id"), binding.get("dock_id")) != (
                "campus_s1", "uav_01", "dock_s1"):
            problems.append(f"{mission_id}:not_bound_to_the_p1_site")
        if (mission.get("request") or {}).get("channel") != "workflow":
            problems.append(f"{mission_id}:not_a_workflow_mission")
        if not dispatch.get("claims") or any(c.get("state") != "claimed" for c in dispatch["claims"]):
            problems.append(f"{mission_id}:not_claimed_through_the_gate")
        if not dispatch.get("reservations") or any(r.get("state") != "released" or r.get("reason") != "reconciled"
                                                   for r in dispatch["reservations"]):
            problems.append(f"{mission_id}:hold_not_released_by_reconciliation")
        if not (mission.get("status") == "completed" and judge.get("passed") is True
                and judge.get("replay_agrees") is True and judge.get("false_success_reports") == 0
                and judge.get("problems") == []):
            problems.append(f"{mission_id}:not_independently_verified")
        for flight in (mission.get("cloud") or {}).get("flights") or []:
            public_flights += 1
            own = recorded.get((mission_id, flight.get("version")))
            if own is None or own.get("source_sha") != sha or any(own.get(k) != flight.get(k) for k in (
                    "version", "status", "epoch", "started_at", "ended_at", "manual_cleanup")):
                problems.append(f"{mission_id}:authoritative_flight_binding_mismatch")
    if public_flights < 2:
        problems.append("fewer_than_two_desk_flights")
    return {"status": "failed" if problems else "passed", "problems": problems, "runs": len(runs),
            "missions": len(missions), "flights": public_flights}


def historical() -> dict:
    """Closed milestones' release records are inputs, never current results. / 已关闭里程碑的记录是输入，不是当前结果。"""
    result, problems = {}, []
    for label, path, commit in (("m3", M3_PATH, "9223d74"), ("p0", P0_PATH, "6d60f67"), ("p1", P1_PATH, P1_CLOSE)):
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
    if result["p0"]["status"] != "passed" or result["p1"]["status"] != "passed":
        problems.append("p0_or_p1_not_closed")
    return {"status": "failed" if problems else "passed", "problems": problems, **result}


def capabilities(sha: str, criteria: dict) -> list[dict]:
    passed = all(item["status"] == "passed" for item in criteria.values())
    return [
        {"capability": "durable_workflows_v1", "milestone": "P2", "implemented": True,
         "software_validation": "passed" if passed else "not_passed", "source_sha": sha,
         "layers": {"S0": {"scenarios": 15, "seeds": 3, "logical_uavs": 3, "physical_flight": False},
                    "S1": {"px4_sitl_aircraft": 1, "logical_docks": 1}},
         "analysis": "scripted_and_deterministic_only; recognition quality is P4",
         "work_orders": "simulated; closing an order is P4", "hardware_validation": "not_applicable"},
        {"milestone": "P1", "software_validation": "historical_passed", "source_sha": P1_SHA,
         "counts_as_candidate_validation": False},
        {"milestone": "M3", "status": "not_passed", "reason": "JIL missing; not a P2 dependency"},
        *({"milestone": name, "status": "missing", "hardware_validation": "missing"} for name in ("H1", "H2", "H3")),
        *({"milestone": name, "status": "planned", "software_validation": "missing"}
          for name in ("P3", "P4", "P5", "X1", "X2", "X3")),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--s1", type=Path, nargs="*", default=[])
    parser.add_argument("--m2", type=Path, nargs="*", default=[])
    parser.add_argument("--m1", type=Path, nargs="*", default=[], help="only needed if onboard code changed")
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-workflow", type=Path, help="desk_probe.py workflow receipt (templates, draft, refusals)")
    parser.add_argument("--desk-session", type=Path, help="desk_probe.py workflow --start receipt")
    parser.add_argument("--desk-flights-dir", type=Path, help="the supervisor's flight.json records of that run")
    parser.add_argument("--s0-output", type=Path, default=ROOT / "outputs" / "p2-s0")
    parser.add_argument("--p1-output", type=Path, default=ROOT / "outputs" / "p2-p1-regression")
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
    inputs = [p for p in [args.deployment, args.desk, args.desk_http, args.desk_workflow, args.desk_session,
                          *args.s1, *args.m2, *args.m1] if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    flights = [load(p) for p in sorted(args.desk_flights_dir.glob("*.json"))] if args.desk_flights_dir else None
    criteria = {
        "scope": scope(args.sha, [load(p) for p in args.m1]),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "adversarial": adversarial(),
        "s0_matrix": s0_matrix(args.sha, args.s0_output),
        "p1_regression": p1_regression(args.sha, args.p1_output),
        "s1": s1([load(p) for p in args.s1], args.sha),
        "m2_regression": M2["e2e"]([load(p) for p in args.m2], args.sha),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_workflow), args.sha),
        "desk_session": desk_session(load(args.desk_session), flights, args.sha),
        "historical": historical(),
    }
    paths = inputs + ([*sorted(args.desk_flights_dir.glob("*.json"))] if args.desk_flights_dir else [])
    digests = {p.relative_to(args.output.parent).as_posix() if p.is_relative_to(args.output.parent) else p.name:
               hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {"schema_version": "0.1.0", "milestone": "P2", "source_sha": args.sha, "records_sha": head,
              "status": "passed" if all(v["status"] == "passed" for v in criteria.values()) else "not_passed",
              "criteria": criteria, "capabilities": capabilities(args.sha, criteria), "inputs": digests}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
