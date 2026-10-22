"""Close the approved P6 engineering delivery without granting road business acceptance.

核验已批准的 P6 工程交付范围，不授予道路业务效果验收资格。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = Path("eval/s2/road_business_v1/corpus_checkpoint_20261022d")
RELEASE_SHA = "07099967958b561aa6acc420f353c87bfb7ed6739c33539fa2cb19d5df9f84d1"
CODE = ("src", "scripts", "tests", "configs", "proto", "ros2_ws", "sim", ".github", "pyproject.toml", "uv.lock", "eval/adversarial",
        "eval/s2/road_business_v1/protocol-draft.json", "eval/s2/road_business_v1/engineering_closeout_20261022/scope.json")
STEPS = {"Install the locked environment / 按锁文件安装环境", "Ruff", "Contract field export / 契约字段导出", "Pytest",
         "Console client tests / 任务台客户端测试"}


def read(path: Path) -> dict:
    """Read an evidence object. / 读取证据对象。"""
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    """Hash the exact stored bytes. / 摘要绑定实际存储字节。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources_match(repo: Path, candidate: str, tested: str) -> bool:
    """Bind the working sources and CI merge sources to the exact candidate.

将实际工作源码及 CI 合并源码绑定到准确候选；只封装 Git 读取边界。
"""
    if not all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{40}", x) for x in (candidate, tested)):
        return False
    commands = [["rev-parse", "--verify", candidate + "^{commit}"], ["rev-parse", "--verify", tested + "^{commit}"],
                ["diff", "--quiet", candidate, "--", *CODE], ["diff", "--quiet", candidate, tested, "--", *CODE]]
    try:
        if any(subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=False).returncode for cmd in commands):
            return False
        return not subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--", *CODE], cwd=repo).strip()
    except (OSError, subprocess.SubprocessError):
        return False


def verify(repo: Path, scope: dict, ci: dict, candidate: str) -> dict:
    """Judge engineering completeness and keep every business conclusion deferred.

判定工程交付完整性，业务效果结论始终保持延期未验证。
"""
    checks = []

    def check(name: str, passed: bool) -> None:
        checks.append({"name": name, "passed": bool(passed)})

    check("owner_approved_engineering_scope", scope.get("format") == "drone.p6-engineering-scope/v1"
          and scope.get("decision") == "D091" and scope.get("status") == "approved"
          and scope.get("scope") == "engineering_delivery" and scope.get("business_validation_deferred") is True
          and scope.get("authorization_quote") == "采用工程交付范围关闭（建议）")
    check("business_acceptance_not_granted", scope.get("d089_business_accepted") is False
          and scope.get("human_efficiency_verified") is False and scope.get("field_clearing_verified") is False)
    backlog = scope.get("backlog_file")
    backlog_path = (repo / backlog).resolve() if isinstance(backlog, str) else repo.resolve()
    check("deferred_work_has_handoff", backlog_path.is_relative_to(repo.resolve()) and backlog_path.is_file()
          and backlog_path.stat().st_size > 100)
    contract = repo / "eval/s2/road_business_v1/protocol-draft.json"
    check("d089_contract_preserved", digest(contract) == scope.get("d089_protocol_sha256"))

    archive = repo / ARCHIVE
    check("immutable_archive_manifest", digest(archive / "release-manifest.json") == RELEASE_SHA)
    manifest = read(archive / "release-manifest.json")
    rows = manifest.get("files", [])
    archive_ok = len(rows) == 750 and len({r["file"] for r in rows}) == 750
    for row in rows:
        path = (archive / row["file"]).resolve()
        if not path.is_relative_to(archive.resolve()) or not path.is_file():
            archive_ok = False
            continue
        archive_ok &= path.stat().st_size == row["bytes"] and digest(path) == row["sha256"]
    check("all_archive_files_exact", archive_ok)
    annotation = read(archive / "annotation-audit.json")
    check("reference_delivery_bound", annotation.get("status") == "passed" and annotation.get("checked") == annotation.get("passed") == 331
          and annotation.get("scope_errors") == annotation.get("pending_protocol_reverification") == annotation.get("historical_unmatched") == []
          and annotation.get("formal_independent_assets_admitted") == annotation.get("formal_stability_pairs_admitted") == 0
          and annotation.get("formal_live_model_calls") == 0 and annotation.get("formal_data_frozen") is False)
    partial = read(archive / "corpora/audit19.json")
    check("partial_result_keeps_global_gaps", partial.get("samples") == partial.get("derived_captures_verified") == 19
          and partial.get("data_ready") is False and partial.get("formal_release_eligible") is False and len(partial.get("issues", [])) == 15
          and all(r.get("sample_id") is None for r in partial["issues"]))

    check("ci_exact_head", ci.get("format") == "drone.p6-github-checks/v1" and ci.get("repository") == "SuperdeMan/drone-agent"
          and ci.get("head_sha") == candidate and isinstance(ci.get("run_id"), int) and ci["run_id"] > 0
          and ci.get("workflow") == "checks" and ci.get("status") == "completed" and ci.get("conclusion") == "success")
    steps = {r["name"]: r for r in ci.get("steps", [])}
    check("full_ci_checks_passed", STEPS <= steps.keys()
          and all(steps[n].get("status") == "completed" and steps[n].get("conclusion") == "success" for n in STEPS))
    check("same_candidate_sources", sources_match(repo, candidate, ci.get("tested_merge_sha")))
    passed = all(c["passed"] for c in checks)
    return {"format": "drone.p6-engineering-readiness/v1", "scope": "engineering_delivery", "decision": "D091",
            "candidate_commit": candidate, "ci_run_id": ci.get("run_id"), "ci_tested_merge_sha": ci.get("tested_merge_sha"),
            "status": "passed" if passed else "not_passed", "engineering_delivery_closed": passed,
            "d089_business_accepted": False, "road_business_validation": "deferred", "formal_independent_assets": 0,
            "formal_stability_pairs": 0, "formal_model_calls": 0, "human_efficiency_verified": False, "field_clearing_verified": False,
            "checks": checks}


def main() -> None:
    """Write a separate engineering verdict. / 单独写出工程交付结论。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--ci", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(ROOT, read(args.scope), read(args.ci), args.candidate)
        result["input_sha256"] = {"scope": digest(args.scope), "ci": digest(args.ci), "archive_release_manifest": RELEASE_SHA,
                                  "backlog": digest(ROOT / read(args.scope)["backlog_file"]), "verifier": digest(Path(__file__))}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {"format": "drone.p6-engineering-readiness/v1", "status": "not_passed", "engineering_delivery_closed": False,
                  "d089_business_accepted": False, "error_type": type(exc).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["engineering_delivery_closed"] else 1)


if __name__ == "__main__":
    main()
