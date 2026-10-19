"""Recorded absolute evidence paths resolve inside the current data root only (D090).

记录的绝对证据路径只在当前数据根内解析（D090）。
"""
from __future__ import annotations

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
R = runpy.run_path(str(ROOT / "scripts/p6_raster_replay.py"))
C = runpy.run_path(str(ROOT / "scripts/p6_business_eval.py"))


@pytest.mark.parametrize("recorded", [
    "D:\\drone-agent-data\\p6-corpus\\annotation\\a\\x.jpg",
    "D:/drone-agent-data/p6-corpus/annotation/a/x.jpg",
    "/home/ubuntu/drone-agent-data/p6-corpus-completion-20261018-cloud/data/p6-corpus/annotation/a/x.jpg",
    "p6-corpus/annotation/a/x.jpg",
])
def test_known_recorded_roots_map_to_the_same_relative_file(recorded, tmp_path):
    assert R["portable"](recorded) == "p6-corpus/annotation/a/x.jpg"
    assert R["rooted"](tmp_path, recorded) == (tmp_path / "p6-corpus/annotation/a/x.jpg").resolve()
    assert C["within"](tmp_path, recorded) == (tmp_path / "p6-corpus/annotation/a/x.jpg").resolve()


@pytest.mark.parametrize("recorded", [
    "/home/ubuntu/other-project/data/x.json",
    "C:/drone-agent-data/x.json",
    "D:/drone-agent-data/../secrets/x.json",
    "../outside.json",
])
def test_unknown_roots_and_traversal_are_still_rejected(recorded, tmp_path):
    with pytest.raises(ValueError):
        C["within"](tmp_path, recorded)


def test_mapped_evidence_is_still_digest_checked(tmp_path):
    (tmp_path / "p6").mkdir()
    (tmp_path / "p6/evidence.json").write_text("{}", encoding="utf-8")
    digest = C["digest"](tmp_path / "p6/evidence.json")
    assert C["bound_evidence"](tmp_path, "D:\\drone-agent-data\\p6\\evidence.json", digest, {}) == {}
    with pytest.raises(ValueError, match="changed"):
        C["bound_evidence"](tmp_path, "D:/drone-agent-data/p6/evidence.json", "0" * 64, {})
