"""P5 gate: the hardware-free platform v0.1 on one immutable candidate (WP-P5-05, D069-D073).

Criteria, bound to the same full commit unless stated:
  scope            — changes since the P4 candidate stay in P5 paths, the D067 collector and the D068 desk pages; the
                     generic workflow kernel, the business layer and every onboard path are unchanged, except one new
                     platform file for the vendor aircraft that no onboard process reads
  checks           — the cloud deployment receipt: the whole suite on Linux with 0 failures, errors and skips
  adversarial      — the M2, workflow-draft and vision corpora still authorize nothing (P4's criterion, run here)
  s0, s3           — run here: P5-R01..R08 and P5-V01..V12 x 3 seeds, the independent judge online and from the
                     recordings, every count 0
  authz            — run here: every API method, page frame, media read and push as every identity class; 0 escapes
  p1/p2/p3/p4_regression — run here: the P1-P4 S0 matrices, since the service and the operations layer changed
  s1               — remote_p5 receipts: every road case x seed on PX4 SITL passed online and from the recordings, every
                     count 0, closures checked against the harness's obstacle history
  m2_regression, p1/p2/p3/p4_s1_regression — the earlier end-to-end and S1 suites on this candidate
  desk             — the resident desk activated on this candidate with the four p5_desk_v1 catalogs, three backends,
                     the vendor link and every dock session active, the migration and catalog-switch drills passed, the
                     new residents inside their boundaries; page, script, health and the fixed page match; forged
                     identity headers are ignored; the browser rendered every workspace including the audit one and
                     the fixed page without console errors
  main_scenario    — through the page only: damage in the world, the run, approvals, the finding confirmed, the order,
                     the repair reported (damage removed), the reinspection approved and its round confirmed, the order
                     closed and the finding resolved; every flight independently judged; the audit workspace shows the
                     person's approvals, review, repair and round decision
  soak             — the soak judge on the full 72-hour plan of this candidate: every criterion passed
  backup           — an online backup and a restore drill that read the copy back offline without touching the live
                     ledger
  p4_dependency    — the P4 machine gate passed every criterion except s2, which stays open (D069)
  historical       — the M3 and P0-P4 release records are byte-identical to their closing commits
A criterion without evidence is `missing`, never `passed`. S0, S1, S2 and S3 are counted separately; nothing here proves
a real device, a real vendor's firmware, field networks or any model accuracy.

P5 门禁：在同一不可变候选上核对无硬件平台 v0.1（WP-P5-05，D069–D073）。判据见上文英文；除特别说明外都绑定同一完整提交。
没有证据的判据是 `missing`，绝不是 `passed`。S0、S1、S2、S3 分开计数；这里的任何内容都不证明真实设备、真实厂商固件、现场
网络或任何模型精度。
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

P4_SHA = "8f41112c299f01f868113acc6be5311fdb96d14a"
P4_CLOSE = "bb63075"
P4_PATH = "docs/verification/p4-2026-09-28/release.json"
M2 = runpy.run_path(str(ROOT / "scripts/verify_m2_release.py"))
P1 = runpy.run_path(str(ROOT / "scripts/verify_p1_release.py"))
P2 = runpy.run_path(str(ROOT / "scripts/verify_p2_release.py"))
P3 = runpy.run_path(str(ROOT / "scripts/verify_p3_release.py"))
P4 = runpy.run_path(str(ROOT / "scripts/verify_p4_release.py"))
DOCS = P2["DOCS"]
ONBOARD = P2["ONBOARD"]
P5_PATHS = ("src/drone_agent/fleet/", "src/drone_agent/console/", "src/drone_agent/eval/",
            "src/drone_agent/runtime/permission.py", "src/drone_agent/runtime/issues.py",
            "configs/sites/p5_", "configs/workflows/p5_", "configs/analysis/p5_", "configs/scheduling/p5_",
            "configs/scenarios/p5_", "configs/vendors/", "configs/soak/", "scripts/", "sim/compose.p5.yaml",
            "sim/compose.desk.yaml", "sim/p5.Dockerfile", "sim/p5_setup.py", "tests/", "pyproject.toml", "uv.lock",
            # D067 (2026-09-28): the collector exits without interpreter finalization. / 采集器退出时跳过解释器收尾。
            "sim/collect.py")
# D069: a platform file for the vendor aircraft, read by the service's capability table and the vendor task compiler
# only; no onboard process loads it, so it does not call for the M1 regression. It must be new.
# D069：厂商飞行器的平台文件，只由服务的能力表与厂商任务编译器读取；没有机载进程加载它，因此不要求 M1 回归。它必须是新文件。
NEW_PLATFORM = "configs/platforms/vendor_dock_sim.yaml"
KERNEL = tuple(f"src/drone_agent/fleet/{name}" for name in (
    "workflow.py", "workflow_store.py", "workflow_models.py", "business.py", "business_models.py", "business_store.py",
    "findings.py", "work_orders.py", "analysis.py", "analysis_jobs.py", "media_index.py"))
CATALOGS = {"operations": "p5_desk_v1", "workflows": "p5_desk_v1", "scheduling": "p5_desk_v1", "business": "p5_desk_v1"}
BACKENDS = ["px4_sitl", "logical_sim", "vendor_protocol_sim"]
DOCKS = ("dock_s1", "dock_fa", "dock_fb", "dock_vd")
WORKSPACES = {"overview", "missions", "workflows", "tasks", "fleet", "business", "audit"}
CHAIN = ("mission.approved", "finding.reviewed", "order.repair_reported")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def show(sha: str, path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{sha}:{path}"], cwd=ROOT)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scope(sha: str, m1: list[dict]) -> dict:
    if subprocess.run(["git", "merge-base", "--is-ancestor", P4_SHA, sha], cwd=ROOT).returncode:
        return {"status": "failed", "reason": "candidate does not descend from the P4 candidate"}
    changed = git("diff", "--name-only", P4_SHA, sha).splitlines()
    added = set(git("diff", "--name-only", "--diff-filter=A", P4_SHA, sha).splitlines())
    outside = [p for p in changed if not p.startswith((*DOCS, *P5_PATHS, *ONBOARD))]
    kernel = [p for p in changed if p in KERNEL]
    onboard = [p for p in changed if p.startswith(ONBOARD) and not (p == NEW_PLATFORM and p in added)]
    result = {"base_sha": P4_SHA, "changed": len(changed), "outside_p5_scope": outside, "kernel_changed": kernel,
              "onboard_changed": onboard, "new_platform_file": NEW_PLATFORM in added}
    if outside or kernel:
        return {"status": "failed", **result}
    if onboard:
        regression = M2["m1_regression"](m1, sha)
        return {"status": regression["status"], "m1_regression": regression, **result}
    return {"status": "passed", "m1_regression": "not_required_onboard_runtime_wire_and_policies_unchanged", **result}


def s0_s3(sha: str, output: Path) -> tuple[dict, dict]:
    """Run the P5 S0 and S3 matrices here on the clean candidate. / 在干净候选上当场运行 P5 S0 与 S3 矩阵。"""
    from drone_agent.eval.judge_p5 import COUNTS
    from drone_agent.eval.p5_world import run_suite, suite

    definition = suite(ROOT)
    summary = asyncio.run(run_suite(output, ROOT, sha=sha))
    found = {}
    for layer in ("s0", "s3"):
        expected = {(case["id"], seed) for case in definition[layer] for seed in definition["seeds"]}
        rows = [r for r in summary["results"] if (r["scenario"], r["seed"]) in expected]
        problems = [] if {(r["scenario"], r["seed"]) for r in rows} == expected else ["case_coverage_mismatch"]
        problems += [f"{r['scenario']}-{r['seed']}:{r['classification']}" for r in rows if not r["passed"]]
        counts = {k: sum((r.get("counts") or {}).get(k, 0) for r in rows) for k in COUNTS}
        if any(counts.values()):
            problems.append("nonzero_safety_counts")
        found[layer] = {"status": "passed" if not problems else "failed", "cases": len(rows), "expected": len(expected),
                        "counts": counts, "problems": problems,
                        "summary_sha256": digest((output / "suite.json").read_bytes())}
    return found["s0"], found["s3"]


def authz(output: Path) -> dict:
    from drone_agent.eval.p5_authz import run

    output.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(output, ROOT))
    (output / "authz.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return {"status": result["status"], **{k: result[k] for k in ("escapes", "api_rows", "frame_rows", "methods",
                                                                  "forbidden_refused", "audit_denials")},
            "identities": result["identities"]}


def p4_regression(sha: str, output: Path) -> dict:
    return P4["s0_matrix"](sha, output)


def s1(receipts: list[dict], sha: str) -> dict:
    """Every P5 S1 case x seed on PX4 SITL. / PX4 SITL 上每个 P5 S1 用例 × 种子。"""
    from drone_agent.eval.judge_p5 import COUNTS

    if not receipts:
        return {"status": "missing"}
    suite = yaml.safe_load(show(sha, "configs/scenarios/p5_suite.yaml").decode("utf-8"))
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
            "signature_fractions": {f"{k[0]}-{k[1]}": r.get("signature_fractions") for k, r in results.items()}}


def regression(name: str, function, receipts: list[dict], sha: str) -> dict:
    """An earlier phase's S1 criterion on this candidate's receipts. / 早期阶段的 S1 判据作用于本候选的回执。"""
    if not receipts:
        return {"status": "missing", "phase": name}
    return {"phase": name, **function(receipts, sha)}


def desk(activation: dict | None, http: dict | None, spoof: dict | None, browser: dict | None, sha: str) -> dict:
    if activation is None or http is None or spoof is None or browser is None:
        return {"status": "missing", "reason": "desk activation, HTTP, forged-header and browser receipts"}
    problems = []
    if activation.get("status") != "verified" or activation.get("source_sha") != sha:
        problems.append("desk_not_activated_on_candidate")
    for name, catalog in CATALOGS.items():
        part = activation.get(name) or {}
        if part.get("catalog_id") != catalog:
            problems.append(f"desk_without_p5_{name}_catalog")
        if (part.get("migration") or {}).get("status") not in ("migrated", "current"):
            problems.append(f"desk_without_{name}_migration")
        if (part.get("drill") or {}).get("status") not in ("passed", "not_applicable"):
            problems.append(f"{name}_migration_drill_not_passed")
    if (activation.get("switch_drill") or {}).get("status") not in ("passed", "not_applicable"):
        problems.append("catalog_switch_drill_not_passed")
    if activation.get("execution_backends") != BACKENDS or (activation.get("vendor") or {}).get("docks") != ["dock_vd"]:
        problems.append("backends_or_vendor_link_differ")
    if any((activation.get("dock_sessions") or {}).get(d) != "active" for d in DOCKS):
        problems.append("a_dock_session_was_not_active")
    containers = activation.get("containers") or {}
    if not {"desk-fleet", "desk-vendor", "desk-dock"} <= set(containers) or any(
            c.get("docker_access") for c in containers.values()):
        problems.append("residents_missing_or_privileged")
    if not str((activation.get("business") or {}).get("vision", "")).startswith("live:"):
        problems.append("vision_role_not_live")
    health = http.get("health") or {}
    if not ((http.get("page") or {}).get("status") == 200 and (http.get("script") or {}).get("matches_checkout")
            and health.get("ok") and health.get("console_source_sha") == sha == health.get("service_source_sha")
            and (http.get("fixed_page") or {}).get("status") == 200
            and (http.get("fixed_script") or {}).get("matches_checkout")):
        problems.append("page_script_health_or_fixed_page_mismatch")
    if spoof.get("forged_identity_used") is not False:
        problems.append("forged_identity_header_used")
    offered = set()
    for tour in (browser.get("tours") or {}).values():
        offered |= set(tour.get("offered") or [])
        if any(view.get("rendered") is False or (view.get("overflow_px") or 0) > 1 for view in
               (tour.get("views") or {}).values() if isinstance(view, dict)):
            problems.append("a_workspace_did_not_render_or_overflowed")
            break
    if not WORKSPACES <= offered:
        problems.append("not_every_workspace_offered")
    if browser.get("console_errors"):
        problems.append("browser_console_errors")
    fixed = browser.get("fixed") or {}
    if not fixed or any(v.get("status") != 200 or not v.get("steps") for v in fixed.values()):
        problems.append("fixed_page_not_rendered")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "catalogs": {n: (activation.get(n) or {}).get("catalog_id") for n in CATALOGS},
            "switch_drill": (activation.get("switch_drill") or {}).get("status"), "offered": sorted(offered)}


def main_scenario(receipt: dict | None, flights: list[dict] | None, sha: str) -> dict:
    """The one-entry main scenario to closure, read from the browser receipt and the supervisor's records.

    从浏览器回执与监管者记录读取的一入口主场景（到关单）。
    """
    if receipt is None or flights is None:
        return {"status": "missing", "reason": "a desk_browser --loop --repair --world receipt and its flight records"}
    problems = []
    loop = receipt.get("loop") or {}
    if receipt.get("console_errors"):
        problems.append("browser_console_errors")
    if (receipt.get("hello") or {}).get("identity_scheme") != "tailnet":
        problems.append("not_a_tailnet_person")
    world = loop.get("world") or []
    if [(w.get("state"), w.get("ok")) for w in world] != [("damaged", True), ("normal", True)] and \
            [(w.get("state"), w.get("ok")) for w in world] != [("obstructed", True), ("normal", True)]:
        problems.append("world_not_damaged_then_repaired_outside_the_page")
    if (loop.get("decided") or {}).get("state") != "confirmed" or not (loop.get("decided") or {}).get("order"):
        problems.append("finding_not_confirmed_into_an_order")
    if not loop.get("round_reviewed_in_page"):
        problems.append("round_not_reviewed_in_the_page")
    order = loop.get("order") or {}
    if order.get("state") != "closed" or not any(r.get("state") == "passed" for r in order.get("rounds") or []):
        problems.append("order_not_closed_by_a_passed_round")
    if loop.get("finding_after") != "resolved":
        problems.append("finding_not_resolved")
    if len(loop.get("approved") or []) < 2:
        problems.append("fewer_than_two_flights_approved_in_the_page")
    audit = loop.get("audit") or []
    person = {e["action"] for e in audit if e.get("actor_scheme") == "tailnet"}
    if not set(CHAIN) <= person:
        problems.append(f"audit_lacks_the_person's:{','.join(sorted(set(CHAIN) - person))}")
    recorded = {(f.get("mission_id"), f.get("version")): f for f in flights}
    for mission_id in loop.get("approved") or []:
        own = [f for (m, _), f in recorded.items() if m == mission_id]
        if not own or any(f.get("source_sha") != sha or f.get("status") != "finished" for f in own):
            problems.append(f"{mission_id}:flight_not_recorded_on_candidate")
    placed = [p for f in flights for p in ((f.get("world") or {}).get("placed") or []) if p.get("ok")]
    if not placed:
        problems.append("no_flight_saw_the_damage_placed")
    return {"status": "failed" if problems else "passed", "problems": problems, "approved": len(loop.get("approved") or []),
            "order": order.get("state"), "audit_actions": sorted({e["action"] for e in audit})}


def soak(result: dict | None, sha: str) -> dict:
    if result is None:
        return {"status": "missing", "reason": "the soak judge result"}
    criteria = result.get("criteria") or {}
    problems = []
    if result.get("source_sha") != sha:
        problems.append("soak_on_another_revision")
    if result.get("passed") is not True or any(v.get("status") != "passed" for v in criteria.values()):
        problems.append("soak_criteria_not_all_passed")
    if any((result.get("counts") or {}).values()):
        problems.append("nonzero_soak_counts")
    plan = yaml.safe_load(show(sha, "configs/soak/p5_soak_v1.yaml"))
    if result.get("plan_sha256") != digest(show(sha, "configs/soak/p5_soak_v1.yaml")) or \
            (criteria.get("duration") or {}).get("hours", 0) < plan["criteria"]["min_duration_h"]:
        problems.append("not_the_frozen_full_plan")
    return {"status": "failed" if problems else "passed", "problems": problems,
            **{k: result.get(k) for k in ("soak_id", "t0", "end", "counts")},
            "criteria": {k: v.get("status") for k, v in criteria.items()},
            "layers": (criteria.get("layers") or {}).get("by_layer")}


def backup(taken: dict | None, drill: dict | None, sha: str) -> dict:
    if taken is None or drill is None:
        return {"status": "missing", "reason": "desk-backup and desk-restore-drill receipts"}
    problems = []
    if taken.get("source_sha") != sha or (taken.get("ledger") or {}).get("integrity") != "ok":
        problems.append("backup_not_on_candidate_or_not_intact")
    if drill.get("status") != "passed" or drill.get("backup_id") != taken.get("backup_id") or \
            drill.get("live_ledger_touched") is not False:
        problems.append("restore_drill_not_passed_on_that_backup")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "tables": len((taken.get("ledger") or {}).get("tables") or {}), "media_files": taken.get("media_files")}


def p4_dependency() -> dict:
    """P4's machine gate: every criterion passed but s2, which stays open (D069). / P4 机器门禁：除 s2 外全部通过。"""
    record = json.loads(show(P4_CLOSE, P4_PATH))
    statuses = {k: v["status"] for k, v in record["criteria"].items()}
    others = {k: v for k, v in statuses.items() if k != "s2"}
    ok = record.get("source_sha") == P4_SHA and statuses.get("s2") == "failed" and \
        all(v == "passed" for v in others.values()) and (ROOT / P4_PATH).read_bytes() == show(P4_CLOSE, P4_PATH)
    return {"status": "passed" if ok else "failed", "p4_sha": record.get("source_sha"), "open": ["s2"],
            "s2_status": statuses.get("s2"), "other_criteria": others}


