"""The X1 judge's own rules on hand-made records (D088): offer histories, acks, holds inside a silent ground robot's
envelope and holders of one resource; the matrix itself runs the judge on real cases.

X1 裁判自身规则的构造记录测试（D088）：提议历史、确认、失联地面车包络内的持有与同一资源的持有者；矩阵本身在真实用例上运行
裁判。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from drone_agent.eval.judge_x1 import check_envelopes, check_ownership, check_protocol, envelope
from drone_agent.fleet.scheduling_models import load_scheduling

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def event(handoff: str, seq: int, kind: str, actor: str, seconds: float, **body) -> dict:
    return {"handoff_id": handoff, "seq": seq, "kind": kind, "actor": actor, "at": at(seconds), "body": body}


def case(**fields) -> NS:
    values = dict(ho_events=[], handoffs={}, assignments=[], events=[], injections=[], footprints={},
                  scheduling=load_scheduling(ROOT / "configs/scheduling/x1_campus_v1.yaml"), end=T0 + timedelta(1))
    values.update(fields)
    return NS(**values)


def test_offer_histories_follow_the_state_machine_and_acks_carry_their_epoch():
    good = [event("ho-1", 1, "offered", "coordinator", 0), event("ho-1", 2, "accepted", "ugv_01", 1),
            event("ho-1", 3, "reply_ignored", "ugv_01", 2), event("ho-1", 4, "acked", "coordinator", 3,
                                                                    assignment_epoch=1),
            event("ho-1", 5, "completed", "coordinator", 30)]
    handoffs = {"ho-1": {"handoff_id": "ho-1", "task_id": "t1", "to_robot_id": "ugv_01", "state": "completed",
                         "assignment_epoch": 1}}
    assignments = [{"task_id": "t1", "epoch": 1, "decision_id": "handoff:ho-1"}]
    problems: list[str] = []
    assert check_protocol(case(ho_events=good, handoffs=handoffs, assignments=assignments), problems) == 0, problems
    skipping = [event("ho-1", 1, "offered", "coordinator", 0), event("ho-1", 2, "acked", "coordinator", 3,
                                                                     assignment_epoch=1)]
    problems = []
    assert check_protocol(case(ho_events=skipping, handoffs={"ho-1": {**handoffs["ho-1"], "state": "acked"}},
                               assignments=assignments), problems) >= 2
    assert any(p.startswith("illegal_transition") for p in problems)
    assert any(p.startswith("ack_without_acceptance") for p in problems)
    foreign = [event("ho-1", 1, "offered", "coordinator", 0), event("ho-1", 2, "accepted", "ugv_02", 1),
               event("ho-1", 3, "acked", "coordinator", 3, assignment_epoch=2)]
    problems = []
    check_protocol(case(ho_events=foreign, handoffs={"ho-1": {**handoffs["ho-1"], "state": "acked"}},
                        assignments=assignments), problems)
    assert {p.split(":")[0] for p in problems} >= {"ack_without_acceptance", "ack_epoch_mismatch"}


def reserved(subject: str, seconds: float, resources: list[str], *, ident: int) -> dict:
    return {"id": ident, "subject": subject, "kind": "reservation.reserved", "created_at": at(seconds),
            "body": {"resources": resources}}


def released(subject: str, seconds: float, *, ident: int) -> dict:
    return {"id": ident, "subject": subject, "kind": "reservation.released", "created_at": at(seconds), "body": {}}


def test_one_holder_per_resource_including_shared_areas():
    first, second = "mission:m-1:v1", "mission:m-2:v1"
    events = [reserved(first, 0, ["ugv_01.motion", "shared.pad_a"], ident=1),
              reserved(second, 5, ["uav_a.motion", "shared.pad_a"], ident=2),
              released(first, 9, ident=3), released(second, 12, ident=4)]
    problems: list[str] = []
    assert check_ownership(case(events=events), problems) == 1 and "shared.pad_a" in problems[0]
    events[1] = reserved(second, 10, ["uav_a.motion", "shared.pad_a"], ident=2)
    assert check_ownership(case(events=sorted(events, key=lambda e: e["id"])), []) == 0


def test_a_hold_inside_a_silent_ground_robots_envelope_is_counted():
    activity = "mission:m-1:v1"
    footprint = {"cells": ["ground.campus.0.0", "ugv_01.motion"], "box": [0.0, 0.0, 4.0, 4.0],
                 "volume_box": [-10.0, -10.0, 20.0, 20.0]}
    grown = envelope(case(), footprint, 6.0)
    assert "ground.campus.1.1" in grown and "air.campus.1.1" not in grown
    events = [reserved(activity, 0, ["ugv_01.motion", "ground.campus.0.0"], ident=1),
              reserved("mission:m-2:v1", 12, ["ugv_02.motion", "ground.campus.1.1"], ident=2)]
    silent = case(events=events, footprints={activity: footprint},
                  injections=[{"kind": "link_lost", "robot_id": "ugv_01", "at": at(1)},
                              {"kind": "link_restored", "robot_id": "ugv_01", "at": at(20)}])
    problems: list[str] = []
    assert check_envelopes(silent, problems) == 1 and problems[0].startswith("hold_inside_envelope")
    # Before the contact timeout the robot is not silent yet. / 联络超时之前机器人还不算失联。
    events[1] = reserved("mission:m-2:v1", 4, ["ugv_02.motion", "ground.campus.1.1"], ident=2)
    assert check_envelopes(case(events=events, footprints={activity: footprint}, injections=silent.injections),
                           []) == 0
