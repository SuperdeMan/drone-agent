"""WP-P6-02 gate: active recapture and target-region quality on one immutable candidate (D078).

Criteria, bound to the same full commit unless stated:
  scope          — changes since the P4 closure records stay in P6 paths; no onboard path, wire or flight
                   configuration changed, so the M1 regression is not required
  checks         — the cloud deployment receipt: the whole suite on Linux with 0 failures, errors and skips
  s0             — run here: P6-F01..F10 x 3 seeds, the independent judge online and from the recordings, every count
                   0, and no false suspicion from a recapture template
  p1..p4_regression, p5_regression, authz — run here: the earlier S0 matrices, and the P5 authorization matrix
                   over the desk's P6 catalogs, since the workflow kernel, the business layer and the service changed
  s1             — remote_p6 receipts: every P6 S1 case x seed on PX4 SITL passed online and from the recordings
  p2_s1_regression, p4_s1_regression — the earlier S1 suites the kernel and the business layer touch, on this candidate
  desk           — the resident desk activated on this candidate with the p6_desk_v1 workflow and business catalogs
                   (operations and scheduling stay p5_desk_v1), the migration and catalog-switch drills passed, three
                   backends, every dock session active; page, script and health match; forged identity is ignored
  desk_session   — through the desk entry: a `recapture_watch` run whose first capture was refused for its target
                   region, the world cleared before the person approved the recapture, the recapture analysed and the
                   run completed, both flights approved by the person and independently judged
  historical     — the P5 release record and the P4 S2 re-verification record are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`. S0 and S1 are counted separately; nothing here proves a
real device, a real camera or any recognition accuracy.

WP-P6-02 门禁：在同一不可变候选上核对主动补拍与目标区域质量（D078）。判据见上文英文；除特别说明外都绑定同一完整提交。
没有证据的判据是 `missing`，绝不是 `passed`。S0 与 S1 分开计数；这里的任何内容都不证明真实设备、真实相机或任何识别精度。
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

BASE_SHA = "2c6a5da91afd123c831a4cb75e8a8eeb467fb02f"
M2 = runpy.run_path(str(ROOT / "scripts/verify_m2_release.py"))
P2 = runpy.run_path(str(ROOT / "scripts/verify_p2_release.py"))
P3 = runpy.run_path(str(ROOT / "scripts/verify_p3_release.py"))
P4 = runpy.run_path(str(ROOT / "scripts/verify_p4_release.py"))
P5 = runpy.run_path(str(ROOT / "scripts/verify_p5_release.py"))
DOCS = P2["DOCS"]
ONBOARD = P2["ONBOARD"]
P6_PATHS = ("src/drone_agent/fleet/", "src/drone_agent/console/", "src/drone_agent/eval/", "configs/workflows/p6_",
            "configs/analysis/p6_", "configs/analysis/quality_target_", "configs/scenarios/p6_", "scripts/",
            "sim/compose.p6.yaml", "sim/compose.desk.yaml", "tests/")
CATALOGS = {"operations": "p5_desk_v1", "workflows": "p6_desk_v1", "scheduling": "p5_desk_v1", "business": "p6_desk_v1"}
BACKENDS = ["px4_sitl", "logical_sim", "vendor_protocol_sim"]
DOCKS = ("dock_s1", "dock_fa", "dock_fb", "dock_vd")
RECORDS = {"docs/verification/p5-2026-10-06/release.json": "32dcb70",
           "docs/verification/review-2026-10-10/p4-s2-release.json": "2c6a5da"}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def show(sha: str, path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{sha}:{path}"], cwd=ROOT)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scope(sha: str) -> dict:
    if subprocess.run(["git", "merge-base", "--is-ancestor", BASE_SHA, sha], cwd=ROOT).returncode:
        return {"status": "failed", "reason": "candidate does not descend from the P4 closure records"}
    changed = git("diff", "--name-only", BASE_SHA, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *P6_PATHS, *ONBOARD))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": BASE_SHA, "changed": len(changed), "outside_p6_scope": outside, "onboard_changed": onboard}
    if outside or onboard:
        return {"status": "failed", **result}
    return {"status": "passed", "m1_regression": "not_required_onboard_runtime_wire_and_policies_unchanged", **result}


def s0(sha: str, output: Path) -> dict:
    """Run the P6 S0 matrix here on the clean candidate. / 在干净候选上当场运行 P6 S0 矩阵。"""
    from drone_agent.eval.judge_p6 import COUNTS
    from drone_agent.eval.p6_world import run_suite, suite

    definition = suite(ROOT)
    summary = asyncio.run(run_suite(output, ROOT, sha=sha))
    expected = {(case["id"], seed) for case in definition["s0"] for seed in definition["seeds"]}
    rows = summary["results"]
    problems = [] if {(r["scenario"], r["seed"]) for r in rows} == expected else ["case_coverage_mismatch"]
    problems += [f"{r['scenario']}-{r['seed']}:{r['classification']}" for r in rows if not r["passed"]]
    counts = {k: sum((r.get("counts") or {}).get(k, 0) for r in rows) for k in COUNTS}
    if any(counts.values()):
        problems.append("nonzero_safety_counts")
    metrics = summary["metrics"]
    if metrics.get("false_suspected_recapture"):
        problems.append("a_recapture_template_produced_a_false_suspicion")
    if not metrics.get("recaptured_completions"):
        problems.append("no_refusal_was_turned_into_a_completed_run")
    return {"status": "passed" if not problems else "failed", "cases": len(rows), "expected": len(expected),
            "counts": counts, "metrics": metrics, "voided_attempts": len(summary["voided_attempts"]),
            "problems": problems, "summary_sha256": digest((output / "suite.json").read_bytes())}


def p5_regression(sha: str, output: Path) -> dict:
    s0_result, s3_result = P5["s0_s3"](sha, output)
    ok = s0_result["status"] == s3_result["status"] == "passed"
    return {"status": "passed" if ok else "failed", "s0": s0_result, "s3": s3_result}


def authz(output: Path) -> dict:
    """The P5 authorization matrix over the desk as activated since D078 (the p6_desk_v1 catalogs).

    在 D078 起激活的任务台（p6_desk_v1 目录）上运行 P5 授权矩阵。
    """
    from drone_agent.eval.p5_authz import run
    from drone_agent.eval.p5_desk_world import P6DeskWorld

    output.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(output, ROOT, world_class=P6DeskWorld))
    (output / "authz.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return {"status": result["status"], **{k: result[k] for k in ("escapes", "api_rows", "frame_rows", "methods",
                                                                  "forbidden_refused", "audit_denials")},
            "identities": result["identities"], "catalogs": "p6_desk_v1 workflows and business"}


def s1(receipts: list[dict], sha: str) -> dict:
    """Every P6 S1 case x seed on PX4 SITL. / PX4 SITL 上每个 P6 S1 用例 × 种子。"""
    from drone_agent.eval.judge_p6 import COUNTS

    if not receipts:
        return {"status": "missing"}
    suite = yaml.safe_load(show(sha, "configs/scenarios/p6_suite.yaml").decode("utf-8"))
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
                    and row.get("problems") == [] and set(counts) == set(COUNTS) and not any(counts.values())):
                problems.append(f"case {key} did not meet the independent criteria")
            results[key] = row
    missing = sorted(expected - set(results))
    return {"status": "passed" if not problems and not missing else "failed", "cases": len(results),
            "expected": len(expected), "missing": missing, "problems": problems,
            "flights": sum(len(r.get("flown_versions") or []) for r in results.values()),
            "metrics": {f"{k[0]}-{k[1]}": r.get("metrics") for k, r in results.items()}}


def regression(name: str, function, receipts: list[dict], sha: str) -> dict:
    if not receipts:
        return {"status": "missing", "phase": name}
    return {"phase": name, **function(receipts, sha)}


def desk(activation: dict | None, http: dict | None, spoof: dict | None, sha: str) -> dict:
    if activation is None or http is None or spoof is None:
        return {"status": "missing", "reason": "desk activation, HTTP and forged-header receipts"}
    problems = []
    if activation.get("status") != "verified" or activation.get("source_sha") != sha:
        problems.append("desk_not_activated_on_candidate")
    for name, catalog in CATALOGS.items():
        part = activation.get(name) or {}
        if part.get("catalog_id") != catalog:
            problems.append(f"desk_without_{catalog}_{name}_catalog")
        if (part.get("migration") or {}).get("status") not in ("migrated", "current"):
            problems.append(f"desk_without_{name}_migration")
        if (part.get("drill") or {}).get("status") not in ("passed", "not_applicable"):
            problems.append(f"{name}_migration_drill_not_passed")
    if (activation.get("switch_drill") or {}).get("status") not in ("passed", "not_applicable"):
        problems.append("catalog_switch_drill_not_passed")
    if activation.get("execution_backends") != BACKENDS:
        problems.append("backends_differ")
    if any((activation.get("dock_sessions") or {}).get(d) != "active" for d in DOCKS):
        problems.append("a_dock_session_was_not_active")
    if any(c.get("docker_access") for c in (activation.get("containers") or {}).values()):
        problems.append("a_resident_is_privileged")
    health = http.get("health") or {}
    if not ((http.get("page") or {}).get("status") == 200 and (http.get("script") or {}).get("matches_checkout")
            and health.get("ok") and health.get("console_source_sha") == sha == health.get("service_source_sha")):
        problems.append("page_script_or_health_mismatch")
    if spoof.get("forged_identity_used") is not False:
        problems.append("forged_identity_header_used")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "catalogs": {n: (activation.get(n) or {}).get("catalog_id") for n in CATALOGS},
            "switch_drill": (activation.get("switch_drill") or {}).get("status")}


def desk_session(receipt: dict | None, sha: str) -> dict:
    """The recapture session through the desk entry, read from the probe receipt. / 从探针回执读取经任务台入口的补拍会话。"""
    if receipt is None:
        return {"status": "missing", "reason": "a desk_probe.py workflow --start --clear-before-recapture receipt"}
    problems = []
    if (receipt.get("hello") or {}).get("identity_scheme") != "tailnet":
        problems.append("not_a_tailnet_person")
    root = (receipt.get("runs") or {}).get(receipt.get("root_run") or "", {})
    run = root.get("run") or {}
    nodes = {n[0]: {"activity": n[1], "state": n[2], "reason": n[3]} for n in root.get("nodes") or []}
    if run.get("workflow_id") != "recapture_watch" or run.get("state") != "completed":
        problems.append("the_recapture_run_did_not_complete")
    first = nodes.get("analyze") or {}
    if first.get("state") != "failed" or not str(first.get("reason") or "").startswith("quality.target_"):
        problems.append("the_first_capture_was_not_refused_for_its_target_region")
    if (nodes.get("recapture") or {}).get("state") != "completed" or \
            (nodes.get("analyze_recapture") or {}).get("state") != "completed" or \
            (nodes.get("select") or {}).get("state") != "completed":
        problems.append("the_recapture_did_not_replace_the_refusal")
    cleared = receipt.get("cleared") or {}
    if cleared.get("exit_code") != 0:
        problems.append("the_world_was_not_cleared_before_the_recapture_approval")
    approvals = [s for s in receipt.get("sent") or [] if s.get("action") == "approve"]
    if cleared and approvals and not any(s["t"] >= cleared.get("t", 0) for s in approvals):
        problems.append("the_recapture_was_approved_before_the_world_was_cleared")
    missions = receipt.get("missions") or {}
    if len(missions) != 2 or len(approvals) < 2:
        problems.append("not_two_missions_approved_by_the_person")
    for mission_id, mission in missions.items():
        judge = ((mission.get("cloud") or {}).get("judge") or {})
        if mission.get("status") != "completed" or judge.get("passed") is not True \
                or judge.get("replay_agrees") is not True or judge.get("false_success_reports") != 0:
            problems.append(f"{mission_id}:flight_not_completed_or_not_judged_on_candidate")
    return {"status": "failed" if problems else "passed", "problems": problems, "run": run.get("run_id"),
            "first_refusal": first.get("reason"), "missions": len(missions), "approvals": len(approvals)}


def historical() -> dict:
    problems = []
    found = {}
    for path, commit in RECORDS.items():
        original = show(commit, path)
        found[path] = {"sha256": digest(original), "commit": commit}
        if (ROOT / path).read_bytes() != original:
            problems.append(f"record_changed:{path}")
    return {"status": "failed" if problems else "passed", "problems": problems, "records": found}


def capabilities(sha: str, criteria: dict) -> list[dict]:
    passed = all(item["status"] == "passed" for item in criteria.values())
    return [{"capability": "active_recapture_v1", "work_package": "WP-P6-02", "decision": "D078",
             "software_validation": "passed" if passed else "not_passed", "source_sha": sha,
             "layers": {"S0": {"logical_aircraft": "P4 world", "effects": "glare, shadow, blur, capture drift"},
                        "S1": {"px4_sitl_aircraft": 1, "effect": "an emissive Gazebo disc over the marker",
                               "analyzer": "deterministic"}},
             "real_devices": False, "real_cameras": False, "recognition_accuracy": "not claimed",
             "recapture": "same registered pose, at most once per inspection, each flight approved by a person"}]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--s1", type=Path, nargs="*", default=[])
    parser.add_argument("--p2-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--p4-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--desk-session", type=Path,
                        help="desk_probe.py workflow --start --workflow recapture_watch --clear-before-recapture receipt")
    parser.add_argument("--s0-output", type=Path, default=ROOT / "outputs" / "p6-s0")
    parser.add_argument("--p1-output", type=Path, default=ROOT / "outputs" / "p6-p1-regression")
    parser.add_argument("--p2-output", type=Path, default=ROOT / "outputs" / "p6-p2-regression")
    parser.add_argument("--p3-output", type=Path, default=ROOT / "outputs" / "p6-p3-regression")
    parser.add_argument("--p4-output", type=Path, default=ROOT / "outputs" / "p6-p4-regression")
    parser.add_argument("--p5-output", type=Path, default=ROOT / "outputs" / "p6-p5-regression")
    parser.add_argument("--authz-output", type=Path, default=ROOT / "outputs" / "p6-authz")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    head = git("rev-parse", "HEAD")
    if subprocess.run(["git", "merge-base", "--is-ancestor", args.sha, head], cwd=ROOT).returncode:
        parser.error("HEAD must be the candidate or its documentation-only descendant")
    newer = git("diff", "--name-only", args.sha, head).splitlines()
    if any(not p.startswith(DOCS) for p in newer) or git(
            "status", "--porcelain", "--", "src", "tests", "scripts", "configs", "proto", "sim", "ros2_ws", "eval/s2",
            "eval/adversarial", "pyproject.toml", "uv.lock"):
        parser.error("gate implementation and runtime must match the clean candidate")
    lists = (*args.s1, *args.p2_s1, *args.p4_s1)
    singles = (args.deployment, args.desk, args.desk_http, args.desk_spoof, args.desk_session)
    inputs = [p for p in (*singles, *lists) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "s0": s0(args.sha, args.s0_output),
        "p1_regression": P2["p1_regression"](args.sha, args.p1_output),
        "p2_regression": P2["s0_matrix"](args.sha, args.p2_output),
        "p3_regression": P3["s0_matrix"](args.sha, args.p3_output),
        "p4_regression": P4["s0_matrix"](args.sha, args.p4_output),
        "p5_regression": p5_regression(args.sha, args.p5_output),
        "authz": authz(args.authz_output),
        "s1": s1([load(p) for p in args.s1], args.sha),
        "p2_s1_regression": regression("P2", P2["s1"], [load(p) for p in args.p2_s1], args.sha),
        "p4_s1_regression": regression("P4", P4["s1"], [load(p) for p in args.p4_s1], args.sha),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_spoof), args.sha),
        "desk_session": desk_session(load(args.desk_session), args.sha),
        "historical": historical(),
    }
    digests = {p.relative_to(args.output.parent).as_posix() if p.is_relative_to(args.output.parent) else p.name:
               digest(p.read_bytes()) for p in inputs}
    report = {"schema_version": "0.1.0", "milestone": "P6", "work_package": "WP-P6-02", "source_sha": args.sha,
              "records_sha": head,
              "status": "passed" if all(v["status"] == "passed" for v in criteria.values()) else "not_passed",
              "criteria": criteria, "capabilities": capabilities(args.sha, criteria), "inputs": digests}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
