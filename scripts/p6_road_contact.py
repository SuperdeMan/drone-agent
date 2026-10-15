"""Freeze a no-minimum-size road probe and replay its recorded outputs.

冻结无最小尺寸门槛的道路开发探针，并回放录制结果。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import runpy
import shutil
from collections import Counter
from pathlib import Path

from PIL import Image

from drone_agent.providers.replay import Recording, ReplayProvider

VIEWS = runpy.run_path(str(Path(__file__).with_name("p6_road_views.py")))
digest, write_json = VIEWS["digest"], VIEWS["write_json"]
VERSION = "road-region-contact-dev-v3"


def strict_json(text: str):
    """Reject repeated keys and nonfinite constants before interpreting an answer.

解释回答前拒绝重复键及非有限常量。
"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite JSON constant: " + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def read(path: Path):
    """Read exact UTF-8 JSON. / 读取确切的 UTF-8 JSON。"""
    return strict_json(path.read_text(encoding="utf-8"))


def bound_file(root: Path, name: str, expected: str) -> Path:
    """Keep inputs inside the frozen directory and match their bytes. / 限定输入位于冻结目录内且字节匹配。"""
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("input path must stay relative")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or digest(path) != expected:
        raise ValueError("input path or digest mismatch")
    return path


def prepare(previous: Path, inputs: Path, config_file: Path, output: Path) -> dict:
    """Reuse every previous full view unchanged, permitting only the prompt revision.

原样复用上一轮全部整图视图，只允许提示版本及指令变更。
"""
    if output.exists():
        raise ValueError("output already exists; preserve it")
    old, config, baseline = read(previous / "plan.json"), read(config_file), read(previous / "config.json")
    if (digest(previous / "config.json") != old["config_sha256"] or
            digest(previous / "input-manifest.json") != old["manifest_sha256"]):
        raise ValueError("previous frozen inputs changed")
    expected = dict(baseline, prompt_version=VERSION, instruction=config.get("instruction"))
    if config != expected or not isinstance(config.get("instruction"), str) or not config["instruction"].strip():
        raise ValueError("only the prompt instruction and version may change")
    if (config.get("formal_release_eligible") is not False or config.get("human_ground_truth_available") is not False
            or config.get("calls_per_case") != 1):
        raise ValueError("one-call development-only policy required")
    manifest = read(previous / "input-manifest.json")
    lookup = {r["case_id"]: r for r in manifest}
    all_views = [v["case_id"] for p in old["parents"] for v in p["views"]]
    if len(lookup) != len(manifest) or len(set(all_views)) != len(all_views) or set(lookup) != set(all_views):
        raise ValueError("duplicate or unaccounted previous view")
    if any(not isinstance(ident, str) or re.fullmatch(r"[A-Za-z0-9_-]+", ident) is None for ident in all_views):
        raise ValueError("invalid view identifier")
    old_results = read(previous / "diagnostic/results.json")
    old_run = read(previous / "diagnostic/run.json")
    if old_run["model_requested"] != old["model"]:
        raise ValueError("historical model identity mismatch")
    results = {r["case_id"]: r for r in old_results}
    if len(results) != len(old_results):
        raise ValueError("duplicate historical result")
    selected, parents, seen = {}, [], set()
    for parent in old["parents"]:
        full = [v for v in parent["views"] if v["arm"] == "full"]
        if (len(full) != 1 or full[0]["crop_px"] != [0, 0, *parent["source_size"]]
                or parent["registered_asset_boundary"] is not False or parent["case_id"] in seen):
            raise ValueError("one provisional whole-image view per unique target required")
        seen.add(parent["case_id"])
        ident = full[0]["case_id"]
        row = lookup[ident]
        if len(row["images"]) != 2 or row["images"][0]["file"] == row["images"][1]["file"]:
            raise ValueError("original and neutral outline pair required")
        sizes = []
        for item in row["images"]:
            path = bound_file(inputs, item["file"], item["sha256"])
            with Image.open(path) as image:
                image.load()
                sizes.append(image.size)
        if sizes[0] != sizes[1]:
            raise ValueError("view dimensions differ")
        answer = VIEWS["parsed_answer"](results.get(ident), old["model"])
        selected[ident] = row
        parents.append({k: parent[k] for k in ("case_id", "source_image_sha256", "source_target_sha256", "source_size")}
                       | {"view_id": ident, "input_size": list(sizes[0]), "registered_asset_boundary": False,
                          "historical_verdict": answer["verdict"] if answer else "invalid_or_missing"})
    if not parents:
        raise ValueError("at least one target required")
    chosen = [r for r in manifest if r["case_id"] in selected]
    output.mkdir(parents=True)
    (output / "inputs").mkdir()
    for row in chosen:
        for item in row["images"]:
            path = bound_file(inputs, item["file"], item["sha256"])
            destination = output / "inputs" / item["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copyfile(path, destination)
    shutil.copyfile(config_file, output / "config.json")
    write_json(output / "inputs/manifest.json", chosen)
    plan = {"format": "drone.road-contact-development-plan/v1", "prompt_version": VERSION,
            "policy_id": "road-contact-dev-v1", "model": config["model"], "targets": len(parents),
            "planned_calls": len(chosen), "parents": parents, "minimum_size_threshold": None,
            "minimum_occupancy_ratio": None, "human_ground_truth_available": False,
            "formal_release_eligible": False, "default_profile_change": False,
            "images_identical_to_previous_full_views": True, "ai_geometry_supplied_to_model": False,
            "source_index_sha256": old["source_index_sha256"], "previous_plan_sha256": digest(previous / "plan.json"),
            "previous_config_sha256": digest(previous / "config.json"),
            "previous_manifest_sha256": digest(previous / "input-manifest.json"),
            "previous_results_sha256": digest(previous / "diagnostic/results.json"),
            "previous_run_sha256": digest(previous / "diagnostic/run.json"),
            "expected_runtime": {k: old_run[k] for k in ("runtime_source_sha", "runner_sha256", "provider", "endpoint_host")},
            "config_sha256": digest(output / "config.json"), "manifest_sha256": digest(output / "inputs/manifest.json"),
            "builder_sha256": digest(Path(__file__)), "legacy_validator_sha256": digest(Path(__file__).with_name("p6_road_views.py")),
            "comparison": "historical exposed development inputs; not a contemporaneous randomized comparison",
            "no_accuracy_or_causal_improvement_claim": True}
    write_json(output / "plan.json", plan)
    return plan


def checked_answer(result: dict | None, model: str) -> dict | None:
    """Require explicit explanations while retaining the recorded schema.

保留录制协议，并要求明确的依据和未知原因。
"""
    answer = VIEWS["parsed_answer"](result, model)
    if answer is None:
        return None
    try:
        strict_json(result["raw_content"])
    except (ValueError, TypeError):
        return None
    if not answer["reason"].strip() or (answer["verdict"] == "unknown" and not answer["uncertainty"].strip()):
        return None
    return answer


def reduce_results(plan: dict, results: list[dict]) -> dict:
    """Report historical transitions, preserving every missing or invalid case.

只报告历史回答变化，保留每个缺失或无效条目。
"""
    expected = {p["view_id"] for p in plan["parents"]}
    lookup = {r["case_id"]: r for r in results}
    if len(lookup) != len(results) or set(lookup) - expected:
        raise ValueError("duplicate or foreign result")
    rows = []
    for parent in plan["parents"]:
        result = lookup.get(parent["view_id"])
        answer = checked_answer(result, plan["model"])
        rows.append({"case_id": parent["case_id"], "view_id": parent["view_id"],
                     "historical_verdict": parent["historical_verdict"],
                     "new_verdict": answer["verdict"] if answer else "unknown",
                     "response_valid": answer is not None,
                     "response_status": "missing" if result is None else result.get("status", "invalid"),
                     "obstacles": answer["obstacles"] if answer else [],
                     "reason": answer["reason"] if answer else None,
                     "uncertainty": answer["uncertainty"] if answer else None,
                     "independent_human_verdict": None})
    transitions = Counter(f"{r['historical_verdict']}->{r['new_verdict']}" for r in rows if r["response_valid"])
    return {"format": "drone.road-contact-development-summary/v1", "prompt_version": VERSION,
            "targets": len(rows), "results_received": len(results), "valid_answers": sum(r["response_valid"] for r in rows),
            "rows": rows, "valid_response_transitions": dict(transitions),
            "invalid_or_missing_views": [r["view_id"] for r in rows if not r["response_valid"]],
            "accuracy_computed": False, "causal_improvement_estimated": False, "human_reviews_completed": 0,
            "formal_release_eligible": False, "default_profile_change": False}


async def replay_and_summarize(package: Path, cloud: Path) -> dict:
    """Replay requests from frozen bytes, checking each raw answer and recording.

从冻结字节重建请求，逐项核对原始回答及录制。
"""
    plan, config, manifest = read(package / "plan.json"), read(package / "config.json"), read(package / "inputs/manifest.json")
    header, results = read(cloud / "run.json"), read(cloud / "results.json")
    for key, path in [("config_sha256", package / "config.json"), ("manifest_sha256", package / "inputs/manifest.json")]:
        if header[key] != plan[key] or digest(path) != plan[key]:
            raise ValueError("run does not match frozen inputs")
    if header["prompt_version"] != VERSION or header["model_requested"] != plan["model"]:
        raise ValueError("run identity mismatch")
    if (header.get("source") != "live_model_development_probe"
            or any(header.get(k) != v for k, v in plan["expected_runtime"].items())):
        raise ValueError("runtime or provider provenance mismatch")
    summary = reduce_results(plan, results)
    lookup = {r["case_id"]: r for r in results}
    kwargs = {k: config[k] for k in ("model", "temperature", "max_tokens", "thinking", "reasoning_effort", "timeout_s")}
    replayed = []
    for row in manifest:
        content = [{"type": "text", "text": config["instruction"]}]
        for item in row["images"]:
            path = bound_file(package / "inputs", item["file"], item["sha256"])
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode(), "detail": "high"}})
        result = lookup.get(row["case_id"])
        if not result or result["status"] != "answered":
            continue
        path = bound_file(cloud, "recordings/" + row["case_id"] + ".json", result["recording_sha256"])
        recording = Recording.load(path)
        expected_recording = {"source": "recorded", "provider_id": header["provider"], "model": result["model_used"],
                              "endpoint_host": header["endpoint_host"], "prompt_version": VERSION,
                              "prompt_sha256": plan["config_sha256"], "software_revision": header["runtime_source_sha"],
                              "label": row["case_id"]}
        if any(getattr(recording, k) != v for k, v in expected_recording.items()):
            raise ValueError("recording provenance mismatch")
        replay = ReplayProvider(recording)
        response = await replay.complete([{"role": "system", "content": config["system_prompt"]}, {"role": "user", "content": content}], **kwargs)
        if (not replay.exhausted or response[:3] != (result["raw_content"], result["model_used"], result["finish"])
                or list(response[3]) != result["usage"]):
            raise ValueError("recording or response mismatch")
        replayed.append(row["case_id"])
    summary.update(local_strict_replays=len(replayed), config_sha256=plan["config_sha256"],
                   manifest_sha256=plan["manifest_sha256"], results_sha256=digest(cloud / "results.json"),
                   runtime_source_sha=header["runtime_source_sha"], runner_sha256=header["runner_sha256"],
                   provider=header["provider"], model=plan["model"])
    return summary


def main() -> None:
    """Expose offline preparation and strict replay only. / 仅提供离线准备与严格回放。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    for name in ("previous-batch", "previous-inputs", "config", "output"):
        prepare_parser.add_argument("--" + name, type=Path, required=True)
    replay_parser = commands.add_parser("replay")
    for name in ("package", "cloud-output", "output"):
        replay_parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        value = prepare(args.previous_batch, args.previous_inputs, args.config, args.output)
    else:
        value = asyncio.run(replay_and_summarize(args.package, args.cloud_output))
        write_json(args.output, value)
    print(json.dumps({k: v for k, v in value.items() if k not in ("rows", "parents")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
