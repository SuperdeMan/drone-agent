"""Check every M3-SITL exit criterion on one immutable revision; M3 closes only with M3-JIL as well (WP-M3-20, D038).

Criteria, all bound to the same full commit (the tested revision):
  checks        — the cloud deployment receipt: full Linux suite with 0 failures, errors and skips; contract fields
                  and the v1 wire freeze re-checked from that revision
  adversarial   — the M2 adversarial corpora are still blocked where expected with 0 authorized packages
  m3_suite      — every m3_suite scenario × seed has a non-void passing case with replay agreement, no problems and
                  no false success; any non-void failure on the revision fails the criterion (a rerun never hides a
                  finding); void cases (D045) are counted and listed; every external-mode case archived a shadow report
                  and resource samples
  m1_regression — the full M1 matrix passes on this revision (the guardian changed)
  m2_regression — the full M2 end-to-end suite passes on this revision over gRPC (the service and uplink changed)
  m2_zenoh      — the nominal and service-outage M2 cases pass over the Zenoh FleetTransport on every seed (D046)
  measurement   — the D040 tiers (idle, perception, inference) with the guardian on its own quota meet the supervision
                  (p99 ≤ 120 ms, max ≤ 200 ms), intent (p99 ≤ 250 ms) and event-detection (p99 ≤ 1 s) budgets on
                  every seed; the shared-core tiers are reported for the D003 language conclusion, not gated
  recovery_edges— every edge of multirotor_m3@v2 carries a validation record bound to its content and this revision
  jil           — Jetson-in-the-loop evidence; pending until hardware exists, so M3 itself cannot close yet
The recovery records necessarily live in a later commit than the revision they cite; that commit may differ from the
tested revision only in those records and in documentation, and its edges must equal the tested ones.
A criterion without evidence is `missing`, never `passed`.

在同一不可变版本上核对 M3-SITL 的全部退出判据；M3 需 M3-JIL 同时通过才关闭（WP-M3-20，D038）。判据：checks（云端
部署回执）；adversarial（M2 对抗语料仍全部在期望处被拦）；m3_suite（每个场景 × 种子都有非作废的通过用例，回放一致、
无问题、无虚报；该版本上任何非作废失败都使判据失败，重跑不能掩盖发现；作废用例计数并列出；外部模式用例都归档了影子
报告与资源采样）；m1_regression；m2_regression（gRPC 下完整 M2 端到端）；m2_zenoh（Zenoh 下标称与服务停机用例全部种子
通过，D046）；measurement（guardian 独立配额下 D040 三档满足监督周期、意图延迟与事件检测预算；共享核心档位只报告，
用于 D003 语言结论）；recovery_edges（v2 每条边都有绑定其内容与本版本的验证记录）；jil（待硬件，因此 M3 本身尚不能
关闭）。恢复记录必然位于其所引用版本之后的提交中；该提交与被测版本之间只能相差这些记录与文档，且边定义必须相同。
没有证据的判据是 `missing`，绝不是 `passed`。
"""

from __future__ import annotations

import argparse
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
M2 = runpy.run_path(str(ROOT / "scripts/verify_m2_release.py"))
SUPERVISION_P99_S, SUPERVISION_MAX_S, INTENT_P99_MS, EDGE_P99_MS = 0.120, 0.200, 250.0, 1000.0
RECORDS_MAY_CHANGE = ("configs/recovery_policies/multirotor_m3_v2.yaml", "docs/", "eval/BASELINES.md", "README.md",
                      "README.zh-CN.md", "CLAUDE.md")
ZENOH_CASES = ("nl_inspect_red", "service_outage")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8")


def at_records_revision(sha: str) -> dict:
    """HEAD is the tested revision or a clean descendant that only adds records and documentation.

    HEAD 是被测版本，或只增加记录与文档的干净后代提交。
    """
    head = git("rev-parse", "HEAD").strip()
    if git("status", "--porcelain", "--", "src", "configs", "eval", "proto", "scripts", "sim", "ros2_ws").strip():
        raise SystemExit("run the gate from a clean tree")
    if head == sha:
        return {"records_sha": head, "changed": []}
    if subprocess.run(["git", "merge-base", "--is-ancestor", sha, head], cwd=ROOT).returncode:
        raise SystemExit("HEAD does not descend from the tested revision")
    changed = [path for path in git("diff", "--name-only", sha, head).splitlines() if path]
    outside = [path for path in changed if not path.startswith(RECORDS_MAY_CHANGE)]
    if outside:
        raise SystemExit(f"the records commit changes more than records and documentation: {outside}")
    return {"records_sha": head, "changed": changed}


