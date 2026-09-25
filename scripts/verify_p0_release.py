"""P0 gate: current software evidence, with historical and hardware capabilities kept separate (D054).

P0 门禁：核对当前软件证据，历史能力与硬件状态分别保留（D054）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from drone_agent.eval.provenance import audit_view  # noqa: E402

BASELINE = "9223d74cd06f459d067bb91030373b44b4f02ed2"
M3_PATH = "docs/verification/m3-2026-09-25/release.json"
M2 = runpy.run_path(str(ROOT / "scripts/verify_m2_release.py"))
DOCS = ("docs/", "README.md", "README.zh-CN.md", "AGENTS.md", "CLAUDE.md", "eval/BASELINES.md",
        "proto/README.md", "sim/README.md")
P0_PATHS = ("src/drone_agent/fleet/", "src/drone_agent/console/", "src/drone_agent/providers/guarded.py",
            "src/drone_agent/eval/provenance.py", "src/drone_agent/eval/viewer.py", "scripts/verify_p0_release.py",
            "scripts/desk_probe.py", "tests/", "sim/compose.m2.yaml", "sim/compose.desk.yaml")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def revision_scope(sha: str) -> dict:
    """Reject changes outside this metadata release; hardware/control changes need broader gates.

    拒绝超出本次元数据发行的变更；硬件与控制改动需要更广的门禁。
    """
    if subprocess.run(["git", "merge-base", "--is-ancestor", BASELINE, sha], cwd=ROOT).returncode:
        return {"status": "failed", "reason": "candidate does not descend from the reviewed baseline"}
    changed = git("diff", "--name-only", BASELINE, sha).splitlines()
    outside = [p for p in changed if not p.startswith((*DOCS, *P0_PATHS))]
    return {"status": "failed" if outside else "passed", "base_sha": BASELINE, "changed": changed,
            "outside_p0_scope": outside,
            "control_regression": "not_required_only_while_guardian_executive_adapters_wire_and_policies_unchanged"}


def historical_boundaries() -> dict:
    """History is an immutable input, not a current-candidate pass. / 历史是不可变输入，不是当前候选的通过成绩。"""
    original = subprocess.check_output(["git", "show", f"{BASELINE}:{M3_PATH}"], cwd=ROOT)
    current = (ROOT / M3_PATH).read_bytes()
    old = json.loads(original)
    valid = (current == original and old.get("m3_sitl") == "passed" and old.get("status") == "not_passed"
             and old.get("criteria", {}).get("jil", {}).get("status") == "missing")
    return {"status": "passed" if valid else "failed", "source_sha": old.get("source_sha"),
            "m3_sitl": old.get("m3_sitl"), "m3_combined": old.get("status"),
            "jil": old.get("criteria", {}).get("jil", {}).get("status"),
            "sha256": hashlib.sha256(original).hexdigest(), "counts_as_candidate_validation": False}


def source_views(receipts: list[dict], directory: Path | None, sha: str) -> dict:
    """Require a view for every E2E case, bound to its independent judge's artifact digest.

    每个 E2E 用例都必须有视图，并绑定独立裁判记录的产物摘要。
    """
    if directory is None or not directory.is_dir() or not receipts:
        return {"status": "missing", "reason": "source views and E2E receipts are required"}
    expected = {}
    for receipt in receipts:
        for row in receipt.get("results", []):
            name = f"{row.get('scenario')}-{row.get('seed')}"
            expected[name] = row
    paths = {p.stem: p for p in directory.glob("*.json")}
    problems = []
    if set(paths) != set(expected) or not expected:
        problems.append("case_coverage_mismatch")
    audits = {}
    for name in sorted(set(paths) & set(expected)):
        raw = paths[name].read_bytes()
        recorded = expected[name].get("artifacts", {}).get("service-export/view.json")
        if not recorded or hashlib.sha256(raw).hexdigest() != recorded:
            problems.append(f"view_not_bound_to_judge:{name}")
        view = json.loads(raw)
        audits[name] = audit_view(view, sha, planning_source="scripted", root=ROOT)
        if audits[name]["status"] != "passed":
            problems.append(f"source_audit_failed:{name}")
    return {"status": "failed" if problems else "passed", "cases": audits, "problems": problems}


def live_sources(receipts: list[dict], sha: str, flight_records: list[dict] | None = None) -> dict:
    """Require actual HTTPS desk evidence, including one completed live-planned flight.

    要求真实 HTTPS 任务台证据，其中至少一次实调规划飞行完成。
    """
    if not receipts:
        return {"status": "missing", "reason": "no live desk source probe"}
    if flight_records is None:
        return {"status": "missing", "reason": "authoritative supervisor flight.json records are required"}
    audits, problems, completed = [], [], 0
    flights = {}
    for record in flight_records:
        key = record.get("mission_id"), record.get("version")
        if key in flights:
            problems.append("duplicate_authoritative_flight_receipt")
        flights[key] = record
    for receipt in receipts:
        mission = receipt.get("mission") or {}
        if receipt.get("errors"):
            problems.append("desk_probe_reported_errors")
        result = audit_view(mission, sha, planning_source="live_model", root=ROOT)
        audits.append(result)
        if result["status"] != "passed":
            problems.append("live_source_audit_failed")
        judge = (mission.get("cloud") or {}).get("judge") or {}
        public_flights = (mission.get("cloud") or {}).get("flights", [])
        for public in public_flights:
            key = mission.get("mission_id"), public.get("version")
            recorded = flights.get(key)
            if recorded is None:
                problems.append("missing_authoritative_flight_receipt")
                continue
            # The public summary omits source_sha; the supervisor's persisted receipt is authoritative.
            # 公开摘要没有 source_sha；版本依据是监管者持久化的回执。
            fields = ("version", "status", "epoch", "started_at", "ended_at", "manual_cleanup")
            if (recorded.get("source_sha") != sha
                    or Path(recorded.get("package", "")).name != f"{key[0]}-v{key[1]}.json"
                    or any(public.get(k) != recorded.get(k) for k in fields)):
                problems.append("authoritative_flight_binding_mismatch")
        if mission.get("status") == "completed":
            if (judge.get("passed") is True and judge.get("replay_agrees") is True
                    and judge.get("false_success_reports") == 0 and judge.get("problems") == []
                    and judge.get("flown_versions") and mission.get("evidence") and receipt.get("media")
                    and (mission.get("report") or {}).get("all_targets_completed") is True
                    and public_flights and all(f.get("status") == "finished" for f in public_flights)
                    and set(judge["flown_versions"]) == {f.get("version") for f in public_flights}):
                completed += 1
            else:
                problems.append("live_flight_not_independently_verified")
    if completed == 0:
        problems.append("no_completed_live_planned_flight")
    return {"status": "failed" if problems else "passed", "completed_flights": completed,
            "audits": audits, "problems": problems}


def capabilities(sha: str, criteria: dict) -> list[dict]:
    """Machine-readable capability inventory; future plans never inherit historical passes.

    机器可读能力清单；未来计划不继承历史通过成绩。
    """
    p0_passed = all(item["status"] == "passed" for item in criteria.values())
    result = [{"capability": "run_provenance_and_product_gate", "milestone": "P0", "implemented": True,
               "software_validation": "passed" if p0_passed else "not_passed", "source_sha": sha,
               "hardware_validation": "not_applicable"}]
    for milestone, revision, evidence in (
        ("M1", "eefe76e", "docs/m1-readiness.md"), ("M2", "f362b9e", "docs/m2-readiness.md"),
        ("M3-SITL", "75382dc", M3_PATH),
    ):
        result.append({"milestone": milestone, "implemented": True, "software_validation": "historical_passed",
                       "source_sha": git("rev-parse", revision), "evidence": evidence,
                       "counts_as_candidate_validation": False, "hardware_validation": "missing"})
    for milestone in ("H1", "H2", "H3"):
        result.append({"milestone": milestone, "status": "missing", "hardware_validation": "missing"})
    for milestone in ("P1", "P2", "P3", "P4", "P5", "X1", "X2", "X3"):
        result.append({"milestone": milestone, "status": "planned", "software_validation": "missing",
                       "hardware_validation": "missing"})
    result.append({"milestone": "M3", "status": "not_passed", "reason": "JIL missing; not a P0 dependency"})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--e2e", type=Path, nargs="*", default=[])
    parser.add_argument("--views-dir", type=Path)
    parser.add_argument("--live-probe", type=Path, nargs="*", default=[])
    parser.add_argument("--live-flights-dir", type=Path, help="raw supervisor flight.json records for the live probes")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    head = git("rev-parse", "HEAD")
    if subprocess.run(["git", "merge-base", "--is-ancestor", args.sha, head], cwd=ROOT).returncode:
        parser.error("HEAD must be the candidate or its documentation-only descendant")
    newer = git("diff", "--name-only", args.sha, head).splitlines()
    if any(not p.startswith(DOCS) for p in newer) or git(
        "status", "--porcelain", "--", "src", "tests", "scripts", "configs", "proto", "sim", "ros2_ws",
        "pyproject.toml", "uv.lock"
    ):
        parser.error("gate implementation and runtime must match the clean candidate")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8")) if path else None

    e2e = [load(p) for p in args.e2e]
    flight_paths = sorted(args.live_flights_dir.glob("*.json")) if args.live_flights_dir else None
    criteria = {
        "scope": revision_scope(args.sha), "checks": M2["checks"](load(args.deployment), args.sha),
        "adversarial": M2["adversarial"](), "e2e": M2["e2e"](e2e, args.sha),
        "source_views": source_views(e2e, args.views_dir, args.sha),
        "live_sources": live_sources([load(p) for p in args.live_probe], args.sha,
                                     [load(p) for p in flight_paths] if flight_paths is not None else None),
        "historical_boundaries": historical_boundaries(),
    }
    paths = [p for p in [args.deployment, *args.e2e, *args.live_probe] if p]
    paths += flight_paths or []
    if args.views_dir and args.views_dir.is_dir():
        paths += sorted(args.views_dir.glob("*.json"))
    inputs = {p.relative_to(args.output.parent).as_posix() if p.is_relative_to(args.output.parent) else str(p):
              hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {"schema_version": "0.1.0", "milestone": "P0", "source_sha": args.sha, "records_sha": head,
              "status": "passed" if all(v["status"] == "passed" for v in criteria.values()) else "not_passed",
              "criteria": criteria, "capabilities": capabilities(args.sha, criteria), "inputs": inputs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
