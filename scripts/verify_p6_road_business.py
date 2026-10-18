"""Judge approved road S2 using bound reference labels and actual live evidence.

使用来源绑定的参考标签与真实实调证据判定已批准的道路 S2 范围。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from statistics import median

from p6_road_s2 import METRICS, VERSION, digest, matching_targets, read, replay, verify_package, within, write

RUNTIME = "6249129596b7cfe88c7322f8b425c7b432dba24b"
IMAGE = "sha256:4f84962ee6b75f899ee50afa153dca439942d9bf518b25d7c780c0b17c2b2377"
PATHS = {"manifest", "labels", "protocol", "exposure", "tasks", "test_package", "test_cloud", "human_reviews", "software_verification",
         "calibration_package", "calibration_cloud", "calibration_tasks", "calibration_selection"}
SOFTWARE = ("p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py", "p6_raster_replay.py", "p6_road_s2_runner.py",
            "p6_road_s2_remote.py", "p6_road_s2_cloud.py", "p6_candidate_remote.py", "p6_road_labeling.py", "verify_p6_road_business.py")


def canonical_digest(value: dict) -> str:
    """Bind replay semantics independent of JSON layout. / 不依赖 JSON 排版绑定回放语义。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def candidate_sources_match(candidate: str) -> bool:
    """A same-candidate claim must match committed source bytes, not just a SHA string.

同候选声明必须符合实际已提交源码字节，而非仅填写 SHA 字符串。
"""
    try:
        for name in SOFTWARE:
            data = subprocess.check_output(["git", "show", candidate + ":scripts/" + name], cwd=Path(__file__).resolve().parents[1], stderr=subprocess.PIPE)
            if hashlib.sha256(data).hexdigest() != digest(Path(__file__).with_name(name)):
                return False
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


