"""WP-P6-07 gate: capacity observability and data retention on one immutable candidate (D085).

Criteria, bound to the same full commit unless stated:
  scope        — changes since the D084 records stay in the fleet service and its stores, the evaluation code, the desk
                 scripts and compose file, tests and docs; no onboard path, wire or flight configuration changed
  checks       — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
                 skips
  prereg       — the pre-registration names the candidate and freezes by digest the code under test, the design, the
                 template and the three generated ledgers, and the run order; it is an evidence-only descendant of the
                 candidate, every measurement started after it in the registered order, and every self-check of the
                 generated ledgers passed before it
  reproducible — run here when `--template` is given: the 1x ledger regenerated from the registered template matches
                 its registered digest byte for byte
  ladder       — the pre-registered judgement (`capacity_growth.judge`) recomputed from the six measurements: valid,
                 and every criterion holds for the migrated variant
  regression   — run here: the P1-P6 S0 matrices and the authorization matrix on the desk catalogs
  desk         — the resident desk activated on this candidate: every catalog and migration including the capacity
                 migration and its drill on the copy, a 7-day status retention, page, script, health, forged identity
                 ignored
  desk_capacity — the desk's read-only capacity receipt: this revision serving, the archive read back chunk by chunk,
                 nothing older than the retention plus a day left, the startup reconciliation finished, and a fresh
                 snapshot with pass p95 and dock report p99 within 100 ms
  historical   — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`.

WP-P6-07 门禁：在同一不可变候选上核对容量观测与数据保留（D085）。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的
判据是 `missing`，绝不是 `passed`。
"""

from __future__ import annotations

import argparse
import json
import re
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drone_agent.eval.capacity_growth import ORDER, judge  # noqa: E402

PC = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
M2, P2, P6R = PC["M2"], PC["P2"], PC["P6R"]
P3 = runpy.run_path(str(ROOT / "scripts/verify_p3_release.py"))
P4 = runpy.run_path(str(ROOT / "scripts/verify_p4_release.py"))
DOCS, ONBOARD = PC["DOCS"], PC["ONBOARD"]
git, show, digest, ancestor, input_name = (PC[k] for k in ("git", "show", "digest", "ancestor", "input_name"))
BASE_SHA = "039d15a"
PATHS = ("src/drone_agent/fleet/", "src/drone_agent/eval/", "scripts/", "sim/compose.desk.yaml", "tests/")
EVIDENCE_DIR = "docs/verification/capacity-retention-2026-10-12"
EVIDENCE = (*DOCS,)
SCALES = sorted({scale for scale, _ in ORDER})
RECORDS = {"docs/verification/fine-reservations-2026-10-12/release.json": "5b49d3f",
           "docs/verification/explain-2026-10-11/release.json": "22d4464",
           "docs/verification/reference-check-2026-10-11/release.json": "898b2fe",
           "docs/verification/p6-adversarial-2026-10-11/release.json": "017970e"}


def scope(sha: str) -> dict:
    base = git("rev-parse", BASE_SHA)
    if not ancestor(base, sha):
        return {"status": "failed", "reason": "candidate does not descend from the D084 records"}
    changed = git("diff", "--name-only", base, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *PATHS))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": base, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard}
    return {"status": "failed" if outside or onboard else "passed", **result}


def _load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def measurements(evidence: Path) -> dict:
    return {(scale, variant): _load(evidence / f"measure-x{scale}-{variant}.json") for scale, variant in ORDER}


