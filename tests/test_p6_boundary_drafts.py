"""Boundary drafts preserve provenance, uncertain contact and candidate-only authority.

边界草稿保留来源、不确定接触与仅供候选复核的权限边界。
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOL = runpy.run_path(str(ROOT / "scripts/p6_boundary_drafts.py"))


def rectangle(left, top, right, bottom):
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def domains(core, outer=None):
    return {"core": [] if core is None else [core], "outer": [outer if outer is not None else core]}


def support():
    return TOOL["raster"]([rectangle(0.2, 0.2, 0.8, 0.8)], (100, 100))


@pytest.mark.parametrize("box,expected", [
    ((0.4, 0.4, 0.5, 0.5), "inside"),
    ((0.9, 0.3, 0.98, 0.6), "outside"),
    ((0.8, 0.3, 0.9, 0.6), "edge_uncertain"),
])
def test_inside_outside_and_boundary_touch_are_distinct(box, expected):
    road = domains(rectangle(0.2, 0.2, 0.8, 0.8))
    result = TOOL["relation"](road, domains(rectangle(*box)), support())
    assert result["relation"] == expected
    assert result["business_verdict"] is None and result["formal_release_eligible"] is False


def test_a_support_hole_does_not_turn_a_covering_object_into_outside():
    mask = support()
    mask[35:65, 35:65] = False
    result = TOOL["relation"](domains(rectangle(0.2, 0.2, 0.8, 0.8)),
                              domains(rectangle(0.4, 0.4, 0.5, 0.5)), mask)
    assert result["relation"] == "inside" and result["source_support_overlap_pixels"] == 0


def test_a_model_box_cannot_replace_the_physical_object_footprint():
    entity = domains(rectangle(0.9, 0.3, 0.98, 0.6))
    entity["model_bbox"] = [0.5, 0.2, 0.99, 0.7]
    result = TOOL["relation"](domains(rectangle(0.2, 0.2, 0.8, 0.8)), entity, support())
    assert result["relation"] == "outside"


def test_possible_envelopes_and_unresolved_entities_remain_uncertain():
    road = domains(rectangle(0.3, 0.3, 0.7, 0.7), rectangle(0.2, 0.2, 0.8, 0.8))
    assert TOOL["relation"](road, domains(rectangle(0.74, 0.4, 0.77, 0.6)), support())["relation"] == "edge_uncertain"
    assert TOOL["relation"](road, domains(None, rectangle(0.4, 0.4, 0.5, 0.5)), support())["relation"] == "edge_uncertain"


def test_source_support_only_expands_the_possible_road():
    mask = support()
    mask[40:60, 90:98] = True
    result = TOOL["relation"](domains(rectangle(0.3, 0.3, 0.7, 0.7), rectangle(0.2, 0.2, 0.8, 0.8)),
                              domains(rectangle(0.91, 0.45, 0.95, 0.55)), mask)
    assert result["relation"] == "edge_uncertain" and result["core_overlap_pixels"] == 0


def test_disjoint_entity_parts_do_not_fill_the_gap_over_the_road():
    left, right = rectangle(0.01, 0.4, 0.1, 0.6), rectangle(0.9, 0.4, 0.99, 0.6)
    entity = {"core": [left, right], "outer": [left, right]}
    assert TOOL["relation"](domains(rectangle(0.2, 0.2, 0.8, 0.8)), entity, support())["relation"] == "outside"


def test_inner_domain_outside_outer_domain_and_empty_support_are_rejected():
    with pytest.raises(ValueError, match="inner"):
        TOOL["relation"](domains(rectangle(0.2, 0.2, 0.8, 0.8)),
                         domains(rectangle(0.3, 0.3, 0.6, 0.6), rectangle(0.4, 0.4, 0.5, 0.5)), support())
    with pytest.raises(ValueError, match="support"):
        TOOL["relation"](domains(rectangle(0.2, 0.2, 0.8, 0.8)),
                         domains(rectangle(0.4, 0.4, 0.5, 0.5)), np.zeros((100, 100), dtype=bool))


@pytest.mark.parametrize("polygon", [
    [[0, 0], [1, 1], [0, 1], [1, 0]],
    [[0, 0], [0.2, 0.2], [0.4, 0.4]],
    [[0, 0], [1, 0], [1, 1], [0.5, 0], [0, 1]],
    [[0, 0], [1, 0], [1, 0], [0, 1]],
    [[0, 0], [True, 0], [0, 1]],
    [[0, 0], [float("nan"), 0], [0, 1]],
    [[0, 0], [float("inf"), 0], [0, 1]],
    [[0, 0], [1.1, 0], [0, 1]],
])
def test_invalid_geometry_fails_without_a_candidate_judgment(polygon):
    assert TOOL["simple_polygon"](polygon) is False
    with pytest.raises(ValueError, match="polygon"):
        TOOL["raster"]([polygon], (100, 100))


def test_small_geometry_is_not_silently_removed_by_a_business_threshold():
    tiny = rectangle(0.4001, 0.4001, 0.4002, 0.4002)
    assert TOOL["simple_polygon"](tiny) is True
    value = TOOL["relation"](domains(rectangle(0.2, 0.2, 0.8, 0.8)), domains(tiny), support())
    assert value["relation"] == "edge_uncertain" and value["business_verdict"] is None


def fixture(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    Image.new("RGB", (100, 100), "gray").save(data / "photo.png")
    Image.fromarray(support().astype(np.uint8) * 255).save(data / "support.png")
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"cases": [{"case_id": "target", "image_file": "photo.png",
        "image_sha256": TOOL["digest"](data / "photo.png"), "width": 100, "height": 100,
        "target_region": {"file": "support.png", "sha256": TOOL["digest"](data / "support.png")}}]}))
    summary = tmp_path / "previous.json"
    summary.write_text(json.dumps({"rows": [{"case_id": "target", "obstacles": [{"view_id": "view"}]}]}))
    bundle = {"author_kind": "ai_draft", "formal_release_eligible": False, "policy_id": "road-contact-dev-v1",
              "minimum_size_threshold": None, "minimum_occupancy_ratio": None,
              "source_index_sha256": TOOL["digest"](index), "prior_summary_sha256": TOOL["digest"](summary),
              "targets": [{"case_id": "target", "road_envelope": domains(rectangle(0.2, 0.2, 0.8, 0.8)),
                  "entities": [{"entity_id": "object", "candidate_refs": ["target:view:0"],
                      "object_envelope": domains(rectangle(0.4, 0.4, 0.5, 0.5)), "description": "fixture",
                      "ai_notes": "synthetic geometry, not a human annotation"}]}]}
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps(bundle))
    return path, bundle, index, summary, data


def test_auditing_keeps_human_and_business_decisions_unset(tmp_path):
    path, _, index, summary, data = fixture(tmp_path)
    value = TOOL["audit"](path, index, summary, data)
    assert value["raw_candidates"] == value["proposed_entity_groups"] == 1
    assert value["rows"][0]["draft_disposition"] == "retain_occlusion_candidate"
    assert value["rows"][0]["reviewer"] is None and value["human_reviews_completed"] == 0
    assert value["scene_verdict"] is None and value["formal_release_eligible"] is False


@pytest.mark.parametrize("fault", ["authority", "threshold", "prior_digest", "image", "duplicate", "missing", "foreign"])
def test_changed_sources_or_bindings_fail_closed(tmp_path, fault):
    path, bundle, index, summary, data = fixture(tmp_path)
    if fault == "authority":
        bundle["formal_release_eligible"] = True
    elif fault == "threshold":
        bundle["minimum_occupancy_ratio"] = 0.1
    elif fault == "prior_digest":
        bundle["prior_summary_sha256"] = "0" * 64
    elif fault == "image":
        (data / "photo.png").write_bytes(b"changed")
    elif fault == "duplicate":
        bundle["targets"][0]["entities"][0]["candidate_refs"].append("target:view:0")
    elif fault == "missing":
        bundle["targets"] = []
    else:
        bundle["targets"][0]["entities"][0]["candidate_refs"] = ["foreign:view:0"]
    path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError):
        TOOL["audit"](path, index, summary, data)
