"""P4 S2 re-verification gate (D076 §6): the S2 criterion of P4 on a new candidate; the other fourteen come from the
archived P4 gate output of 8f41112 and are never re-claimed for the new candidate.

Criteria:
  p4_record     — the archived P4 gate output is byte-identical to its closing commit, belongs to the P4 candidate and
                  failed on s2 alone: every other criterion passed
  checks        — the candidate's cloud deployment receipt: the whole suite on Linux with 0 failures, errors and skips
  s2            — D065 / D076: manifest and protocol keep their committed digests; the calibration run of the
                  re-verified profile ran on that profile's bytes before the freeze and chose its tau, and the frozen
                  file differs from those bytes only in its threshold and calibration record; the freeze and the
                  pre-registration were both committed before the only live test run of the frozen profile; every
                  live test run on the test split is disclosed (the archived first use included), so the number of uses
                  is reported; the live metrics met every frozen threshold with enough samples; a replay on this
                  candidate reproduced it sample by sample; a scripted pipeline run of the same profile exists and is
                  not counted; latency and tokens are complete and the cost is priced or declared under a plan; the
                  retrieval report ran on this candidate (no threshold)
  s0_matrix, p1/p2/p3_regression — run here on the clean candidate: P4-F01..F15 and the P1, P2 and P3 S0 matrices
A criterion without evidence is `missing`, never `passed`. S2 proves the analyzer's metrics on the VisA circuit-board
subset only, not drone field accuracy or other equipment.

P4 S2 复验门禁（D076 §6）：在新候选上判定 P4 的 S2；其余十四项来自已归档的 8f41112 P4 门禁输出，从不重新认领为新候选的成绩。
判据：p4_record（归档的 P4 门禁输出与其关闭提交逐字节一致，属于 P4 候选，且只有 s2 未通过、其余全部通过）、checks（候选的云端
部署回执：Linux 全量 0 失败 0 错误 0 跳过）、s2（D065 / D076：清单与协议摘要不变；复验画像的校准运行使用冻结前的画像字节并
选出其 τ，冻结后的文件与之相比只多阈值与校准记录；冻结与预登记都提交于该冻结画像唯一一次实调测试之前；测试拆分上的每次实调测试
都要披露（含已归档的第 1 次），并报告使用次数；实调指标满足全部冻结阈值且样本充足；本候选上的回放逐样本复现；同一画像的脚本
管线运行存在且不计入；时延与 token 完整，费用有价格或声明的计划口径；检索报告在本候选运行，不设门槛）、s0_matrix 与
p1/p2/p3_regression（在干净候选上当场运行）。没有证据的判据是 `missing`，绝不是 `passed`。S2 只证明分析器在 VisA 电路板子集
上的指标，不证明无人机现场精度或其他设备。
"""

from __future__ import annotations

import argparse
import json
import re
import runpy
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

P4 = runpy.run_path(str(ROOT / "scripts/verify_p4_release.py"))
P4_SHA = "8f41112c299f01f868113acc6be5311fdb96d14a"
P4_RECORD = "docs/verification/p4-2026-09-28/release.json"
P4_CLOSE = "bb63075"
FIRST_USE = "docs/verification/p4-2026-09-28/s2/test-live.json"
PROFILE = "configs/analysis/vlm_change_v6.yaml"
QUALITY, MANIFEST, PROTOCOL, QUERIES = P4["QUALITY"], P4["MANIFEST"], P4["PROTOCOL"], P4["QUERIES"]
DECISIONS = "docs/decisions.md"
# The heading of D076's pre-registration, committed before the live test run. / D076 预登记的标题，提交于实调测试之前。
PREREGISTRATION = "用户决定与测试预登记（2026-10-10，测试运行之前提交）"
DOCS, M2, P2, P3 = P4["DOCS"], P4["M2"], P4["P2"], P4["P3"]
git, show, digest, committed_at = P4["git"], P4["show"], P4["digest"], P4["committed_at"]