def prereg(sha: str, evidence: Path, runs: dict) -> dict:
    path = evidence / "preregistration.json"
    body = _load(path)
    if body is None:
        return {"status": "missing", "reason": f"{path.relative_to(ROOT)}"}
    problems = []
    relative = path.relative_to(ROOT).as_posix()
    added = git("log", "--diff-filter=A", "--format=%H %cI", "--", relative).splitlines()
    if len(added) != 1:
        return {"status": "failed", "reason": "the pre-registration must be added in exactly one commit"}
    commit, committed = added[0].split(" ", 1)
    if not ancestor(sha, commit) or any(not p.startswith(EVIDENCE) for p in
                                        git("diff", "--name-only", sha, commit).splitlines()):
        problems.append("the pre-registration commit is not an evidence-only descendant of the candidate")
    if body.get("candidate_sha") != sha:
        problems.append("the pre-registration names another candidate")
    for name, value in (body.get("files") or {}).items():
        if digest(show(sha, name)) != value:
            problems.append(f"digest_differs:{name}")
    if body.get("order") != [list(item) for item in ORDER]:
        problems.append("the registered order is not the ladder's order")
    template = _load(evidence / "template.json")
    if template is None or template.get("ledger_sha256") != body.get("template_sha256"):
        problems.append("the template record does not match the registered template")
    for scale in SCALES:
        generated, check = _load(evidence / f"generated-x{scale}.json"), _load(evidence / f"check-x{scale}.json")
        if generated is None or generated.get("ledger_sha256") != (body.get("generated") or {}).get(str(scale)):
            problems.append(f"generated_x{scale}_differs_from_the_registration")
        if check is None or check.get("status") != "passed" or check.get("writes"):
            problems.append(f"check_x{scale}_not_passed")
    previous = _at(committed)
    for (scale, variant), run in runs.items():
        if run is None:
            continue
        if _at(run["started_at"]) <= previous:
            problems.append(f"x{scale}-{variant} started before the pre-registration or out of order")
        previous = _at(run["started_at"])
        if run["generated"].get("ledger_sha256") != (body.get("generated") or {}).get(str(scale)):
            problems.append(f"x{scale}-{variant} measured another ledger")
    return {"status": "failed" if problems else "passed", "problems": problems, "commit": commit,
            "committed_at": committed}


def reproducible(sha: str, template: Path | None, evidence: Path, work: Path) -> dict:
    """Regenerate the 1x ledger from the template and compare it byte for byte. / 由模板重新生成 1 倍账本并逐字节比较。"""
    body = _load(evidence / "preregistration.json") or {}
    if template is None:
        return {"status": "missing", "reason": "pass --template with the registered template directory"}
    manifest = _load(template / "template.json")
    if manifest is None or manifest.get("ledger_sha256") != body.get("template_sha256") or digest(
            (template / "case/service/ledger.sqlite3").read_bytes()) != body.get("template_sha256"):
        return {"status": "failed", "reason": "the template directory is not the registered template"}
    from drone_agent.eval.capacity_growth import generate

    generated = generate(template, work / "x1", scale=1)
    expected = (body.get("generated") or {}).get("1")
    ok = generated["ledger_sha256"] == expected
    return {"status": "passed" if ok else "failed", "ledger_sha256": generated["ledger_sha256"], "expected": expected}


def ladder(runs: dict, evidence: Path) -> dict:
    body = _load(evidence / "preregistration.json")
    if body is None or any(run is None for run in runs.values()):
        return {"status": "missing", "reason": "the pre-registration and six measurements"}
    verdict = judge(list(runs.values()), body)
    summary = {f"x{scale}-{variant}": {
        "pass_p95": run["phases"]["steady"].get("pass", {}).get("p95"),
        "refresh_mean_ms": run["reconcile"]["refresh_ms"].get("mean"), "reconcile_capped": run["reconcile"]["capped"],
        "docks_report_p99": run["phases"]["steady"].get("api.docks.report", {}).get("p99"),
        "archived": (run.get("archive") or {}).get("archived")} for (scale, variant), run in runs.items()}
    return {"status": "passed" if verdict["status"] == "passed" else "failed", "judgement": verdict,
            "summary": summary}


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
    if (capacity.get("migration") or {}).get("status") not in ("migrated", "current"):
        problems.append("desk_without_capacity_migration")
    if (capacity.get("drill") or {}).get("status") != "passed":
        problems.append("capacity_migration_drill_not_passed")
    if capacity.get("status_retention_days") != 7:
        problems.append("status_retention_is_not_7_days")
    return {**base, "status": "failed" if problems else "passed", "problems": problems,
            "capacity": {"migration": (capacity.get("migration") or {}).get("status"),
                         "drill": (capacity.get("drill") or {}).get("status"),
                         "status_retention_days": capacity.get("status_retention_days")}}


