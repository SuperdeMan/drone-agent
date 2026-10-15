"""Exports remain development entries with explicit dependencies. / 导出保持开发记录属性并显式保留依赖。"""
from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

review = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_boundary_review.py")))


def geometry(left=.2, right=.8):
    polygon = [[left, .2], [right, .2], [right, .8], [left, .8]]
    return {"core": [polygon], "outer": [polygon]}


@pytest.fixture
def package(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    Image.new("RGB", (100, 100), "gray").save(data / "photo.jpg")
    Image.new("L", (100, 100), 255).save(data / "support.png")
    source = {"case_id": "road", "image_id": "1", "image_file": "photo.jpg", "image_sha256": review.digest(data / "photo.jpg"),
              "width": 100, "height": 100, "target_region": {"file": "support.png", "sha256": review.digest(data / "support.png")}}
    index, prior, drafts = (tmp_path / f"{n}.json" for n in ("index", "prior", "drafts"))
    index.write_bytes(review.encoded({"cases": [source]}))
    prior.write_bytes(review.encoded({"rows": [{"case_id": "road", "obstacles": [{"view_id": "view", "bbox_in_source": [.3, .3, .4, .4]}]}]}))
    entity = {"entity_id": "E01", "candidate_refs": ["road:view:0"], "object_envelope": geometry(.3, .4),
              "description": "QA object", "ai_notes": "QA only"}
    drafts.write_bytes(review.encoded({"author_kind": "ai_draft", "formal_release_eligible": False,
        "policy_id": "road-contact-dev-v1", "source_index_sha256": review.digest(index), "prior_summary_sha256": review.digest(prior),
        "targets": [{"case_id": "road", "image_id": "1", "road_envelope": geometry(), "road_description": "QA road", "entities": [entity]}]}))
    output = tmp_path / "package"
    receipt = review.build(drafts, index, prior, data, output)
    bundle = review.verified_bundle(output, receipt["bundle_sha256"])
    t = bundle["targets"][0]
    base = {k: t[k] for k in ("case_id", "image_sha256", "support_sha256")}
    base.update(reviewer="QA_FIXTURE_NOT_HUMAN", notes="Synthetic fixture only", origin="qa_fixture",
                started_at="2026-10-15T00:00:00Z", completed_at="2026-10-15T00:01:00Z", active_review_ms=1000,
                ai_hints_viewed=True, source_support_viewed=False)
    road = dict(base, revision="revision-0001", decision="accepted_draft", geometry=geometry())
    entity_record = dict(base, entity_id="E01", road_revision=road["revision"], entity_kind="physical", target_relation="inside",
                         group_confirmed=True, geometry_source="ai_draft", geometry=geometry(.3, .4))
    value = {"format": review.FORMAT, "bundle_id": bundle["bundle_id"], "policy_id": bundle["policy_id"],
             "formal_release_eligible": False, "reviewer_identity_authenticated": False, "roads": [road], "entities": [entity_record]}
    return output, bundle, value, receipt


def test_verified_roundtrip_preserves_source_bindings_and_qa_origin(package):
    output, bundle, value, receipt = package
    result = review.validate_reviews(value, bundle, output)
    assert result["qa_fixture_entries"] == 2 and result["operator_entity_entries"] == 0
    assert result["rows"][0]["development_disposition"] == "candidate"
    assert result["rows"][0]["candidate_refs"] == ["road:view:0"]
    assert not result["formal_release_eligible"] and not result["independent_ground_truth"]
    assert receipt["human_reviews_created"] == 0
    assert result["pending_roads"] == ["road"] and result["pending_entities"] == ["E01"]


def test_partial_export_keeps_unreviewed_work_pending(package):
    output, bundle, value, _ = package
    value["roads"], value["entities"] = [], []
    result = review.validate_reviews(value, bundle, output)
    assert result["pending_roads"] == ["road"] and result["pending_entities"] == ["E01"]
    assert result["scene_verdict"] is None


@pytest.mark.parametrize("field,new", [
    ("image_sha256", "wrong"), ("support_sha256", "wrong"), ("road_revision", "old-revision"),
    ("entity_id", "E99"), ("active_review_ms", -1), ("active_review_ms", 62000), ("active_review_ms", True),
    ("started_at", "2026-10-15T00:00:00"), ("completed_at", "2026-10-14T00:00:00Z"),
    ("ai_hints_viewed", False), ("group_confirmed", None), ("origin", "operator_entered"),
    ("reviewer", ""), ("notes", ""),
])
def test_bad_records_do_not_become_labels(package, field, new):
    output, bundle, value, _ = package
    value["entities"][0][field] = new
    with pytest.raises(ValueError):
        review.validate_reviews(value, bundle, output)


def test_invalid_containment_rejected_even_if_browser_structure_was_valid(package):
    output, bundle, value, _ = package
    r = value["entities"][0]
    r["geometry_source"] = "drawn"
    r["geometry"]["core"] = geometry(.1, .5)["core"]
    with pytest.raises(ValueError, match="inner domain"):
        review.validate_reviews(value, bundle, output)


def test_declared_outside_cannot_overrule_support_or_geometry(package):
    output, bundle, value, _ = package
    value["entities"][0]["target_relation"] = "outside"
    row = review.validate_reviews(value, bundle, output)["rows"][0]
    assert row["development_disposition"] == "unresolved"
    assert row["conflicts"] == ["declared_relation_disagrees_with_geometry"]


def test_unknown_road_or_grouping_does_not_produce_a_candidate(package):
    output, bundle, value, _ = package
    value["entities"][0]["group_confirmed"] = False
    assert review.validate_reviews(value, bundle, output)["rows"][0]["development_disposition"] == "unresolved"
    value["roads"][0].update(decision="unknown", geometry=None)
    with pytest.raises(ValueError, match="unknown road"):
        review.validate_reviews(value, bundle, output)
    value["entities"][0]["target_relation"] = "unknown"
    assert review.validate_reviews(value, bundle, output)["rows"][0]["development_disposition"] == "unresolved"


def test_nonentity_is_not_a_normal_scene_and_cannot_keep_obstruction_geometry(package):
    output, bundle, value, _ = package
    value["entities"][0]["entity_kind"] = "shadow"
    with pytest.raises(ValueError, match="nonphysical"):
        review.validate_reviews(value, bundle, output)
    value["entities"][0].update(geometry=None, geometry_source=None, target_relation="unknown")
    result = review.validate_reviews(value, bundle, output)
    assert result["rows"][0]["development_disposition"] == "non_entity" and result["scene_verdict"] is None


@pytest.mark.parametrize("field", ["formal_release_eligible", "reviewer_identity_authenticated"])
def test_authority_escalation_rejected(package, field):
    output, bundle, value, _ = package
    value[field] = True
    with pytest.raises(ValueError, match="authority"):
        review.validate_reviews(value, bundle, output)


def test_duplicates_and_foreign_fields_rejected(package):
    output, bundle, value, _ = package
    value["entities"].append(copy.deepcopy(value["entities"][0]))
    with pytest.raises(ValueError, match="duplicate entity"):
        review.validate_reviews(value, bundle, output)
    value["entities"].pop()
    value["entities"][0]["human_ground_truth"] = True
    with pytest.raises(ValueError, match="fields"):
        review.validate_reviews(value, bundle, output)


@pytest.mark.parametrize("path", ["bundle.json", "data.js", "app.js", "image"])
def test_package_or_assets_cannot_change_silently(package, path):
    output, bundle, _, receipt = package
    file = output / (bundle["targets"][0]["image_file"] if path == "image" else path)
    file.write_bytes(file.read_bytes() + b"corrupt")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        review.verified_bundle(output, receipt["bundle_sha256"])


def test_json_duplicate_keys_and_nan_rejected(tmp_path):
    path = tmp_path / "bad.json"
    for text in ['{"origin":"qa_fixture","origin":"operator_entered"}', '{"time":NaN}']:
        path.write_text(text)
        with pytest.raises(ValueError):
            review.read_json(path)


def test_operator_declarations_remain_unauthenticated(package):
    output, bundle, value, _ = package
    for r in [*value["roads"], *value["entities"]]:
        r.update(origin="operator_entered", reviewer="test_operator")
    result = review.validate_reviews(value, bundle, output)
    assert result["operator_entity_entries"] == 1 and not result["reviewer_identity_authenticated"]
    assert result["accuracy_computed"] is False and result["formal_release_eligible"] is False


def test_builder_never_overwrites_an_existing_directory(tmp_path):
    with pytest.raises(ValueError, match="new directory"):
        review.build(Path("missing"), Path("missing"), Path("missing"), tmp_path, tmp_path)
