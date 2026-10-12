"""The D087 gate's desk criteria on hand-made receipts. / D087 门禁的任务台判据，用手工回执检验。"""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / "scripts/verify_asset_archive.py"))
SHA = "a" * 40


def activation(**assets) -> dict:
    body = {"migration": {"status": "migrated", "backfill": {"observations": 40}}, "maps": {"revisions_added": 0},
            "compare": {"version": "compare-v1", "sha256": "c" * 64},
            "drill": {"status": "passed", "migration": "migrated", "again": "current", "other_sha256_before": "d",
                      "other_sha256_after": "d", "rows": {"ar_observations": 40}, "eligible_captures": 40}}
    body.update(assets)
    return {"capacity": {"migration": {"status": "current"}, "drill": {"status": "passed"}}, "assets": body}


def probe(**changes) -> dict:
    body = {"hello": {"identity_scheme": "tailnet"}, "errors": [],
            "assets": [{"asset_key": "campus_s1/*/asset_red", "revisions": 1}],
            "timeline": {"items": 50, "observations": 12, "measured": 12, "model_fields_in_fact_column": []},
            "compare": {"definition": "same", "viewpoint": "consistent", "model_fields_in_factors": [],
                        "summary": {"zh": "两次采集之间没有维修或维护登记。", "en": "no repair."},
                        "compare": {"version": "compare-v1"}}}
    body.update(changes)
    return body


def test_the_desk_criterion_adds_the_archive_and_its_drill(monkeypatch):
    base = {"status": "passed", "problems": [], "catalogs": {}, "switch_drill": "passed"}
    monkeypatch.setitem(GATE["desk"].__globals__, "P6R", {"desk": lambda *_: copy.deepcopy(base)})
    assert GATE["desk"](activation(), {}, {}, SHA)["status"] == "passed"
    drill = activation()["assets"]["drill"]
    for bad in (activation(migration={"status": "skipped"}), activation(drill={**drill, "again": "migrated"}),
                activation(drill={**drill, "other_sha256_after": "e"}),
                activation(drill={**drill, "rows": {"ar_observations": 39}}),
                activation(drill={**drill, "status": "failed"}), activation(compare={"version": "compare-v0"})):
        assert GATE["desk"](bad, {}, {}, SHA)["status"] == "failed"


def test_the_entry_must_show_measured_real_captures_and_no_model_field():
    assert GATE["desk_assets"](probe())["status"] == "passed"
    for bad in (probe(hello={"identity_scheme": "harness"}), probe(errors=[{"type": "error"}]),
                probe(assets=[{"asset_key": "x", "revisions": 0}]),
                probe(timeline={"observations": 3, "measured": 0, "model_fields_in_fact_column": []}),
                probe(timeline={"observations": 3, "measured": 3, "model_fields_in_fact_column": ["score"]}),
                probe(compare={**probe()["compare"], "viewpoint": "unknown"}),
                probe(compare={**probe()["compare"], "model_fields_in_factors": ["verdict"]}),
                probe(compare={**probe()["compare"], "summary": {"zh": "", "en": "x"}}), probe(compare={})):
        assert GATE["desk_assets"](bad)["status"] == "failed"
    assert GATE["desk_assets"](None)["status"] == "missing"