async def live_evidence(package: Path, cloud: Path, candidate: str) -> dict:
    """Require an exact held-lock run and all request-bound recorded responses.

要求确切持锁运行及全部绑定请求的真实录制回答。
"""
    profile, inputs, plan = verify_package(package)
    if any(r["task_origin"] == "qa_fixture" for r in inputs["rows"]):
        raise ValueError("QA model tasks cannot establish live business evidence")
    execution, envelope = read(cloud / "execution.json"), read(cloud / "execution-plan.json")
    folder = cloud / "output/batch"
    run = read(folder / "run.json")
    if (execution.get("format") != "drone.p6-road-cloud-execution/v1" or execution.get("status") != "completed"
            or execution.get("project_lock_acquired") is not True or execution.get("container_started") is not True
            or execution.get("lock_held_through_checks") is not True or execution.get("resident_container_identities_unchanged") is not True
            or not execution.get("before") or execution.get("before") != execution.get("after")
            or execution.get("container_exit_code") != 0 or execution.get("runtime_source_sha") != RUNTIME
            or execution.get("runtime_image") != IMAGE or execution.get("eval_source_commit") != candidate
            or execution.get("execution_plan_sha256") != digest(cloud / "execution-plan.json")
            or execution.get("data_plan_sha256") != digest(package / "plan.json")):
        raise ValueError("live execution or environment evidence is not qualified")
    if (envelope.get("eval_source_commit") != candidate or envelope.get("data_plan_sha256") != digest(package / "plan.json")
            or envelope.get("runtime_image") != IMAGE or envelope.get("runtime_source_sha") != RUNTIME
            or envelope.get("run_id") != execution.get("run_id")):
        raise ValueError("execution envelope differs from receipt")
    if set(envelope.get("source_sha256", {})) != {"p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py", "p6_raster_replay.py", "p6_road_s2_runner.py", "p6_road_s2_remote.py", "p6_candidate_remote.py"}:
        raise ValueError("execution source bindings incomplete")
    for name, expected in envelope["source_sha256"].items():
        if Path(name).name != name or digest(Path(__file__).with_name(name)) != expected:
            raise ValueError("evaluated source differs from current judge inputs")
    if (run.get("format") != "drone.p6-road-s2-run/v1" or run.get("source") != "recorded" or run.get("status") != "completed"
            or run.get("provider_id") != "minimax-vl" or run.get("endpoint_host") != "api.minimaxi.com"
            or run.get("model") != profile["model"] or run.get("eval_source_commit") != candidate
            or run.get("runtime_source_sha") != RUNTIME or run.get("plan_sha256") != digest(package / "plan.json")
            or run.get("profile_sha256") != plan["profile_sha256"] or run.get("inputs_sha256") != plan["inputs_sha256"]
            or run.get("runner_sha256") != envelope["source_sha256"]["p6_road_s2_runner.py"] or run.get("responses_failed") != 0):
        raise ValueError("recorded run provenance differs")
    if any(v != plan["planned_calls"] for v in (execution.get("attempts_started"), execution.get("responses_recorded"),
                                                run.get("attempts_started"), run.get("responses_recorded"), run.get("planned_calls"))):
        raise ValueError("live call or response coverage incomplete")
    if plan["split"] == "test":
        claim = read(cloud / "test-exposure.json")
        reservation = execution.get("test_exposure", {})
        if (claim.get("format") != "drone.p6-road-test-exposure/v1" or claim.get("run_id") != execution["run_id"]
                or claim.get("data_plan_sha256") != digest(package / "plan.json") or claim.get("image_and_group_keys") != plan["test_exposure_keys"]
                or reservation.get("required") is not True or reservation.get("reserved_keys") != len(plan["test_exposure_keys"])
                or reservation.get("claim_sha256") != digest(cloud / "test-exposure.json")):
            raise ValueError("single-use test exposure reservation is missing")
    latencies, usage = [], []
    for row in inputs["rows"]:
        ident = row["sample_id"]
        recording = read(folder / "recordings" / (ident + ".json"))
        result, marker = read(folder / (ident + ".result.json")), read(folder / (ident + ".started.json"))
        if (recording.get("source") != "recorded" or recording.get("provider_id") != "minimax-vl"
                or recording.get("endpoint_host") != "api.minimaxi.com" or recording.get("software_revision") != RUNTIME
                or recording.get("prompt_version") != VERSION or any(e.get("model_used") != profile["model"] for e in recording["exchanges"])
                or result.get("status") != "answered" or result.get("source") != "recorded" or result.get("strict_replay") != "passed"
                or result.get("recording_sha256") != digest(folder / "recordings" / (ident + ".json"))
                or marker.get("sample_id") != ident or marker.get("source") != "recorded" or marker.get("plan_sha256") != digest(package / "plan.json")):
            raise ValueError("one recorded observation has inconsistent provenance")
        for exchange in recording["exchanges"]:
            latency, counters = exchange.get("latency_ms"), exchange.get("usage")
            if (type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0
                    or not isinstance(counters, list) or len(counters) != 2 or any(type(n) is not int or n < 0 for n in counters)):
                raise ValueError("recorded model usage or latency is invalid")
            latencies.append(latency)
            usage.append(counters)
    predictions = await replay(package, folder / "recordings")
    if not predictions["strict_replay_complete"] or predictions["recording_origins"] != ["recorded"]:
        raise ValueError("all observations need strict recorded replay")
    for row in predictions["rows"]:
        if read(folder / (row["sample_id"] + ".result.json")).get("prediction") != row:
            raise ValueError("cloud verdict differs from strict local recomputation")
    predictions["live_environment_verified"] = True
    latencies.sort()
    predictions["recorded_resources"] = {"responses": len(latencies), "provider": "minimax-vl", "model": profile["model"],
        "latency_median_ms": median(latencies), "latency_p95_ms": latencies[math.ceil(.95 * len(latencies)) - 1],
        "reported_input_tokens": sum(u[0] for u in usage), "reported_output_tokens": sum(u[1] for u in usage),
        "responses_with_nonzero_usage_counters": sum(any(u) for u in usage), "cost_status": "unpriced", "cost": None}
    return predictions


