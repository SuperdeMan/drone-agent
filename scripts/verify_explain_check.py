"""D086 gate: the pre-registered choice between explanation checks v1 and v2 on one immutable candidate.

Criteria, bound to the same full commit unless stated:
  scope      — changes since the D086 design stay in the explanation module, the service read path and construction,
               the evaluation code, corpora and recordings, scripts, tests and docs; nothing onboard
  checks     — the cloud deployment receipt for this candidate: the whole suite on Linux with 0 failures, errors and
               skips (it replays every committed recording)
  prereg     — the pre-registration names the check, the prompt, the scoring and rule code, both held-out sets and both
               agents' instructions by digest, matches this candidate, and was committed before the live run started
  dev        — run here: the explain_v1 doubles pass under both versions, its recordings replay exactly as the D083
               receipt under v1, and v2's statuses on those answers are reported
  held_out_a — run here: both versions on the held-out sentences
  live       — one live receipt of explain_v2 on a revision between the pre-registered one and this candidate, every
               case answered and recorded
  replay     — run here: the explain_v2 recordings replay strictly, case by case, both versions' problems included
  labels     — the independent labels cover every live case and were committed after the live run started
  decision   — the pre-registered rule over both sets (`explain_check_choice.decide`), and the service's check follows it
  regression — run here: the P1 S0 matrix, the authorization matrix and the two page client test files
  desk       — the resident desk activated on this candidate (page, script, health, forged identity ignored); an
               explanation through the entry is checked with the service's check and cites or names the decisive code
  historical — the earlier release records are byte-identical to their commits
A criterion without evidence is `missing`, never `passed`. Adopting v2 or keeping v1 are both valid outcomes; model
quality is reported, never thresholded.

D086 门禁：在同一不可变候选上核对解释核对 v1 与 v2 之间的预登记选择。判据见上文英文；除特别说明外都绑定同一完整提交。没有证据的
判据是 `missing`，绝不是 `passed`。采用 v2 与保留 v1 都是有效结果；模型质量只报告，不设阈值。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

D083 = runpy.run_path(str(ROOT / "scripts/verify_explain.py"))
PC = runpy.run_path(str(ROOT / "scripts/verify_planner_choice.py"))
M2, P6R = PC["M2"], PC["P6R"]
DOCS, ONBOARD, EVIDENCE = PC["DOCS"], PC["ONBOARD"], PC["EVIDENCE"]
git, show, digest, ancestor, between, input_name = (PC[k] for k in ("git", "show", "digest", "ancestor", "between",
                                                                    "input_name"))
BASE_SHA = "5172dc3"
PATHS = ("src/drone_agent/fleet/explain.py", "src/drone_agent/fleet/service.py", "src/drone_agent/fleet/main.py",
         "src/drone_agent/eval/", "eval/adversarial/explain_v2/", "eval/adversarial/explain_check_dev.yaml",
         "eval/adversarial/recordings/p6/explain_v2/", "scripts/", "tests/")
CODE = {"check": "src/drone_agent/fleet/explain.py", "prompt": "src/drone_agent/planner/prompts/explain-v1.md",
        "scoring": "src/drone_agent/eval/explain_eval.py", "rule": "src/drone_agent/eval/explain_check_choice.py",
        "sentences": "eval/adversarial/explain_v2/sentences.yaml", "corpus": "eval/adversarial/explain_v2/manifest.yaml",
        "author": "eval/adversarial/explain_v2/author.md", "labeller": "eval/adversarial/explain_v2/labeler.md"}
RECEIPT = "eval/adversarial/recordings/p6/explain_v2/receipt.json"
D083_RECEIPT = "eval/adversarial/recordings/p6/explain_v1/receipt.json"
LABELS = "eval/adversarial/explain_v2/labels.yaml"
RECORDS = {"docs/verification/explain-2026-10-11/release.json": "0cc60a3",
           "docs/verification/fine-reservations-2026-10-12/release.json": "5b49d3f"}


def scope(sha: str) -> dict:
    base = git("rev-parse", BASE_SHA)
    if not ancestor(base, sha):
        return {"status": "failed", "reason": "candidate does not descend from the D086 design"}
    changed = git("diff", "--name-only", base, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *PATHS))]
    onboard = [p for p in changed if p.startswith(ONBOARD)]
    result = {"base_sha": base, "changed": len(changed), "outside_scope": outside, "onboard_changed": onboard}
    return {"status": "failed" if outside or onboard else "passed", **result}


def committed_at(path: str) -> datetime | None:
    found = git("log", "--diff-filter=A", "--format=%cI", "--", path)
    return datetime.fromisoformat(found.splitlines()[-1]) if found else None


def prereg(registration: Path | None, sha: str, receipt: dict | None) -> dict:
    if registration is None:
        return {"status": "missing", "reason": "the D086 pre-registration"}
    data = json.loads(registration.read_text(encoding="utf-8"))
    problems = []
    added = committed_at(registration.resolve().relative_to(ROOT).as_posix())
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


async def doubles_under(check: str) -> list[str]:
    from drone_agent.eval.explain_eval import doubles, load
    from drone_agent.fleet.explain import MissionExplainer, facts_of
    from drone_agent.planner.engine import ModelIdentity
    from drone_agent.providers import KeyedScriptedProvider

    wrong = []
    for corpus in ("eval/adversarial/explain_v1/manifest.yaml", CODE["corpus"]):
        manifest, views = load(ROOT, corpus)
        for case in manifest["cases"]:
            facts = facts_of(views[case["state"]])
            for name, (reply, expected) in doubles(case, facts).items():
                explainer = MissionExplainer(KeyedScriptedProvider({case["question"]: reply}),
                                             ModelIdentity("scripted", "scripted-fixture"), check_version=check)
                outcome = await explainer.explain(case["question"], facts, label=case["id"])
                if outcome["status"] != expected:
                    wrong.append(f"{manifest['corpus']}:{case['id']}:{name}")
    return wrong


def dev(work: Path) -> dict:
    from drone_agent.eval.explain_check_choice import V1, V2
    from drone_agent.eval.p6_adversarial import compare, run

    wrong = {check: asyncio.run(doubles_under(check)) for check in (V1, V2)}
    receipt = json.loads((ROOT / D083_RECEIPT).read_text(encoding="utf-8"))
    replayed = asyncio.run(run(ROOT, "explain_v1", "replay", work / "explain_v1-replay",
                               recordings=ROOT / Path(D083_RECEIPT).parent))
    compared = compare(receipt, replayed)
    rows = replayed["report"]["results"]
    under_v2 = {"validated": sum(1 for r in rows if r["checks"] and not r["checks"][V2]), "cases": len(rows)}
    ok = not any(wrong.values()) and compared["status"] == "passed"
    return {"status": "passed" if ok else "failed", "doubles_wrong": wrong, "d083_replay": compared["status"],
            "d083_problems": compared["problems"][:10], "explain_v1_under_v2": under_v2}


def held_out_a() -> dict:
    from drone_agent.eval.explain_check_choice import load_sentences, score_sentences

    path = ROOT / CODE["sentences"]
    if not path.is_file():
        return {"status": "missing", "reason": CODE["sentences"]}
    return {"status": "passed", **score_sentences(load_sentences(path))}


def live(receipt: dict | None, sha: str, registered: str | None) -> dict:
    return D083["live"](receipt, sha, registered) | ({} if receipt else {"reason": "the live receipt of explain_v2"})


def replay(receipt: dict | None, work: Path) -> dict:
    if receipt is None:
        return {"status": "missing", "reason": "nothing to replay"}
    from drone_agent.eval.p6_adversarial import compare, run

    replayed = asyncio.run(run(ROOT, "explain_v2", "replay", work / "explain_v2-replay",
                               recordings=ROOT / Path(RECEIPT).parent))
    compared = compare(receipt, replayed)
    ok = replayed["status"] == "passed" and compared["status"] == "passed"
    return {"status": "passed" if ok else "failed", "compare": compared["status"], "problems": compared["problems"][:10]}


def labels(receipt: dict | None) -> dict:
    from drone_agent.eval.explain_check_choice import load_labels

    path = ROOT / LABELS
    if receipt is None or not path.is_file():
        return {"status": "missing", "reason": "the live receipt and the independent labels"}
    found = load_labels(path)
    cases = {r["id"] for r in receipt["report"]["results"]}
    problems = []
    if set(found) != cases:
        problems.append(f"labels do not match the cases: {sorted(set(found) ^ cases)[:10]}")
    added = committed_at(LABELS)
    start = datetime.fromisoformat(receipt["started_at"].replace("Z", "+00:00"))
    if added is None or added <= start:
        problems.append("the labels are not committed after the live run started")
    return {"status": "failed" if problems else "passed", "problems": problems, "labelled": len(found),
            "unsupported": sum(1 for item in found.values() if item["unsupported"]),
            "committed_at": added.isoformat() if added else None}


def decision(a: dict, receipt: dict | None, labelled: dict) -> dict:
    from drone_agent.eval.explain_check_choice import V1, V2, decide, load_labels, score_answers
    from drone_agent.fleet.main import EXPLAIN_CHECK

    if a["status"] != "passed" or receipt is None or labelled["status"] != "passed":
        return {"status": "missing", "reason": "both held-out sets and the labels"}
    answers = score_answers(receipt["report"]["results"], load_labels(ROOT / LABELS))
    chosen = decide(a, answers)
    expected = V2 if chosen["adopt"] else V1
    problems = [] if EXPLAIN_CHECK == expected else [f"the service checks with {EXPLAIN_CHECK}, the rule says {expected}"]
    return {"status": "failed" if problems else "passed", "problems": problems, "service_check": EXPLAIN_CHECK,
            "held_out_b": answers, **chosen}


def desk(activation: dict | None, http: dict | None, spoof: dict | None, probe: dict | None, sha: str) -> dict:
    from drone_agent.fleet.main import EXPLAIN_CHECK

    found = D083["desk"](activation, http, spoof, probe, sha)
    used = ((probe or {}).get("explanation") or {}).get("check_version")
    if probe is not None and used != EXPLAIN_CHECK:
        found = {**found, "status": "failed", "problems": [*found.get("problems", []), f"desk checked with {used}"]}
    return {**found, "check_version": used}


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
    parser.add_argument("--work", type=Path, default=ROOT / "outputs" / "explain-check-gate")
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
                "eval/adversarial/explain_v2", "pyproject.toml", "uv.lock")
    if any(not p.startswith((*EVIDENCE, LABELS)) for p in newer) or dirty:
        parser.error("gate implementation, corpora and runtime must match the clean candidate")
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
    a = held_out_a()
    labelled = labels(receipt)
    criteria = {
        "scope": scope(args.sha),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "prereg": prereg(args.preregistration, args.sha, receipt),
        "dev": dev(args.work),
        "held_out_a": a,
        "live": live(receipt, args.sha, registered),
        "replay": replay(receipt, args.work),
        "labels": labelled,
        "decision": decision(a, receipt, labelled),
        "regression": D083["regression"](args.sha, args.work),
        "desk": desk(load(args.desk), load(args.desk_http), load(args.desk_spoof), load(args.desk_explain), args.sha),
        "historical": historical(),
    }
    passed = all(v["status"] == "passed" for v in criteria.values())
    adopted = bool(criteria["decision"].get("adopt"))
    report = {"schema_version": "0.1.0", "decision_record": "D086", "source_sha": args.sha, "records_sha": head,
              "checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed" if passed else "not_passed",
              "criteria": criteria,
              "capabilities": [{"capability": "explanation_check_v2", "decision": "D086",
                                "software_validation": "passed" if passed else "not_passed", "source_sha": args.sha,
                                "adopted": adopted, "service_check": criteria["decision"].get("service_check"),
                                "real_devices": False, "model_quality": "reported, not thresholded"}],
              "inputs": {input_name(p): digest(p.read_bytes())
                         for p in (*inputs, *([receipt_file] if receipt else []),
                                   *([ROOT / LABELS] if (ROOT / LABELS).is_file() else []))}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], "adopted": adopted, **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
