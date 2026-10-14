"""Development views preserve coverage, coordinates, provenance and unknown outcomes.

开发视图保留覆盖范围、坐标、来源与未知结论。
"""
from __future__ import annotations

import copy
import hashlib
import json
import runpy
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOL = runpy.run_path(str(ROOT / "scripts/p6_road_views.py"))
MODEL = "MiniMax-M3.1-Flash-Preview"


def plan():
    return {"model": MODEL, "parents": [{
        "case_id": "target", "source_size": [100, 80], "source_support_fully_covered": True,
        "source_support_pixels": 100, "covered_support_pixels": 100,
        "views": [{"case_id": "full", "arm": "full", "crop_px": [0, 0, 100, 80]},
                  {"case_id": "left", "arm": "tiles", "crop_px": [0, 0, 60, 80]},
                  {"case_id": "right", "arm": "tiles", "crop_px": [40, 0, 100, 80]}],
    }]}


def answer(ident, verdict="normal"):
    parsed = {"image_usable": True, "target_visible": True, "verdict": verdict, "obstacles": [],
              "reason": "QA fixture", "uncertainty": "Not ground truth"}
    if verdict == "suspected":
        parsed["obstacles"] = [{"type": "debris", "bbox": [0.25, 0.25, 0.75, 0.75]}]
    return {"case_id": ident, "status": "answered", "model_used": MODEL, "finish": "stop",
            "strict_replay": "passed", "schema_issues": [], "raw_content": json.dumps(parsed), "parsed": parsed}


@pytest.mark.parametrize("shape", [(7, 11), (1, 1), (8, 10)])
def test_tiles_cover_edges_without_inventing_positive_support(shape):
    mask = np.zeros(shape, dtype=bool)
    mask[0, 0] = mask[-1, -1] = True
    boxes, count = TOOL["tiles"](mask)
    union = np.zeros(shape, dtype=bool)
    for x0, y0, x1, y1 in boxes:
        assert 0 <= x0 < x1 <= shape[1] and 0 <= y0 < y1 <= shape[0]
        assert mask[y0:y1, x0:x1].any()
        union[y0:y1, x0:x1] = True
    assert (union & mask).sum() == count == mask.sum()
    assert len(boxes) == len(set(boxes))
    with pytest.raises(ValueError):
        TOOL["tiles"](np.zeros(shape, dtype=bool))


def test_boxes_map_to_the_original_frame_including_overlapping_views():
    assert TOOL["original_box"]([0, 0, 1, 1], [40, 20, 100, 80], [100, 80]) == [0.4, 0.25, 1.0, 1.0]
    assert TOOL["original_box"]([0.25, 0.5, 0.75, 1], [40, 20, 100, 80], [100, 80]) == [0.55, 0.625, 0.85, 1.0]


@pytest.mark.parametrize("fault", ["missing", "unknown", "invalid", "coverage_flag", "coverage_count"])
def test_partial_or_uncertain_evidence_never_becomes_whole_target_normal(fault):
    p = plan()
    rows = [answer("full"), answer("left"), answer("right")]
    if fault == "missing":
        rows.pop()
    elif fault == "unknown":
        rows[-1] = answer("right", "unknown")
    elif fault == "invalid":
        rows[-1]["strict_replay"] = "not_checked"
    elif fault == "coverage_flag":
        p["parents"][0]["source_support_fully_covered"] = False
    else:
        p["parents"][0]["covered_support_pixels"] = 99
    result = TOOL["summarize"](p, rows)
    assert result["rows"][0]["tiles_verdict"] == "unknown"
    assert result["formal_release_eligible"] is False


def test_a_positive_candidate_is_retained_alongside_missing_views():
    result = TOOL["summarize"](plan(), [answer("full"), answer("right", "suspected")])
    row = result["rows"][0]
    assert row["tiles_verdict"] == "suspected" and row["invalid_or_missing_views"] == ["left"]
    assert row["obstacles"][0]["bbox_in_source"] == [0.55, 0.25, 0.85, 0.75]
    assert result["accuracy_computed"] is False and row["independent_human_verdict"] is None


