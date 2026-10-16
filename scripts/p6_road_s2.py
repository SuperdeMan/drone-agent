"""Prepare road-only S2 inputs and strictly replay recordings without ground-truth leakage.

制备道路专用 S2 输入并严格回放录制，不泄露业务真值。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import re
import runpy
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

METRICS = runpy.run_path(str(Path(__file__).with_name("p6_business_eval.py")))
read, write, digest, within, polygon_valid, timestamp = (METRICS[k] for k in ("read", "write", "digest", "within", "polygon_valid", "timestamp"))
VERSION = "road-whole-target-eval-v1"
TASK_KEYS = {"sample_id", "image_sha256", "target_brief", "target_polygon", "registered_at", "registration_reference", "origin"}
ANSWER_KEYS = {"schema_version", "image_usable", "target_present", "boundary_certain", "candidate_present", "defect_types", "confidence", "reasons", "summary"}
TYPES = {"debris", "vegetation", "other_entity"}
REASONS = {"quality.unusable", "target.absent_or_unresolved", "target.boundary_uncertain", "entity.uncertain", "relation.uncertain", "vehicle.context_missing"}
ARGUMENTS = ("model", "temperature", "max_tokens", "thinking", "reasoning_effort", "timeout_s")


def strict_json(text: str):
    """Reject duplicate keys and nonfinite answers. / 拒绝重复键与非有限回答。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate response key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite response constant: " + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def validate_answer(text: str, finish: str) -> tuple[dict | None, list[str]]:
    """Reject inconsistent certainty and preserve every refusal reason.

拒绝矛盾的确定性声明，保留全部拒判原因。
"""
    issues = [] if finish == "stop" else ["finish_not_stop"]
    try:
        value = strict_json(text)
    except (ValueError, TypeError):
        return None, issues + ["invalid_json"]
    if not isinstance(value, dict) or set(value) != ANSWER_KEYS:
        return None, issues + ["response_fields"]
    if value["schema_version"] != "1.0.0":
        issues.append("schema_version")
    if any(type(value[k]) is not bool for k in ("image_usable", "target_present", "boundary_certain")):
        issues.append("quality_flags")
    if value["candidate_present"] is not None and type(value["candidate_present"]) is not bool:
        issues.append("candidate_flag")
    for field, allowed in (("defect_types", TYPES), ("reasons", REASONS)):
        rows = value[field]
        if not isinstance(rows, list) or any(not isinstance(x, str) or x not in allowed for x in rows) or len(rows) != len(set(rows)):
            issues.append("invalid_" + field)
    confidence = value["confidence"]
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        issues.append("confidence")
    if not isinstance(value["summary"], str) or not 1 <= len(value["summary"].strip()) <= 600:
        issues.append("summary")
    if issues:
        return value, sorted(set(issues))
    usable, present, boundary = (value[k] for k in ("image_usable", "target_present", "boundary_certain"))
    candidate, reasons = value["candidate_present"], set(value["reasons"])
    if (candidate is True) != bool(value["defect_types"]):
        issues.append("candidate_types_disagree")
    if not usable and (present or boundary or candidate is not None or "quality.unusable" not in reasons):
        issues.append("unusable_image_assertion")
    if not present and (boundary or candidate is not None or "target.absent_or_unresolved" not in reasons):
        issues.append("absent_target_assertion")
    if not boundary and (candidate is not None or "target.boundary_uncertain" not in reasons):
        issues.append("uncertain_boundary_assertion")
    if candidate is None and not reasons:
        issues.append("uncertainty_reason_missing")
    if candidate is None and usable and present and boundary and not reasons.intersection({"entity.uncertain", "relation.uncertain", "vehicle.context_missing"}):
        issues.append("decision_uncertainty_missing")
    if candidate is not None and reasons:
        issues.append("determinate_with_uncertainty")
    return value, sorted(set(issues))


def prediction(sample: dict, content: str, used: str, finish: str, profile: dict) -> dict:
    """Map uncertainty and malformed replies to refusals, never default normal.

不确定或格式错误统一保留拒判，不缺省为正常。
"""
    parsed, issues = validate_answer(content, finish)
    row = {"sample_id": sample["sample_id"], "image_sha256": sample["image_sha256"], "verdict": "refused", "defect_types": [], "reasons": []}
    if used != profile["model"]:
        row["reasons"].append("model.unexpected_identity")
    if issues:
        row["reasons"] += ["protocol." + i for i in issues]
    if row["reasons"]:
        return row
    if parsed["candidate_present"] is None:
        row["reasons"] = parsed["reasons"]
    elif parsed["confidence"] < profile["confidence_threshold"]:
        row["reasons"] = ["model.low_confidence"]
    else:
        row.update(verdict="suspected" if parsed["candidate_present"] else "normal", defect_types=parsed["defect_types"])
    return row