def suite_at(sha: str, path: str) -> dict:
    return yaml.safe_load(git("show", f"{sha}:{path}"))


def good(row: dict, sha: str) -> bool:
    return (row.get("source_sha") == sha and row.get("passed") is True and row.get("replay_agrees") is True
            and row.get("judge_exit_code") == 0 and row.get("false_success_reports") == 0 and row.get("problems") == []
            and not row.get("void"))


def m3_rows(receipts: list[dict], sha: str) -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for receipt in receipts:
        if receipt.get("source_sha") != sha:
            problems.append("a receipt belongs to another revision")
        if receipt.get("other_containers_before") != receipt.get("other_containers_after"):
            problems.append("shared-server isolation not verified")
        rows += receipt.get("results", [])
    return rows, problems


def default_tier(row: dict) -> bool:
    measure = row.get("measure") or {}
    return measure.get("edge", True) and measure.get("isolation", "separate") == "separate" and measure.get(
        "edge_period_s", 1.0) == 1.0


def m3_suite(receipts: list[dict], sha: str) -> dict:
    if not receipts:
        return {"status": "missing"}
    suite = suite_at(sha, "configs/scenarios/m3_suite.yaml")
    expected = {(case["id"], seed) for case in suite["scenarios"] for seed in suite["seeds"]}
    nodes = {case["id"]: case.get("nodes", True) for case in suite["scenarios"]}
    rows, problems = m3_rows(receipts, sha)
    rows = [row for row in rows if default_tier(row)]
    passed, void = set(), []
    for row in rows:
        key = row.get("scenario"), row.get("seed")
        if key not in expected:
            problems.append(f"unexpected case {key}")
        elif row.get("void"):
            void.append(key)
        elif not good(row, sha):
            problems.append(f"case {key} failed: {row.get('classification')} {row.get('problems') or row.get('error')}")
        else:
            if nodes[key[0]] and (row.get("shadow_report") or {}).get("exit_code") != 0:
                problems.append(f"case {key} has no shadow report")
            if not (row.get("metrics") or {}).get("resources"):
                problems.append(f"case {key} has no resource samples")
            passed.add(key)
    missing = sorted(expected - passed)
    judged = len(rows)
    return {"status": "passed" if not problems and not missing else "failed", "expected": len(expected),
            "passed": len(passed), "missing": missing, "problems": problems, "void": sorted(void),
            "void_rate": round(len(void) / judged, 3) if judged else None,
            "false_success_reports": sum(r.get("false_success_reports", 0) for r in rows)}


def m2_zenoh(receipts: list[dict], sha: str) -> dict:
    if not receipts:
        return {"status": "missing"}
    suite = suite_at(sha, "configs/scenarios/m2_suite.yaml")
    expected = {(case, seed) for case in ZENOH_CASES for seed in suite["seeds"]}
    passed, problems = set(), []
    for receipt in receipts:
        if receipt.get("source_sha") != sha or receipt.get("transport") != "zenoh":
            problems.append("a receipt is not a Zenoh run of this revision")
        for row in receipt.get("results", []):
            key = row.get("scenario"), row.get("seed")
            if key in expected and row.get("passed") is True and row.get("transport") == "zenoh":
                passed.add(key)
            elif key in expected:
                problems.append(f"case {key} failed over Zenoh")
    missing = sorted(expected - passed)
    return {"status": "passed" if not problems and not missing else "failed", "passed": len(passed),
            "missing": missing, "problems": problems}


def tier_of(row: dict) -> str | None:
    measure = row.get("measure") or {}
    if row.get("scenario") == "route_fallback":
        return "idle"
    if row.get("scenario") != "ext_inspect":
        return None
    if not measure.get("edge", True):
        return "perception"
    return "inference" if measure.get("edge_period_s") == 0.0 else None