def p4_record() -> dict:
    """The archived P4 output: unchanged since its closing commit, s2 its only failure. / 归档 P4 输出：未变，仅 s2 失败。"""
    original = show(P4_CLOSE, P4_RECORD)
    current = (ROOT / P4_RECORD).read_bytes()
    value = json.loads(original)
    criteria = value.get("criteria") or {}
    others = {name: item.get("status") for name, item in criteria.items() if name != "s2"}
    problems = []
    if current != original:
        problems.append("p4_record_changed")
    if value.get("source_sha") != P4_SHA or value.get("status") != "not_passed":
        problems.append("not_the_p4_candidate_record")
    if (criteria.get("s2") or {}).get("status") != "failed" or len(others) != 14 or \
            any(status != "passed" for status in others.values()):
        problems.append("s2_is_not_the_only_open_criterion")
    return {"status": "failed" if problems else "passed", "problems": problems, "source_sha": value.get("source_sha"),
            "sha256": digest(original), "reused_criteria": sorted(others), "counts_as_candidate_validation": False}


def _started(run: dict) -> datetime | None:
    value = (run or {}).get("started_at")
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def s2(calibration: dict | None, live: list[dict], replay: dict | None, scripted: dict | None,
       retrieval: dict | None, sha: str, cost_basis: str = "") -> dict:
    if calibration is None or not live or replay is None or scripted is None or retrieval is None:
        return {"status": "missing", "reason": "calibration, live test, replay, scripted and retrieval receipts"}
    problems = []
    profile_bytes, quality_bytes = show(sha, PROFILE), show(sha, QUALITY)
    profile = yaml.safe_load(profile_bytes)
    protocol = yaml.safe_load(show(sha, PROTOCOL))
    manifest_sha = digest(show(sha, MANIFEST))
    if protocol.get("manifest_sha256") != manifest_sha:
        problems.append("manifest_differs_from_the_frozen_protocol")
    tau = profile.get("threshold")
    chosen = ((calibration.get("calibration") or {}).get("chosen") or {}).get("threshold")
    run = calibration.get("run") or {}
    if tau is None or chosen != tau or calibration.get("split") != "calibration" or calibration.get("mode") != "live":
        problems.append("profile_tau_not_the_calibration_choice")
    if run.get("manifest_sha256") != manifest_sha or run.get("profile") != PROFILE:
        problems.append("calibration_on_another_manifest_or_profile")
    # The calibration ran on the profile before its freeze; freezing may only add the threshold and its record.
    # 校准运行使用冻结前的画像；冻结只能加上阈值及其记录。
    try:
        before = show(run.get("software_revision") or "", PROFILE)
    except (subprocess.CalledProcessError, KeyError, ValueError):
        before = b""
    unfrozen = {k: v for k, v in (yaml.safe_load(before) or {}).items() if k not in ("threshold", "calibration")}
    frozen = {k: v for k, v in profile.items() if k not in ("threshold", "calibration")}
    if not before or digest(before) != run.get("profile_sha256") or unfrozen != frozen:
        problems.append("frozen_profile_is_not_the_calibrated_one")
    frozen_commit, frozen_at = committed_at(PROFILE, sha, lambda body: yaml.safe_load(body).get("threshold") == tau)
    registered_commit, registered_at = committed_at(DECISIONS, sha,
                                                    lambda body: PREREGISTRATION in body.decode("utf-8"))
    tests = [r for r in live if r.get("split") == "test" and r.get("mode") == "live"]
    ours = [r for r in tests if (r.get("run") or {}).get("profile_sha256") == digest(profile_bytes)]
    if len(ours) != 1:
        problems.append(f"expected exactly one live test run of this profile, found {len(ours)}")
    test = ours[0] if ours else {}
    header = test.get("run") or {}
    started = _started(header)
    if frozen_at is None or started is None or not frozen_at < started:
        problems.append("profile_not_committed_before_the_live_test_run")
    if registered_at is None or started is None or not registered_at < started:
        problems.append("preregistration_not_committed_before_the_live_test_run")
    # Every use of the test split is disclosed: the archived first use must be among the inputs.
    # 测试拆分的每次使用都要披露：已归档的第 1 次必须在输入之中。
    first = json.loads(show(P4_CLOSE, FIRST_USE))
    uses = sorted({(r.get("deployment_id"), Path(r.get("artifact_directory") or "").name) for r in tests})
    if (first.get("deployment_id"), Path(first.get("artifact_directory") or "").name) not in uses:
        problems.append("earlier_use_of_the_test_split_not_disclosed")
    if header.get("quality_sha256") != digest(quality_bytes) or header.get("manifest_sha256") != manifest_sha or \
            header.get("threshold") != tau or header.get("source") != "live_model":
        problems.append("live_test_run_not_on_the_frozen_inputs")
    metrics = test.get("metrics") or {}
    if metrics.get("status") != "passed" or metrics.get("counted") is not True or not metrics.get("minimum_met"):
        problems.append("frozen_thresholds_not_met_or_too_few_samples")
    usage = metrics.get("usage") or {}
    cost = usage.get("cost") or ({"basis": cost_basis} if cost_basis else None)
    if usage.get("latency_p50_ms") is None or usage.get("latency_p95_ms") is None or \
            not usage.get("input_tokens") or not usage.get("output_tokens") or not cost:
        problems.append("latency_tokens_or_cost_incomplete")
    live_id = f"{test.get('deployment_id')}/{Path(test.get('artifact_directory') or '').name}"
    if replay.get("source_sha") != sha or replay.get("mode") != "replay" or replay.get("replay_of") != live_id or \
            (replay.get("replay") or {}).get("status") != "passed" or \
            (replay.get("metrics") or {}).get("counted") is not False:
        problems.append("no_replay_on_this_candidate_reproducing_the_live_run")
    if scripted.get("mode") != "scripted" or (scripted.get("metrics") or {}).get("counted") is not False or \
            (scripted.get("run") or {}).get("source") != "scripted" or \
            (scripted.get("run") or {}).get("profile") != PROFILE:
        problems.append("scripted_pipeline_run_missing_or_counted")
    report = retrieval.get("retrieval") or {}
    if retrieval.get("source_sha") != sha or retrieval.get("status") != "completed" or \
            report.get("query_set_sha256") != digest(show(sha, QUERIES)) or report.get("manifest_sha256") != manifest_sha \
            or report.get("threshold") is not None:
        problems.append("retrieval_report_missing_or_on_other_inputs")
    return {"status": "failed" if problems else "passed", "problems": problems, "profile": PROFILE, "tau": tau,
            "model": profile.get("model"), "reasoning_effort": profile.get("reasoning_effort"),
            "profile_commit": frozen_commit, "profile_committed_at": frozen_at.isoformat() if frozen_at else None,
            "preregistration_commit": registered_commit,
            "preregistered_at": registered_at.isoformat() if registered_at else None,
            "test_split_uses": len(uses), "uses": [f"{d}/{r}" for d, r in uses], "live_test": live_id,
            "metrics": {k: metrics.get(k) for k in ("precision", "precision_ci", "recall", "recall_ci", "coverage",
                                                    "coverage_ci", "false_assurance", "false_assurance_ci",
                                                    "instruction_following_rate", "injection_effects", "samples",
                                                    "checks")},
            "usage": {**usage, "cost": cost}, "retrieval": {
                "map_at_10": (report.get("text_to_image") or {}).get("map_at_10"),
                "device_top1": (report.get("image_to_reference") or {}).get("top1")}}