def historical() -> dict:
    earlier = P4["historical"]()
    original = show(P4_CLOSE, P4_PATH)
    problems = list(earlier.get("problems", []))
    if (ROOT / P4_PATH).read_bytes() != original:
        problems.append("p4_record_changed")
    return {"status": "failed" if problems or earlier["status"] != "passed" else "passed", "problems": problems,
            "earlier": {k: v for k, v in earlier.items() if k not in ("status", "problems")},
            "p4": {"sha256": digest(original), "status": json.loads(original).get("status"),
                   "counts_as_candidate_validation": False}}


def capabilities(sha: str, criteria: dict) -> list[dict]:
    passed = all(item["status"] == "passed" for item in criteria.values())
    return [
        {"capability": "platform_v0_1", "milestone": "P5", "implemented": True,
         "software_validation": "passed" if passed else "not_passed", "source_sha": sha,
         "layers": {"S0": {"logical_aircraft": 2, "template": "road obstacle candidate review"},
                    "S1": {"px4_sitl_aircraft": 1, "obstacle": "Gazebo model", "analyzer": "deterministic"},
                    "S2": {"probes": "frozen change-v3 on VisA calibration images", "metrics": "none claimed"},
                    "S3": {"vendor": "protocol simulator of a profile not verified against any firmware"}},
         "soak": "72 h on real wall-clock time with injected faults", "real_devices": False,
         "work_orders": "simulated in the ledger", "field_accuracy": "not claimed"},
        {"milestone": "P4", "status": "not_passed", "open": ["s2"], "source_sha": P4_SHA,
         "counts_as_candidate_validation": False},
        {"milestone": "M3", "status": "not_passed", "reason": "JIL missing; not a P5 dependency"},
        *({"milestone": name, "status": "missing", "hardware_validation": "missing"} for name in ("H1", "H2", "H3")),
        *({"milestone": name, "status": "planned", "software_validation": "missing"} for name in ("X1", "X2", "X3")),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--s1", type=Path, nargs="*", default=[])
    parser.add_argument("--m2", type=Path, nargs="*", default=[])
    parser.add_argument("--p1-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--p2-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--p3-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--p4-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--m1", type=Path, nargs="*", default=[], help="only needed if onboard code changed")
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--browser", type=Path, help="desk_browser.py tour receipt (all workspaces, /fixed/)")
    parser.add_argument("--main-scenario", type=Path, help="desk_browser.py --loop ... --repair --world receipt")
    parser.add_argument("--main-flights-dir", type=Path, help="the supervisor's flight.json records of that run")
    parser.add_argument("--soak", type=Path, help="desk-soak --judge result")
    parser.add_argument("--backup", type=Path, help="desk-backup receipt")
    parser.add_argument("--restore", type=Path, help="desk-restore-drill receipt")
    parser.add_argument("--s0-output", type=Path, default=ROOT / "outputs" / "p5-s0")
    parser.add_argument("--authz-output", type=Path, default=ROOT / "outputs" / "p5-authz")
    parser.add_argument("--p1-output", type=Path, default=ROOT / "outputs" / "p5-p1-regression")
    parser.add_argument("--p2-output", type=Path, default=ROOT / "outputs" / "p5-p2-regression")
    parser.add_argument("--p3-output", type=Path, default=ROOT / "outputs" / "p5-p3-regression")
    parser.add_argument("--p4-output", type=Path, default=ROOT / "outputs" / "p5-p4-regression")
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
    lists = (*args.s1, *args.m2, *args.p1_s1, *args.p2_s1, *args.p3_s1, *args.p4_s1, *args.m1)
    singles = (args.deployment, args.desk, args.desk_http, args.desk_spoof, args.browser, args.main_scenario,
               args.soak, args.backup, args.restore)
    inputs = [p for p in (*singles, *lists) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    flights = [load(p) for p in sorted(args.main_flights_dir.glob("*.json"))] if args.main_flights_dir else None
    s0_result, s3_result = s0_s3(args.sha, args.s0_output)
    criteria = {
        "scope": scope(args.sha, [load(p) for p in args.m1]),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "adversarial": P4["adversarial"](),
        "s0": s0_result,
        "s3": s3_result,
        "authz": authz(args.authz_output),
        "p1_regression": P2["p1_regression"](args.sha, args.p1_output),
        "p2_regression": P2["s0_matrix"](args.sha, args.p2_output),
        "p3_regression": P3["s0_matrix"](args.sha, args.p3_output),
        "p4_regression": p4_regression(args.sha, args.p4_output),
        "s1": s1([load(p) for p in args.s1], args.sha),
        "m2_regression": M2["e2e"]([load(p) for p in args.m2], args.sha),
        "p1_s1_regression": regression("P1", P1["s1"], [load(p) for p in args.p1_s1], args.sha),
        "p2_s1_regression": regression("P2", P2["s1"], [load(p) for p in args.p2_s1], args.sha),
        "p3_s1_regression": regression("P3", P4["p3_s1_regression"], [load(p) for p in args.p3_s1], args.sha),
        "p4_s1_regression": regression("P4", P4["s1"], [load(p) for p in args.p4_s1], args.sha),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_spoof), load(args.browser), args.sha),
        "main_scenario": main_scenario(load(args.main_scenario), flights, args.sha),
        "soak": soak(load(args.soak), args.sha),
        "backup": backup(load(args.backup), load(args.restore), args.sha),
        "p4_dependency": p4_dependency(),
        "historical": historical(),
    }
    paths = inputs + ([*sorted(args.main_flights_dir.glob("*.json"))] if args.main_flights_dir else [])
    digests = {p.relative_to(args.output.parent).as_posix() if p.is_relative_to(args.output.parent) else p.name:
               digest(p.read_bytes()) for p in paths}
    report = {"schema_version": "0.1.0", "milestone": "P5", "source_sha": args.sha, "records_sha": head,
              "status": "passed" if all(v["status"] == "passed" for v in criteria.values()) else "not_passed",
              "criteria": criteria, "capabilities": capabilities(args.sha, criteria), "inputs": digests}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