async def judge(evidence_file: Path, data_root: Path) -> dict:
    """Report missing requirements without weakening D089 or inventing closure.

报告缺失要求，不放宽 D089 或编造关闭结论。
"""
    evidence = read(evidence_file)
    root = evidence_file.resolve().parent
    if evidence.get("format") != "drone.p6-road-business-evidence/v1":
        raise ValueError("unsupported road evidence declaration")
    checks = []

    def check(name, passed, detail=None):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    paths = {}
    protocol_value = evidence.get("paths", {}).get("protocol")
    declared_protocol = {}
    if isinstance(protocol_value, str) and protocol_value:
        protocol_path = within(root, protocol_value)
        if protocol_path.is_file():
            declared_protocol = read(protocol_path)
    human_timing_deferred = METRICS["timing_deferred"](declared_protocol)
    for name in sorted(PATHS):
        value = evidence.get("paths", {}).get(name)
        if name == "human_reviews" and not value and human_timing_deferred:
            check("human_timing_scope_amendment", True, "deferred to operations; no human efficiency claim")
            continue
        if not isinstance(value, str) or not value:
            check(name, False, "missing path")
        else:
            paths[name] = within(root, value)
            check(name, paths[name].is_dir() if name in {"test_package", "test_cloud", "calibration_package", "calibration_cloud"} else paths[name].is_file(), "required evidence location")
    candidate = evidence.get("candidate_commit", "")
    check("exact_candidate_commit", isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{40}", candidate) is not None)
    result = {"format": "drone.p6-road-business-judgment/v1", "candidate_commit": candidate, "checks": checks,
              "scope": "delegated-AI-reviewed road-candidate S2 only" if METRICS["delegated_ai_approved"](declared_protocol) else "human-reviewed road-candidate S2 only",
              "human_review_efficiency": "not_verified_deferred_to_operations" if human_timing_deferred else "required",
              "field_clearance_effect": "not_verified_outside_this_gate",
              "gate_source_sha256": digest(Path(__file__)), "evidence_sha256": digest(evidence_file),
              "status": "missing", "wp_p6_01_closed": False, "formal_release_eligible": False}
    if any(not c["passed"] for c in checks):
        return result
    try:
        manifest, labels, protocol, exposure, tasks = (read(paths[k]) for k in ("manifest", "labels", "protocol", "exposure", "tasks"))
        scope = protocol.get("scope", {})
        ai_scope = (scope.get("mode") == "delegated_ai_reviewed_road_candidate_s2" and METRICS["delegated_ai_approved"](protocol)
                    and scope.get("independent_human_truth_claimed") is False)
        check("approved_s2_scope", (scope.get("mode") == "human_reviewed_road_candidate_s2" or ai_scope) and bool(scope.get("approval_reference"))
              and scope.get("real_clearance_effect_required") is False and scope.get("hardware_or_field_operation_claimed") is False)
        audit = METRICS["audit"](manifest, labels, protocol, exposure, data_root)
        check("data_admission", audit["data_ready"], audit["issue_counts"])
        profile, test_inputs, plan = verify_package(paths["test_package"])
        check("reference_method_binding", bool(plan.get("delegated_ai_reference")) == METRICS["delegated_ai_approved"](protocol))
        if audit["ai_assisted_labels_declared"] or audit.get("ai_verified_pairs_declared"):
            policy = protocol.get("annotation_policy", {})
            check("annotation_evaluated_model_binding", policy.get("evaluated_model") == profile["model"]
                  and policy.get("evaluated_provider_id") == "minimax-vl")
        check("frozen_test_profile", plan["split"] == "test" and profile["status"] == "frozen")
        check("test_source_manifest", plan["source_manifest_sha256"] == digest(paths["manifest"]))
        check("independent_model_tasks", tasks.get("corpus_id") == manifest["corpus_id"] and plan["tasks_sha256"] == digest(paths["tasks"]))
        task_lookup = METRICS["unique"](tasks["rows"], "sample_id")
        matching_targets([s for s in manifest["samples"] if s["split"] == "test"], task_lookup)
        check("test_target_binding", all(row["task_sha256"] == canonical_digest(task_lookup[row["sample_id"]])
              and row["target_brief"] == task_lookup[row["sample_id"]]["target_brief"]
              and row["image_sha256"] == task_lookup[row["sample_id"]]["image_sha256"] for row in test_inputs["rows"]))
        expected_admission = {"status": "data_admitted", "labels_sha256": digest(paths["labels"]),
                              "protocol_sha256": digest(paths["protocol"]), "exposure_sha256": digest(paths["exposure"])}
        check("frozen_admission_inputs", plan["data_admission"] == expected_admission)
        software = read(paths["software_verification"])
        suite = software.get("full_pytest", {})
        check("same_candidate_software", software.get("code_freeze_commit") == candidate and software.get("ruff", {}).get("exit_code") == 0
              and suite.get("status") == "passed" and suite.get("exit_code") == 0 and suite.get("failed") == 0 and suite.get("errors") == 0
              and type(suite.get("passed")) is int and suite["passed"] > 0 and suite.get("source_unchanged_during_suite") is True
              and (suite.get("skipped") == 0 or (suite.get("skipped") == 1 and "Unix socket" in suite.get("skip_reason", ""))))
        check("software_source_binding", all(software.get("source_sha256", {}).get("scripts/" + name) == digest(Path(__file__).with_name(name)) for name in SOFTWARE))
        check("candidate_source_commit_binding", candidate_sources_match(candidate))
        if any(not c["passed"] for c in checks):
            return result
        calibration_profile, calibration_inputs, calibration_plan = verify_package(paths["calibration_package"])
        selection = read(paths["calibration_selection"])
        calibration_candidate = selection.get("calibration_candidate_commit", "")
        check("calibration_selection_identity", selection.get("format") == "drone.p6-road-profile-selection/v1"
              and selection.get("status") == "selected" and selection.get("split") == "calibration"
              and selection.get("calibration_source") == "recorded_live" and selection.get("replay_digest_encoding") == "canonical_sorted_json/v1"
              and isinstance(calibration_candidate, str) and re.fullmatch(r"[0-9a-f]{40}", calibration_candidate) is not None
              and profile["calibration_record_sha256"] == digest(paths["calibration_selection"])
              and selection.get("profile_identity") == plan["profile_identity"] == calibration_plan["profile_identity"])
        check("calibration_inputs_bound", calibration_plan["split"] == "calibration" and calibration_plan["data_admission"] == expected_admission
              and calibration_plan["source_manifest_sha256"] == digest(paths["manifest"])
              and calibration_plan["tasks_sha256"] == digest(paths["calibration_tasks"])
              and calibration_profile["model"] == profile["model"])
        calibration_tasks = METRICS["unique"](read(paths["calibration_tasks"])["rows"], "sample_id")
        matching_targets([s for s in manifest["samples"] if s["split"] == "calibration"], calibration_tasks)
        check("calibration_target_binding", all(row["task_sha256"] == canonical_digest(calibration_tasks[row["sample_id"]])
              and row["target_brief"] == calibration_tasks[row["sample_id"]]["target_brief"]
              and row["image_sha256"] == calibration_tasks[row["sample_id"]]["image_sha256"] for row in calibration_inputs["rows"]))
        if any(not c["passed"] for c in checks):
            return result
        calibration_predictions = await live_evidence(paths["calibration_package"], paths["calibration_cloud"], calibration_candidate)
        check("calibration_recorded_replay", canonical_digest(calibration_predictions) == selection.get("calibration_replay_sha256")
              and selection.get("calibration_execution_sha256") == digest(paths["calibration_cloud"] / "execution.json"))
        predictions = await live_evidence(paths["test_package"], paths["test_cloud"], candidate)
        check("recorded_live_and_strict_replay", predictions["live_environment_verified"])
        reviews = read(paths["human_reviews"]) if "human_reviews" in paths else {
            "format": "drone.p6-road-review-sessions/v1", "corpus_id": manifest["corpus_id"], "sessions": []}
        metrics = METRICS["scores"](manifest, labels, predictions, reviews, "test")
        metrics["annotation_provenance"] = {"ai_assisted_labels_declared": audit["ai_assisted_labels_declared"],
                                           "delegated_ai_labels_present": audit.get("delegated_ai_labels_present", 0),
                                           "ai_verified_pairs_declared": audit.get("ai_verified_pairs_declared", 0),
                                           "human_labels_present": audit["human_labels_present"],
                                           "claim": audit["annotation_claim"], "data_admission_verified": True}
        metrics["provenance_verified"] = True
        metrics["counted_as_formal_live_result"] = True
        metrics["scope"] = "recomputed after exact candidate data admission, recorded live execution and strict replay; final gate checks remain separate"
        comparison = METRICS["assess"](protocol, metrics)
        check("all_d089_targets", comparison["metric_status"] == "met", comparison["checks"])
        result["metrics"] = metrics
        result["comparison"] = comparison
        result["recorded_resources"] = predictions["recorded_resources"]
    except (ValueError, KeyError, TypeError, OSError) as error:
        check("evidence_integrity", False, type(error).__name__ + ": " + str(error)[:300])
    passed = all(c["passed"] for c in checks)
    result.update(status="passed" if passed else "not_passed", wp_p6_01_closed=passed, formal_release_eligible=passed)
    return result


def main() -> None:
    """Create one immutable judgment from an explicit evidence root. / 从明确的证据根创建一次不可覆盖判定。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(judge(args.evidence, args.data_root))
    write(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"metrics", "comparison"}}, ensure_ascii=False))
    if result["status"] != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