def desk_capacity(receipt: dict | None, sha: str) -> dict:
    if receipt is None:
        return {"status": "missing", "reason": "a dev_stack.py desk-capacity receipt"}
    problems = []
    if receipt.get("status") != "ok" or receipt.get("source_sha") != sha:
        problems.append("the_desk_does_not_serve_this_candidate")
    if (receipt.get("meta") or {}).get("capacity_schema") != "1":
        problems.append("the_desk_ledger_is_not_migrated")
    archive = receipt.get("archive") or {}
    if not archive.get("chunks") or archive.get("problems"):
        problems.append("the_archive_is_empty_or_does_not_read_back")
    if receipt.get("overdue_rows") != 0:
        problems.append("status_rows_older_than_the_retention_remain")
    if (receipt.get("startup") or {}).get("reconciled_uptime_s") is None:
        problems.append("the_startup_reconciliation_did_not_finish")
    latest = receipt.get("latest") or {}
    series = latest.get("series") or {}
    if not latest or (_at(receipt["checked_at"]) - _at(latest["at"])).total_seconds() > 900:
        problems.append("no_fresh_capacity_snapshot")
    elif (series.get("pass") or {}).get("p95", float("inf")) > 100 or \
            (series.get("api.docks.report") or {}).get("p99", float("inf")) > 100:
        problems.append("desk_pass_or_dock_report_latency_over_100_ms")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "archive": {k: archive.get(k) for k in ("chunks", "rows", "bytes", "files", "first_at", "last_at")},
            "status_rows": receipt.get("status_rows"), "overdue_rows": receipt.get("overdue_rows"),
            "startup": receipt.get("startup"),
            "latest": {"at": latest.get("at"), "pass": series.get("pass"),
                       "docks_report": series.get("api.docks.report"), "backlog": latest.get("backlog")}}


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
    parser.add_argument("--template", type=Path, help="the registered template directory (template.json and case/)")
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--desk-capacity", type=Path, help="dev_stack.py desk-capacity receipt")
    parser.add_argument("--evidence", type=Path, default=ROOT / EVIDENCE_DIR)
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "capacity-gate")
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
    args.evidence = args.evidence.resolve()
    inputs = [p for p in (args.deployment, args.desk, args.desk_http, args.desk_spoof, args.desk_capacity) if p]
    inputs += sorted(args.evidence.glob("*.json")) if args.evidence.is_dir() else []
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    runs = measurements(args.evidence)
    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "prereg": prereg(args.sha, args.evidence, runs),
        "reproducible": reproducible(args.sha, args.template, args.evidence, args.work),
        "ladder": ladder(runs, args.evidence),
        "regression": regression(args.sha, args.work),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_spoof), args.sha),
        "desk_capacity": desk_capacity(load(args.desk_capacity), args.sha),
        "historical": historical(),
    }
    passed = all(v["status"] == "passed" for v in criteria.values())
    report = {"schema_version": "0.1.0", "work_package": "WP-P6-07", "decision_record": "D085", "source_sha": args.sha,
              "records_sha": head, "checked_at": datetime.now(UTC).isoformat(),
              "status": "passed" if passed else "not_passed", "criteria": criteria,
              "capabilities": [{"capability": "capacity_retention_v1", "decision": "D085",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "status_retention_days": 7, "real_devices": False}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in inputs}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
