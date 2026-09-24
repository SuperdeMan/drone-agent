"""Onboard M3 checks: registered goals only, live-mode validation, goal verification, belief facts (D039–D042).

机载 M3 检查：只接受登记目标、按实时模式校验、按目标点验证、信念事实（D039–D042）。
"""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from drone_agent.autonomy.messages import BeliefFact
from drone_agent.contracts import ControlMode, EffectVerdict, utcnow
from drone_agent.eval.m3_fixture import SHAPES, make_m3_package
from drone_agent.mission.executive import Executive
from drone_agent.mission.registry import M3_SCENE, Registry
from drone_agent.mission.verify import EffectVerifier
from drone_agent.runtime.launch import load_policy
from drone_agent.runtime.ledger import Journal
from tests.guardian.m3 import Adapter

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = Registry(ROOT, scene=ROOT / M3_SCENE)


def test_goals_must_be_registered_and_inside_the_flight_band():
    assert REGISTRY.local_goal("green_observe") == [0, 28, 4]
    with pytest.raises(ValueError):
        REGISTRY.local_goal("free_point")
    low, high = REGISTRY.task_scope()
    assert low[2] == 2 and high[2] == 10


@pytest.mark.parametrize("shape", SHAPES)
def test_every_fixture_shape_validates_with_the_optional_mode_and_not_without(shape):
    package = make_m3_package(REGISTRY, 19, "m3-" + shape, shape)
    REGISTRY.validate_package(package)
    if shape == "inspect_route":
        REGISTRY.validate_package(package, control_modes={ControlMode.MISSION_UPLOAD})
    else:
        with pytest.raises(ValueError, match="unsupported capability"):
            REGISTRY.validate_package(package, control_modes={ControlMode.MISSION_UPLOAD})


@pytest.mark.parametrize(
    ("change", "message"),
    [({"approach_goal_id": "east_hold"}, "registered goal"), ({"approach_goal_id": "nowhere"}, "registered goal"),
     ({"quality_profile_ref": "m2_campus_v2.image"}, "quality profile")],
)
def test_the_local_inspection_is_bound_to_the_assets_registered_goal(change, message):
    package = make_m3_package(REGISTRY, 7, "m3-bad", "inspect_external")
    node = package.nodes[1]
    node.params = {**node.params, **change}
    with pytest.raises(ValueError, match=message):
        REGISTRY._validate_inspect_local(node, camera_available=True)


def test_liveness_predicates_are_false_unless_the_guardian_supplies_them():
    package = make_m3_package(REGISTRY, 7, "m3-pred", "goto_external")
    node, observation = package.nodes[1], Adapter(REGISTRY).snapshot()
    context = dict(lease_valid=True, heartbeat_ok=True, camera_available=True)
    closed = REGISTRY.predicates(node, observation, package, **context)
    assert closed["local_goal_resolved_and_in_scope"] is True
    assert closed["external_mode_available"] is False and closed["local_autonomy_fresh"] is False
    opened = REGISTRY.predicates(node, observation, package, **context,
                                 overrides={"external_mode_available": True, "local_autonomy_fresh": True,
                                            "not_a_predicate": True})
    assert opened["external_mode_available"] and opened["local_autonomy_fresh"] and "not_a_predicate" not in opened


def test_goal_verification_needs_a_fresh_pose_held_inside_the_tolerance():
    package = make_m3_package(REGISTRY, 7, "m3-verify", "goto_external")
    verifier = EffectVerifier(REGISTRY, package.nodes[1])
    adapter = Adapter(REGISTRY)
    adapter.position = [0.3, 27.6, 4.0]
    assert verifier.observe(adapter.snapshot(), 0.0) is EffectVerdict.UNVERIFIED
    assert verifier.observe(adapter.snapshot(), 1.2) is EffectVerdict.VERIFIED
    far = EffectVerifier(REGISTRY, package.nodes[1])
    adapter.position = [2.0, 28.0, 4.0]
    assert far.observe(adapter.snapshot(), 5.0) is EffectVerdict.UNVERIFIED
    approach = EffectVerifier(REGISTRY, make_m3_package(REGISTRY, 7, "m3-a", "inspect_external").nodes[1], "approach")
    assert approach.action == "approach_local"


def test_policies_load_by_the_packages_reference_only():
    assert load_policy(ROOT, "multirotor_m3@v2").version == "v2"
    assert load_policy(ROOT, "multirotor_m1@v1").version == "v1"
    for bad in ("../x@v1", "multirotor_m3@", "multirotor_m1@v2"):
        with pytest.raises((ValueError, FileNotFoundError)):
            load_policy(ROOT, bad)


def executive(tmp_path):
    package = make_m3_package(REGISTRY, 7, "m3-facts", "inspect_external")
    journal = Journal(tmp_path / "executive.jsonl")
    recorder = SimpleNamespace(write=lambda topic, row: None, flush=lambda: None)
    return Executive(client=None, registry=REGISTRY, package=package, journal=journal, recorder=recorder,
                     artifacts=tmp_path, executive_id="executive:m3"), journal


def fact(**overrides):
    now = utcnow()
    values = {"fact_id": "f", "subject": "asset_green", "predicate": "detected_at", "timestamp": now.isoformat(),
              "valid_until": (now + timedelta(seconds=2)).isoformat(), "source": "sensor", "confidence": 0.95,
              "frame": {"frame_id": "map_enu", "map_version": "campus_v3"},
              "position": {"x": 0, "y": 28, "z": 0, "covariance": [0.3, 0, 0, 0, 0.3, 0, 0, 0, 0.3]}}
    values.update(overrides)
    return BeliefFact(producer_id="perception", fact=values, latency_ms=30)


def test_the_executive_journals_facts_marks_candidates_and_refuses_foreign_frames(tmp_path):
    runtime, journal = executive(tmp_path)
    runtime.accept_fact(fact())
    runtime.accept_fact(fact())  # Same key within a second is rate-limited. / 一秒内同键限流。
    runtime.accept_fact(fact(subject="event:asset_green", predicate="event_relevance", source="model",
                             source_version="clip-sha256:abc", confidence=0.7, position=None, frame=None))
    runtime.accept_fact(fact(subject="asset_red", frame={"frame_id": "map_enu", "map_version": "campus_v2"}))
    rows = [row["data"] for row in journal.rows]
    accepted = [row for row in rows if "fact" in row]
    assert [row["candidate"] for row in accepted] == [False, True]
    assert runtime.fact_counts == {"accepted": 2, "rate_limited": 1, "rejected": 1}
    assert any(row.get("reason") == "frame_or_map_mismatch" for row in rows)
    journal.close()


def test_an_expired_fact_is_refused(tmp_path):
    runtime, journal = executive(tmp_path)
    past = utcnow() - timedelta(seconds=10)
    runtime.accept_fact(fact(timestamp=past.isoformat(), valid_until=(past + timedelta(seconds=1)).isoformat()))
    assert runtime.fact_counts["rejected"] == 1 and runtime.fact_counts["accepted"] == 0
    journal.close()
