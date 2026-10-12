"""WP-P6-04 gate: the asset archive on one immutable candidate (D087).

Criteria, bound to the same full commit unless stated:
  scope       — changes since the D086 records stay in the fleet service and its stores, the evaluation code, the
                desk console's server frames, the desk scripts, the archive's configuration and S0 fixtures, tests
                and docs; no onboard path, wire or flight configuration changed
  checks      — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
                skips
  s0          — run here: the archive matrix E1-E6 for every seed, judged by `eval/judge_assets.py` with every P6, P4,
                P2 and P1 check, all as expected and every count zero
  regression  — run here: the P1-P6 S0 matrices and the authorization matrix (which now holds the `assets.*` methods
                and frames) on the desk catalogs
  desk        — the resident desk activated on this candidate: every catalog and migration, the capacity migration,
                and the archive migrated (or current) with its drill on the copy passed: migrated again without a
                change, nothing outside the archive changed, and one observation per verified capture of an inspected
                asset; page, script, health, forged identity ignored
  desk_assets — through the entry, as a tailnet person: the project's assets with their revisions, one asset's
                timeline holding observations whose viewpoint the real telemetry measures, and a comparison whose
                viewpoint is measured, with no model field in the fact column or the factors and a summary in both
                languages
  historical  — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`.

WP-P6-04 门禁：在同一不可变候选上核对资产档案（D087）。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的判据是
`missing`，绝不是 `passed`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PC = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
M2, P2, P6R = PC["M2"], PC["P2"], PC["P6R"]
P3 = runpy.run_path(str(ROOT / "scripts/verify_p3_release.py"))
P4 = runpy.run_path(str(ROOT / "scripts/verify_p4_release.py"))
DOCS, ONBOARD = PC["DOCS"], PC["ONBOARD"]
git, show, digest, ancestor, input_name = (PC[k] for k in ("git", "show", "digest", "ancestor", "input_name"))
BASE_SHA = "a80beb4"
PATHS = ("src/drone_agent/fleet/", "src/drone_agent/eval/", "src/drone_agent/console/mission.py", "scripts/",
         "configs/analysis/compare_v1.yaml", "configs/scenarios/p6_assets_suite.yaml",
         "configs/scenarios/p6_site_b_e4_v1.yaml", "tests/")
EVIDENCE = "docs/verification/asset-archive-2026-10-13/"
# Each earlier record and the commit that added it. / 每份先前记录及引入它的提交。
RECORDS = {"docs/verification/explain-check-2026-10-12/release.json": "a80beb4",
           "docs/verification/capacity-retention-2026-10-12/release.json": "cd327cb",
           "docs/verification/fine-reservations-2026-10-12/release.json": "5b49d3f",
           "docs/verification/explain-2026-10-11/release.json": "22d4464"}


def scope(sha: str) -> dict:
    base = git("rev-parse", BASE_SHA)
    if not ancestor(base, sha):
        return {"status": "failed", "reason": "candidate does not descend from the D086 records"}
    changed = git("diff", "--name-only", base, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *PATHS))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": base, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard}
    return {"status": "failed" if outside or onboard else "passed", **result}


def s0(sha: str, output: Path) -> dict:
    """Run the archive matrix here on the clean candidate. / 在干净候选上当场运行档案矩阵。"""
    from drone_agent.eval.judge_assets import COUNTS
    from drone_agent.eval.p6_assets import run_suite, suite

    definition = suite(ROOT)
    summary = asyncio.run(run_suite(output, ROOT, sha=sha))
    expected = {(case["id"], seed) for case in definition["s0"] for seed in definition["seeds"]}
    rows = summary["results"]
    problems = [] if {(r["scenario"], r["seed"]) for r in rows} == expected else ["case_coverage_mismatch"]
    problems += [f"{r['scenario']}-{r['seed']}:{r['classification']}" for r in rows if not r["passed"]]
    counts = {k: sum((r.get("counts") or {}).get(k, 0) for r in rows) for k in COUNTS}
    if any(counts.values()):
        problems.append("nonzero_counts")
    return {"status": "passed" if not problems else "failed", "cases": len(rows), "expected": len(expected),
            "counts": counts, "voided_attempts": len(summary["voided_attempts"]), "problems": problems,
            "summary_sha256": digest((output / "suite.json").read_bytes())}


def regression(sha: str, work: Path) -> dict:
    parts = {"p1": P2["p1_regression"](sha, work / "p1"), "p2": P2["s0_matrix"](sha, work / "p2"),
             "p3": P3["s0_matrix"](sha, work / "p3"), "p4": P4["s0_matrix"](sha, work / "p4"),
             "p5": P6R["p5_regression"](sha, work / "p5"), "p6": P6R["s0"](sha, work / "p6"),
             "authz": P6R["authz"](work / "authz")}
    ok = all(part["status"] == "passed" for part in parts.values())
    return {"status": "passed" if ok else "failed", **parts}


def desk(activation: dict | None, http: dict | None, spoof: dict | None, sha: str) -> dict:
    base = P6R["desk"](activation, http, spoof, sha)
    if base["status"] == "missing":
        return base
    problems = list(base.get("problems") or [])
    capacity = (activation or {}).get("capacity") or {}
    if (capacity.get("migration") or {}).get("status") not in ("migrated", "current") or \
            (capacity.get("drill") or {}).get("status") != "passed":
        problems.append("desk_capacity_migration_or_drill_not_passed")
    assets = (activation or {}).get("assets") or {}
    migration, drill = assets.get("migration") or {}, assets.get("drill") or {}
    if migration.get("status") not in ("migrated", "current"):
        problems.append("desk_without_the_asset_archive")
    if drill.get("status") != "passed" or drill.get("again") != "current" or \
            drill.get("other_sha256_before") != drill.get("other_sha256_after"):
        problems.append("asset_archive_drill_not_passed")
    rows = drill.get("rows") or {}
    if rows.get("ar_observations") != drill.get("eligible_captures"):
        problems.append("the_drill_did_not_observe_every_verified_capture")
    if (assets.get("compare") or {}).get("version") != "compare-v1":
        problems.append("the_service_did_not_load_compare_v1")
    return {**base, "status": "failed" if problems else "passed", "problems": problems,
            "assets": {"migration": migration.get("status"), "backfill": migration.get("backfill"),
                       "maps": assets.get("maps"), "compare": assets.get("compare"),
                       "drill": {k: drill.get(k) for k in ("status", "migration", "again", "rows", "eligible_captures",
                                                           "viewpoint_unknown", "elapsed_s")}}}


def desk_assets(receipt: dict | None) -> dict:
    """The archive read through the desk entry, from the probe receipt. / 从探针回执读取经任务台入口的档案。"""
    if receipt is None:
        return {"status": "missing", "reason": "a desk_probe.py assets receipt"}
    problems = []
    if (receipt.get("hello") or {}).get("identity_scheme") != "tailnet":
        problems.append("not_a_tailnet_person")
    if receipt.get("errors"):
        problems.append("the_entry_refused_a_read")
    listed = receipt.get("assets") or []
    if not listed or not all(item.get("revisions") for item in listed):
        problems.append("assets_without_revisions")
    timeline = receipt.get("timeline") or {}
    if not timeline.get("observations") or not timeline.get("measured"):
        problems.append("no_observation_with_a_measured_viewpoint")
    if timeline.get("model_fields_in_fact_column"):
        problems.append("model_field_in_the_fact_column")
    compare = receipt.get("compare") or {}
    if not compare:
        problems.append("no_comparison")
    else:
        if compare.get("viewpoint") == "unknown" or compare.get("definition") == "unknown":
            problems.append("the_comparison_could_not_measure_the_real_captures")
        if compare.get("model_fields_in_factors"):
            problems.append("model_field_in_a_factor")
        summary = compare.get("summary") or {}
        if not summary.get("zh") or not summary.get("en"):
            problems.append("summary_missing")
        if (compare.get("compare") or {}).get("version") != "compare-v1":
            problems.append("comparison_not_under_compare_v1")
    return {"status": "failed" if problems else "passed", "problems": problems, "assets": len(listed),
            "timeline": {k: timeline.get(k) for k in ("items", "observations", "measured", "kinds")},
            "compare": {k: compare.get(k) for k in ("definition", "viewpoint", "viewpoint_measures", "exceeded",
                                                    "events", "summary")}}


def historical() -> dict:
    problems, found = [], {}
    for path, commit in RECORDS.items():
        original = show(commit, path)
        found[path] = {"sha256": digest(original), "commit": commit}
        if (ROOT / path).read_bytes() != original:
            problems.append(f"record_changed:{path}")
    return {"status": "failed" if problems else "passed", "problems": problems, "records": found}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--desk-assets", type=Path, help="desk_probe.py assets receipt")
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "asset-archive-gate")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    if args.work.exists() and any(args.work.iterdir()):
        parser.error(f"use an empty work directory: {args.work}")
    head = git("rev-parse", "HEAD")
    if not ancestor(args.sha, head):
        parser.error("HEAD must be the candidate or its evidence-only descendant")
    newer = git("diff", "--name-only", args.sha, head).splitlines()
    dirty = git("status", "--porcelain", "--", "src", "tests", "scripts", "configs", "proto", "sim", "ros2_ws",
                "pyproject.toml", "uv.lock")
    if any(not p.startswith(EVIDENCE) for p in newer) or dirty:
        parser.error("gate implementation and runtime must match the clean candidate")
    inputs = [p for p in (args.deployment, args.desk, args.desk_http, args.desk_spoof, args.desk_assets) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "s0": s0(args.sha, args.work / "assets"),
        "regression": regression(args.sha, args.work),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_spoof), args.sha),
        "desk_assets": desk_assets(load(args.desk_assets)),
        "historical": historical(),
    }
    passed = all(v["status"] == "passed" for v in criteria.values())
    report = {"schema_version": "0.1.0", "work_package": "WP-P6-04", "decision_record": "D087", "source_sha": args.sha,
              "records_sha": head, "checked_at": datetime.now(UTC).isoformat(),
              "status": "passed" if passed else "not_passed", "criteria": criteria,
              "capabilities": [{"capability": "asset_archive_v1", "decision": "D087",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "compare": "compare-v1", "real_devices": False}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in inputs}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
