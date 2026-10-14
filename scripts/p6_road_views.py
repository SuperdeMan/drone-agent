"""Prepare and reduce a development-only full-view / tiled-road comparison.

准备并离线汇总仅供开发的道路整图与分块比较，不调用模型或控制服务。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def digest(path: Path) -> str:
    """Hash exact bytes. / 对原始字节求摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    """Create an artifact once, with platform-independent newlines. / 一次性创建使用统一换行的产物。"""
    with path.open("xb") as stream:
        stream.write((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def tiles(mask: np.ndarray) -> tuple[list[tuple[int, int, int, int]], int]:
    """Retain overlapping quadrants touching support and count covered pixels.

保留接触支持域的重叠象限，并计算被覆盖的支持像素数。
"""
    if mask.ndim != 2 or not mask.any():
        raise ValueError("a nonempty two-dimensional support mask is required")
    height, width = mask.shape
    tw, th = max(1, math.ceil(width * 0.6)), max(1, math.ceil(height * 0.6))
    candidates = dict.fromkeys((x, y, x + tw, y + th) for y in (0, height - th) for x in (0, width - tw))
    selected = [b for b in candidates if mask[b[1]:b[3], b[0]:b[2]].any()]
    covered = np.zeros_like(mask, dtype=bool)
    for x0, y0, x1, y1 in selected:
        covered[y0:y1, x0:x1] = True
    return selected, int((covered & mask.astype(bool)).sum())


def source_path(root: Path, name: str) -> Path:
    """Allow the declared sibling data folder, never escape its shared parent.

允许清单声明的相邻数据目录，不允许逃出共同父目录。
"""
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve().parent):
        raise ValueError("source path leaves the data workspace")
    return path


def view_images(image: Image.Image, mask: Image.Image, box: tuple[int, int, int, int], folder: Path,
                view_id: str) -> list[dict]:
    """Keep an unannotated crop paired with its neutral outline. / 将无标记裁剪图与中性轮廓图配对保存。"""
    raw = image.crop(box).convert("RGB")
    raw.thumbnail((1344, 1344))
    support = mask.crop(box).resize(raw.size, Image.Resampling.NEAREST)
    edge = support.filter(ImageFilter.FIND_EDGES()).filter(ImageFilter.MaxFilter(3))
    overlay = np.zeros((raw.height, raw.width, 4), dtype=np.uint8)
    overlay[np.asarray(edge) > 0] = (32, 176, 222, 220)
    outlined = Image.alpha_composite(raw.convert("RGBA"), Image.fromarray(overlay)).convert("RGB")
    images = []
    for label, frame in (("original", raw), ("target", outlined)):
        path = folder / f"{view_id}-{label}.jpg"
        with path.open("xb") as stream:
            frame.save(stream, format="JPEG", quality=90)
        images.append({"file": path.name, "sha256": digest(path)})
    return images


def prepare(index_file: Path, data_root: Path, base_config: Path, selected: list[str], output: Path) -> dict:
    """Freeze inputs and an answer-independent call order, without changing sources.

冻结输入与不依赖回答的调用顺序，保持源文件不变。
"""
    if output.exists():
        raise ValueError("output already exists; preserve the previous attempt")
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("case selection must be nonempty and unique")
    source = json.loads(index_file.read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in source["cases"]}
    checked = []
    for ident in selected:
        case = cases[ident]
        region = case.get("target_region")
        if not region or region.get("registered_asset_boundary") is not False:
            raise ValueError("this experiment requires a declared provisional support region")
        image_file = source_path(data_root, case["image_file"])
        mask_file = source_path(data_root, region["file"])
        if digest(image_file) != case["image_sha256"] or digest(mask_file) != region["sha256"]:
            raise ValueError("source digest mismatch")
        with Image.open(image_file) as image, Image.open(mask_file) as mask:
            if image.size != mask.size or image.size != (case["width"], case["height"]):
                raise ValueError("source dimensions mismatch")
            values = np.unique(np.asarray(mask))
            if mask.mode != "L" or not set(values.tolist()) <= {0, 255} or 255 not in values:
                raise ValueError("support must contain only 0 and 255, including positive support")
        checked.append((case, image_file, mask_file))
    config = json.loads(base_config.read_text(encoding="utf-8"))
    expected = "Image 1 is the original photograph. Image 2 is the same photograph"
    coordinate = "normalized 0..1 in the ORIGINAL image"
    if expected not in config["instruction"] or coordinate not in config["instruction"]:
        raise ValueError("unexpected baseline prompt")
    config["prompt_version"] = "road-region-views-dev-v2"
    config["instruction"] = config["instruction"].replace(
        expected, "Image 1 is an unannotated view from one photograph. Image 2 is the same view"
    ).replace(coordinate, "normalized 0..1 in the supplied view (image 1)")
    config["instruction"] += (
        "\nAssess only the designated road visible in this supplied view, which may be a crop. "
        "Do not make a conclusion about portions outside this view. Do not convert boxes to another coordinate frame."
    )
    output.mkdir(parents=True)
    inputs = output / "inputs"
    inputs.mkdir()
    rng = random.Random(20261014)
    rng.shuffle(checked)
    manifest, parents = [], []
    for ordinal, (case, image_file, mask_file) in enumerate(checked):
        with Image.open(image_file) as image, Image.open(mask_file) as mask:
            support = np.asarray(mask) > 0
            boxes, covered = tiles(support)
            height, width = support.shape
            y, x = np.nonzero(support)
            parent = {
                "case_id": case["case_id"], "source_image_sha256": case["image_sha256"],
                "source_target_sha256": case["target_region"]["sha256"], "source_size": [width, height],
                "source_support_bbox": [int(x.min()), int(y.min()), int(x.max()) + 1, int(y.max()) + 1],
                "source_support_pixels": int(support.sum()), "covered_support_pixels": covered,
                "source_support_fully_covered": covered == int(support.sum()),
                "registered_asset_boundary": False, "views": [],
            }
            full = (0, 0, width, height)
            entries = [("tiles", b) for b in boxes]
            rng.shuffle(entries)
            entries.insert(0 if ordinal % 2 == 0 else len(entries), ("full", full))
            for arm, box in entries:
                identity = json.dumps([case["case_id"], arm, box], separators=(",", ":"))
                view_id = "view-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
                view = {"case_id": view_id, "arm": arm, "crop_px": list(box),
                        "source_support_pixels": int(support[box[1]:box[3], box[0]:box[2]].sum())}
                images = view_images(image, mask, box, inputs, view_id)
                manifest.append({"case_id": view_id, "images": images})
                parent["views"].append(view)
            parents.append(parent)
    write_json(output / "config.json", config)
    write_json(inputs / "manifest.json", manifest)
    plan = {
        "format": "drone.road-view-development-plan/v1", "seed": 20261014,
        "prompt_version": config["prompt_version"], "model": config["model"],
        "source_index_sha256": digest(index_file), "baseline_config_sha256": digest(base_config),
        "builder_sha256": digest(Path(__file__)), "config_sha256": digest(output / "config.json"),
        "manifest_sha256": digest(inputs / "manifest.json"), "targets": len(parents),
        "unique_source_images": len({c["source_image_sha256"] for c in parents}), "planned_calls": len(manifest),
        "formal_release_eligible": False, "human_ground_truth_available": False,
        "selection": "exposed development cases; not random or held out", "parents": parents,
    }
    write_json(output / "plan.json", plan)
    return plan


def parsed_answer(result: dict | None, model: str) -> dict | None:
    """Fail closed on missing, unverified or inconsistent answers. / 缺失、未核验或不一致的回答保持无结论。"""
    if not result or result.get("status") != "answered" or result.get("model_used") != model:
        return None
    if result.get("finish") != "stop" or result.get("strict_replay") != "passed" or result.get("schema_issues") != []:
        return None
    try:
        answer = json.loads(result["raw_content"])
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(answer, dict) or answer != result.get("parsed"):
        return None
    if set(answer) != {"image_usable", "target_visible", "verdict", "obstacles", "reason", "uncertainty"}:
        return None
    if any(type(answer[k]) is not bool for k in ("image_usable", "target_visible")):
        return None
    verdict, obstacles = answer["verdict"], answer["obstacles"]
    if (not isinstance(verdict, str) or verdict not in {"normal", "suspected", "unknown"}
            or not isinstance(obstacles, list) or len(obstacles) > 8):
        return None
    for obstacle in obstacles:
        if not isinstance(obstacle, dict) or set(obstacle) != {"type", "bbox"}:
            return None
        if not isinstance(obstacle["type"], str) or obstacle["type"] not in {
                "debris", "fallen_tree", "large_object", "sand_deposit", "vehicle_uncertain", "unknown"}:
            return None
        box = obstacle["bbox"]
        if not isinstance(box, list) or len(box) != 4 or not all(
                type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box):
            return None
        if not (box[0] < box[2] and box[1] < box[3]):
            return None
    if (verdict == "normal" and obstacles) or (verdict == "suspected" and not obstacles):
        return None
    if not all(answer[k] for k in ("image_usable", "target_visible")) and verdict != "unknown":
        return None
    if any(not isinstance(answer[k], str) for k in ("reason", "uncertainty")):
        return None
    return answer


def original_box(box: list[float], crop: list[int], size: list[int]) -> list[float]:
    """Map view coordinates to the original photograph without model arithmetic.

确定性地映射视图坐标到原图，不依赖模型做坐标运算。
"""
    x0, y0, x1, y1 = crop
    width, height = size
    return [round((x0 + box[0] * (x1 - x0)) / width, 6),
            round((y0 + box[1] * (y1 - y0)) / height, 6),
            round((x0 + box[2] * (x1 - x0)) / width, 6),
            round((y0 + box[3] * (y1 - y0)) / height, 6)]


def summarize(plan: dict, results: list[dict]) -> dict:
    """Compare paired views; never infer whole-target normal from a subset.

比较成对视图，不把局部正常推成整个目标正常。
"""
    expected = {v["case_id"] for p in plan["parents"] for v in p["views"]}
    lookup = {r["case_id"]: r for r in results}
    if len(lookup) != len(results) or set(lookup) - expected:
        raise ValueError("duplicate or foreign result")
    rows = []
    for parent in plan["parents"]:
        answers, obstacles, invalid = {}, [], []
        for view in parent["views"]:
            ident = view["case_id"]
            answer = parsed_answer(lookup.get(ident), plan["model"])
            answers[ident] = answer
            if answer is None:
                invalid.append(ident)
            else:
                for item in answer["obstacles"]:
                    obstacles.append({"view_id": ident, "arm": view["arm"], "type": item["type"],
                                      "bbox_in_view": item["bbox"],
                                      "bbox_in_source": original_box(item["bbox"], view["crop_px"], parent["source_size"])})
        tiled = [answers[v["case_id"]] for v in parent["views"] if v["arm"] == "tiles"]
        full = [answers[v["case_id"]] for v in parent["views"] if v["arm"] == "full"]
        if len(full) != 1 or not tiled:
            raise ValueError("each target requires one full view and at least one tile")
        coverage_ok = (parent["source_support_fully_covered"] is True and parent["source_support_pixels"] > 0
                       and parent["covered_support_pixels"] == parent["source_support_pixels"])
        if any(a and a["verdict"] == "suspected" for a in tiled):
            verdict = "suspected"
        elif coverage_ok and all(a and a["verdict"] == "normal" for a in tiled):
            verdict = "normal"
        else:
            verdict = "unknown"
        rows.append({"case_id": parent["case_id"], "full_verdict": full[0]["verdict"] if full[0] else "unknown",
                     "tiles_verdict": verdict, "tile_verdicts": [a["verdict"] if a else "invalid_or_missing" for a in tiled],
                     "invalid_or_missing_views": invalid, "source_support_fully_covered": coverage_ok,
                     "obstacles": obstacles, "independent_human_verdict": None})
    return {"format": "drone.road-view-development-summary/v1", "targets": len(rows), "planned_calls": len(expected),
            "results_received": len(results), "valid_answers": sum(parsed_answer(r, plan["model"]) is not None for r in results),
            "rows": rows, "overlapping_views_may_duplicate_obstacles": True, "accuracy_computed": False,
            "formal_release_eligible": False}


def main() -> None:
    """Expose only offline preparation and reduction. / 仅提供离线准备与汇总命令。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--index", type=Path, required=True)
    prep.add_argument("--data-root", type=Path, required=True)
    prep.add_argument("--base-config", type=Path, required=True)
    prep.add_argument("--case", action="append", required=True)
    prep.add_argument("--output", type=Path, required=True)
    reduce = commands.add_parser("summarize")
    reduce.add_argument("--plan", type=Path, required=True)
    reduce.add_argument("--results", type=Path, required=True)
    reduce.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.index, args.data_root, args.base_config, args.case, args.output)
        print(json.dumps({k: v for k, v in result.items() if k != "parents"}))
    else:
        result = summarize(json.loads(args.plan.read_text(encoding="utf-8")),
                           json.loads(args.results.read_text(encoding="utf-8")))
        write_json(args.output, result)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}))


if __name__ == "__main__":
    main()
