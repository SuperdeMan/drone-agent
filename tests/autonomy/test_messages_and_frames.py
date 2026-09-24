"""The local autonomy protocol rejects unbounded, undeclared or truth-bearing content (D039).

本地自主层协议拒收无界、未声明或携带真值的内容（D039）。
"""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from drone_agent.autonomy.frames import FrameError, pack, unpack
from drone_agent.autonomy.messages import (
    AuthorizationRevoked,
    AuthorizedSetpoint,
    BeliefFact,
    EgressStatus,
    Implementation,
    LocalTask,
    ObstacleSet,
    SegmentStatus,
    TrajectoryPoint,
    TrajectorySegment,
    Vector3,
)
from drone_agent.contracts import LearnedStage, utcnow


def segment(**overrides):
    now = utcnow()
    values = dict(
        robot_id="uav_01", task_id="goto.1.1", lease_epoch=1, segment_seq=0, source_id="planner",
        source_version="0.1.0", implementation=Implementation.DETERMINISTIC, created_at=now,
        valid_until=now + timedelta(milliseconds=400), frame_id="map_enu", map_version="campus_v3",
        points=[TrajectoryPoint(t_offset_s=0.5, position=Vector3(x=0, y=1, z=4)),
                TrajectoryPoint(t_offset_s=1.0, position=Vector3(x=0, y=2, z=4))],
        max_speed_mps=2.0, status=SegmentStatus.OK, cycle_ms=40.0,
    )
    values.update(overrides)
    return TrajectorySegment(**values)


def test_learned_segments_declare_a_stage_and_shadow_never_executes():
    with pytest.raises(ValidationError):
        segment(implementation=Implementation.LEARNED)
    shadow = segment(implementation=Implementation.LEARNED, learned_stage=LearnedStage.SHADOW)
    assert shadow.may_execute is False
    assert segment().may_execute is True
    with pytest.raises(ValidationError):
        segment(learned_stage=LearnedStage.FULL)


@pytest.mark.parametrize("lifetime_ms", [0, 2500])
def test_segment_validity_is_a_short_window(lifetime_ms):
    now = utcnow()
    with pytest.raises(ValidationError):
        segment(created_at=now, valid_until=now + timedelta(milliseconds=lifetime_ms))


def test_segment_points_must_be_time_ordered_and_finite():
    with pytest.raises(ValidationError):
        segment(points=[TrajectoryPoint(t_offset_s=1.0, position=Vector3(x=0, y=0, z=4)),
                        TrajectoryPoint(t_offset_s=0.5, position=Vector3(x=0, y=0, z=4))])
    with pytest.raises(ValidationError):
        Vector3(x=float("nan"), y=0, z=0)


def test_obstacles_need_uncertainty_and_setpoints_a_bounded_ttl():
    now = utcnow()
    with pytest.raises(ValidationError):
        ObstacleSet(robot_id="uav_01", frame_id="map_enu", map_version="campus_v3", stamp=now,
                    valid_until=now + timedelta(milliseconds=500), source_id="map", source_version="1",
                    points=[], point_radius_m=0.1, position_std_m=0, confidence=0.9, map_seq=1,
                    coverage_radius_m=10)
    with pytest.raises(ValidationError):
        AuthorizedSetpoint(robot_id="uav_01", mission_id="m", mission_version=1, step_id="s", lease_epoch=1,
                           command_seq=0, issued_at=now, ttl_ms=501, position_ned=Vector3(x=0, y=0, z=-4),
                           max_horizontal_speed_mps=1, max_vertical_speed_mps=1)


def test_local_task_goal_must_lie_in_its_scope():
    now = utcnow()
    with pytest.raises(ValidationError):
        LocalTask(robot_id="uav_01", mission_id="m", mission_version=1, step_id="s", lease_epoch=1, task_id="s.1.1",
                  frame_id="map_enu", map_version="campus_v3", goal=Vector3(x=0, y=0, z=20), goal_tolerance_m=1,
                  max_speed_mps=2, scope_min=Vector3(x=-40, y=-40, z=2), scope_max=Vector3(x=40, y=40, z=10),
                  issued_at=now, deadline=now + timedelta(seconds=60))


def fact(**overrides):
    now = utcnow()
    values = {"fact_id": "f1", "subject": "asset_green", "predicate": "detected_at", "timestamp": now.isoformat(),
              "valid_until": (now + timedelta(seconds=2)).isoformat(), "source": "sensor", "confidence": 0.9,
              "frame": {"frame_id": "map_enu", "map_version": "campus_v3"},
              "position": {"x": 0, "y": 28, "z": 0, "covariance": [0.2, 0, 0, 0, 0.2, 0, 0, 0, 0.2]}}
    values.update(overrides)
    return values


def test_belief_facts_refuse_truth_uncertainty_free_positions_and_missing_expiry():
    assert BeliefFact(producer_id="perception", fact=fact(), latency_ms=12).world_fact().subject == "asset_green"
    for bad in (fact(source="sim_truth", world_kind="truth"), fact(world_kind="predicted"),
                fact(position={"x": 0, "y": 0, "z": 0}), fact(valid_until=None),
                fact(source="model", confidence=0.5)):
        with pytest.raises(ValidationError):
            BeliefFact(producer_id="perception", fact=bad, latency_ms=1)


def test_egress_nav_state_is_only_accepted_in_the_external_range():
    status = EgressStatus(robot_id="uav_01", stamp=utcnow(), node_version="1", registered=True, mode_nav_state=23,
                          mode_active=False, fmu_link_ok=True, compatibility_ok=True, highest_epoch=-1,
                          last_forwarded_seq=-1, forwarded_setpoints=0, rejected_stale=0, rejected_epoch=0,
                          rejected_seq=0, watchdog_exits=0)
    assert status.external_nav_state == 23
    assert status.model_copy(update={"mode_nav_state": 4}).external_nav_state is None
    assert status.model_copy(update={"registered": False}).external_nav_state is None


def test_frames_round_trip_and_reject_malformed_bodies():
    original = segment()
    frame = pack(original)
    assert int.from_bytes(frame[:4], "big") == len(frame) - 4
    kind, restored = unpack(frame[4:])
    assert kind == "trajectory_segment" and restored == original
    with pytest.raises(FrameError):
        unpack(b"\xff\xff\xff")
    with pytest.raises(FrameError):
        unpack(b"")
    with pytest.raises(TypeError):
        pack(Vector3(x=0, y=0, z=0))


def test_a_revocation_round_trips_and_needs_a_reason():
    # D047: the guardian says it ends the authorization itself; the node must never have to guess why it lapsed.
    # D047：guardian 明说由它自己结束授权；节点不必猜测授权为何过期。
    revocation = AuthorizationRevoked(robot_id="uav_01", lease_epoch=1, command_seq=7, issued_at=utcnow(),
                                      reason="intervention:trajectory_stale")
    kind, restored = unpack(pack(revocation)[4:])
    assert kind == "authorization_revoked" and restored == revocation
    with pytest.raises(ValidationError):
        AuthorizationRevoked(robot_id="uav_01", lease_epoch=1, command_seq=7, issued_at=utcnow(), reason="")
