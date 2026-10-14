"""Audit provisional road/object envelopes without creating business ground truth.

核对道路与实体包络草稿，不生成权威业务真值或调用模型。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def digest(path: Path) -> str:
    """Hash source bytes. / 对源文件字节求摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def simple_polygon(points: object) -> bool:
    """Reject nonfinite, degenerate or self-intersecting normalized polygons.

拒绝非有限、退化或自交的归一化多边形。
"""
    if not isinstance(points, list) or not 3 <= len(points) <= 500:
        return False
    if any(not isinstance(p, list) or len(p) != 2 or not all(
            type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1 for x in p) for p in points):
        return False

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on(a, b, c):
        return (abs(cross(a, b, c)) < 1e-12 and min(a[0], b[0]) - 1e-12 <= c[0] <= max(a[0], b[0]) + 1e-12
                and min(a[1], b[1]) - 1e-12 <= c[1] <= max(a[1], b[1]) + 1e-12)

    area = 0.0
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        if math.dist(a, b) < 1e-12:
            return False
        area += a[0] * b[1] - b[0] * a[1]
        for j in range(i + 2, len(points)):
            if i == 0 and j == len(points) - 1:
                continue
            c, d = points[j], points[(j + 1) % len(points)]
            if ((cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0)
                    or on(a, b, c) or on(a, b, d) or on(c, d, a) or on(c, d, b)):
                return False
    return abs(area) > 1e-12


def raster(polygons: object, size: tuple[int, int], *, allow_empty: bool = False) -> np.ndarray:
    """Rasterize declared polygons only; do not turn a model box into an object.

仅栅格化声明的多边形，不把模型框当成实体轮廓。
"""
    if not isinstance(polygons, list) or (not polygons and not allow_empty):
        raise ValueError("polygon list is missing")
    if not all(simple_polygon(p) for p in polygons):
        raise ValueError("invalid polygon")
    width, height = size
    if type(width) is not int or type(height) is not int or min(size) < 2 or width * height > 24_000_000:
        raise ValueError("unsupported raster size")
    image = Image.new("L", size, 0)
    draw = ImageDraw.Draw(image)
    for polygon in polygons:
        draw.polygon([(x * (width - 1), y * (height - 1)) for x, y in polygon], fill=255)
    return np.asarray(image) > 0


def morph(mask: np.ndarray, *, shrink: bool) -> np.ndarray:
    """Use one pixel as a numerical contact guard, not a business size threshold.

用一像素作为数值接触误差带，不作为业务大小阈值。
"""
    padded = np.pad(mask.astype(np.uint8) * 255, 1)
    image = Image.fromarray(padded).filter(ImageFilter.MinFilter(3) if shrink else ImageFilter.MaxFilter(3))
    return np.asarray(image)[1:-1, 1:-1] > 0


def envelope(value: dict, size: tuple[int, int], *, core_optional: bool) -> tuple[np.ndarray, np.ndarray]:
    """Keep the declared inner domain inside the declared outer domain.

保持已声明的内域包含在已声明外域中。
"""
    core = raster(value["core"], size, allow_empty=core_optional)
    outer = raster(value["outer"], size)
    if (core & ~outer).any():
        raise ValueError("inner domain leaves outer domain")
    return core, outer


def relation(road: dict, entity: dict, support: np.ndarray) -> dict:
    """Report a conditional image-space relation, never a scene verdict.

只报告以草稿为条件的图像空间关系，不给出整图业务结论。
"""
    if support.ndim != 2 or not support.any():
        raise ValueError("nonempty source support required")
    size = (support.shape[1], support.shape[0])
    road_core, manual_outer = envelope(road, size, core_optional=False)
    object_core, object_outer = envelope(entity, size, core_optional=True)
    possible_road = manual_outer | support.astype(bool)
    certain = morph(road_core, shrink=True) & morph(object_core, shrink=True)
    possible = morph(possible_road, shrink=False) & morph(object_outer, shrink=False)
    outcome = "inside" if certain.any() else "edge_uncertain" if possible.any() else "outside"
    return {"relation": outcome, "core_overlap_pixels": int(certain.sum()),
            "possible_overlap_pixels": int(possible.sum()), "object_core_pixels": int(object_core.sum()),
            "object_outer_pixels": int(object_outer.sum()),
            "source_support_overlap_pixels": int((support.astype(bool) & object_outer).sum()),
            "source_support_only_expands_possible_road": True, "numerical_guard_pixels": 1,
            "visible_frame_only": True, "conditional_on_ai_drafts": True,
            "business_verdict": None, "formal_release_eligible": False}


