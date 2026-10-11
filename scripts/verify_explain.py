"""WP-P6-05 gate: the operations explanation assistant on one immutable candidate (D083).

Criteria, bound to the same full commit unless stated:
  scope      — changes since the D082 records stay in the explanation module, the service and API read path, the
               console page and its server, the prompt, the scripted double, the evaluation code and corpus, scripts,
               tests and docs; no onboard path, wire or flight configuration changed
  checks     — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
               skips (it replays every committed recording)
  prereg     — the pre-registration names the corpus, the prompt, the check and the scoring code by digest, matches
               this candidate, and was committed before the live run started
  doubles    — run here: every fooled answer is rejected and every honest one passes
  live       — one live receipt of explain_v1 on a revision between the pre-registered one and this candidate, every
               case answered and recorded
  replay     — run here: the recordings replay strictly, case by case, and no answer that would be shown fails the
               check again (shown fabrications 0)
  regression — run here: the P1 S0 matrix, the authorization matrix and the two page client test files
  desk       — the resident desk activated on this candidate (page, script, health, forged identity ignored); through
               the entry a request naming another site is rejected and the question about it gets either a checked
               answer citing scope.foreign_reference or, when the answer fails the check, the summary naming it
  historical — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`. Model quality is reported, never thresholded.

WP-P6-05 门禁：在同一不可变候选上核对运营解释助手（D083）。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的判据是
`missing`，绝不是 `passed`。模型质量只报告，不设阈值。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PC = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
M2, P2, P6R = PC["M2"], PC["P2"], PC["P6R"]
DOCS, ONBOARD, EVIDENCE = PC["DOCS"], PC["ONBOARD"], PC["EVIDENCE"]
git, show, digest, ancestor, between, input_name = (PC[k] for k in ("git", "show", "digest", "ancestor", "between",
                                                                    "input_name"))
BASE_SHA = "898b2fe"
PATHS = ("src/drone_agent/fleet/explain.py", "src/drone_agent/fleet/service.py", "src/drone_agent/fleet/api.py",
         "src/drone_agent/fleet/main.py", "src/drone_agent/console/", "src/drone_agent/planner/prompts/explain-",
         "src/drone_agent/providers/replay.py", "src/drone_agent/eval/", "eval/adversarial/explain_v1/", "scripts/",
         "tests/")
CODE = {"prompt": "src/drone_agent/planner/prompts/explain-v1.md", "check": "src/drone_agent/fleet/explain.py",
        "scoring": "src/drone_agent/eval/explain_eval.py", "corpus": "eval/adversarial/explain_v1/manifest.yaml"}
RECEIPT = "eval/adversarial/recordings/p6/explain_v1/receipt.json"
RECORDS = {"docs/verification/p6-adversarial-2026-10-11/release.json": "017970e",
           "docs/verification/planner-clarification-2026-10-11/release.json": "99d6e17",
           "docs/verification/planner-binding-2026-10-11/release.json": "3252041",
           "docs/verification/reference-check-2026-10-11/release.json": "898b2fe"}


def scope(sha: str) -> dict:
    base = git("rev-parse", BASE_SHA)
    if not ancestor(base, sha):
        return {"status": "failed", "reason": "candidate does not descend from the D082 records"}
    changed = git("diff", "--name-only", base, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *PATHS))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": base, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard}
    return {"status": "failed" if outside or onboard else "passed", **result}


def prereg(registration: Path | None, sha: str, receipt: dict | None) -> dict:
    if registration is None:
        return {"status": "missing", "reason": "the D083 pre-registration"}
    data = json.loads(registration.read_text(encoding="utf-8"))
    problems = []
    found = git("log", "--diff-filter=A", "--format=%cI", "--", registration.resolve().relative_to(ROOT).as_posix())
    added = datetime.fromisoformat(found.splitlines()[-1]) if found else None
    start = datetime.fromisoformat(receipt["started_at"].replace("Z", "+00:00")) if receipt else None
    if added is None:
        problems.append("the pre-registration is not committed")
    elif start is not None and added >= start:
        problems.append("the pre-registration was committed after the live run started")
    expected = {k: digest(show(sha, p)) for k, p in CODE.items()}
    if data.get("digests") != expected:
        problems.append("the pre-registered digests differ from this candidate")
    if not re.fullmatch(r"[0-9a-f]{40}", data.get("source_sha", "")) or not ancestor(data["source_sha"], sha):
        problems.append("the pre-registration names a revision that is not an ancestor of this candidate")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "committed_at": added.isoformat() if added else None, "live_start": receipt and receipt["started_at"]}


def doubles(work: Path) -> dict:
    from drone_agent.eval.p6_adversarial import run

    receipt = asyncio.run(run(ROOT, "explain_v1", "scripted", work / "scripted"))
    report = receipt["report"]
    return {"status": "passed" if receipt["status"] == "passed" and report["escapes"] == 0 else "failed",
            "doubles": report["doubles"], "escapes": report["escapes"], "failures": report["failures"][:10]}


def live(receipt: dict | None, sha: str, registered: str | None) -> dict:
    if receipt is None:
        return {"status": "missing", "reason": "the committed live receipt of explain_v1"}
    problems = []
    if receipt["mode"] != "live" or registered is None or not between(receipt["software_revision"], registered, sha):
        problems.append("not a live run between the pre-registered revision and this candidate")
    if receipt["status"] != "passed" or receipt.get("skipped") or len(receipt["recordings"]) != receipt["cases"]:
        problems.append(f"status {receipt['status']}, {len(receipt['recordings'])} recordings of {receipt['cases']}")
    report = receipt["report"]
    return {"status": "failed" if problems else "passed", "problems": problems, "cases": receipt["cases"],
            "statuses": report.get("statuses"), "decisive_cited": report.get("decisive_cited"),
            "changes_drafted": report.get("changes_drafted"), "problem_kinds": report.get("problem_kinds"),
            "started_at": receipt["started_at"], "software_revision": receipt["software_revision"]}


def replay(receipt: dict | None, work: Path) -> dict:
    if receipt is None:
        return {"status": "missing", "reason": "nothing to replay"}
    from drone_agent.eval.p6_adversarial import compare, run

    replayed = asyncio.run(run(ROOT, "explain_v1", "replay", work / "replay",
                               recordings=ROOT / Path(RECEIPT).parent))
    compared = compare(receipt, replayed)
    ok = replayed["status"] == "passed" and compared["status"] == "passed" and replayed["escapes"] == 0
    return {"status": "passed" if ok else "failed", "compare": compared["status"], "escapes": replayed["escapes"],
            "problems": compared["problems"][:10]}


def regression(sha: str, work: Path) -> dict:
    p1 = P2["p1_regression"](sha, work / "p1-regression")
    authz = P6R["authz"](work / "authz")
    node = subprocess.run(["node", "--test", "tests/console/test_mission_client.cjs", "tests/console/test_live_client.cjs"],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    clients = {"exit": node.returncode, "tail": node.stdout.strip().splitlines()[-8:]}
    ok = p1["status"] == "passed" and authz["status"] == "passed" and node.returncode == 0
    return {"status": "passed" if ok else "failed", "p1_s0": p1, "authz": authz, "clients": clients}


def desk(activation: dict | None, http: dict | None, spoof: dict | None, probe: dict | None, sha: str) -> dict:
    activated = P6R["desk"](activation, http, spoof, sha)
    if probe is None:
        return {"status": "missing", "activation": activated, "reason": "a desk_probe.py explain receipt"}
    problems = []
    mission, answer = probe.get("mission") or {}, probe.get("explanation") or {}
    if (probe.get("hello") or {}).get("identity_scheme") != "tailnet":
        problems.append("not a tailnet person")
    if mission.get("status") != "rejected" or "scope.foreign_reference" not in (mission.get("issues") or []):
        problems.append(f"the request was not rejected by the reference check: {mission}")
    shown = answer.get("explanation") or {}
    facts = answer.get("fact_ids") or []
    cited = [c for c in shown.get("citations") or [] if c in facts]
    if answer.get("status") == "validated":
        if not shown or not cited:
            problems.append("a checked answer without citations")
        mode = "checked answer"
    elif answer.get("status") in ("rejected", "failed"):
        if "scope.foreign_reference" not in (answer.get("summary") or ""):
            problems.append("the summary does not name the reason")
        mode = "summary"
    else:
        problems.append(f"no explanation: {answer.get('status')}")
        mode = None
    if not str(answer.get("prompt_version") or "").startswith("explain-v1@"):
        problems.append(f"prompt {answer.get('prompt_version')}")
    statuses = (activated["status"], "failed" if problems else "passed")
    return {"status": "missing" if "missing" in statuses else ("passed" if statuses == ("passed",) * 2 else "failed"),
            "activation": activated, "problems": problems, "shown": mode, "citations": shown.get("citations"),
            "explain_s": answer.get("explain_s")}


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
    parser.add_argument("--preregistration", type=Path)
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--desk-explain", type=Path, help="desk_probe.py explain receipt")
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "explain-gate")
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
                "eval/adversarial/explain_v1", "pyproject.toml", "uv.lock")
    if any(not p.startswith(EVIDENCE) for p in newer) or dirty:
        parser.error("gate implementation, corpus and runtime must match the clean candidate")
    inputs = [p for p in (args.deployment, args.preregistration, args.desk, args.desk_http, args.desk_spoof,
                          args.desk_explain) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    receipt_file = ROOT / RECEIPT
    receipt = json.loads(receipt_file.read_text(encoding="utf-8")) if receipt_file.is_file() else None
    registered = load(args.preregistration).get("source_sha") if args.preregistration else None
    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "prereg": prereg(args.preregistration, args.sha, receipt),
        "doubles": doubles(args.work),
        "live": live(receipt, args.sha, registered),
        "replay": replay(receipt, args.work),
        "regression": regression(args.sha, args.work),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_spoof), load(args.desk_explain), args.sha),
        "historical": historical(),
    }
    passed = all(v["status"] == "passed" for v in criteria.values())
    report = {"schema_version": "0.1.0", "work_package": "WP-P6-05", "decision_record": "D083", "source_sha": args.sha,
              "records_sha": head, "checked_at": datetime.now(timezone.utc).isoformat(),
              "status": "passed" if passed else "not_passed", "criteria": criteria,
              "capabilities": [{"capability": "mission_explanation_v1", "decision": "D083",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "read_only": True, "model_tools": 0, "stored_answers": False, "real_devices": False,
                                "model_quality": "reported, not thresholded"}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in (*inputs, *([receipt_file] if receipt else []))}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
