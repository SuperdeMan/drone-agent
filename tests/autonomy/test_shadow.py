"""Shadow-stage policies are only recorded, never executed, and their envelope violations are counted (WP-M3-18).

影子阶段策略只被记录、从不执行，其包络违反被计数（WP-M3-18）。
"""

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from drone_agent.autonomy.interfaces import PlanningInput
from drone_agent.autonomy.messages import Implementation, LocalTask, Vector3
from drone_agent.autonomy.shadow import GoalDirectBaseline, ShadowEvaluator, replay
from drone_agent.contracts import LearnedStage, utcnow
from drone_agent.guardian.constraint_filter import FilterSettings
from drone_agent.runtime.ledger import Journal
from drone_agent.runtime.recording import Recorder
from tests.guardian.m3 import segment

ROOT = Path(__file__).resolve().parents[2]
SCENE = yaml.safe_load((ROOT / "configs/scenarios/m3_campus_v3.yaml").read_text(encoding="utf-8"))
BOUNDS = {"x": (-40, 40), "y": (-40, 40), "z": (2, 10)}


def task():
    now = utcnow()
    return LocalTask(robot_id="uav_01", mission_id="m", mission_version=1, step_id="inspect_green", lease_epoch=1,
                     task_id="goto.1.1", frame_id="map_enu", map_version="campus_v3", goal=Vector3(x=0, y=28, z=4),
                     goal_tolerance_m=0.8, max_speed_mps=2.0, scope_min=Vector3(x=-40, y=-40, z=2),
                     scope_max=Vector3(x=40, y=40, z=10), issued_at=now, deadline=now + timedelta(minutes=2))


def evaluator(policy=None):
    return ShadowEvaluator(policy or GoalDirectBaseline(), BOUNDS, SCENE["obstacles"], FilterSettings(**SCENE["autonomy"]["cbf"]))


def observe(y):
    return PlanningInput(task=task(), position_enu=(0.0, y, 4.0), velocity_enu=(0, 0, 0), obstacles=None,
                         stamp_monotonic=0.0)


def test_the_baseline_is_declared_learned_and_shadow():
    proposal = GoalDirectBaseline().propose(observe(4.0))
    assert proposal.implementation is Implementation.LEARNED and proposal.learned_stage is LearnedStage.SHADOW
    assert proposal.may_execute is False


def test_executable_policies_are_refused():
    class Executable(GoalDirectBaseline):
        def propose(self, observation):
            return super().propose(observation).model_copy(update={"learned_stage": LearnedStage.FULL})

    with pytest.raises(ValueError, match="shadow"):
        evaluator(Executable()).evaluate(observe(4.0), None)


def test_blind_proposals_near_the_wall_count_as_envelope_violations():
    shadow = evaluator()
    free = shadow.evaluate(observe(4.0), segment("goto.1.1", target=(0, 5.5, 4)))
    assert free["cbf_accepted"] and not free["cbf_modified"] and free["deviation_m"] == pytest.approx(0.5, abs=0.2)
    near = shadow.evaluate(observe(13.0), segment("goto.1.1", target=(1.5, 13.0, 4)))
    assert near["cbf_modified"] or not near["cbf_accepted"]
    report = shadow.report()
    assert report["cycles"] == 2 and report["envelope_violations"] == 1 and report["executed"] == 0
    assert report["policy"]["learned_stage"] == "shadow"


def test_replay_evaluates_recorded_planner_segments(tmp_path):
    (tmp_path / "aircraft").mkdir()
    journal = Journal(tmp_path / "aircraft/guardian.jsonl")
    journal.append("external_start", {"mission_id": "m", "step_id": "inspect_green", "task_id": "goto.1.1",
                                      "nav_state": 23, "goal": [0, 28, 4], "epoch": 1})
    journal.close()
    recorder = Recorder(tmp_path / "aircraft/guardian.mcap")
    for y in (4.0, 8.0, 12.5):
        recorder.write("flight/observation", {"pose": {"position": {"x": 0.0, "y": y, "z": 4.0}},
                                              "velocity_enu_mps": [0, 1, 0]})
        recorder.write("autonomy/trajectory_segment", segment("goto.1.1", target=(1.0, y + 1, 4)).model_dump(mode="json"))
    recorder.close()
    report = replay(tmp_path, SCENE, GoalDirectBaseline())
    assert report["cycles"] == 3 and report["executed"] == 0
    assert report["envelope_violations"] >= 1