def safe_source(root: Path, name: str) -> Path:
    """Permit the existing sibling dataset, not unrelated paths. / 允许现有相邻数据集路径，不访问无关路径。"""
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve().parent):
        raise ValueError("source leaves data workspace")
    return path


def audit(bundle_file: Path, index_file: Path, prior_summary: Path, data_root: Path) -> dict:
    """Bind drafts to exact sources and account for every earlier candidate.

将草稿绑定到确切来源，并核对上一轮每个候选的归属。
"""
    bundle = json.loads(bundle_file.read_text(encoding="utf-8"))
    if bundle.get("author_kind") != "ai_draft" or bundle.get("formal_release_eligible") is not False:
        raise ValueError("this tool accepts development AI drafts only")
    if (bundle.get("policy_id") != "road-contact-dev-v1" or bundle.get("minimum_size_threshold") is not None
            or bundle.get("minimum_occupancy_ratio") is not None):
        raise ValueError("unexpected business policy")
    if bundle.get("source_index_sha256") != digest(index_file) or bundle.get("prior_summary_sha256") != digest(prior_summary):
        raise ValueError("source manifest or previous summary changed")
    cases = {c["case_id"]: c for c in json.loads(index_file.read_text(encoding="utf-8"))["cases"]}
    prior = json.loads(prior_summary.read_text(encoding="utf-8"))["rows"]
    originals = {}
    for row in prior:
        counts = {}
        for obstacle in row["obstacles"]:
            view = obstacle["view_id"]
            ordinal = counts.get(view, 0)
            counts[view] = ordinal + 1
            originals[f"{row['case_id']}:{view}:{ordinal}"] = obstacle
    seen, group_ids, target_ids, rows = set(), set(), set(), []
    for target in bundle["targets"]:
        ident = target["case_id"]
        if ident in target_ids or ident not in cases:
            raise ValueError("duplicate or foreign target")
        target_ids.add(ident)
        source = cases[ident]
        image_file = safe_source(data_root, source["image_file"])
        mask_file = safe_source(data_root, source["target_region"]["file"])
        if digest(image_file) != source["image_sha256"] or digest(mask_file) != source["target_region"]["sha256"]:
            raise ValueError("image or support digest mismatch")
        with Image.open(image_file) as image, Image.open(mask_file) as mask:
            if image.size != mask.size or image.size != (source["width"], source["height"]):
                raise ValueError("image/support dimensions mismatch")
            values = np.asarray(mask)
            if mask.mode != "L" or not set(np.unique(values).tolist()) <= {0, 255}:
                raise ValueError("source support must be neutral binary")
            support = values > 0
        for group in target["entities"]:
            gid = group["entity_id"]
            if gid in group_ids or not group["candidate_refs"]:
                raise ValueError("duplicate entity or missing source candidates")
            group_ids.add(gid)
            for ref in group["candidate_refs"]:
                if ref in seen or ref not in originals or not ref.startswith(ident + ":"):
                    raise ValueError("duplicate or foreign candidate binding")
                seen.add(ref)
            hints = relation(target["road_envelope"], group["object_envelope"], support)
            rows.append({"case_id": ident, "entity_id": gid, "candidate_refs": group["candidate_refs"],
                         "entity_description": group["description"], "ai_notes": group["ai_notes"], **hints,
                         "draft_disposition": {"inside": "retain_occlusion_candidate", "outside": "scope_review",
                                               "edge_uncertain": "boundary_review"}[hints["relation"]],
                         "reviewer": None, "human_approved_road": None, "human_approved_object": None})
    if seen != set(originals):
        raise ValueError("not every previous candidate has an explicit disposition")
    return {"format": "drone.p6-boundary-draft-audit/v1", "policy_id": bundle["policy_id"],
            "drafts_sha256": digest(bundle_file), "tool_sha256": digest(Path(__file__)),
            "targets": len(target_ids), "raw_candidates": len(seen), "proposed_entity_groups": len(group_ids),
            "rows": rows, "grouping_is_ai_proposed": True, "minimum_business_size_threshold": None,
            "accuracy_computed": False, "scene_verdict": None, "human_reviews_completed": 0,
            "formal_release_eligible": False}


def main() -> None:
    """Run an offline audit and create a new receipt. / 离线核对，并创建新的回执。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--previous-summary", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.drafts, args.index, args.previous_summary, args.data_root)
    with args.output.open("xb") as stream:
        stream.write((json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}))


if __name__ == "__main__":
    main()