def capabilities(sha: str, criteria: dict) -> list[dict]:
    passed = all(item["status"] == "passed" for item in criteria.values())
    return [
        {"capability": "business_loop_v1", "milestone": "P4",
         "software_validation": "passed" if passed else "not_passed",
         "s2": {"source_sha": sha, "profile": PROFILE, "test_split_uses": criteria["s2"].get("test_split_uses"),
                "claims": "the analyzer's metrics on the VisA circuit-board close-ups only"},
         "other_criteria": {"source_sha": P4_SHA, "record": P4_RECORD, "counts_as_candidate_validation": False},
         "field_accuracy": "not claimed", "hardware_validation": "not_applicable"},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--s2-calibration", type=Path, help="calibration receipt of the re-verified profile")
    parser.add_argument("--s2-live", type=Path, nargs="*", default=[],
                        help="every p4-s2 --split test --mode live receipt, the archived first use included")
    parser.add_argument("--s2-replay", type=Path, help="p4-s2 --mode replay receipt on this candidate")
    parser.add_argument("--s2-scripted", type=Path, help="p4-s2 --split test --mode scripted receipt")
    parser.add_argument("--s2-retrieval", type=Path, help="p4-s2 --mode retrieval receipt on this candidate")
    parser.add_argument("--cost-basis", default="", help="declared plan basis when the vendor publishes no price")
    parser.add_argument("--s0-output", type=Path, default=ROOT / "outputs" / "p4s2-s0")
    parser.add_argument("--p1-output", type=Path, default=ROOT / "outputs" / "p4s2-p1-regression")
    parser.add_argument("--p2-output", type=Path, default=ROOT / "outputs" / "p4s2-p2-regression")
    parser.add_argument("--p3-output", type=Path, default=ROOT / "outputs" / "p4s2-p3-regression")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("a full immutable commit is required")
    head = git("rev-parse", "HEAD")
    if subprocess.run(["git", "merge-base", "--is-ancestor", args.sha, head], cwd=ROOT).returncode:
        parser.error("HEAD must be the candidate or its documentation-only descendant")
    newer = git("diff", "--name-only", args.sha, head).splitlines()
    if any(not p.startswith(DOCS) for p in newer) or git(
            "status", "--porcelain", "--", "src", "tests", "scripts", "configs", "proto", "sim", "ros2_ws", "eval/s2",
            "eval/adversarial", "pyproject.toml", "uv.lock"):
        parser.error("gate implementation and runtime must match the clean candidate")
    inputs = [p for p in [args.deployment, args.s2_calibration, args.s2_replay, args.s2_scripted, args.s2_retrieval,
                          *args.s2_live] if p]
    crlf = [p.name for p in inputs if b"\r\n" in p.read_bytes()]
    if crlf:
        parser.error(f"convert these inputs to LF line endings first: {crlf}")

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    criteria = {
        "p4_record": p4_record(),
        "checks": M2["checks"](load(args.deployment), args.sha),
        "s2": s2(load(args.s2_calibration), [load(p) for p in args.s2_live], load(args.s2_replay),
                 load(args.s2_scripted), load(args.s2_retrieval), args.sha, args.cost_basis),
        "s0_matrix": P4["s0_matrix"](args.sha, args.s0_output),
        "p1_regression": P2["p1_regression"](args.sha, args.p1_output),
        "p2_regression": P2["s0_matrix"](args.sha, args.p2_output),
        "p3_regression": P3["s0_matrix"](args.sha, args.p3_output),
    }
    digests = {p.relative_to(args.output.parent).as_posix() if p.is_relative_to(args.output.parent) else p.name:
               digest(p.read_bytes()) for p in inputs}
    report = {"schema_version": "0.1.0", "milestone": "P4", "gate": "s2_reverification", "source_sha": args.sha,
              "records_sha": head,
              "status": "passed" if all(v["status"] == "passed" for v in criteria.values()) else "not_passed",
              "criteria": criteria, "capabilities": capabilities(args.sha, criteria), "inputs": digests}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], **{k: v["status"] for k, v in criteria.items()}}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
