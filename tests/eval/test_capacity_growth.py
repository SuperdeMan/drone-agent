"""The capacity growth ladder's pure parts: instance ids, renaming, the mix, and the pre-registered judgement (D085).

容量规模阶梯的纯逻辑部分：实例 id、改名、构成，以及预登记判定（D085）。
"""

from __future__ import annotations

import copy
import random

from drone_agent.eval import capacity_growth as cg


def test_instance_ids_are_found_and_shared_ids_are_not():
    text = ('{"mission_id": "m-a9413a0cd3e9", "run": "wr-1de75b9097174c1fa6d7", "delivery_id": '
            '"96845ff140574cbdabe53a7a5fbfd625", "evidence_id": "vendor:a0013dc2-162e-5534-ab6c-4f0bea7c7942:3", '
            '"capture": "capture:' + "7" * 64 + '", "request": "rv-1de75b90-analyze", '
            '"worker": "wfw-e6dc2f2d92a6", "service": "service-25844e907db44be68ff51af361c752d9", '
            '"boot": "boot-dock_s1-4a372b89f2", "key": "ed25519:2aec0ecefe8cc95b", "media": "' + "a" * 64 + '"}')
    found = [m.group(0) for m in cg.TOKEN.finditer(text)]
    assert found == ["m-a9413a0cd3e9", "wr-1de75b9097174c1fa6d7", "96845ff140574cbdabe53a7a5fbfd625",
                     "a0013dc2-162e-5534-ab6c-4f0bea7c7942", "capture:" + "7" * 64, "rv-1de75b90"]


def test_renaming_keeps_each_shape_and_maps_one_to_one():
    renamer = cg.Renamer(random.Random(1), set())
    text = "m-a9413a0cd3e9 m-a9413a0cd3e9 a0013dc2-162e-5534-ab6c-4f0bea7c7942 96845ff140574cbdabe53a7a5fbfd625"
    renamed = cg.TOKEN.sub(renamer, text).split()
    assert renamed[0] == renamed[1] != "m-a9413a0cd3e9" and len(renamed[0]) == len("m-a9413a0cd3e9")
    assert renamed[2].count("-") == 4 and len(renamed[2]) == 36 and len(renamed[3]) == 32
    assert [m.group(0) for m in cg.TOKEN.finditer(" ".join(renamed))] == renamed


def test_the_mix_splits_every_clone_by_the_desk_projects():
    for total in (1282, 5155, 20647):
        found = cg._proportions(total)
        assert sum(found.values()) == total and found["fleet"] > found["vendor"] > found["campus"]


def results(changes: dict | None = None) -> list[dict]:
    base = {"bench_ms_before": 300.0, "bench_ms_after": 310.0, "steady_s": 600.0, "issues_added": [],
            "generated": {"template_sha256": "t", "code_sha256": "c"}, "code_sha256": "c",
            "reconcile": {"capped": False, "refresh_ms": {"mean": 4.0}},
            "archive": {"conserved": True, "verify": {"status": "passed"}, "due": 157_000, "archived": 156_000},
            "phases": {"archive": {"pass": {"p99": 40.0}},
                       "steady": {"pass": {"p95": 20.0}, "api.docks.report": {"p99": 8.0},
                                  **{f"api.{m}": {"p95": 5.0} for m in cg.API_METHODS}}}}
    found = []
    for scale, variant in cg.ORDER:
        record = copy.deepcopy(base)
        record.update(scale=scale, variant=variant)
        for (key_scale, key_variant), patch in (changes or {}).items():
            if (key_scale, key_variant) == (scale, variant):
                patch(record)
        found.append(record)
    return found


def test_the_judgement_passes_flat_costs_and_fails_growth_or_invalid_runs():
    prereg = {"template_sha256": "t", "code_sha256": "c"}
    assert cg.judge(results(), prereg)["status"] == "passed"

    def slower(record):
        record["phases"]["steady"]["pass"]["p95"] = 35.0  # 1.75x and 15 ms over / 1.75 倍且多 15 ms

    assert cg.judge(results({(16, "indexed"): slower}), prereg)["criteria"]["pass_p95"] is False

    def slightly(record):
        record["phases"]["steady"]["pass"]["p95"] = 29.0  # 1.45x / 1.45 倍

    assert cg.judge(results({(16, "indexed"): slightly}), prereg)["status"] == "passed"

    def throttled(record):
        record["bench_ms_after"] = 700.0

    assert cg.judge(results({(4, "plain"): throttled}), prereg)["status"] == "invalid"

    def capped(record):
        record["reconcile"]["capped"] = True

    assert cg.judge(results({(16, "indexed"): capped}), prereg)["status"] == "failed"
    assert cg.judge(results({(16, "plain"): capped}), prereg)["status"] == "passed", "the control is not judged"
    assert cg.judge(results()[:5], prereg)["status"] == "invalid"