def measurement(receipts: list[dict], sha: str) -> dict:
    if not receipts:
        return {"status": "missing"}
    rows, problems = m3_rows(receipts, sha)
    cells: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        tier = tier_of(row)
        if tier is None or row.get("void") or row.get("source_sha") != sha:
            continue
        isolation = (row.get("measure") or {}).get("isolation", "separate")
        cells.setdefault((tier, isolation), []).append(row)
    table = {}
    for (tier, isolation), cell in sorted(cells.items()):
        metrics = [row.get("metrics") or {} for row in cell]
        supervision = [m.get("supervision") or {} for m in metrics]
        edge = [((m.get("edge_inference") or {}).get("latency_ms") or {}).get("p99") for m in metrics]
        entry = {
            "seeds": sorted(row.get("seed") for row in cell),
            "supervision_p99_s": max((s.get("p99_s") or 0) for s in supervision),
            "supervision_max_s": max((s.get("max_s") or 0) for s in supervision),
            "intent_p99_ms": max((m.get("intent_latency_p99_ms") or 0) for m in metrics),
            "edge_p99_ms": max((value or 0) for value in edge),
            "guardian_cpu_mean": max(((m.get("resources") or {}).get("guardian") or {}).get("cpu_mean_cores", 0)
                                     for m in metrics),
            "guardian_throttled": max(((m.get("resources") or {}).get("guardian") or {}).get("throttled_fraction", 0)
                                      for m in metrics),
        }
        entry["within_budget"] = (entry["supervision_p99_s"] <= SUPERVISION_P99_S
                                  and entry["supervision_max_s"] <= SUPERVISION_MAX_S
                                  and entry["intent_p99_ms"] <= INTENT_P99_MS and entry["edge_p99_ms"] <= EDGE_P99_MS)
        table[f"{tier}/{isolation}"] = entry
    gated = [table.get(f"{tier}/separate") for tier in ("idle", "perception", "inference")]
    if any(entry is None or len(set(entry["seeds"])) < 3 for entry in gated):
        return {"status": "missing", "cells": table, "problems": problems}
    ok = all(entry["within_budget"] for entry in gated) and not problems
    return {"status": "passed" if ok else "failed", "cells": table, "problems": problems}


def recovery_edges(sha: str) -> dict:
    from drone_agent.contracts import RecoveryPolicy

    path = "configs/recovery_policies/multirotor_m3_v2.yaml"
    policy = RecoveryPolicy.model_validate(yaml.safe_load((ROOT / path).read_text(encoding="utf-8")))
    tested = RecoveryPolicy.model_validate(suite_at(sha, path))
    problems = []
    if [e.validation_hash() for e in policy.edges] != [e.validation_hash() for e in tested.edges]:
        problems.append("the recorded policy's edges differ from the tested revision")
    unverified = [e.fault_injection_scenario for e in policy.unverified_edges()]
    stale = [e.fault_injection_scenario for e in policy.edges
             if e.validation is not None and e.validation.software_revision != sha]
    status = "passed" if not problems and not unverified and not stale else "failed"
    return {"status": status, "edges": len(policy.edges), "unverified": unverified, "other_revision": stale,
            "problems": problems}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="the tested revision")
    parser.add_argument("--deployment", type=Path, help="cloud deployment receipt for this revision")
    parser.add_argument("--m3", type=Path, nargs="*", default=[], help="remote_m3 receipts (suite and measurement)")
    parser.add_argument("--m1", type=Path, nargs="*", default=[], help="remote_m1 receipts on this revision")
    parser.add_argument("--m2", type=Path, nargs="*", default=[], help="remote_m2 gRPC receipts on this revision")
    parser.add_argument("--m2-zenoh", type=Path, nargs="*", default=[], help="remote_m2 Zenoh receipts")
    parser.add_argument("--jil", type=Path, help="Jetson-in-the-loop receipt (none before hardware)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    inputs = [p for p in [args.deployment, args.jil, *args.m3, *args.m1, *args.m2, *args.m2_zenoh] if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")
    records = at_records_revision(args.sha)

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    m3 = [load(p) for p in args.m3]
    criteria = {
        "checks": M2["checks"](load(args.deployment), args.sha),
        "adversarial": M2["adversarial"](),
        "m3_suite": m3_suite(m3, args.sha),
        "m1_regression": M2["m1_regression"]([load(p) for p in args.m1], args.sha),
        "m2_regression": M2["e2e"]([load(p) for p in args.m2], args.sha),
        "m2_zenoh": m2_zenoh([load(p) for p in args.m2_zenoh], args.sha),
        "measurement": measurement(m3, args.sha),
        "recovery_edges": recovery_edges(args.sha),
        "jil": {"status": "missing", "reason": "no Jetson-in-the-loop hardware yet (D038)"} if args.jil is None
        else {"status": "failed", "reason": "JIL receipts are not evaluated by this revision of the gate"},
    }
    sitl = all(c["status"] == "passed" for name, c in criteria.items() if name != "jil")
    report = {
        "schema_version": "0.1.0", "milestone": "M3", "source_sha": args.sha, "records": records,
        "m3_sitl": "passed" if sitl else "not_passed",
        "status": "passed" if sitl and criteria["jil"]["status"] == "passed" else "not_passed",
        "criteria": criteria,
        "inputs": {str(p.name): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"m3_sitl": report["m3_sitl"], "status": report["status"],
                      **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if sitl else 1)


if __name__ == "__main__":
    main()