def test_complete_normal_views_remain_development_only():
    result = TOOL["summarize"](plan(), [answer("full"), answer("left"), answer("right")])
    assert result["rows"][0]["tiles_verdict"] == "normal"
    assert result["valid_answers"] == 3 and result["formal_release_eligible"] is False


@pytest.mark.parametrize("corruption", ["bounds", "nan", "hidden_target", "wrong_model", "raw_mismatch",
                                      "verdict_array", "type_array"])
def test_declared_schema_success_cannot_hide_invalid_content(corruption):
    row = answer("left", "suspected")
    if corruption == "bounds":
        row["parsed"]["obstacles"][0]["bbox"][0] = -0.1
    elif corruption == "nan":
        row["parsed"]["obstacles"][0]["bbox"][0] = float("nan")
    elif corruption == "hidden_target":
        row["parsed"]["target_visible"] = False
    elif corruption == "wrong_model":
        row["model_used"] = "different-model"
    elif corruption == "verdict_array":
        row["parsed"]["verdict"] = ["suspected"]
    elif corruption == "type_array":
        row["parsed"]["obstacles"][0]["type"] = ["debris"]
    else:
        row["parsed"]["reason"] = "changed after recording"
    if corruption != "raw_mismatch":
        row["raw_content"] = json.dumps(row["parsed"])
    assert TOOL["parsed_answer"](row, MODEL) is None


def test_foreign_or_duplicate_results_are_rejected():
    with pytest.raises(ValueError):
        TOOL["summarize"](plan(), [answer("foreign")])
    with pytest.raises(ValueError):
        TOOL["summarize"](plan(), [answer("left"), answer("left")])


def source_fixture(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    Image.new("RGB", (40, 30), "gray").save(data / "image.png")
    mask = np.zeros((30, 40), dtype=np.uint8)
    mask[2:28, 18:22] = 255
    Image.fromarray(mask).save(data / "mask.png")
    case = {"case_id": "opaque-case", "image_file": "image.png", "image_sha256": TOOL["digest"](data / "image.png"),
            "width": 40, "height": 30, "target_region": {"file": "mask.png", "sha256": TOOL["digest"](data / "mask.png"),
                                                       "registered_asset_boundary": False},
            "source_class": "ANSWER_MUST_NOT_LEAK"}
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"cases": [case]}), encoding="utf-8")
    config = ROOT / "eval/s2/p6_candidates_v3/diagnostic/config.json"
    return data, index, config


def test_preparation_is_deterministic_does_not_leak_labels_or_overwrite(tmp_path):
    data, index, config = source_fixture(tmp_path)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.iterdir()}
    first, second = tmp_path / "first", tmp_path / "second"
    a = TOOL["prepare"](index, data, config, ["opaque-case"], first)
    b = TOOL["prepare"](index, data, config, ["opaque-case"], second)
    assert a == b and a["targets"] == 1 and a["planned_calls"] == 5
    assert a["parents"][0]["source_support_fully_covered"] is True
    for p in first.rglob("*"):
        if p.is_file():
            assert p.read_bytes() == (second / p.relative_to(first)).read_bytes()
            assert b"ANSWER_MUST_NOT_LEAK" not in p.read_bytes()
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.iterdir()}
    with pytest.raises(ValueError):
        TOOL["prepare"](index, data, config, ["opaque-case"], first)


def test_a_substituted_source_is_rejected_before_preparation(tmp_path):
    data, index, config = source_fixture(tmp_path)
    original = json.loads(index.read_text())
    changed = copy.deepcopy(original)
    changed["cases"][0]["image_sha256"] = "0" * 64
    index.write_text(json.dumps(changed), encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="digest"):
        TOOL["prepare"](index, data, config, ["opaque-case"], out)
    assert not out.exists()
    with pytest.raises(ValueError, match="workspace"):
        TOOL["source_path"](data, "../../secret.png")
