"""WP-P6-06 gate: fine airspace reservations on one immutable candidate (D084).

Criteria, bound to the same full commit unless stated:
  scope      — changes since the cleanup record stay in the reservation, scheduling and judge code, the P3 world and
               layout, the decision rule, the corridor catalogs and suite, the S1 runner plumbing, the experiment,
               scripts, tests and docs; no onboard path, wire or flight configuration changed
  checks     — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
               skips
  prereg     — the pre-registration names the candidate, the rule, suites, catalogs and generator by digest; its
               thresholds recompute from the two committed box noise runs, which finished before it was committed;
               every comparison run started after that commit and ran the pre-registered layouts
  decision   — the pre-registered rule applied to the recorded comparison runs and the corridor S1 receipt: every
               input is what it claims and safety holds everywhere (every case passed, every count and containment
               violation 0); whether corridors are adopted is the rule's benefit verdict, reported as `adopt`
  s1         — the P3 S1 receipts under the box and the corridor catalogs: all cases passed and replayed, counts 0,
               every airborne Gazebo truth sample inside its activity's cells, the shared host unchanged
  regression — run here: the P1 S0 matrix and the P3 S0 matrix under both footprint models
  desk       — the resident desk activated on this candidate (it keeps its v1 catalogs): page, script, health, forged
               identity ignored
  historical — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`.

WP-P6-06 门禁：在同一不可变候选上核对精细空域预约（D084）。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的判据
是 `missing`，绝不是 `passed`。是否采用走廊由预登记规则的收益判据决定，以 `adopt` 报告。
"""

from __future__ import annotations

import argparse
import json
import re
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drone_agent.eval.airspace_choice import decide, thresholds  # noqa: E402

PC = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
M2, P2, P6R = PC["M2"], PC["P2"], PC["P6R"]
DOCS, ONBOARD = PC["DOCS"], PC["ONBOARD"]
EVIDENCE = (*DOCS,)
git, show, digest, ancestor, input_name = (PC[k] for k in ("git", "show", "digest", "ancestor", "input_name"))
BASE_SHA = "73fdb4b"
PATHS = ("src/drone_agent/fleet/reservation.py", "src/drone_agent/fleet/scheduling_models.py",
         "src/drone_agent/fleet/scheduling_store.py", "src/drone_agent/fleet/operations_store.py",
         "src/drone_agent/fleet/dispatch.py", "src/drone_agent/fleet/scheduler.py", "src/drone_agent/eval/",
         "configs/scheduling/p3_campus_corridor_v1.yaml", "configs/scheduling/p3_s1_corridor_v1.yaml",
         "configs/scenarios/p6_airspace_suite.yaml", "sim/compose.p3.yaml", "experiments/fine_reservations/",
         "scripts/", "tests/")
EVIDENCE_DIR = "docs/verification/fine-reservations-2026-10-12"
COMPARISONS = {"box_long": "box-long.json", "corridor_long": "corridor-long.json", "box_ladder": "box-ladder.json",
               "corridor_ladder": "corridor-ladder.json", "box_matrix": "box-matrix.json",
               "corridor_matrix": "corridor-matrix.json"}
NOISE = ("noise-1.json", "noise-2.json")
CASES_S1 = [("p3_s1_parallel", 7), ("p3_s1_parallel", 19), ("p3_s1_parallel", 41), ("p3_s1_contention", 7),
            ("p3_s1_relay", 7)]
RECORDS = {"docs/verification/explain-2026-10-11/release.json": "22d4464",
           "docs/verification/reference-check-2026-10-11/release.json": "898b2fe",
           "docs/verification/p6-adversarial-2026-10-11/release.json": "017970e"}


def scope(sha: str) -> dict:
    base = git("rev-parse", BASE_SHA)
    if not ancestor(base, sha):
        return {"status": "failed", "reason": "candidate does not descend from the cleanup record"}
    changed = git("diff", "--name-only", base, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *PATHS))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": base, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard}
    return {"status": "failed" if outside or onboard else "passed", **result}


