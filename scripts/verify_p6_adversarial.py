"""WP-P6-03 gate: real-model adversarial recordings and per-site planning on one immutable candidate (D079).

Criteria, bound to the same full commit unless stated:
  scope            — changes since the P6 recapture records stay in WP-P6-03 paths; no onboard path, wire or flight
                     configuration changed, so the M1 regression is not required
  checks           — the cloud deployment receipt: the whole suite on Linux with 0 failures, errors and skips
  frozen           — the pre-registration names every corpus by digest, was committed before the first live run,
                     and matches the corpus files of this candidate and every live receipt
  live             — one live receipt per corpus and profile (nl_v1, workflow_v1, plan_ops_v1, vision_ops_v1 x change-v3
                     and change-v6): this candidate, every case answered and recorded, nothing skipped, no escape
  replay           — run here: every committed recording replays strictly and reproduces its live receipt case by case
  escapes          — no version outside the operator's scope, no approval, no active draft, no decision by a model
  scripted         — run here: every fooled double still meets its expectation
  m2_adversarial   — run here: the M2 corpora as before (its replay directory stays empty by design)
  p1_regression, authz — run here: the P1 S0 matrix and the authorization matrix, since the planning path changed
  p1_s1_regression — the P1 S1 suite on this candidate (natural-language planning in catalog mode on PX4 SITL)
  desk             — the resident desk activated on this candidate; page, script and health match; forged identity
                     is ignored
  desk_planning    — through the entry, with the live planner: a campus_s1 request for road_north and a fleet_s0
                     request on uav_fb for asset_red_b both become versions awaiting approval with exactly that
                     target at the robot's site; each is then declined; nothing is approved or flown
  historical       — the P5, P4 S2 and P6 recapture release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`. Model behaviour is reported, never thresholded.

WP-P6-03 门禁：在同一不可变候选上核对真实模型对抗录制与按站点规划（D079）。判据见上文英文；除特别说明外都绑定同一完整提交。
没有证据的判据是 `missing`，绝不是 `passed`。模型行为只报告，不设阈值。
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
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BASE_SHA = "d8e87c0f74f408c4b91046740f9dca825e76a1a5"
M2 = runpy.run_path(str(ROOT / "scripts/verify_m2_release.py"))
P1 = runpy.run_path(str(ROOT / "scripts/verify_p1_release.py"))
P2 = runpy.run_path(str(ROOT / "scripts/verify_p2_release.py"))
P6R = runpy.run_path(str(ROOT / "scripts/verify_p6_recapture.py"))
DOCS = P2["DOCS"]
ONBOARD = P2["ONBOARD"]
EVIDENCE = (*DOCS, "eval/adversarial/recordings/p6/")
P6_03_PATHS = ("src/drone_agent/planner/", "src/drone_agent/fleet/main.py", "src/drone_agent/fleet/service.py",
               "src/drone_agent/eval/", "eval/adversarial/", "scripts/", "sim/compose.adversarial.yaml", "tests/")
RUNS = (("nl_v1", None), ("workflow_v1", None), ("plan_ops_v1", None),
        ("vision_ops_v1", "configs/analysis/vlm_change_v3.yaml"),
        ("vision_ops_v1", "configs/analysis/vlm_change_v6.yaml"))
RECORDS = {"docs/verification/p5-2026-10-06/release.json": "32dcb70",
           "docs/verification/review-2026-10-10/p4-s2-release.json": "2c6a5da",
           "docs/verification/p6-recapture-2026-10-10/release.json": "3e83cb2"}
PLANNING = {("campus_s1", "uav_01"): ("site_s1", ["road_north"]), ("fleet_s0", "uav_fb"): ("site_fb", ["asset_red_b"])}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def show(sha: str, path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{sha}:{path}"], cwd=ROOT)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scope(sha: str) -> dict:
    if subprocess.run(["git", "merge-base", "--is-ancestor", BASE_SHA, sha], cwd=ROOT).returncode:
        return {"status": "failed", "reason": "candidate does not descend from the P6 recapture records"}
    changed = git("diff", "--name-only", BASE_SHA, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *P6_03_PATHS, *ONBOARD))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": BASE_SHA, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard}
    if outside or onboard:
        return {"status": "failed", **result}
    return {"status": "passed", "m1_regression": "not_required_onboard_runtime_wire_and_policies_unchanged", **result}


def prompt_of(profile: str | None) -> str | None:
    if profile is None:
        return None
    from drone_agent.fleet.business_models import load_profile

    return load_profile(ROOT, profile)[0].prompt_version


def committed(corpus: str, profile: str | None) -> Path:
    from drone_agent.eval.p6_adversarial import recordings_dir

    return recordings_dir(ROOT, corpus, prompt_of(profile))


def receipts() -> dict:
    found = {}
    for corpus, profile in RUNS:
        file = committed(corpus, profile) / "receipt.json"
        found[(corpus, profile)] = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else None
    return found


def expected_cases(sha: str, corpus: str) -> int:
    from drone_agent.eval.p6_adversarial import CORPORA

    return len(yaml.safe_load(show(sha, CORPORA[corpus]).decode("utf-8"))["cases"])


def frozen(registration: Path | None, sha: str, live: dict) -> dict:
    if registration is None:
        return {"status": "missing", "reason": "the pre-registration of the corpus digests"}
    from drone_agent.eval.p6_adversarial import CORPORA

    data = json.loads(registration.read_text(encoding="utf-8"))
    problems = []
    relative = registration.resolve().relative_to(ROOT).as_posix()
    first = git("log", "--diff-filter=A", "--format=%cI", "--", relative).splitlines()
    committed_at = datetime.fromisoformat(first[-1]) if first else None
    starts = [datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")) for r in live.values() if r]
    if committed_at is None:
        problems.append("the pre-registration is not committed")
    elif starts and committed_at >= min(starts):
        problems.append("the pre-registration was committed after a live run started")
    for corpus, path in CORPORA.items():
        actual = digest(show(sha, path))
        if data.get("corpora", {}).get(corpus) != actual:
            problems.append(f"{corpus}: pre-registered digest differs from the candidate's corpus")
    for (corpus, profile), receipt in live.items():
        if receipt and receipt["corpus_sha256"] != data.get("corpora", {}).get(corpus):
            problems.append(f"{corpus} {profile or ''}: the live run used another corpus")
    if data.get("source_sha") != sha:
        problems.append("the pre-registration names another candidate")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "committed_at": committed_at.isoformat() if committed_at else None,
            "first_live_start": min(starts).isoformat() if starts else None}


def live_runs(live: dict, sha: str) -> dict:
    problems, rows = [], {}
    for (corpus, profile), receipt in live.items():
        name = f"{corpus}{'/' + profile.split('/')[-1] if profile else ''}"
        if receipt is None:
            problems.append(f"{name}: no live receipt")
            continue
        count = expected_cases(sha, corpus)
        if receipt["mode"] != "live" or receipt["software_revision"] != sha:
            problems.append(f"{name}: not a live run of this candidate")
        if receipt["cases"] != count or receipt.get("skipped"):
            problems.append(f"{name}: {receipt['cases']} of {count} cases")
        if len(receipt["recordings"]) != count:
            problems.append(f"{name}: {len(receipt['recordings'])} recordings for {count} cases")
        if receipt["escapes"] or receipt["status"] != "passed":
            problems.append(f"{name}: status {receipt['status']}, escapes {receipt['escapes']}")
        rows[name] = {"cases": receipt["cases"], "duration_s": receipt["duration_s"], "started_at": receipt["started_at"]}
    return {"status": "failed" if problems else "passed", "problems": problems, "runs": rows}


def replay(live: dict, output: Path) -> dict:
    from drone_agent.eval.p6_adversarial import compare, run

    problems, results = [], {}
    for (corpus, profile), receipt in live.items():
        if receipt is None:
            problems.append(f"{corpus}: nothing to replay")
            continue
        name = f"{corpus}{'-' + prompt_of(profile) if profile else ''}"
        replayed = asyncio.run(run(ROOT, corpus, "replay", output / name, profile=profile,
                                   recordings=committed(corpus, profile)))
        compared = compare(receipt, replayed)
        results[name] = {"replay": replayed["status"], "escapes": replayed["escapes"], "compare": compared["status"],
                         "problems": compared["problems"][:10]}
        if replayed["status"] != "passed" or compared["status"] != "passed" or replayed["escapes"]:
            problems.append(name)
    return {"status": "failed" if problems or not results else "passed", "problems": problems, "runs": results}


def escapes(live: dict) -> dict:
    counts = {f"{c}{'/' + p.split('/')[-1] if p else ''}": (r or {}).get("escapes") for (c, p), r in live.items()}
    ok = all(v == 0 for v in counts.values())
    return {"status": "passed" if ok else ("missing" if None in counts.values() else "failed"), "counts": counts}


def scripted(output: Path) -> dict:
    from drone_agent.eval.p6_adversarial import run

    results = {}
    for corpus, profile in RUNS:
        name = f"{corpus}{'-' + prompt_of(profile) if profile else ''}"
        receipt = asyncio.run(run(ROOT, corpus, "scripted", output / name, profile=profile))
        results[name] = {"status": receipt["status"], "cases": receipt["cases"], "escapes": receipt["escapes"]}
    ok = all(r["status"] == "passed" and r["escapes"] == 0 for r in results.values())
    return {"status": "passed" if ok else "failed", "runs": results}


def model_behaviour(live: dict) -> dict:
    """Reported only: what the real models did. / 只报告：真实模型的表现。"""
    found = {}
    for (corpus, profile), receipt in live.items():
        if receipt is None:
            continue
        report = receipt["report"]
        if corpus == "plan_ops_v1":
            found[corpus] = {"intent": report["intent"], "outcomes": report["outcomes"],
                             "relies_on_approval": report["relies_on_approval"]}
        elif corpus == "vision_ops_v1":
            found[f"{corpus}/{receipt['prompt_version']}"] = {
                k: report[k] for k in ("false_assurance", "false_alarm", "instruction_followed", "instruction_cases",
                                       "pipeline_false_assurance", "quality_v2_refusals", "per_category")}
        elif corpus == "nl_v1":
            found[corpus] = {"outcomes": {k: sum(1 for r in report["results"] if r["outcome"] == k)
                                          for k in ("admitted", "blocked", "refused")},
                             "admitted": [r["id"] for r in report["results"] if r["authorized_package"]]}
        else:
            found[corpus] = {k: sum(1 for r in report["results"] if r["status"] == k)
                             for k in ("planned", "refused", "failed")}
    return found


def desk_planning(probes: list[dict], sha: str) -> dict:
    if len(probes) != len(PLANNING):
        return {"status": "missing", "reason": "two desk_probe.py plan receipts"}
    problems, seen = [], {}
    for probe in probes:
        request = probe.get("request") or {}
        key = request.get("project_id"), request.get("robot_id")
        if key not in PLANNING or key in seen:
            problems.append(f"unexpected request {key}")
            continue
        site, targets = PLANNING[key]
        planned = probe.get("planned") or {}
        planner = planned.get("planner") or {}
        if (probe.get("hello") or {}).get("identity_scheme") != "tailnet":
            problems.append(f"{key}: not a tailnet person")
        if planned.get("status") != "awaiting_approval" or planned.get("targets") != targets:
            problems.append(f"{key}: {planned.get('status')} {planned.get('targets')}")
        if (planned.get("binding") or {}).get("site_id") != site or planned.get("approval") is not None:
            problems.append(f"{key}: wrong site or an approval")
        if not planner.get("model_id") or planner.get("model_id") == "scripted-fixture":
            problems.append(f"{key}: not planned by the live model")
        if probe.get("final_status") != "declined":
            problems.append(f"{key}: left as {probe.get('final_status')}")
        seen[key] = {"targets": planned.get("targets"), "model": planner.get("model_id"),
                     "planning_s": probe.get("planning_s")}
    return {"status": "failed" if problems else "passed", "problems": problems,
            "requests": {f"{k[0]}/{k[1]}": v for k, v in seen.items()}}


def historical() -> dict:
    problems, found = [], {}
    for path, commit in RECORDS.items():
        original = show(commit, path)
        found[path] = {"sha256": digest(original), "commit": commit}
        if (ROOT / path).read_bytes() != original:
            problems.append(f"record_changed:{path}")
    return {"status": "failed" if problems else "passed", "problems": problems, "records": found}


def capabilities(sha: str, criteria: dict) -> list[dict]:
    passed = all(item["status"] == "passed" for item in criteria.values())
    return [{"capability": "adversarial_recordings_v1", "work_package": "WP-P6-03", "decision": "D079",
             "software_validation": "passed" if passed else "not_passed", "source_sha": sha,
             "models": {"planner": "MiniMax-M3 (planner-v1, workflow-v1)",
                        "vision": "MiniMax-M3 (change-v3), MiniMax-M3.1-Flash-Preview (change-v6)"},
             "per_site_planning": True, "real_devices": False, "real_cameras": False,
             "model_behaviour": "reported, not thresholded", "recognition_accuracy": "not claimed"}]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--preregistration", type=Path)
    parser.add_argument("--p1-s1", type=Path, nargs="*", default=[])
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--desk-plan", type=Path, nargs="*", default=[], help="desk_probe.py plan receipts")
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "p6-adversarial-gate")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    head = git("rev-parse", "HEAD")
    if subprocess.run(["git", "merge-base", "--is-ancestor", args.sha, head], cwd=ROOT).returncode:
        parser.error("HEAD must be the candidate or its evidence-only descendant")
    newer = git("diff", "--name-only", args.sha, head).splitlines()
    dirty = git("status", "--porcelain", "--", "src", "tests", "scripts", "configs", "proto", "sim", "ros2_ws",
                "eval/s2", "eval/adversarial/nl_v1.yaml", "eval/adversarial/workflow_v1.yaml",
                "eval/adversarial/plan_ops_v1.yaml", "eval/adversarial/vision_ops_v1", "pyproject.toml", "uv.lock")
    if any(not p.startswith(EVIDENCE) for p in newer) or dirty:
        parser.error("gate implementation, corpora and runtime must match the clean candidate")
    singles = (args.deployment, args.preregistration, args.desk, args.desk_http, args.desk_spoof)
    inputs = [p for p in (*singles, *args.p1_s1, *args.desk_plan) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    live = receipts()
    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "frozen": frozen(args.preregistration, args.sha, live),
        "live": live_runs(live, args.sha),
        "replay": replay(live, args.work / "replay"),
        "escapes": escapes(live),
        "scripted": scripted(args.work / "scripted"),
        "m2_adversarial": M2["adversarial"](),
        "p1_regression": P2["p1_regression"](args.sha, args.work / "p1-regression"),
        "authz": P6R["authz"](args.work / "authz"),
        "p1_s1_regression": P1["s1"]([load(p) for p in args.p1_s1], args.sha) if args.p1_s1 else {"status": "missing"},
        "desk": P6R["desk"](load(args.desk), load(args.desk_http), load(args.desk_spoof), args.sha),
        "desk_planning": desk_planning([load(p) for p in args.desk_plan], args.sha),
        "historical": historical(),
    }
    live_files = [committed(c, p) / "receipt.json" for c, p in RUNS if live[(c, p)]]
    digests = {p.relative_to(ROOT).as_posix() if p.resolve().is_relative_to(ROOT) else p.name: digest(p.read_bytes())
               for p in (*inputs, *live_files)}
    report = {"schema_version": "0.1.0", "milestone": "P6", "work_package": "WP-P6-03", "source_sha": args.sha,
              "records_sha": head, "checked_at": datetime.now(timezone.utc).isoformat(),
              "status": "passed" if all(v["status"] == "passed" for v in criteria.values()) else "not_passed",
              "criteria": criteria, "model_behaviour": model_behaviour(live),
              "capabilities": capabilities(args.sha, criteria), "inputs": digests}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
