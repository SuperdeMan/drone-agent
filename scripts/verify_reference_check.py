"""D082 gate: the cross-site reference check on one immutable candidate.

Criteria, bound to the same full commit unless stated:
  scope      — changes since the D081 records stay in the reference module, the service, operations assembly, the issue
               code table, the evaluation code, the alias files, the desk reference cases, scripts, tests and docs; no
               onboard path, wire or flight configuration changed
  checks     — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
               skips
  evaluation — run here: no false rejection on the three labelled operations corpora and every desk case as expected;
               caught and missed requests are reported
  regression — run here: the P1 S0 matrix and the authorization matrix, since the service now rejects before planning
  desk       — the resident desk activated on this candidate (page, script, health, forged identity ignored); the two
               own-site requests planned live with planner-v3 and declined; the request naming site A from uav_fb
               rejected with scope.foreign_reference, with no planner record and nothing to approve
  historical — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`.

D082 门禁：在同一不可变候选上核对跨站点指代检查。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的判据是 `missing`，
绝不是 `passed`。
"""

from __future__ import annotations

import argparse
import json
import re
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PC = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
M2, P2, P6R, P6A = PC["M2"], PC["P2"], PC["P6R"], PC["P6A"]
DOCS, ONBOARD, EVIDENCE = PC["DOCS"], PC["ONBOARD"], PC["EVIDENCE"]
git, show, digest, ancestor, input_name = (PC[k] for k in ("git", "show", "digest", "ancestor", "input_name"))
BASE_SHA = "32520412710b40b68c388169ebe3bfbfa82c0088"
PATHS = ("src/drone_agent/fleet/references.py", "src/drone_agent/fleet/service.py", "src/drone_agent/fleet/dispatch.py",
         "src/drone_agent/runtime/issues.py", "src/drone_agent/eval/", "configs/sites/aliases/",
         "eval/adversarial/references_desk_v1.yaml", "scripts/", "tests/")
RECORDS = {"docs/verification/p6-recapture-2026-10-10/release.json": "3e83cb2",
           "docs/verification/p6-adversarial-2026-10-11/release.json": "017970e",
           "docs/verification/planner-clarification-2026-10-11/release.json": "99d6e17",
           "docs/verification/planner-binding-2026-10-11/release.json": "3252041"}
FOREIGN = {"project_id": "fleet_s0", "robot_id": "uav_fb", "text": "Inspect the red equipment marker at site A."}


def scope(sha: str) -> dict:
    if not ancestor(BASE_SHA, sha):
        return {"status": "failed", "reason": "candidate does not descend from the D081 records"}
    changed = git("diff", "--name-only", BASE_SHA, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *PATHS))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": BASE_SHA, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard}
    return {"status": "failed" if outside or onboard else "passed", **result}


def evaluation() -> dict:
    from drone_agent.eval.reference_check import evaluate

    result = evaluate(ROOT)
    return {"status": result["status"], "false_rejections": result["false_rejections"], "caught": result["caught"],
            "missed": result["missed"],
            "desk": {"as_expected": result["desk"]["as_expected"], "cases": result["desk"]["cases"],
                     "unexpected": result["desk"]["unexpected"]},
            "per_corpus": {name: {"verdicts": r["verdicts"], "missed": [m["id"] for m in r["missed"]],
                                  "false_rejections": [f["id"] for f in r["false_rejections"]]}
                           for name, r in result["corpora"].items()},
            "aliases_sha256": {"p3_campus_v1": next(iter(result["corpora"].values()))["aliases_sha256"],
                               "p5_desk_v1": result["desk"]["aliases_sha256"]}}


def regression(sha: str, work: Path) -> dict:
    p1 = P2["p1_regression"](sha, work / "p1-regression")
    authz = P6R["authz"](work / "authz")
    ok = p1["status"] == "passed" and authz["status"] == "passed"
    return {"status": "passed" if ok else "failed", "p1_s0": p1, "authz": authz}


def rejection(probe: dict | None) -> dict:
    """The probe of the request naming another site. / 点名其他站点的请求的探针。"""
    if probe is None:
        return {"status": "missing", "reason": "a desk_probe.py plan receipt of the request naming site A"}
    request, planned = probe.get("request") or {}, probe.get("planned") or {}
    problems = []
    if {k: request.get(k) for k in FOREIGN} != FOREIGN:
        problems.append(f"another request: {request}")
    if (probe.get("hello") or {}).get("identity_scheme") != "tailnet":
        problems.append("not a tailnet person")
    if planned.get("status") != "rejected" or probe.get("final_status") != "rejected":
        problems.append(f"{planned.get('status')} / {probe.get('final_status')}, not rejected")
    if "scope.foreign_reference" not in (probe.get("issues") or []):
        problems.append(f"issues {probe.get('issues')}")
    if planned.get("planner") is not None or planned.get("targets") or planned.get("approval") is not None:
        problems.append("the planner was asked or something is left to approve")
    return {"status": "failed" if problems else "passed", "problems": problems, "issues": probe.get("issues")}


def desk(activation: dict | None, http: dict | None, spoof: dict | None, probes: list[dict], foreign: dict | None,
         sha: str) -> dict:
    activated = P6R["desk"](activation, http, spoof, sha)
    planning = P6A["desk_planning"](probes, sha)
    prompts = [((p.get("planned") or {}).get("planner") or {}).get("prompt_version") or "" for p in probes]
    wrong = [p for p in prompts if p.split("@")[0] != "planner-v3"]
    rejected = rejection(foreign)
    statuses = (activated["status"], planning["status"], rejected["status"])
    status = "missing" if "missing" in statuses else (
        "passed" if statuses == ("passed",) * 3 and not wrong else "failed")
    return {"status": status, "activation": activated, "planning": planning, "prompt_versions": prompts,
            "foreign_request": rejected}


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
    parser.add_argument("--desk-plan", type=Path, nargs="*", default=[], help="desk_probe.py plan receipts (own site)")
    parser.add_argument("--desk-foreign", type=Path, help="desk_probe.py plan receipt of the request naming site A")
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "reference-check-gate")
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
        parser.error("gate implementation, aliases and runtime must match the clean candidate")
    inputs = [p for p in (args.deployment, args.desk, args.desk_http, args.desk_spoof, *args.desk_plan,
                          args.desk_foreign) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "evaluation": evaluation(),
        "regression": regression(args.sha, args.work),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_spoof), [load(p) for p in args.desk_plan],
                     load(args.desk_foreign), args.sha),
        "historical": historical(),
    }
    passed = all(v["status"] == "passed" for v in criteria.values())
    report = {"schema_version": "0.1.0", "decision_record": "D082", "source_sha": args.sha, "records_sha": head,
              "checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed" if passed else "not_passed",
              "criteria": criteria,
              "capabilities": [{"capability": "foreign_reference_check_v1", "decision": "D082",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "deterministic": True, "model_calls": 0, "real_devices": False,
                                "coverage": "names in the catalog and its alias files only"}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in inputs}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