def _load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
    noise = [_load(evidence / name) for name in NOISE]
    if any(n is None for n in noise):
        problems.append("noise runs missing")
    else:
        recorded = {n["file"]: n["sha256"] for n in body.get("noise_runs") or []}
        if recorded != {name: digest((evidence / name).read_bytes()) for name in NOISE}:
            problems.append("noise run files differ from the pre-registered digests")
        try:
            if thresholds(*noise) != body.get("thresholds"):
                problems.append("thresholds do not recompute from the noise runs")
        except ValueError as error:
            problems.append(str(error))
        if any(_at(n["finished_at"]) >= _at(committed) for n in noise):
            problems.append("a noise run finished after the pre-registration commit")
    layouts = body.get("layouts") or {}
    for name, summary in runs.items():
        if summary is None:
            continue
        if _at(summary["started_at"]) <= _at(committed):
            problems.append(f"{name} started before the pre-registration commit")
        for row in summary["results"]:
            recorded = ((row.get("metrics") or {}).get("ladder") or {}).get("layout_sha256")
            key = f"{row['scenario']}-{row['seed']}:{summary.get('footprint')}"
            if recorded is not None and layouts.get(key) != recorded:
                problems.append(f"layout_differs:{key}")
    return {"status": "failed" if problems else "passed", "problems": problems, "commit": commit,
            "committed_at": committed, "thresholds": body.get("thresholds")}


def decision(runs: dict, corridor_s1: dict | None, frozen: dict | None) -> dict:
    if any(v is None for v in runs.values()) or corridor_s1 is None or frozen is None:
        return {"status": "missing", "reason": "comparison runs, the corridor S1 receipt or the thresholds"}
    result = decide(**runs, corridor_s1=corridor_s1, frozen=frozen)
    hard = ("inputs", "safety")
    ok = all(result["criteria"][k]["status"] == "passed" for k in hard)
    return {"status": "passed" if ok else "failed", "adopt": result["adopt"], "criteria": result["criteria"]}


def combine(receipts: list[dict] | None) -> dict | None:
    """Several S1 batches of one model as one receipt: each batch must itself pass on an unchanged host.

    同一模型的多批 S1 合为一份回执：每一批都必须在主机不变的情况下自身通过。
    """
    if not receipts:
        return None
    first = receipts[0]
    same = all(r.get("source_sha") == first.get("source_sha") and r.get("layer") == first.get("layer")
               and r.get("footprint", "box-v1") == first.get("footprint", "box-v1") for r in receipts)
    isolated = all(r.get("status") == "passed" and r.get("other_containers_before") == r.get("other_containers_after")
                   for r in receipts)
    body = {"source_sha": first.get("source_sha") if same else None, "layer": first.get("layer") if same else None,
            "status": "passed" if same and isolated else "failed", "batches": len(receipts),
            "results": [row for r in receipts for row in r.get("results") or []],
            "other_containers_before": [], "other_containers_after": []}
    return {**body, "footprint": first["footprint"]} if same and "footprint" in first else body


def s1(box: dict | None, corridor: dict | None, sha: str) -> dict:
    if box is None or corridor is None:
        return {"status": "missing", "reason": "the box and corridor P3 S1 receipts"}
    problems, margins = [], {}
    for label, receipt in (("box-v1", box), ("corridor-v1", corridor)):
        if receipt.get("source_sha") != sha or receipt.get("layer") != "S1":
            problems.append(f"{label}: another commit or layer")
        if receipt.get("footprint", "box-v1") != label:
            problems.append(f"{label}: the receipt ran {receipt.get('footprint')}")
        if receipt.get("status") != "passed" or receipt.get("other_containers_before") != \
                receipt.get("other_containers_after"):
            problems.append(f"{label}: not passed or the shared host changed")
        cases = sorted((row.get("scenario"), row.get("seed")) for row in receipt.get("results") or [])
        if cases != sorted(CASES_S1):
            problems.append(f"{label}: cases {cases} differ from the P3 S1 suite")
        for row in receipt.get("results") or []:
            contained = row.get("containment") or {}
            if not (row.get("passed") is True and row.get("replay_agrees") is True and row.get("judge_exit_code") == 0
                    and not any((row.get("counts") or {}).values()) and contained.get("violations") == 0
                    and contained.get("samples", 0) > 0):
                problems.append(f"{label}: {row.get('scenario')}-{row.get('seed')} did not meet the criteria")
            margins.setdefault(label, []).append(contained.get("min_margin_m"))
    return {"status": "failed" if problems else "passed", "problems": problems,
            "min_margin_m": {k: min((m for m in v if m is not None), default=None) for k, v in margins.items()}}


