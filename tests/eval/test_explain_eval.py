"""WP-P6-05 (D083): the frozen explanation corpus and its labelled doubles.

WP-P6-05（D083）：冻结的解释语料及其带标注替身。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from drone_agent.eval import p6_adversarial
from drone_agent.eval.explain_eval import load
from drone_agent.fleet.explain import facts_of

ROOT = Path(__file__).resolve().parents[2]


def test_the_corpus_is_frozen_and_covers_twelve_mission_states():
    manifest, views = load(ROOT)
    statuses = Counter(v["mission"]["status"] for v in views.values())
    assert statuses == {"rejected": 4, "refused": 1, "awaiting_approval": 1, "declined": 1, "delivered": 1,
                        "completed": 1, "cancelled": 2, "dispatch_expired": 1}
    assert len(manifest["cases"]) == 26 and sum(c["change"] for c in manifest["cases"]) == 2
    for case in manifest["cases"]:
        ids = {f["id"] for f in facts_of(views[case["state"]])}
        assert set(case["decisive"]) <= ids, case["id"]
    for view in views.values():
        text = str(view)
        assert "tailnet:" not in text and "@" not in text, "no login is frozen into the corpus"


async def test_every_fooled_answer_is_rejected_and_every_honest_one_passes(tmp_path):
    receipt = await p6_adversarial.run(ROOT, "explain_v1", "scripted", tmp_path / "explain")
    report = receipt["report"]
    assert receipt["status"] == "passed" and report["escapes"] == 0, report["failures"]
    assert report["doubles"] == 170
