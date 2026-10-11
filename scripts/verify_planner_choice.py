"""Planner prompt choice gate (D080, D081): one pre-registered comparison of two prompts on one immutable candidate.

`--decision D080` compares planner-v1 with planner-v2 on `plan_ops_v2`; `--decision D081` compares planner-v2 with
planner-v3 on `plan_ops_v3`. Criteria, bound to the same full commit unless stated:
  scope      — changes since the previous records stay in the comparison's paths: prompts, evaluation code, corpora,
               scripts, tests and docs; D080 also the engine's default prompt line and nothing else in the engine;
               D081 also the engine, the site planner and the service that pass the catalog binding. No onboard path,
               wire or flight configuration changed
  checks     — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
               skips, which replays every committed recording under the prompt it was recorded with
  prereg     — the candidate prompt is byte-identical to the commit that added it, and the held-out corpus entered the
               history only after that commit; the pre-registration names both prompts, the three corpora, the
               scoring and rule code, the six runs in order and the pacing, all matching this candidate, and was
               committed before the first live run of the comparison started
  live       — the six pre-registered runs: live, on a revision between the pre-registered one and this candidate,
               started in the pre-registered order with the pre-registered prompt and round, every case answered and
               recorded, nothing skipped, no escape
  replay     — run here: every recording of the comparison replays strictly under its recorded prompt and reproduces
               its live receipt case by case
  decision   — run here: the pre-registered rule over the five deciding runs reaches adopt or keep, and the
               candidate's default prompt agrees (the candidate prompt if and only if adopt)
  regression — D081, run here: the P1 S0 matrix and the authorization matrix, since the service passes the binding to
               the planner; D080 changed no planning code and needs none
  desk       — after adoption: the resident desk activated on this candidate, page, script and health match, forged
               identity ignored, and two live plans through the entry recorded with the candidate prompt at the
               robots' sites and then declined; when the current prompt is kept the desk is unchanged
  historical — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`.

规划提示选择门禁（D080、D081）：在同一不可变候选上核对一次预登记的两提示比较。`--decision D080` 在 `plan_ops_v2` 上比较
planner-v1 与 planner-v2；`--decision D081` 在 `plan_ops_v3` 上比较 planner-v2 与 planner-v3。判据见上文英文；除特别说明外都
绑定同一完整提交。没有证据的判据是 `missing`，绝不是 `passed`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import runpy
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

P6A = runpy.run_path(str(ROOT / "scripts/verify_p6_adversarial.py"))
M2, P2, P6R = P6A["M2"], P6A["P2"], P6A["P6R"]
DOCS, ONBOARD = P6A["DOCS"], P6A["ONBOARD"]
git, show, digest, between, input_name = (P6A[k] for k in ("git", "show", "digest", "between", "input_name"))
EVIDENCE = (*DOCS, "eval/adversarial/recordings/p6/")
ENGINE = "src/drone_agent/planner/engine.py"
CODE = {"rule": "src/drone_agent/eval/planner_choice.py", "scoring": "src/drone_agent/eval/plan_ops.py"}
COMMON_PATHS = ("src/drone_agent/planner/prompts/", "src/drone_agent/eval/", "eval/adversarial/", "scripts/", "tests/")
P6_RECORDS = {"docs/verification/p6-recapture-2026-10-10/release.json": "3e83cb2",
              "docs/verification/p6-adversarial-2026-10-11/release.json": "017970e"}


@dataclass(frozen=True)
class Comparison:
    """One pre-registered comparison: the current and candidate prompts, the corpora and what may change.

    一次预登记的比较：现行与候选提示、语料，以及允许改动的范围。
    """

    record: str
    base_sha: str
    current: str
    candidate: str
    held_out: str
    development: str
    paths: tuple[str, ...]
    engine_default_only: bool
    regressions: bool
    records: tuple[tuple[str, str], ...]
    security: str = "nl_v1"

    @property
    def runs(self) -> tuple[tuple[str, str, str], ...]:
        return ((self.held_out, self.current, "r1"), (self.held_out, self.candidate, "r1"),
                (self.held_out, self.current, "r2"), (self.held_out, self.candidate, "r2"),
                (self.security, self.candidate, "r1"), self.development_run)

    @property
    def development_run(self) -> tuple[str, str, str]:
        return self.development, self.candidate, "r1"

    @property
    def prompts(self) -> dict[str, str]:
        return {p: f"src/drone_agent/planner/prompts/{p}.md" for p in (self.current, self.candidate)}

    @property
    def corpora(self) -> dict[str, str]:
        from drone_agent.eval.p6_adversarial import CORPORA

        return {c: CORPORA[c] for c in (self.held_out, self.development, self.security)}


COMPARISONS = {
    "D080": Comparison("D080", "2c711ad5baaf257b0d7350e0d75369695a6a60ca", "planner-v1", "planner-v2", "plan_ops_v2",
                       "plan_ops_v1", (*COMMON_PATHS, ENGINE), True, False, tuple(P6_RECORDS.items())),
    "D081": Comparison("D081", "99d6e17800b23961f8b7df054ec2b5d8641689c0", "planner-v2", "planner-v3", "plan_ops_v3",
                       "plan_ops_v2", (*COMMON_PATHS, ENGINE, "src/drone_agent/planner/sites.py",
                                       "src/drone_agent/fleet/service.py"), False, True,
                       (*P6_RECORDS.items(),
                        ("docs/verification/planner-clarification-2026-10-11/release.json", "99d6e17"))),
}


def ancestor(older: str, newer: str) -> bool:
    return not subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=ROOT,
                              capture_output=True).returncode


def engine_change(cmp: Comparison, sha: str) -> list[str]:
    """Changed lines of the planner engine other than its default prompt. / 规划引擎中默认提示之外的改动行。"""
    diff = git("diff", "-U0", cmp.base_sha, sha, "--", ENGINE).splitlines()
    lines = [x for x in diff if x[:1] in "+-" and not x.startswith(("+++", "---"))]
    return [x for x in lines if not re.fullmatch(r'[+-]DEFAULT_PROMPT = "planner-v\d+"', x)]


def scope(cmp: Comparison, sha: str) -> dict:
    if not ancestor(cmp.base_sha, sha):
        return {"status": "failed", "reason": f"candidate does not descend from {cmp.base_sha[:8]}"}
    changed = git("diff", "--name-only", cmp.base_sha, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *cmp.paths))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    engine = engine_change(cmp, sha) if cmp.engine_default_only else []
    result = {"base_sha": cmp.base_sha, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard,
              "engine_lines_beyond_default_prompt": engine}
    return {"status": "failed" if outside or onboard or engine else "passed", **result}


def run_dir(corpus: str, prompt: str, label: str) -> Path:
    from drone_agent.eval.p6_adversarial import recordings_dir

    return recordings_dir(ROOT, corpus, None, f"{prompt}-{label}")


def receipts(cmp: Comparison) -> dict:
    found = {}
    for run in cmp.runs:
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


def prereg(cmp: Comparison, registration: Path | None, sha: str, found: dict) -> dict:
    if registration is None:
        return {"status": "missing", "reason": f"the {cmp.record} pre-registration"}
    from drone_agent.eval import planner_choice

    data = json.loads(registration.read_text(encoding="utf-8"))
    problems = []
    frozen = first_commit(cmp.prompts[cmp.candidate])
    if frozen is None or show(frozen[0], cmp.prompts[cmp.candidate]) != show(sha, cmp.prompts[cmp.candidate]):
        problems.append(f"{cmp.candidate} changed after the commit that added it")
    corpus = first_commit(cmp.corpora[cmp.held_out])
    if frozen is None or corpus is None or not ancestor(frozen[0], corpus[0]) or corpus[0] == frozen[0]:
        problems.append("the held-out corpus is not committed after the frozen prompt")
    added = first_commit(registration.resolve().relative_to(ROOT).as_posix())
    starts = [started(r) for r in found.values() if r]
    if added is None:
        problems.append("the pre-registration is not committed")
    elif starts and added[1] >= min(starts):
        problems.append("the pre-registration was committed after a live run started")
    expected = {"prompts": {k: digest(show(sha, p)) for k, p in cmp.prompts.items()},
                "corpora": {k: digest(show(sha, p)) for k, p in cmp.corpora.items()},
                "code": {k: digest(show(sha, p)) for k, p in CODE.items()},
                "runs": [{"corpus": c, "planner_prompt": p, "label": label} for c, p, label in cmp.runs],
                "rule": {"rounds": planner_choice.ROUNDS, "min_wrong": planner_choice.MIN_WRONG,
                         "over_refusal_allowance": planner_choice.OVER_REFUSAL_ALLOWANCE}}
    for key, value in expected.items():
        if data.get(key) != value:
            problems.append(f"{key}: the pre-registration differs from this candidate")
    if not re.fullmatch(r"[0-9a-f]{40}", data.get("source_sha", "")) or not ancestor(data["source_sha"], sha):
        problems.append("the pre-registration names a revision that is not an ancestor of this candidate")
    return {"status": "failed" if problems else "passed", "problems": problems,
            "frozen_prompt_commit": frozen[0] if frozen else None,
            "committed_at": added[1].isoformat() if added else None,
            "first_live_start": min(starts).isoformat() if starts else None}


def expected_cases(cmp: Comparison, sha: str, corpus: str) -> int:
    return len(yaml.safe_load(show(sha, cmp.corpora[corpus]).decode("utf-8"))["cases"])


def live_runs(cmp: Comparison, found: dict, sha: str, registered: str | None) -> dict:
    problems, rows = [], {}
    for run in cmp.runs:
        corpus, prompt, label = run
        receipt, name = found.get(run), name_of(run)
        if receipt is None:
            problems.append(f"{name}: no live receipt")
            continue
        count = expected_cases(cmp, sha, corpus)
        if receipt["mode"] != "live" or registered is None or not between(receipt["software_revision"], registered,
                                                                           sha):
            problems.append(f"{name}: not a live run between the pre-registered revision and this candidate")
        if (receipt.get("planner_prompt"), receipt.get("label")) != (prompt, label):
            problems.append(f"{name}: recorded as {receipt.get('planner_prompt')}/{receipt.get('label')}")
        if receipt["corpus_sha256"] != digest(show(sha, cmp.corpora[corpus])):
            problems.append(f"{name}: another corpus")
        if receipt["cases"] != count or receipt.get("skipped") or len(receipt["recordings"]) != count:
            problems.append(f"{name}: {receipt['cases']} cases and {len(receipt['recordings'])} recordings of {count}")
        if receipt["escapes"] or receipt["status"] != "passed":
            problems.append(f"{name}: status {receipt['status']}, escapes {receipt['escapes']}")
        rows[name] = {"started_at": receipt["started_at"], "duration_s": receipt["duration_s"],
                      "software_revision": receipt["software_revision"], "resumed": len(receipt.get("resumed", [])),
                      "patience": receipt.get("patience")}
    present = [found[run] for run in cmp.runs if found.get(run)]
    if len(present) == len(cmp.runs) and any(started(a) >= started(b) for a, b in zip(present, present[1:])):
        problems.append("the runs did not start in the pre-registered order")
    return {"status": "failed" if problems else "passed", "problems": problems, "runs": rows}


def replay(cmp: Comparison, found: dict, output: Path) -> dict:
    from drone_agent.eval.p6_adversarial import compare, run

    problems, results = [], {}
    for run_key in cmp.runs:
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


def decision(cmp: Comparison, found: dict, sha: str) -> dict:
    from drone_agent.eval.planner_choice import agreement, decide

    deciding = [run for run in cmp.runs if run != cmp.development_run]
    if any(found.get(run) is None for run in deciding):
        return {"status": "missing", "reason": "the five deciding runs"}
    runs = {prompt: [found[(cmp.held_out, prompt, label)] for label in ("r1", "r2")]
            for prompt in (cmp.current, cmp.candidate)}
    result = decide(runs, found[(cmp.security, cmp.candidate, "r1")], current=cmp.current, candidate=cmp.candidate,
                    held_out=cmp.held_out, security_corpus=cmp.security)
    result["round_agreement"] = {prompt: agreement(*runs[prompt]) for prompt in runs}
    default = default_prompt(sha)
    expected = {"adopt": cmp.candidate, "keep": cmp.current}.get(result["decision"])
    ok = expected is not None and default == expected
    return {"status": "passed" if ok else "failed", "default_prompt": default, "expected_default": expected, **result}


def regression(cmp: Comparison, sha: str, work: Path) -> dict:
    if not cmp.regressions:
        return {"status": "passed", "required": False, "reason": "no planning code changed"}
    p1 = P2["p1_regression"](sha, work / "p1-regression")
    authz = P6R["authz"](work / "authz")
    ok = p1["status"] == "passed" and authz["status"] == "passed"
    return {"status": "passed" if ok else "failed", "p1_s0": p1, "authz": authz}


def desk(cmp: Comparison, choice: dict, activation: dict | None, http: dict | None, spoof: dict | None,
         probes: list[dict], sha: str) -> dict:
    if choice.get("decision") == "keep":
        return {"status": "passed", "required": False, "reason": f"{cmp.current} kept; the desk is unchanged"}
    if choice.get("decision") != "adopt":
        return {"status": "missing", "reason": "no adoption to deploy"}
    activated = P6R["desk"](activation, http, spoof, sha)
    planning = P6A["desk_planning"](probes, sha)
    # The desk view names the version; the spec's provenance adds its digest. / 任务台视图记录版本名；规格来源另带摘要。
    prompts = [((p.get("planned") or {}).get("planner") or {}).get("prompt_version") or "" for p in probes]
    wrong = [p for p in prompts if p.split("@")[0] != cmp.candidate]
    statuses = (activated["status"], planning["status"])
    status = "missing" if "missing" in statuses else (
        "passed" if statuses == ("passed", "passed") and not wrong else "failed")
    return {"status": status, "activation": activated, "planning": planning, "prompt_versions": prompts}


def historical(cmp: Comparison) -> dict:
    problems, found = [], {}
    for path, commit in cmp.records:
        original = show(commit, path)
        found[path] = {"sha256": digest(original), "commit": commit}
        if (ROOT / path).read_bytes() != original:
            problems.append(f"record_changed:{path}")
    return {"status": "failed" if problems else "passed", "problems": problems, "records": found}


def behaviour(cmp: Comparison, found: dict) -> dict:
    """Reported only: what each prompt did, by category. / 只报告：各提示按类别的表现。"""
    rows = {}
    for run, receipt in found.items():
        if receipt is None:
            continue
        report = receipt["report"]
        if run[0] == cmp.security:
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
    parser.add_argument("--decision", choices=sorted(COMPARISONS), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--preregistration", type=Path)
    parser.add_argument("--desk", type=Path, help="desk-cloud --apply receipt")
    parser.add_argument("--desk-http", type=Path, help="desk_probe.py http receipt")
    parser.add_argument("--desk-spoof", type=Path, help="desk_probe.py spoof receipt")
    parser.add_argument("--desk-plan", type=Path, nargs="*", default=[], help="desk_probe.py plan receipts")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cmp = COMPARISONS[args.decision]
    work = args.work or ROOT / "outputs" / f"planner-choice-gate-{cmp.record}"
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    if work.exists() and any(work.iterdir()):
        parser.error(f"use an empty work directory: {work}")
    head = git("rev-parse", "HEAD")
    if not ancestor(args.sha, head):
        parser.error("HEAD must be the candidate or its evidence-only descendant")
    newer = git("diff", "--name-only", args.sha, head).splitlines()
    dirty = git("status", "--porcelain", "--", "src", "tests", "scripts", "configs", "proto", "sim", "ros2_ws",
                *cmp.corpora.values(), "pyproject.toml", "uv.lock")
    if any(not p.startswith(EVIDENCE) for p in newer) or dirty:
        parser.error("gate implementation, corpora and runtime must match the clean candidate")
    inputs = [p for p in (args.deployment, args.preregistration, args.desk, args.desk_http, args.desk_spoof,
                          *args.desk_plan) if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    found = receipts(cmp)
    registered = load(args.preregistration).get("source_sha") if args.preregistration else None
    choice = decision(cmp, found, args.sha)
    criteria = {
        "scope": scope(cmp, args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "prereg": prereg(cmp, args.preregistration, args.sha, found),
        "live": live_runs(cmp, found, args.sha, registered),
        "replay": replay(cmp, found, work / "replay"),
        "decision": choice,
        "regression": regression(cmp, args.sha, work),
        "desk": desk(cmp, choice, load(args.desk), load(args.desk_http), load(args.desk_spoof),
                     [load(p) for p in args.desk_plan], args.sha),
        "historical": historical(cmp),
    }
    live_files = [run_dir(*run) / "receipt.json" for run in cmp.runs if found[run]]
    passed = all(v["status"] == "passed" for v in criteria.values())
    report = {"schema_version": "0.1.0", "decision_record": cmp.record, "source_sha": args.sha, "records_sha": head,
              "checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed" if passed else "not_passed",
              "criteria": criteria, "model_behaviour": behaviour(cmp, found),
              "capabilities": [{"capability": "planner_choice_v1", "decision": cmp.record,
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "planner_prompt": choice.get("default_prompt"), "model": "MiniMax-M3",
                                "held_out_rounds": 2, "real_devices": False,
                                "planning_quality": "measured on these inputs only"}],
              "inputs": {input_name(p): digest(p.read_bytes()) for p in (*inputs, *live_files)}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], "verdict": choice.get("decision"),
                      **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