def p3_matrix(sha: str, work: Path, footprint: str) -> dict:
    output = work / f"p3-{footprint}"
    run = subprocess.run([sys.executable, "-m", "drone_agent.eval.p3_world", "--part", "s0", "--footprint", footprint,
                          "--sha", sha, "--output", str(output)], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    summary = _load(output / "suite-s0.json")
    if summary is None:
        return {"status": "failed", "exit": run.returncode, "tail": run.stdout.strip().splitlines()[-5:]}
    ok = (run.returncode == 0 and summary["status"] == "passed" and summary["cases"] == 45
          and not any(summary["counts"].values()) and summary["containment"]["violations"] == 0
          and summary["containment"]["unjudged"] == 0)
    return {"status": "passed" if ok else "failed", "cases": summary["cases"], "passed": summary["passed"],
            "counts": summary["counts"], "containment": summary["containment"],
            "summary_sha256": digest((output / "suite-s0.json").read_bytes())}


def regression(sha: str, work: Path) -> dict:
    p1 = P2["p1_regression"](sha, work / "p1-regression")
    box, corridor = p3_matrix(sha, work, "box-v1"), p3_matrix(sha, work, "corridor-v1")
    ok = all(part["status"] == "passed" for part in (p1, box, corridor))
    return {"status": "passed" if ok else "failed", "p1_s0": p1, "p3_s0_box": box, "p3_s0_corridor": corridor}


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
    parser.add_argument("--s1-box", type=Path, action="append", help="dev_stack.py p3 receipt (box); repeatable")
    parser.add_argument("--s1-corridor", type=Path, action="append",
                        help="dev_stack.py p3 --footprint corridor-v1 receipt; repeatable")
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--evidence", type=Path, default=ROOT / EVIDENCE_DIR)
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "fine-reservations-gate")
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
    inputs = [p for p in (args.deployment, *(args.s1_box or []), *(args.s1_corridor or []), args.desk, args.desk_http,
                          args.desk_spoof) if p]
    inputs += sorted(args.evidence.glob("*.json")) if args.evidence.is_dir() else []
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    runs = {name: _load(args.evidence / file) for name, file in COMPARISONS.items()}
    registered = prereg(args.sha, args.evidence, runs)
    box_s1 = combine([load(p) for p in args.s1_box or []])
    corridor_s1 = combine([load(p) for p in args.s1_corridor or []])
    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "prereg": registered,
        "decision": decision(runs, corridor_s1, registered.get("thresholds")),
        "s1": s1(box_s1, corridor_s1, args.sha),
        "regression": regression(args.sha, args.work),
        "desk": P6R["desk"](load(args.desk), load(args.desk_http), load(args.desk_spoof), args.sha),
        "historical": historical(),
    }
    passed = all(v["status"] == "passed" for v in criteria.values())
    adopt = criteria["decision"].get("adopt")
    report = {"schema_version": "0.1.0", "work_package": "WP-P6-06", "decision_record": "D084", "source_sha": args.sha,
              "records_sha": head, "checked_at": datetime.now(UTC).isoformat(),
              "status": "passed" if passed else "not_passed", "adopt_corridors": adopt, "criteria": criteria,
              "capabilities": [{"capability": "fine_airspace_reservations_v1", "decision": "D084",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "corridors_adopted": adopt, "existing_catalogs_switched": False,
                                "containment_checked_on_truth": True, "real_devices": False}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in inputs}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], "adopt": adopt, **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