def profile_identity(profile: dict) -> str:
    """Hash model behavior separately from its selection receipt. / 将模型行为摘要与选择回执分开。"""
    body = {k: v for k, v in profile.items() if k not in {"status", "calibration_record_sha256"}}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_profile(profile: dict) -> None:
    """Require bounded model-only inference settings. / 要求有界且仅用于模型推理的配置。"""
    expected = {"format", "version", "status", "model", "temperature", "max_tokens", "thinking", "reasoning_effort", "timeout_s",
                "minimum_start_interval_s", "confidence_threshold", "max_image_edge", "jpeg_quality", "calibration_record_sha256", "system_prompt", "instruction"}
    if (set(profile) != expected or profile["format"] != "drone.p6-road-model-profile/v1" or profile["version"] != VERSION
            or profile["status"] not in {"draft", "calibration", "frozen"} or profile["model"] != "MiniMax-M3.1-Flash-Preview"):
        raise ValueError("unsupported road model profile")
    for key, low, high in (("temperature", 0, 1), ("confidence_threshold", 0, 1), ("timeout_s", 1, 600), ("minimum_start_interval_s", 30, 600)):
        value = profile[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("invalid profile setting: " + key)
    for key, low, high in (("max_tokens", 256, 16384), ("max_image_edge", 256, 4096), ("jpeg_quality", 70, 100)):
        if type(profile[key]) is not int or not low <= profile[key] <= high:
            raise ValueError("invalid integer profile setting: " + key)
    if type(profile["thinking"]) is not bool or profile["reasoning_effort"] not in {"low", "medium", "high"}:
        raise ValueError("invalid reasoning configuration")
    if any(not isinstance(profile[k], str) or not 1 <= len(profile[k]) <= 16000 for k in ("instruction", "system_prompt")):
        raise ValueError("model instructions missing")


def input_file(package: Path, item: dict) -> Path:
    """Read only bound input bytes within the package. / 只读取包内摘要绑定的输入。"""
    path = within(package, item["file"])
    if digest(path) != item["sha256"]:
        raise ValueError("model input bytes changed")
    return path


def make_messages(profile: dict, row: dict, package: Path) -> list[dict]:
    """Expose only the task description and two current images, not labels or review notes.

只提供任务定义与两份当前影像，不提供标签或复核依据。
"""
    instruction = profile["instruction"] + "\nRegistered inspection task:\n" + json.dumps({"target_brief": row["target_brief"]}, ensure_ascii=False)
    content = [{"type": "text", "text": instruction}]
    for item in row["images"]:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(input_file(package, item).read_bytes()).decode(), "detail": "high"}})
    return [{"role": "system", "content": profile["system_prompt"]}, {"role": "user", "content": content}]


def matching_targets(samples: list[dict], tasks: dict) -> None:
    """Bind every formal model task to the exact target for which truth was recorded.

将每个正式模型任务绑定到记录真值时采用的确切目标。
"""
    if {s["sample_id"] for s in samples} != set(tasks):
        raise ValueError("formal tasks do not cover labeled observations")
    for sample in samples:
        task, target = tasks[sample["sample_id"]], sample.get("target") or {}
        if (not polygon_valid(target.get("polygon")) or target["polygon"] != task.get("target_polygon")
                or not target.get("brief") or target["brief"] != task.get("target_brief")
                or sample["image_sha256"] != task.get("image_sha256")):
            raise ValueError("registered model task differs from the labeled target")


