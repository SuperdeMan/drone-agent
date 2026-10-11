"""D080 gate: the planner prompt choice on one immutable candidate.

Criteria, bound to the same full commit unless stated:
  scope      — changes since the WP-P6-03 records stay in the planner prompts, the engine's default prompt line, the
               evaluation code, the corpora, scripts, tests and docs; no onboard path, wire, flight configuration,
               admission, service or page changed
  checks     — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
               skips, which replays every committed recording under the prompt it was recorded with
  prereg     — planner-v2 is byte-identical to its frozen commit, and the held-out corpus entered the history only
               after it; the pre-registration names both prompts, the three corpora, the scoring and rule code, the
               six runs in order and the pacing, all matching this candidate, and was committed before the first
               D080 live run started
  live       — the six pre-registered runs: live, on a revision between the pre-registered one and this candidate,
               started in the pre-registered order with the pre-registered prompt and round, every case answered and
               recorded, nothing skipped, no escape
  replay     — run here: every D080 recording replays strictly under its recorded prompt and reproduces its live
               receipt case by case
  decision   — run here: the pre-registered rule over the five deciding runs reaches adopt or keep, and the
               candidate's default prompt agrees (planner-v2 if and only if adopt)
  desk       — after adoption: the resident desk activated on this candidate, page, script and health match, forged
               identity ignored, and two live plans through the entry recorded with planner-v2 at the robots' sites
               and then declined; when planner-v1 is kept the desk is unchanged and nothing is required
  historical — the P6 recapture and WP-P6-03 release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`.

D080 门禁：在同一不可变候选上核对规划提示的选择。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的判据是 `missing`，
绝不是 `passed`。
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

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BASE_SHA = "2c711ad5baaf257b0d7350e0d75369695a6a60ca"
FROZEN_PROMPT_SHA = "210098c6bfe5a1bfc80c18be026059695d218cce"
P6A = runpy.run_path(str(ROOT / "scripts/verify_p6_adversarial.py"))
M2, P6R = P6A["M2"], P6A["P6R"]
DOCS, ONBOARD = P6A["DOCS"], P6A["ONBOARD"]
git, show, digest, between, input_name = (P6A[k] for k in ("git", "show", "digest", "between", "input_name"))
EVIDENCE = (*DOCS, "eval/adversarial/recordings/p6/")
PATHS = ("src/drone_agent/planner/prompts/", "src/drone_agent/planner/engine.py", "src/drone_agent/eval/",
         "eval/adversarial/", "scripts/", "tests/")
ENGINE = "src/drone_agent/planner/engine.py"
PROMPTS = {"planner-v1": "src/drone_agent/planner/prompts/planner-v1.md",
           "planner-v2": "src/drone_agent/planner/prompts/planner-v2.md"}
CORPORA = {"plan_ops_v2": "eval/adversarial/plan_ops_v2.yaml", "plan_ops_v1": "eval/adversarial/plan_ops_v1.yaml",
           "nl_v1": "eval/adversarial/nl_v1.yaml"}
CODE = {"rule": "src/drone_agent/eval/planner_choice.py", "scoring": "src/drone_agent/eval/plan_ops.py"}
RUNS = (("plan_ops_v2", "planner-v1", "r1"), ("plan_ops_v2", "planner-v2", "r1"),
        ("plan_ops_v2", "planner-v1", "r2"), ("plan_ops_v2", "planner-v2", "r2"),
        ("nl_v1", "planner-v2", "r1"), ("plan_ops_v1", "planner-v2", "r1"))
DEVELOPMENT = ("plan_ops_v1", "planner-v2", "r1")
RECORDS = {"docs/verification/p6-recapture-2026-10-10/release.json": "3e83cb2",
           "docs/verification/p6-adversarial-2026-10-11/release.json": "017970e"}


def ancestor(older: str, newer: str) -> bool:
    return not subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=ROOT,
                              capture_output=True).returncode


def engine_change(sha: str) -> list[str]:
    """Changed lines of the planner engine other than its default prompt. / 规划引擎中默认提示之外的改动行。"""
    diff = git("diff", "-U0", BASE_SHA, sha, "--", ENGINE).splitlines()
    lines = [x for x in diff if x[:1] in "+-" and not x.startswith(("+++", "---"))]
    return [x for x in lines if not re.fullmatch(r'[+-]DEFAULT_PROMPT = "planner-v[12]"', x)]


def scope(sha: str) -> dict:
    if not ancestor(BASE_SHA, sha):
        return {"status": "failed", "reason": "candidate does not descend from the WP-P6-03 records"}
    changed = git("diff", "--name-only", BASE_SHA, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *PATHS))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    engine = engine_change(sha)
    result = {"base_sha": BASE_SHA, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard,
              "engine_lines_beyond_default_prompt": engine}
    return {"status": "failed" if outside or onboard or engine else "passed", **result}


def run_dir(corpus: str, prompt: str, label: str) -> Path:
    from drone_agent.eval.p6_adversarial import recordings_dir

    return recordings_dir(ROOT, corpus, None, f"{prompt}-{label}")


def receipts() -> dict:
    found = {}
    for run in RUNS:
        file = run_dir(*run) / "receipt.json"
        found[run] = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else None
    return found


def name_of(run: tuple[str, str, str]) -> str:
    return "/".join(run)


def started(receipt: dict) -> datetime:
    return datetime.fromisoformat(receipt["started_at"].replace("Z", "+00:00"))


def first_commit(path: str) -> tuple[str, datetime] | None:
    """The commit that added `path`, with its commit time. / 加入 `path` 的提交及其提交时间。"""
    found = git("log", "--diff-filter=A", "--format=%H %cI", "--", path).splitlines()
    if not found:
        return None
    sha, when = found[-1].split(" ", 1)
    return sha, datetime.fromisoformat(when)


def prereg(registration: Path | None, sha: str, found: dict) -> dict:
    if registration is None:
        return {"status": "missing", "reason": "the D080 pre-registration"}
    from drone_agent.eval import planner_choice

    data = json.loads(registration.read_text(encoding="utf-8"))
    problems = []
    if show(FROZEN_PROMPT_SHA, PROMPTS["planner-v2"]) != show(sha, PROMPTS["planner-v2"]):
        problems.append("planner-v2 changed after it was frozen")
    corpus = first_commit(CORPORA["plan_ops_v2"])
    if corpus is None or not ancestor(FROZEN_PROMPT_SHA, corpus[0]) or corpus[0] == FROZEN_PROMPT_SHA:
        problems.append("the held-out corpus is not committed after the frozen prompt")
    added = first_commit(registration.resolve().relative_to(ROOT).as_posix())
    starts = [started(r) for r in found.values() if r]
    if added is None:
        problems.append("the pre-registration is not committed")
    elif starts and added[1] >= min(starts):
        problems.append("the pre-registration was committed after a live run started")
    expected = {"prompts": {k: digest(show(sha, p)) for k, p in PROMPTS.items()},
                "corpora": {k: digest(show(sha, p)) for k, p in CORPORA.items()},
                "code": {k: digest(show(sha, p)) for k, p in CODE.items()},
                "runs": [{"corpus": c, "planner_prompt": p, "label": label} for c, p, label in RUNS],
                "rule": {"rounds": planner_choice.ROUNDS, "min_wrong": planner_choice.MIN_WRONG,
                         "over_refusal_allowance": planner_choice.OVER_REFUSAL_ALLOWANCE}}
    for key, value in expected.items():
        if data.get(key) != value:
            problems.append(f"{key}: the pre-registration differs from this candidate")
    if not re.fullmatch(r"[0-9a-f]{40}", data.get("source_sha", "")) or not ancestor(data["source_sha"], sha):
        problems.append("the pre-registration names a revision that is not an ancestor of this candidate")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "committed_at": added[1].isoformat() if added else None,
            "first_live_start": min(starts).isoformat() if starts else None}


def expected_cases(sha: str, corpus: str) -> int:
    return len(yaml.safe_load(show(sha, CORPORA[corpus]).decode("utf-8"))["cases"])


def live_runs(found: dict, sha: str, registered: str | None) -> dict:
    problems, rows = [], {}
    for run in RUNS:
        corpus, prompt, label = run
        receipt, name = found.get(run), name_of(run)
        if receipt is None:
            problems.append(f"{name}: no live receipt")
            continue
        count = expected_cases(sha, corpus)
        if receipt["mode"] != "live" or registered is None or not between(receipt["software_revision"], registered,
                                                                           sha):
            problems.append(f"{name}: not a live run between the pre-registered revision and this candidate")
        if (receipt.get("planner_prompt"), receipt.get("label")) != (prompt, label):
            problems.append(f"{name}: recorded as {receipt.get('planner_prompt')}/{receipt.get('label')}")
        if receipt["corpus_sha256"] != digest(show(sha, CORPORA[corpus])):
            problems.append(f"{name}: another corpus")
        if receipt["cases"] != count or receipt.get("skipped") or len(receipt["recordings"]) != count:
            problems.append(f"{name}: {receipt['cases']} cases and {len(receipt['recordings'])} recordings of {count}")
        if receipt["escapes"] or receipt["status"] != "passed":
            problems.append(f"{name}: status {receipt['status']}, escapes {receipt['escapes']}")
        rows[name] = {"started_at": receipt["started_at"], "duration_s": receipt["duration_s"],
                      "software_revision": receipt["software_revision"], "resumed": len(receipt.get("resumed", [])),
                      "patience": receipt.get("patience")}
    present = [found[run] for run in RUNS if found.get(run)]
    if len(present) == len(RUNS) and any(started(a) >= started(b) for a, b in zip(present, present[1:])):
        problems.append("the runs did not start in the pre-registered order")
    return {"status": "failed" if problems else "passed", "problems": problems, "runs": rows}


def replay(found: dict, output: Path) -> dict:
    from drone_agent.eval.p6_adversarial import compare, run

    problems, results = [], {}
    for run_key in RUNS:
        receipt, name = found.get(run_key), name_of(run_key)
        if receipt is None:
            problems.append(f"{name}: nothing to replay")
            continue
        corpus, prompt, _ = run_key
        replayed = asyncio.run(run(ROOT, corpus, "replay", output / name.replace("/", "-"),
                                   recordings=run_dir(*run_key), planner_prompt=prompt))
        compared = compare(receipt, replayed)
        results[name] = {"replay": replayed["status"], "escapes": replayed["escapes"], "compare": compared["status"],
                         "problems": compared["problems"][:10]}
        if replayed["status"] != "passed" or compared["status"] != "passed" or replayed["escapes"]:
            problems.append(name)
    return {"status": "failed" if problems or not results else "passed", "problems": problems, "runs": results}


def default_prompt(sha: str) -> str | None:
    found = re.search(r'^DEFAULT_PROMPT = "([^"]+)"$', show(sha, ENGINE).decode("utf-8"), re.M)
    return found.group(1) if found else None


def decision(found: dict, sha: str) -> dict:
    from drone_agent.eval.planner_choice import CANDIDATE, CURRENT, agreement, decide

    deciding = [run for run in RUNS if run != DEVELOPMENT]
    if any(found.get(run) is None for run in deciding):
        return {"status": "missing", "reason": "the five deciding runs"}
    runs = {prompt: [found[("plan_ops_v2", prompt, label)] for label in ("r1", "r2")] for prompt in (CURRENT, CANDIDATE)}
    result = decide(runs, found[("nl_v1", CANDIDATE, "r1")])
    result["round_agreement"] = {prompt: agreement(*runs[prompt]) for prompt in runs}
    default = default_prompt(sha)
    expected = {"adopt": CANDIDATE, "keep": CURRENT}.get(result["decision"])
    ok = expected is not None and default == expected
    return {"status": "passed" if ok else "failed", "default_prompt": default, "expected_default": expected, **result}


def desk(choice: dict, activation: dict | None, http: dict | None, spoof: dict | None, probes: list[dict],
         sha: str) -> dict:
    if choice.get("decision") == "keep":
        return {"status": "passed", "required": False, "reason": "planner-v1 kept; the desk is unchanged"}
    if choice.get("decision") != "adopt":
        return {"status": "missing", "reason": "no adoption to deploy"}
    activated = P6R["desk"](activation, http, spoof, sha)
    planning = P6A["desk_planning"](probes, sha)
    # The desk view names the version; the spec's provenance adds its digest. / 任务台视图记录版本名；规格来源另带摘要。
    prompts = [((p.get("planned") or {}).get("planner") or {}).get("prompt_version") or "" for p in probes]
    wrong = [p for p in prompts if p.split("@")[0] != "planner-v2"]
    statuses = (activated["status"], planning["status"])
    status = "missing" if "missing" in statuses else (
        "passed" if statuses == ("passed", "passed") and not wrong else "failed")
    return {"status": status, "activation": activated, "planning": planning, "prompt_versions": prompts}


def historical() -> dict:
    problems, found = [], {}
    for path, commit in RECORDS.items():
        original = show(commit, path)
        found[path] = {"sha256": digest(original), "commit": commit}
        if (ROOT / path).read_bytes() != original:
            problems.append(f"record_changed:{path}")
    return {"status": "failed" if problems else "passed", "problems": problems, "records": found}


def behaviour(found: dict) -> dict:
    """Reported only: what each prompt did, by category. / 只报告：各提示按类别的表现。"""
    rows = {}
    for run, receipt in found.items():
        if receipt is None:
            continue
        report = receipt["report"]
        if run[0] == "nl_v1":
            rows[name_of(run)] = {"outcomes": {k: sum(1 for r in report["results"] if r["outcome"] == k)
                                               for k in ("admitted", "blocked", "refused")},
                                  "admitted": [r["id"] for r in report["results"] if r["authorized_package"]]}
        else:
            rows[name_of(run)] = {"intent": report["intent"], "relies_on_approval": report["relies_on_approval"],
                                  "per_category": {c: {k[7:]: v for k, v in counts.items() if k.startswith("intent_")}
                                                   for c, counts in report["per_category"].items()}}
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--preregistration", type=Path)
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--desk-plan", type=Path, nargs="*", default=[], help="desk_probe.py plan receipts")
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "planner-choice-gate")
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
                *CORPORA.values(), "pyproject.toml", "uv.lock")
    if any(not p.startswith(EVIDENCE) for p in newer) or dirty:
        parser.error("gate implementation, corpora and runtime must match the clean candidate")
    inputs = [p for p in (args.deployment, args.preregistration, args.desk, args.desk_http, args.desk_spoof,
                          *args.desk_plan) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    found = receipts()
    registered = load(args.preregistration).get("source_sha") if args.preregistration else None
    choice = decision(found, args.sha)
    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "prereg": prereg(args.preregistration, args.sha, found),
        "live": live_runs(found, args.sha, registered),
        "replay": replay(found, args.work / "replay"),
        "decision": choice,
        "desk": desk(choice, load(args.desk), load(args.desk_http), load(args.desk_spoof),
                     [load(p) for p in args.desk_plan], args.sha),
        "historical": historical(),
    }
    live_files = [run_dir(*run) / "receipt.json" for run in RUNS if found[run]]
    passed = all(v["status"] == "passed" for v in criteria.values())
    report = {"schema_version": "0.1.0", "decision_record": "D080", "source_sha": args.sha, "records_sha": head,
              "checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed" if passed else "not_passed",
              "criteria": criteria, "model_behaviour": behaviour(found),
              "capabilities": [{"capability": "planner_clarification_v1", "decision": "D080",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "planner_prompt": choice.get("default_prompt"), "model": "MiniMax-M3",
                                "held_out_rounds": 2, "real_devices": False,
                                "planning_quality": "measured on these inputs only"}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in (*inputs, *live_files)}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], "decision": choice.get("decision"),
                      **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
