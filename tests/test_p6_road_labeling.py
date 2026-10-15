"""Keep whole-target operator evidence separate from QA, grouping and review timing.

将完整目标操作员证据与 QA、分组确认及候选复核计时分开。
"""
from __future__ import annotations

import copy
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

C = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_road_labeling.py")))
POLYGON = [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "license.txt").write_bytes(b"QA source evidence only")
    rows = []
    for i in range(3):
        file = source / f"{i}.png"
        Image.new("RGB", (400, 300), (40 + 20 * i, 80, 90)).save(file)
        rows.append({"image_id": str(i), "file": file.name, "image_sha256": C.digest(file), "width": 400, "height": 300,
                     "completeness": "complete_original" if i < 2 else "header_only", "business_label": None,
                     "capture_time_text": "2017:09:03 12:00:00", "gps": [29.5, -95.6], "exif_model": "QA"})
    C.write(source / "pool.json", {"format": "drone.floodnet-acquisition-pool/v1", "rows": rows,
                                    "gps_group_candidates": {"250": []}, "near_duplicate_candidates": []})
    C.write(source / "permission.json", {"permission_status": "documented", "license_snapshot_file": "license.txt",
                                        "license_snapshot_sha256": C.digest(source / "license.txt"), "license": "QA"})
    C.write(source / "protocol.json", {"corpus_id": "p6-road-business-v1"})
    output = tmp_path / "package"
    result = C.build(source / "pool.json", source, source / "permission.json", source / "protocol.json", output, qa=True)
    return output, result, source


def record(sample):
    return {"sample_id": sample["sample_id"], "image_sha256": sample["image_sha256"], "revision": 1,
            "reviewer_id": "QA_FIXTURE_NOT_HUMAN", "origin": "qa_fixture", "phase": "annotation",
            "started_at": "2026-10-15T00:00:00Z", "completed_at": "2026-10-15T00:01:00Z", "active_ms": 30000,
            "target_brief": "Synthetic test road only", "target_registered_at": "2026-10-15T00:00:10Z",
            "target_polygon": copy.deepcopy(POLYGON), "target_confirmed": True, "full_target_inspected": True,
            "verdict": "normal", "objects": [], "notes": "Synthetic QA annotation, not human truth", "task_reference": "",
            "model_outputs_seen": False, "source_labels_seen": False, "metadata_seen": False,
            "asset_id": "", "site_id": "", "capture_batch_id": "", "near_duplicate_group": "",
            "grouping_verified": False, "grouping_evidence": ""}


def export(package, rows):
    path, result, _ = package
    bundle = C.read(path / "bundle.json")
    file = path.parent / "operator.json"
    C.write(file, {"format": "drone.p6-road-operator-labels/v1", "bundle_id": result["bundle_id"],
                   "corpus_id": bundle["corpus_id"], "qa_fixture": True, "records": rows,
                   "formal_release_eligible": False, "reviewer_identity_authenticated": False})
    return file


def test_only_complete_originals_enter_the_blank_blind_workbench(package):
    path, result, _ = package
    bundle = C.verify_bundle(path, result["bundle_sha256"])
    assert result["samples"] == 2 and result["human_reviews_created"] == 0
    assert all(s["p6_split"] == "unassigned_quarantine" and "business_label" not in s for s in bundle["samples"])
    assert "model_answer" not in bundle and bundle["qa_fixture"]


@pytest.mark.parametrize("file", ["data.js", "app.js", "license.html"])
def test_changed_package_files_cannot_be_used_to_import_truth(package, file):
    path, result, _ = package
    (path / file).write_bytes((path / file).read_bytes() + b"changed")
    with pytest.raises(ValueError):
        C.verify_bundle(path, result["bundle_sha256"])


def test_changed_image_and_untrusted_bundle_hash_are_rejected(package):
    path, result, _ = package
    sample = C.read(path / "bundle.json")["samples"][0]
    with pytest.raises(ValueError, match="trusted bundle"):
        C.verify_bundle(path, "0" * 64)
    (path / sample["image_file"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="bound image"):
        C.verify_bundle(path, result["bundle_sha256"])


@pytest.mark.parametrize("changes", [
    {"full_target_inspected": False}, {"target_confirmed": False}, {"target_polygon": [[0, 0], [1, 1], [0, 1], [1, 0]]},
    {"phase": "candidate_review"}, {"origin": "operator_entered"}, {"active_ms": 70000},
    {"model_outputs_seen": None}, {"started_at": "2026-10-15T00:00:00"},
    {"target_registered_at": "2026-10-14T23:59:59Z"}, {"grouping_verified": True},
    {"verdict": "wrong_target", "task_reference": ""}, {"verdict": "obstacle", "objects": []},
])
def test_incomplete_or_promoted_records_are_rejected(package, changes):
    sample = C.read(package[0] / "bundle.json")["samples"][0]
    with pytest.raises(ValueError):
        C.validate({**record(sample), **changes}, sample, True)


def test_import_preserves_unknown_groups_and_annotation_phase(package, tmp_path):
    path, result, _ = package
    sample = C.read(path / "bundle.json")["samples"][0]
    source = export(package, [record(sample)])
    destination = tmp_path / "imported"
    receipt = C.import_records(path, result["bundle_sha256"], source, destination)
    draft = C.read(destination / "manifest-draft.json")
    assert receipt["business_labels"] == 1 and receipt["human_provided_labels"] == 0
    assert receipt["candidate_review_sessions"] == 0 and not receipt["formal_release_eligible"]
    assert draft["samples"][0]["asset_id"] is None and draft["samples"][0]["split"] is None
    assert draft["samples"][0]["target"]["status"] == "unconfirmed"
    assert C.read(destination / "annotation-sessions.json")["sessions"][0]["phase"] == "annotation"
    assert (destination / "operator-export.json").read_bytes() == source.read_bytes()


def test_small_object_has_no_business_size_floor(package):
    sample = C.read(package[0] / "bundle.json")["samples"][0]
    row = record(sample)
    row.update(verdict="obstacle", objects=[{"defect_type": "debris", "polygon": [[.5, .5], [.50001, .5], [.50001, .50001], [.5, .50001]]}])
    C.validate(row, sample, True)


def test_unsuitable_source_is_not_converted_to_wrong_target(package, tmp_path):
    path, result, _ = package
    sample = C.read(path / "bundle.json")["samples"][0]
    row = record(sample)
    row.update(verdict="excluded_source", target_confirmed=False, target_registered_at=None, target_polygon=None, full_target_inspected=False)
    source = export(package, [row])
    receipt = C.import_records(path, result["bundle_sha256"], source, tmp_path / "excluded")
    assert receipt["excluded_sources"] == 1 and receipt["business_labels"] == 0


def test_duplicate_records_do_not_inflate_human_labels(package, tmp_path):
    path, result, _ = package
    sample = C.read(path / "bundle.json")["samples"][0]
    source = export(package, [record(sample), record(sample)])
    with pytest.raises(ValueError, match="duplicate"):
        C.import_records(path, result["bundle_sha256"], source, tmp_path / "duplicate")


def test_builder_keeps_sources_immutable_and_refuses_output_reuse(package):
    output, _, source = package
    before = (source / "0.png").read_bytes()
    with pytest.raises(ValueError, match="new"):
        C.build(source / "pool.json", source, source / "permission.json", source / "protocol.json", output, qa=True)
    assert (source / "0.png").read_bytes() == before