def prepare(manifest_file: Path, tasks_file: Path, profile_file: Path, data_root: Path, output: Path, split: str,
            *, labels_file: Path | None = None, protocol_file: Path | None = None, exposure_file: Path | None = None,
            calibration_file: Path | None = None) -> dict:
    """Keep formal preparation blocked until actual independent inputs satisfy admission.

真实独立输入通过准入之前，阻断正式制备。
"""
    manifest, tasks, profile = read(manifest_file), read(tasks_file), read(profile_file)
    validate_profile(profile)
    if (manifest.get("format") != "drone.p6-road-corpus/v1" or tasks.get("format") != "drone.p6-road-model-tasks/v1"
            or not manifest.get("corpus_id") or tasks.get("corpus_id") != manifest["corpus_id"] or split not in METRICS["SPLITS"]):
        raise ValueError("road input corpus mismatch")
    samples = [s for s in manifest["samples"] if s["split"] == split]
    METRICS["unique"](manifest["samples"], "sample_id")
    by_task = METRICS["unique"](tasks["rows"], "sample_id")
    if not samples or set(by_task) != {s["sample_id"] for s in samples}:
        raise ValueError("tasks must cover the selected split exactly once")
    admission = None
    labels = {}
    if split != "development":
        if any(p is None for p in (labels_file, protocol_file, exposure_file)):
            raise ValueError("formal preparation requires labels, protocol and exposure registry")
        protocol = read(protocol_file)
        checked = METRICS["audit"](manifest, read(labels_file), protocol, read(exposure_file), data_root)
        if checked["data_ready"] is not True:
            raise ValueError("formal road data admission is missing: " + ",".join(checked["issue_counts"]))
        if checked["ai_assisted_labels_declared"] and (
                protocol.get("annotation_policy", {}).get("evaluated_model") != profile["model"]
                or protocol.get("annotation_policy", {}).get("evaluated_provider_id") != "minimax-vl"):
            raise ValueError("assisted annotation independence was registered for a different evaluated model")
        labels = METRICS["unique"](read(labels_file)["rows"], "sample_id")
        admission = {"status": "data_admitted", "labels_sha256": digest(labels_file), "protocol_sha256": digest(protocol_file),
                     "exposure_sha256": digest(exposure_file)}
        matching_targets(samples, by_task)
    if split == "test":
        if profile["status"] != "frozen" or calibration_file is None or digest(calibration_file) != profile["calibration_record_sha256"]:
            raise ValueError("test profile must be selected and frozen after calibration")
        selection = read(calibration_file)
        if (selection.get("format") != "drone.p6-road-profile-selection/v1" or selection.get("status") != "selected"
                or selection.get("profile_identity") != profile_identity(profile) or selection.get("split") != "calibration"
                or selection.get("calibration_source") != "recorded_live" or not selection.get("calibration_replay_sha256")
                or selection.get("replay_digest_encoding") != "canonical_sorted_json/v1"
                or not isinstance(selection.get("decision_reason"), str) or not selection["decision_reason"].strip()):
            raise ValueError("calibration selection evidence is incomplete")
        calibration_root = calibration_file.resolve().parent
        calibration_package = within(calibration_root, selection.get("calibration_package", ""))
        calibration_cloud = within(calibration_root, selection.get("calibration_cloud", ""))
        if not (calibration_package / "plan.json").is_file() or not (calibration_cloud / "execution.json").is_file():
            raise ValueError("actual calibration package and live receipt are required before test preparation")
        from verify_p6_road_business import canonical_digest, live_evidence

        _, _, calibration_plan = verify_package(calibration_package)
        if (calibration_plan["split"] != "calibration" or calibration_plan["profile_identity"] != profile_identity(profile)
                or calibration_plan["source_manifest_sha256"] != digest(manifest_file) or calibration_plan["data_admission"] != admission):
            raise ValueError("calibration corpus or selected behavior differs from the test")
        calibration_result = asyncio.run(live_evidence(calibration_package, calibration_cloud, selection.get("calibration_candidate_commit", "")))
        if (canonical_digest(calibration_result) != selection["calibration_replay_sha256"]
                or digest(calibration_cloud / "execution.json") != selection.get("calibration_execution_sha256")):
            raise ValueError("calibration replay or execution binding changed")
    for sample in samples:
        ident = sample["sample_id"]
        if not isinstance(ident, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ident) is None:
            raise ValueError("unsafe sample identity")
        task = by_task[ident]
        if (set(task) != TASK_KEYS or task["image_sha256"] != sample["image_sha256"] or not polygon_valid(task["target_polygon"])
                or not isinstance(task["target_brief"], str) or not 1 <= len(task["target_brief"].strip()) <= 600
                or not isinstance(task["registration_reference"], str) or not task["registration_reference"].strip()
                or task["origin"] not in {"human_task", "development_draft", "qa_fixture"}):
            raise ValueError("every observation needs an independently registered complete task")
        registered = timestamp(task["registered_at"])
        if split != "development" and (task["origin"] != "human_task" or registered > timestamp(labels[ident]["recorded_at"])):
            raise ValueError("formal task registration cannot come from QA or follow the business label")
        original = within(data_root, sample["image_file"])
        if digest(original) != sample["image_sha256"]:
            raise ValueError("source image changed")
        with Image.open(original) as picture:
            if picture.getexif().get(274, 1) != 1:
                raise ValueError("unsupported image orientation; register a normalized coordinate frame first")
            picture.load()
            if list(picture.size) != [sample["width"], sample["height"]]:
                raise ValueError("source dimensions changed")
    output.mkdir(parents=True, exist_ok=False)
    (output / "images").mkdir()
    rows = []
    for sample in samples:
        task, ident = by_task[sample["sample_id"]], sample["sample_id"]
        with Image.open(within(data_root, sample["image_file"])) as picture:
            current = picture.convert("RGB")
            current.thumbnail((profile["max_image_edge"], profile["max_image_edge"]), Image.Resampling.LANCZOS)
        outline = current.copy()
        drawing = ImageDraw.Draw(outline)
        points = [(x * (current.width - 1), y * (current.height - 1)) for x, y in task["target_polygon"]]
        drawing.line(points + [points[0]], fill=(0, 230, 245), width=max(2, current.width // 500))
        images = []
        for suffix, picture in (("current", current), ("target", outline)):
            name = "images/" + ident + "-" + suffix + ".jpg"
            picture.save(output / name, format="JPEG", quality=profile["jpeg_quality"], subsampling=0)
            images.append({"file": name, "sha256": digest(output / name), "role": suffix})
        rows.append({"sample_id": ident, "image_sha256": sample["image_sha256"], "target_brief": task["target_brief"],
                     "task_sha256": hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                     "task_origin": task["origin"], "images": images})
    shutil.copyfile(profile_file, output / "profile.json")
    write(output / "inputs.json", {"format": "drone.p6-road-model-inputs/v1", "corpus_id": manifest["corpus_id"], "split": split, "rows": rows})
    plan = {"format": "drone.p6-road-s2-plan/v1", "corpus_id": manifest["corpus_id"], "split": split, "planned_calls": len(rows),
            "calls_per_sample": 1, "source_manifest_sha256": digest(manifest_file), "tasks_sha256": digest(tasks_file),
            "profile_sha256": digest(output / "profile.json"), "inputs_sha256": digest(output / "inputs.json"),
            "profile_identity": profile_identity(profile), "data_admission": admission,
            "source_sha256": {name: digest(Path(__file__).with_name(name)) for name in ("p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py")},
            "truth_used_in_model_messages": False, "normal_reference_claimed": False, "formal_release_eligible": False}
    exposure_keys = set()
    if split == "test":
        for sample in samples:
            exposure_keys.add("image_" + sample["image_sha256"])
            for name in ("asset_id", "site_id", "capture_batch_id", "near_duplicate_group"):
                identity = json.dumps([sample["source_id"], name, sample[name]], ensure_ascii=False, separators=(",", ":"))
                exposure_keys.add("group_" + hashlib.sha256(identity.encode()).hexdigest())
    plan["test_exposure_keys"] = sorted(exposure_keys)
    write(output / "plan.json", plan)
    return plan


def verify_package(package: Path) -> tuple[dict, dict, dict]:
    """Bind inference and replay to unchanged source, profile and inputs. / 将推理与回放绑定到未变化的源码、画像及输入。"""
    profile, inputs, plan = (read(package / name) for name in ("profile.json", "inputs.json", "plan.json"))
    validate_profile(profile)
    if (plan.get("format") != "drone.p6-road-s2-plan/v1" or inputs.get("format") != "drone.p6-road-model-inputs/v1"
            or digest(package / "profile.json") != plan["profile_sha256"]
            or digest(package / "inputs.json") != plan["inputs_sha256"] or profile_identity(profile) != plan["profile_identity"]
            or inputs["corpus_id"] != plan["corpus_id"] or inputs["split"] != plan["split"]
            or plan["split"] not in METRICS["SPLITS"] or plan.get("formal_release_eligible") is not False
            or plan.get("truth_used_in_model_messages") is not False or plan.get("normal_reference_claimed") is not False):
        raise ValueError("road package identity mismatch")
    if set(plan.get("source_sha256", {})) != {"p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py"}:
        raise ValueError("complete source binding required")
    if plan["split"] != "development" and (not plan.get("data_admission") or plan["data_admission"].get("status") != "data_admitted"):
        raise ValueError("formal package lacks data admission")
    exposure_keys = plan.get("test_exposure_keys")
    if (not isinstance(exposure_keys, list) or len(exposure_keys) != len(set(exposure_keys))
            or any(not isinstance(k, str) or re.fullmatch(r"(?:image|group)_[0-9a-f]{64}", k) is None for k in exposure_keys)
            or (plan["split"] == "test" and (not exposure_keys or profile["status"] != "frozen"
                or not isinstance(profile["calibration_record_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", profile["calibration_record_sha256"]) is None))
            or (plan["split"] != "test" and exposure_keys)):
        raise ValueError("test exposure binding or frozen selection is invalid")
    for name, expected in plan["source_sha256"].items():
        if Path(name).name != name or digest(Path(__file__).with_name(name)) != expected:
            raise ValueError("road inference source differs from preparation")
    rows = METRICS["unique"](inputs["rows"], "sample_id")
    if len(rows) != plan["planned_calls"] or plan["calls_per_sample"] != 1:
        raise ValueError("road package call coverage mismatch")
    for row in rows.values():
        if (set(row) != {"sample_id", "image_sha256", "target_brief", "task_sha256", "task_origin", "images"}
                or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["sample_id"]) is None
                or re.fullmatch(r"[0-9a-f]{64}", row["image_sha256"]) is None
                or not isinstance(row["target_brief"], str) or not 1 <= len(row["target_brief"].strip()) <= 600
                or row["task_origin"] not in {"human_task", "development_draft", "qa_fixture"}
                or (plan["split"] != "development" and row["task_origin"] != "human_task")):
            raise ValueError("invalid frozen model input row")
        if len(row["images"]) != 2 or [r["role"] for r in row["images"]] != ["current", "target"]:
            raise ValueError("two current-image inputs required")
        for item in row["images"]:
            input_file(package, item)
    return profile, inputs, plan


async def replay(package: Path, recordings: Path) -> dict:
    """Reconstruct every request and retain missing answers in the prediction denominator.

逐项重建请求，把缺失回答保留在预测分母中。
"""
    from drone_agent.providers.replay import Recording, ReplayProvider

    profile, inputs, plan = verify_package(package)
    expected = {r["sample_id"] for r in inputs["rows"]}
    present = {p.stem for p in recordings.glob("*.json")}
    if present - expected:
        raise ValueError("foreign recording files")
    rows, checks, origins = [], [], set()
    for sample in inputs["rows"]:
        path = recordings / (sample["sample_id"] + ".json")
        if not path.is_file():
            rows.append({"sample_id": sample["sample_id"], "image_sha256": sample["image_sha256"], "verdict": "refused",
                         "defect_types": [], "reasons": ["execution.recording_missing"]})
            checks.append({"sample_id": sample["sample_id"], "status": "missing"})
            continue
        recording = Recording.load(path)
        if (recording.model != profile["model"] or recording.prompt_version != VERSION or recording.prompt_sha256 != plan["profile_sha256"]
                or recording.label != sample["sample_id"] or len(recording.exchanges) != 1 or any(e.tool_calls for e in recording.exchanges)):
            raise ValueError("recording provenance mismatch")
        provider = ReplayProvider(recording)
        content, used, finish, _ = await provider.complete(make_messages(profile, sample, package), **{k: profile[k] for k in ARGUMENTS})
        if not provider.exhausted:
            raise ValueError("unexpected recording remainder")
        rows.append(prediction(sample, content, used, finish, profile))
        origins.add(recording.source)
        checks.append({"sample_id": sample["sample_id"], "status": "strict_replay_passed", "recording_sha256": digest(path), "source": recording.source})
    return {"format": "drone.p6-road-predictions/v1", "corpus_id": plan["corpus_id"], "split": plan["split"], "rows": rows,
            "checks": checks, "recording_origins": sorted(origins), "plan_sha256": digest(package / "plan.json"),
            "strict_replay_complete": all(c["status"] == "strict_replay_passed" for c in checks),
            "new_provider_calls": 0, "live_environment_verified": False, "formal_release_eligible": False}


def main() -> None:
    """Prepare or replay offline; live invocation belongs to the isolated cloud runner.

离线制备或回放，实调属于隔离云端执行器。
"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    for name in ("manifest", "tasks", "profile", "data-root", "output"):
        build.add_argument("--" + name, type=Path, required=True)
    for name in ("labels", "protocol", "exposure", "calibration-record"):
        build.add_argument("--" + name, type=Path)
    build.add_argument("--split", choices=sorted(METRICS["SPLITS"]), required=True)
    repeat = commands.add_parser("replay")
    for name in ("package", "recordings", "output"):
        repeat.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.manifest, args.tasks, args.profile, args.data_root, args.output, args.split,
                         labels_file=args.labels, protocol_file=args.protocol, exposure_file=args.exposure, calibration_file=args.calibration_record)
    else:
        result = asyncio.run(replay(args.package, args.recordings))
        write(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"rows", "checks"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
