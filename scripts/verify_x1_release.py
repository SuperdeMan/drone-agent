"""X1-01 gate: the air-ground handoff on one immutable candidate (D088, design section 9).

Criteria, bound to the same full commit unless stated:
  scope            — the candidate descends from the D087 records and leaves the resident desk (its catalogs, compose
                     file and pages) unchanged, since the desk is outside X1; the onboard paths that changed are listed,
                     and because there are some, the M1, M2 and M3 regressions below are required
  checks           — the cloud deployment receipt: the whole suite on Linux with 0 failures, errors and skips, and the
                     wire checks
  adversarial      — the deterministic and natural-language corpora authorize nothing (admission gained a ground path)
  s0               — run here: the 13 X1 S0 cases x 3 seeds, judged online and from the recordings, every count 0
  p_regression     — run here: the P1-P6 S0 matrices (P6: recapture and the asset archive) and the P5 authorization
                     matrix
  s1               — remote_x1 receipts: the 5 X1 S1 cases x 3 seeds on PX4 SITL passed online and from the recordings,
                     every count 0, with sufficient capacity and verified isolation
  m1_regression    — the M1 suite on this candidate (the guardian, the executive and the PX4 adapter changed)
  m2_e2e           — the M2 end-to-end suite on this candidate (the uplink and the fleet protocol changed)
  m3_suite         — the M3 scenario set on this candidate (every recovery ends through the guardian's changed terminal)
  p3_s1_regression — the P3 two-aircraft S1 suite on this candidate (scheduling gained a ground layer)
  historical       — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`. S0 and S1 are counted separately; nothing here proves a
real ground robot, Nav2 or a real camera.

X1-01 门禁：在同一不可变候选上核对空地交接（D088，方案第 9 节）。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的
判据是 `missing`，绝不是 `passed`。S0 与 S1 分开计数；这里的任何内容都不证明真实地面车、Nav2 或真实相机。
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

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

AA = runpy.run_path(str(ROOT / "scripts/verify_asset_archive.py"))
PC = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
M2, P3 = AA["M2"], AA["P3"]
M3 = runpy.run_path(str(ROOT / "scripts/verify_m3_release.py"))
DOCS, ONBOARD = PC["DOCS"], PC["ONBOARD"]
git, show, digest, ancestor, input_name = (PC[k] for k in ("git", "show", "digest", "ancestor", "input_name"))
BASE_SHA = "57dabf3"
# The resident desk is outside X1 (design section 13). / 常驻任务台不在 X1 范围内（方案第 13 节）。
DESK = ("sim/compose.desk.yaml", "configs/sites/p5_desk", "configs/workflows/p6_desk", "configs/analysis/p6_desk",
        "configs/scheduling/p5_desk", "src/drone_agent/console/")
# The CI workflow changed with the user's approval of 2026-10-14 (node 24 actions). / 2026-10-14 经用户批准改动的 CI 工作流。
APPROVED = (".github/workflows/checks.yml",)
EVIDENCE = (*DOCS, ".github/")
FINGERPRINTS = {"ca", "service", "robot", "robot-tls-ugv_01"}
RECORDS = {"docs/verification/asset-archive-2026-10-13/release.json": "57dabf3",
           "docs/verification/capacity-retention-2026-10-12/release.json": "cd327cb",
           "docs/verification/fine-reservations-2026-10-12/release.json": "5b49d3f",
           "docs/verification/explain-check-2026-10-12/release.json": "a80beb4",
           "docs/verification/p6-recapture-2026-10-10/release.json": "3e83cb2",
           "docs/verification/p6-adversarial-2026-10-11/release.json": "017970e",
           "docs/verification/m3-2026-09-25/release.json": "0f232d7",
           "docs/verification/p5-2026-10-06/release.json": "32dcb70"}


def scope(sha: str) -> dict:
    base = git("rev-parse", BASE_SHA)
    if not ancestor(base, sha):
        return {"status": "failed", "reason": "candidate does not descend from the D087 records"}
    changed = git("diff", "--name-only", base, sha).splitlines()
    desk = [p for p in changed if p.startswith(DESK)]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": base, "changed": len(changed), "desk_changed": desk, "onboard_changed": onboard,
              "approved_outside_code": [p for p in changed if p in APPROVED],
              "regressions_required": ["m1", "m2_e2e", "m3_suite"] if onboard else []}
    return {"status": "failed" if desk else "passed", **result}


def s0(sha: str, output: Path) -> dict:
    """Run the X1 S0 matrix here on the clean candidate. / 在干净候选上当场运行 X1 S0 矩阵。"""
    from drone_agent.eval.judge_x1 import COUNTS
    from drone_agent.eval.x1_world import run_suite, suite

    definition = suite(ROOT)
    summary = asyncio.run(run_suite(output, ROOT, sha=sha))
    expected = {(case["id"], seed) for case in definition["s0"] for seed in case.get("seeds", definition["seeds"])}
    rows = summary["results"]
    problems = [] if {(r["scenario"], r["seed"]) for r in rows} == expected else ["case_coverage_mismatch"]
    problems += [f"{r['scenario']}-{r['seed']}:{r['classification']}" for r in rows if not r["passed"]]
    counts = {k: sum((r.get("counts") or {}).get(k, 0) for r in rows) for k in COUNTS}
    if any(counts.values()):
        problems.append("nonzero_counts")
    return {"status": "passed" if not problems else "failed", "cases": len(rows), "expected": len(expected),
            "counts": counts, "metrics": summary["metrics"], "voided_attempts": len(summary["voided_attempts"]),
            "problems": problems, "summary_sha256": digest((output / "suite-s0.json").read_bytes())}


def p_regression(sha: str, work: Path) -> dict:
    """P1-P6 S0 and the P5 authorization matrix, plus the asset archive matrix. / P1–P6 S0、P5 授权矩阵与资产档案矩阵。"""
    parts = AA["regression"](sha, work)
    parts["p6_assets"] = AA["s0"](sha, work / "p6-assets")
    ok = all(part["status"] == "passed" for name, part in parts.items() if name != "status")
    return {**parts, "status": "passed" if ok else "failed"}


def s1(receipts: list[dict], sha: str) -> dict:
    """Every X1 S1 case x seed on PX4 SITL. / PX4 SITL 上每个 X1 S1 用例 × 种子。"""
    from drone_agent.eval.judge_x1 import COUNTS

    if not receipts:
        return {"status": "missing"}
    suite = yaml.safe_load(show(sha, "configs/scenarios/x1_suite.yaml").decode("utf-8"))
    expected = {(case["id"], seed) for case in suite["s1"] for seed in case.get("seeds", suite["seeds"])}
    results, problems = {}, []
    for receipt in receipts:
        if receipt.get("source_sha") != sha or receipt.get("status") != "passed" or receipt.get("layer") != "S1":
            problems.append("a receipt failed, belongs to another revision or is not S1")
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
            sufficient = ((row.get("capacity") or {}).get("verdict") or {}).get("sufficient") is True
            if not (row.get("passed") is True and row.get("replay_agrees") is True and row.get("judge_exit_code") == 0
                    and row.get("problems") == [] and row.get("source_sha") == sha and row.get("layer") == "S1"
                    and set(counts) == set(COUNTS) and not any(counts.values()) and sufficient):
                problems.append(f"case {key} did not meet the independent criteria")
            results[key] = row
    missing = sorted(expected - set(results))
    flown = {robot for r in results.values() for robot, runs in (r.get("flown") or {}).items() if runs}
    if results and flown != {"uav_01", "ugv_01"}:
        problems.append("the matrix did not run both robots")
    return {"status": "passed" if not problems and not missing else "failed", "cases": len(results),
            "expected": len(expected), "missing": missing, "problems": problems,
            "counts": {k: sum((r.get("counts") or {}).get(k, 0) for r in results.values()) for k in COUNTS},
            "runs": sum(len(f) for r in results.values() for f in (r.get("flown") or {}).values()),
            "metrics": {f"{k[0]}-{k[1]}": r.get("metrics") for k, r in sorted(results.items())}}


def regression(name: str, function, receipts: list[dict], sha: str) -> dict:
    if not receipts:
        return {"status": "missing", "phase": name}
    return {"phase": name, **function(receipts, sha)}


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
    parser.add_argument("--s1", type=Path, nargs="*", default=[], help="remote_x1 receipts")
    parser.add_argument("--m1", type=Path, nargs="*", default=[], help="M1 suite receipts")
    parser.add_argument("--m2", type=Path, nargs="*", default=[], help="M2 end-to-end receipts")
    parser.add_argument("--m3", type=Path, nargs="*", default=[], help="M3 scenario set receipts")
    parser.add_argument("--p3-s1", type=Path, nargs="*", default=[], help="P3 S1 receipts")
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "x1-gate")
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
                "eval/adversarial", "pyproject.toml", "uv.lock")
    if any(not p.startswith(EVIDENCE) for p in newer) or dirty:
        parser.error("gate implementation and runtime must match the clean candidate")
    lists = (*args.s1, *args.m1, *args.m2, *args.m3, *args.p3_s1)
    inputs = [p for p in (args.deployment, *lists) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "adversarial": M2["adversarial"](),
        "s0": s0(args.sha, args.work / "x1"),
        "p_regression": p_regression(args.sha, args.work),
        "s1": s1([load(p) for p in args.s1], args.sha),
        "m1_regression": regression("M1", M2["m1_regression"], [load(p) for p in args.m1], args.sha),
        "m2_e2e": regression("M2", M2["e2e"], [load(p) for p in args.m2], args.sha),
        "m3_suite": regression("M3", M3["m3_suite"], [load(p) for p in args.m3], args.sha),
        "p3_s1_regression": regression("P3", P3["s1"], [load(p) for p in args.p3_s1], args.sha),
        "historical": historical(),
    }
    passed = all(v["status"] == "passed" for v in criteria.values())
    report = {"schema_version": "0.1.0", "work_package": "WP-X1-01", "decision_record": "D088", "source_sha": args.sha,
              "records_sha": head, "checked_at": datetime.now(UTC).isoformat(),
              "status": "passed" if passed else "not_passed", "criteria": criteria,
              "capabilities": [{"capability": "air_ground_handoff_v1", "work_package": "WP-X1-01", "decision": "D088",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "layers": {"S0": {"logical_aircraft": 2, "logical_ground_robots": 2},
                                           "S1": {"px4_sitl_aircraft": 1, "px4_sitl_rovers": 1,
                                                  "world": "one Gazebo world"}},
                                "real_ground_robot": False, "nav2": False, "real_cameras": False,
                                "approval": "every aircraft and ground mission approved by a person"}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in inputs}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
