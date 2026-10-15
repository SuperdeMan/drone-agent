"""Review frozen model proposals individually without creating a scene verdict.

逐项复核冻结的模型提案，不生成整图结论。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import re
import shutil
from collections import Counter
from pathlib import Path

VERSION = "road-candidate-review-dev-v1"
KINDS = {"physical_object", "vehicle", "shadow", "surface_change", "unknown"}
RELATIONS = {"inside", "outside", "edge_uncertain", "unknown"}
SEPARABILITY = {"isolated", "mixed", "unknown"}
ITEM_KEYS = {"proposal_id", "entity_kind", "target_relation", "separability", "refined_bbox",
             "entity_evidence", "relation_evidence", "uncertainty"}


def digest(path: Path) -> str:
    """Hash source bytes. / 对源字节求摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strict_json(text: str):
    """Reject repeated keys and nonfinite JSON constants. / 拒绝重复键及非有限 JSON 常量。"""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def constant(value):
        raise ValueError("nonfinite JSON constant: " + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def read(path: Path):
    """Read UTF-8 evidence. / 读取 UTF-8 证据。"""
    return strict_json(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    """Write a new immutable artifact. / 创建新的不可覆盖产物。"""
    with path.open("xb") as stream:
        stream.write((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def bbox_valid(box) -> bool:
    """No business size floor; require finite normalized ordered coordinates.

不设业务尺寸下限，只要求有限、有序的归一化坐标。
"""
    return (isinstance(box, list) and len(box) == 4 and
            all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
            and box[0] < box[2] and box[1] < box[3])


def proposals_by_id(proposals: list[dict]) -> dict:
    """Require a bounded, unique and well-formed proposal set. / 要求有界、唯一、完整的提案集合。"""
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 8:
        raise ValueError("one to eight frozen proposals required")
    result = {}
    for p in proposals:
        if (not isinstance(p, dict) or set(p) != {"proposal_id", "bbox"}
                or not isinstance(p["proposal_id"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p["proposal_id"]) is None
                or p["proposal_id"] in result or not bbox_valid(p["bbox"])):
            raise ValueError("invalid or duplicate proposal")
        result[p["proposal_id"]] = p
    return result


def validate_answer(text: str, finish: str, proposals: list[dict]) -> tuple[dict | None, list[str]]:
    """Validate the new wire contract atomically; uncertainty never disappears.

原子校验新输出协议，不静默丢弃不确定项。
"""
    expected = proposals_by_id(proposals)
    issues = [] if finish == "stop" else ["finish_not_stop"]
    try:
        value = strict_json(text)
    except (ValueError, TypeError):
        return None, issues + ["invalid_json"]
    if not isinstance(value, dict) or set(value) != {"schema_version", "image_usable", "target_visible", "observations"}:
        return value, issues + ["response_fields"]
    if value["schema_version"] != "0.1.0":
        issues.append("schema_version")
    if any(type(value[k]) is not bool for k in ("image_usable", "target_visible")):
        issues.append("quality_flags")
    rows = value["observations"]
    if not isinstance(rows, list) or len(rows) != len(expected):
        return value, issues + ["proposal_coverage"]
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ITEM_KEYS:
            issues.append("observation_fields")
            continue
        ident = row["proposal_id"]
        if not isinstance(ident, str) or ident not in expected or ident in seen:
            issues.append("proposal_identity")
            continue
        seen.add(ident)
        kind, relation, separation = row["entity_kind"], row["target_relation"], row["separability"]
        if (not isinstance(kind, str) or kind not in KINDS or not isinstance(relation, str) or relation not in RELATIONS
                or not isinstance(separation, str) or separation not in SEPARABILITY):
            issues.append("observation_enums")
            continue
        text_ok = all(isinstance(row[k], str) and len(row[k]) <= 600 for k in ("entity_evidence", "relation_evidence", "uncertainty"))
        if not text_ok or any(not row[k].strip() for k in ("entity_evidence", "relation_evidence")):
            issues.append("evidence_text")
        uncertain = (kind == "unknown" or relation in {"edge_uncertain", "unknown"} or separation != "isolated"
                     or (kind == "vehicle" and relation != "outside") or value["image_usable"] is not True or value["target_visible"] is not True)
        if uncertain and (not isinstance(row["uncertainty"], str) or not row["uncertainty"].strip()):
            issues.append("uncertainty_reason_missing")
        box, source = row["refined_bbox"], expected[ident]["bbox"]
        if box is not None:
            if not bbox_valid(box):
                issues.append("refined_bbox")
            elif not (box[0] >= source[0] - 1e-9 and box[1] >= source[1] - 1e-9
                      and box[2] <= source[2] + 1e-9 and box[3] <= source[3] + 1e-9):
                issues.append("refinement_leaves_proposal")
        if kind == "physical_object" and separation == "isolated" and box is None:
            issues.append("physical_localization_missing")
        if value["target_visible"] is False and relation != "unknown":
            issues.append("invisible_target_relation")
        if value["image_usable"] is False and (kind != "unknown" or relation != "unknown" or separation != "unknown" or box is not None):
            issues.append("unusable_image_assertion")
    if seen != set(expected):
        issues.append("proposal_coverage")
    return value, sorted(set(issues))


def disposition(row: dict, answer: dict) -> str:
    """Derive model-only groups; never turn a rejected list into a clear scene.

只派生模型开发分组，不把被排除的提案列表变成整图正常。
"""
    if (not answer["image_usable"] or not answer["target_visible"] or row["entity_kind"] == "unknown"
            or row["target_relation"] in {"unknown", "edge_uncertain"} or row["separability"] != "isolated"
            or row["uncertainty"].strip()):
        return "needs_review"
    if row["entity_kind"] in {"shadow", "surface_change"}:
        return "non_entity"
    if row["target_relation"] == "outside":
        return "outside_target"
    if row["entity_kind"] == "physical_object" and row["target_relation"] == "inside":
        return "candidate"
    return "needs_review"


def input_file(folder: Path, item: dict) -> Path:
    """Read only the exact file within the input package. / 只读取输入包内的确切文件。"""
    name = Path(item["file"])
    path = (folder / name).resolve()
    if name.is_absolute() or ".." in name.parts or not path.is_relative_to(folder.resolve()) or digest(path) != item["sha256"]:
        raise ValueError("input binding mismatch")
    return path


def make_messages(config: dict, row: dict, folder: Path) -> list[dict]:
    """Supply only frozen boxes, never old labels or explanations. / 只给出冻结框，不提供旧标签或解释。"""
    proposals_by_id(row["proposals"])
    instruction = config["instruction"] + "\nFrozen unverified proposals:\n" + json.dumps(row["proposals"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    content = [{"type": "text", "text": instruction}]
    for item in row["images"]:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(input_file(folder, item).read_bytes()).decode(), "detail": "high"}})
    return [{"role": "system", "content": config["system_prompt"]}, {"role": "user", "content": content}]


def prepare(previous: Path, previous_inputs: Path, config_file: Path, output: Path) -> dict:
    """Freeze every v7 box for a new task, preserving the failed source-batch status.

将全部 v7 框冻结为新任务，同时保留源批次的环境失败状态。
"""
    if output.exists():
        raise ValueError("output already exists")
    baseline, config = read(previous / "config.json"), read(config_file)
    if config != dict(baseline, prompt_version=VERSION, instruction=config.get("instruction")) or not isinstance(config.get("instruction"), str) or not config["instruction"].strip():
        raise ValueError("only prompt instruction and version may change")
    if config["formal_release_eligible"] is not False or config["human_ground_truth_available"] is not False or config["calls_per_case"] != 1:
        raise ValueError("development-only single-call configuration required")
    old_plan, old_manifest, results = read(previous / "plan.json"), read(previous / "input-manifest.json"), read(previous / "diagnostic/results.json")
    if digest(previous / "config.json") != old_plan["config_sha256"] or digest(previous / "input-manifest.json") != old_plan["manifest_sha256"]:
        raise ValueError("previous inputs changed")
    if digest(previous / "diagnostic/results.json") != read(previous / "replay-summary.json")["results_sha256"]:
        raise ValueError("source results changed after replay")
    lookup = {r["case_id"]: r for r in results}
    if len(lookup) != len(results) or len({r["case_id"] for r in old_manifest}) != len(old_manifest) or set(lookup) != {r["case_id"] for r in old_manifest}:
        raise ValueError("duplicate or missing source result")
    manifest = []
    for row in old_manifest:
        if not isinstance(row["case_id"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["case_id"]) is None:
            raise ValueError("invalid view identifier")
        if len(row["images"]) != 2 or row["images"][0]["file"] == row["images"][1]["file"]:
            raise ValueError("original and neutral target image pair required")
        result = lookup[row["case_id"]]
        if (result.get("status") != "answered" or result.get("finish") != "stop" or result.get("schema_issues") != []
                or result.get("model_used") != config["model"] or result.get("strict_replay") != "passed"):
            raise ValueError("source response is not usable as a frozen proposal list")
        parsed = strict_json(result["raw_content"])
        if parsed != result["parsed"]:
            raise ValueError("source raw and parsed response differ")
        proposals = [{"proposal_id": row["case_id"] + f"-p{i + 1:02}", "bbox": item["bbox"]}
                     for i, item in enumerate(parsed["obstacles"])]
        proposals_by_id(proposals)
        for item in row["images"]:
            input_file(previous_inputs, item)
        manifest.append({"case_id": row["case_id"], "images": row["images"], "proposals": proposals})
    output.mkdir(parents=True)
    (output / "inputs").mkdir()
    for row in manifest:
        for item in row["images"]:
            destination = output / "inputs" / item["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copyfile(input_file(previous_inputs, item), destination)
    shutil.copyfile(config_file, output / "config.json")
    write(output / "inputs/manifest.json", manifest)
    plan = {"format": "drone.road-candidate-review-plan/v1", "prompt_version": VERSION, "model": config["model"],
            "planned_calls": len(manifest), "proposals": sum(len(r["proposals"]) for r in manifest),
            "parents": old_plan["parents"], "source_results_sha256": digest(previous / "diagnostic/results.json"),
            "source_plan_sha256": digest(previous / "plan.json"), "source_execution_sha256": digest(previous / "diagnostic/execution.json"),
            "source_batch_environment_qualified": read(previous / "diagnostic/execution.json")["resident_container_identities_unchanged"],
            "previous_model_boxes_supplied": True, "previous_types_verdicts_reasons_supplied": False,
            "ai_boundary_polygons_supplied": False, "new_discovery_task": False,
            "minimum_size_threshold": None, "minimum_occupancy_ratio": None,
            "config_sha256": digest(output / "config.json"), "manifest_sha256": digest(output / "inputs/manifest.json"),
            "contract_sha256": digest(Path(__file__)), "expected_runtime": {k: v for k, v in old_plan["expected_runtime"].items() if k != "runner_sha256"},
            "runner_sha256": digest(Path(__file__).with_name("p6_candidate_runner.py")),
            "remote_worker_sha256": digest(Path(__file__).with_name("p6_candidate_remote.py")),
            "human_ground_truth_available": False, "formal_release_eligible": False, "default_profile_change": False}
    write(output / "plan.json", plan)
    return plan


def reduce_results(manifest: list[dict], results: list[dict], model: str) -> dict:
    """Retain all proposals on missing or malformed responses. / 回答缺失或不合格时保留全部提案。"""
    expected = {r["case_id"] for r in manifest}
    if len(expected) != len(manifest):
        raise ValueError("duplicate manifest view")
    for row in manifest:
        proposals_by_id(row["proposals"])
    lookup = {r["case_id"]: r for r in results}
    if len(lookup) != len(results) or set(lookup) - expected:
        raise ValueError("duplicate or foreign response")
    rows, cases = [], []
    for item in manifest:
        result = lookup.get(item["case_id"])
        answer, issues = None, ["missing_response"]
        if result:
            answer, issues = validate_answer(result.get("raw_content", ""), result.get("finish", ""), item["proposals"])
            if result.get("status") != "answered" or result.get("model_used") != model or result.get("strict_replay") != "passed":
                issues.append("unverified_response")
            if result.get("parsed") != answer or result.get("schema_issues") != []:
                issues.append("raw_parsed_or_remote_validation_mismatch")
        valid = not issues
        observations = {r["proposal_id"]: r for r in answer["observations"]} if valid else {}
        cases.append({"view_id": item["case_id"], "response_valid": valid, "issues": sorted(set(issues))})
        for p in item["proposals"]:
            observation = observations.get(p["proposal_id"])
            rows.append({"view_id": item["case_id"], "proposal_id": p["proposal_id"], "source_bbox": p["bbox"],
                         "observation": observation, "model_disposition": disposition(observation, answer) if valid else "needs_review",
                         "response_valid": valid, "human_verdict": None})
    return {"format": "drone.road-candidate-review-summary/v1", "planned_calls": len(manifest), "responses_received": len(results),
            "valid_responses": sum(c["response_valid"] for c in cases), "proposals": len(rows), "cases": cases, "rows": rows,
            "model_disposition_counts": dict(Counter(r["model_disposition"] for r in rows)),
            "scene_verdict": None, "accuracy_computed": False, "recall_computed": False, "human_reviews_completed": 0,
            "formal_release_eligible": False, "default_profile_change": False}


async def replay(package: Path, cloud: Path) -> dict:
    """Reconstruct exact per-proposal requests before accepting recorded observations.

接受录制的逐提案观察前，重建确切请求。
"""
    from drone_agent.providers.replay import Recording, ReplayProvider

    config, manifest, plan = read(package / "config.json"), read(package / "inputs/manifest.json"), read(package / "plan.json")
    header, results = read(cloud / "run.json"), read(cloud / "results.json")
    for key, path in [("config_sha256", package / "config.json"), ("manifest_sha256", package / "inputs/manifest.json")]:
        if digest(path) != plan[key] or header[key] != plan[key]:
            raise ValueError("input provenance mismatch")
    if (header["prompt_version"] != VERSION or header["model_requested"] != config["model"]
            or header["contract_sha256"] != plan["contract_sha256"] or digest(Path(__file__)) != plan["contract_sha256"]
            or header.get("source") != "live_model_candidate_review" or header["runner_sha256"] != plan["runner_sha256"]
            or any(header.get(k) != v for k, v in plan["expected_runtime"].items())):
        raise ValueError("runtime or contract provenance mismatch")
    summary = reduce_results(manifest, results, config["model"])
    kwargs = {k: config[k] for k in ("model", "temperature", "max_tokens", "thinking", "reasoning_effort", "timeout_s")}
    replayed = []
    for result in results:
        if result["status"] != "answered":
            continue
        row = next(r for r in manifest if r["case_id"] == result["case_id"])
        path = cloud / "recordings" / (row["case_id"] + ".json")
        if digest(path) != result["recording_sha256"]:
            raise ValueError("recording bytes changed")
        recording = Recording.load(path)
        expected = {"source": "recorded", "label": row["case_id"], "prompt_version": VERSION,
                    "prompt_sha256": plan["config_sha256"], "model": result["model_used"], "provider_id": header["provider"],
                    "endpoint_host": header["endpoint_host"], "software_revision": header["runtime_source_sha"]}
        if any(getattr(recording, k) != v for k, v in expected.items()):
            raise ValueError("recording provenance mismatch")
        provider = ReplayProvider(recording)
        response = await provider.complete(make_messages(config, row, package / "inputs"), **kwargs)
        if not provider.exhausted or response[:3] != (result["raw_content"], result["model_used"], result["finish"]) or list(response[3]) != result["usage"]:
            raise ValueError("recorded response differs")
        replayed.append(row["case_id"])
    summary.update(local_strict_replays=len(replayed), config_sha256=plan["config_sha256"], manifest_sha256=plan["manifest_sha256"],
                   source_results_sha256=plan["source_results_sha256"], results_sha256=digest(cloud / "results.json"))
    return summary


def main() -> None:
    """Expose preparation and offline replay, with no live provider call. / 只提供准备及离线回放，不做实调。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    for key in ("previous-batch", "previous-inputs", "config", "output"):
        build.add_argument("--" + key, type=Path, required=True)
    check = commands.add_parser("replay")
    for key in ("package", "cloud-output", "output"):
        check.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        value = prepare(args.previous_batch, args.previous_inputs, args.config, args.output)
    else:
        value = asyncio.run(replay(args.package, args.cloud_output))
        write(args.output, value)
    print(json.dumps({k: v for k, v in value.items() if k not in ("parents", "rows", "cases")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
