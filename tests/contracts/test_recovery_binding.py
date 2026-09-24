"""Recovery policy v2 edges are bound only to same-revision, multi-seed, non-void evidence (D042).

恢复策略 v2 的边只绑定到同一版本、多种子、非作废的证据（D042）。
"""

import runpy
from pathlib import Path

import pytest
import yaml

from drone_agent.contracts import RecoveryPolicy

ROOT = Path(__file__).resolve().parents[2]
BIND = runpy.run_path(str(ROOT / "scripts/bind_recovery_edges.py"))
V1 = ROOT / "configs/recovery_policies/multirotor_m1_v1.yaml"
V2 = ROOT / "configs/recovery_policies/multirotor_m3_v2.yaml"
SHA = "a" * 40


def receipt(edges, seeds=(7, 19, 41), **row_overrides):
    rows = [{"scenario": edge, "seed": seed, "source_sha": SHA, "passed": True, "replay_agrees": True, "problems": [],
             "false_success_reports": 0, "validated_edge": edge, **row_overrides} for edge in edges for seed in seeds]
    return {"source_sha": SHA, "deployment_id": "20260924T120000Z-00000000", "other_containers_before": {"n": 1},
            "other_containers_after": {"n": 1}, "results": rows}


def edges():
    policy, v1 = RecoveryPolicy.from_yaml(V2), RecoveryPolicy.from_yaml(V1)
    inherited = {e.fault_injection_scenario for e in v1.edges}
    return ([e.fault_injection_scenario for e in policy.edges if e.fault_injection_scenario not in inherited],
            [e.fault_injection_scenario for e in policy.edges if e.fault_injection_scenario in inherited])


def test_inherited_v2_edges_are_the_v1_edges_byte_for_byte():
    v1 = {e.fault_injection_scenario: e.validation_hash() for e in RecoveryPolicy.from_yaml(V1).edges}
    for edge in RecoveryPolicy.from_yaml(V2).edges:
        if edge.fault_injection_scenario in v1:
            assert edge.validation_hash() == v1[edge.fault_injection_scenario], edge.fault_injection_scenario


def test_full_evidence_binds_every_edge_and_the_written_policy_verifies(tmp_path):
    new, inherited = edges()
    report = BIND["bind"](V2, V1, SHA, [("m3.json", receipt(new))], [("m1.json", receipt(inherited))])
    assert report["unbound"] == {} and len(report["bound"]) == len(new) + len(inherited)
    text = BIND["apply"](V2, report["bound"])
    policy = RecoveryPolicy.model_validate(yaml.safe_load(text))
    assert policy.unverified_edges() == []
    assert text.count("# ") == V2.read_text(encoding="utf-8").count("# ")  # comments survive / 注释保留
    # Re-applying replaces the records instead of stacking them. / 重复写入替换而不是叠加记录。
    (tmp_path / "v2.yaml").write_text(text, encoding="utf-8")
    again = BIND["apply"](tmp_path / "v2.yaml", report["bound"])
    assert again == text


@pytest.mark.parametrize("override", [{"void": True}, {"replay_agrees": False}, {"problems": ["x"]},
                                      {"source_sha": "b" * 40}, {"false_success_reports": 1}])
def test_void_failing_or_foreign_rows_never_count(override):
    new, inherited = edges()
    report = BIND["bind"](V2, V1, SHA, [("m3.json", receipt(new, **override))], [("m1.json", receipt(inherited))])
    assert set(report["unbound"]) == set(new)


def test_two_seeds_or_m3_evidence_for_an_inherited_edge_are_not_enough():
    new, inherited = edges()
    report = BIND["bind"](V2, V1, SHA, [("m3.json", receipt(new + inherited, seeds=(7, 19, 41)))],
                          [("m1.json", receipt(inherited, seeds=(7, 19)))])
    # Inherited edges need the M1 regression itself; M3 rows naming them do not substitute. / 继承边必须来自 M1 回归。
    assert set(report["unbound"]) == set(inherited)
