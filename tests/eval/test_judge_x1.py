"""The X1 judge's own rules on hand-made records (D088): offer histories, acks, holds inside a silent ground robot's
envelope and holders of one resource; the matrix itself runs the judge on real cases.

X1 裁判自身规则的构造记录测试（D088）：提议历史、确认、失联地面车包络内的持有与同一资源的持有者；矩阵本身在真实用例上运行
裁判。
"""

from __future__ import annotations

import json
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


def test_s1_armed_periods_come_from_the_flight_log_spans(tmp_path):
    from drone_agent.eval.judge_x1 import armed_periods

    path = tmp_path / "arming.json"
    path.write_text(json.dumps({"files": [
        # A whole-session log with both transitions. / 含两次迁移的整段日志。
        {"start_us": 9_640_000, "last_us": 36_132_000, "status": [
            {"t_us": 9_364_000, "arming_state": 1, "nav_state": 4}, {"t_us": 20_600_000, "arming_state": 2,
                                                                    "nav_state": 4},
            {"t_us": 21_592_000, "arming_state": 2, "nav_state": 3},
            {"t_us": 28_788_000, "arming_state": 1, "nav_state": 4}]},
        # PX4's default: one file per armed period, which ends with the file. / PX4 默认：每个解锁期一个文件，随文件结束。
        {"start_us": 50_010_000, "last_us": 70_000_000, "status": [{"t_us": 50_000_000, "arming_state": 2,
                                                                     "nav_state": 4}]}]}))
    assert armed_periods(path) == [(20.6, 28.788), (50.0, 70.0)]
    assert armed_periods(tmp_path / "missing.json") == []


def test_s1_truth_rows_are_local_with_differenced_speed_and_armed_flags():
    from drone_agent.eval.judge_x1 import truth_rows

    rows = []
    for i in range(80):
        t = 10.0 + i * 0.05
        x = 2.0 + min(t - 10.0, 2.0)  # 1 m/s for 2 s, then still / 1 m/s 行驶 2 s 后静止
        rows.append({"timestamp": at(t), "sim_time": t, "position": [x, -4.0, 0.0]})
    built = truth_rows(rows, "ugv_01", (2.0, -4.0), ground=True, periods=[(10.0, 12.5)])
    assert built[0]["position"][:2] == [0.0, 0.0] and built[0]["in_air"] is False
    assert abs(built[20]["speed"] - 1.0) < 0.05 and built[-1]["speed"] < 0.01
    assert built[20]["armed"] is True and built[-1]["armed"] is False
    air = truth_rows([{**rows[0], "position": [0.0, 0.0, 4.0]}], "uav_01", (0.0, 0.0), ground=False, periods=[])
    assert air[0]["in_air"] is True and air[0]["armed"] is None


def test_every_s1_case_materializes_with_an_srgb_compensated_block(tmp_path):
    from drone_agent.eval.x1_prepare import CASES, block_sdf, load_suite, materialize

    suite = load_suite(ROOT)
    assert [c["id"] for c in suite["s1"]] == list(CASES)
    for case in suite["s1"]:
        for seed in suite["seeds"]:
            metadata = materialize(ROOT, tmp_path / f"{case['id']}-{seed}", case["id"], seed, "test")
            assert metadata["inputs"]["asset"] in ("asset_mid", "asset_west")
            assert (tmp_path / f"{case['id']}-{seed}" / "planner-fixtures.json").is_file()
            # A cancelled ground mission waits for a person to carry the robot home (as in S0). / 被取消的地面任务要等
            # 人把车送回（与 S0 相同）。
            assert ("carry_home_after_stop" in metadata["actions"]) == ("cancel_while_driving" in metadata["actions"])
            assert metadata["parking_pose"].startswith('name: "rover_differential_1", position: {x: 2, y: -4')
    # Gazebo encodes emissive light as sRGB, so 140/255 green is emitted as (140/255) ** 2.2. / Gazebo 按 sRGB 编码
    # 自发光，所以 140/255 的绿色以其 2.2 次幂发出。
    assert "<emissive>1.0000 0.2674 0.0000 1</emissive>" in block_sdf(5.0, 4.0, "orange")
    assert "<diffuse>0 0 0 1</diffuse>" in block_sdf(5.0, 4.0, "orange")
