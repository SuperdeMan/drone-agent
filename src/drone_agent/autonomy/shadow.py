"""Shadow-run framework for learned local policies (WP-M3-18, D013, 08-evaluation §6).

A learned policy in the shadow stage only observes. The evaluator feeds it the same inputs the deterministic planner
saw — task, estimated pose, local obstacle set; never truth — and records, per cycle, how far its proposal deviates
from the deterministic candidate and whether the guardian's CBF filter would have refused or changed it. It refuses
any policy whose segments could execute, and it has no socket to the guardian or the egress node: its output is a
report file and nothing else. `replay` runs the evaluation over a recorded flight (the recorded planner segments,
obstacle sets and observations in the guardian's MCAP), the "replay -> simulation -> shadow" path of D013. The judge
adds the truth-based approval rate afterwards; this module never sees truth.

`GoalDirectBaseline` is a framework exercise, not a trained model: it aims straight at the goal and ignores obstacles,
so its envelope-violation rate shows what the report catches.

学习型局部策略的影子运行框架（WP-M3-18，D013，08-evaluation §6）。

影子阶段的学习型策略只观察。评估器把确定性规划器看到的相同输入——任务、估计位姿、局部障碍集合；从不含真值——交给
它，并逐周期记录其提议与确定性候选的偏差，以及 guardian 的 CBF 过滤是否会拒绝或修改它。评估器拒绝任何片段可能被
执行的策略，也没有通往 guardian 或出口节点的套接字：它的输出只有一份报告文件。`replay` 在一次已记录的飞行上运行评估
（guardian MCAP 中记录的规划片段、障碍集合与观测），即 D013 的「回放 -> 仿真 -> 影子」路径。裁判随后补上基于真值的
认可率；本模块从不接触真值。

`GoalDirectBaseline` 是框架演练而非训练模型：它直指目标、无视障碍，其包络违反率展示报告能捕获什么。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from drone_agent.autonomy.interfaces import PlanningInput
from drone_agent.autonomy.messages import (
    Implementation,
    LocalTask,
    ObstacleSet,
    SegmentStatus,
    TrajectoryPoint,
    TrajectorySegment,
    Vector3,
)
from drone_agent.contracts import LearnedStage, utcnow
from drone_agent.guardian.constraint_filter import FilterSettings, filter_target


class GoalDirectBaseline:
    """Framework exercise: straight at the goal, blind to obstacles; always learned/shadow. / 框架演练：直指目标、无视障碍。"""

    source_id = "goal_direct_baseline"
    source_version = "0.1.0"

    def propose(self, observation: PlanningInput) -> TrajectorySegment:
        task, here = observation.task, observation.position_enu
        goal = task.goal.as_list()
        dx, dy, dz = (g - h for g, h in zip(goal, here, strict=True))
        norm = max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-6)
        step = min(task.max_speed_mps, norm)
        now = utcnow()
        points = [TrajectoryPoint(t_offset_s=t, position=Vector3(
            x=here[0] + dx / norm * step * t, y=here[1] + dy / norm * step * t, z=here[2] + dz / norm * step * t))
            for t in (0.5, 1.0, 1.5)]
        return TrajectorySegment(
            robot_id=task.robot_id, task_id=task.task_id, lease_epoch=task.lease_epoch, segment_seq=0,
            source_id=self.source_id, source_version=self.source_version, implementation=Implementation.LEARNED,
            learned_stage=LearnedStage.SHADOW, created_at=now, valid_until=now + timedelta(milliseconds=400),
            frame_id=task.frame_id, map_version=task.map_version, points=points, max_speed_mps=task.max_speed_mps,
            status=SegmentStatus.OK, cycle_ms=0.0)


POLICIES = {"goal_direct_baseline": GoalDirectBaseline}


def carrot(segment: TrajectorySegment, horizon: float) -> list[float]:
    return min(segment.points, key=lambda p: abs(p.t_offset_s - horizon)).position.as_list()


@dataclass
class ShadowEvaluator:
    policy: object
    bounds: dict
    obstacles: dict
    settings: FilterSettings
    rows: list[dict] = field(default_factory=list)

    def evaluate(self, observation: PlanningInput, deterministic: TrajectorySegment | None) -> dict:
        proposal = self.policy.propose(observation)
        if proposal.may_execute:
            # Only shadow-stage policies may be evaluated here; anything executable belongs elsewhere (D013).
            # 这里只评估影子阶段策略；可执行的策略不属于这里（D013）。
            raise ValueError("shadow evaluation only accepts shadow-stage learned policies")
        target = carrot(proposal, self.settings.horizon_s)
        belief = None if observation.obstacles is None else {
            "points": [p.as_list() for p in observation.obstacles.points],
            "point_radius_m": observation.obstacles.point_radius_m,
            "position_std_m": observation.obstacles.position_std_m}
        result = filter_target(observation.position_enu, tuple(target), v_max=observation.task.max_speed_mps,
                               bounds=self.bounds, obstacles=self.obstacles, belief=belief, settings=self.settings)
        reference = carrot(deterministic, self.settings.horizon_s) if deterministic is not None else None
        row = {
            "position": list(observation.position_enu),
            "shadow_carrot": target,
            "deterministic_carrot": reference,
            "deviation_m": math.dist(target, reference) if reference is not None else None,
            "cbf_accepted": result.accepted,
            "cbf_modified": result.modified,
            "cbf_reason": result.reason,
        }
        self.rows.append(row)
        return row

    def report(self) -> dict:
        deviations = sorted(r["deviation_m"] for r in self.rows if r["deviation_m"] is not None)

        def pick(q):
            return deviations[min(int(len(deviations) * q), len(deviations) - 1)] if deviations else None

        violations = sum(1 for r in self.rows if not r["cbf_accepted"] or r["cbf_modified"])
        return {
            "schema_version": "0.1.0",
            "policy": {"source_id": self.policy.source_id, "source_version": self.policy.source_version,
                       "implementation": "learned", "learned_stage": "shadow"},
            "cycles": len(self.rows),
            "deviation_m": {"p50": pick(0.5), "p95": pick(0.95), "max": deviations[-1] if deviations else None},
            "envelope_violations": violations,
            "envelope_violation_rate": violations / len(self.rows) if self.rows else None,
            "cbf_rejections": sum(1 for r in self.rows if not r["cbf_accepted"]),
            "executed": 0,
            "rows": self.rows[:2000],
        }


def replay(run: Path, registry_data: dict, policy) -> dict:
    """Evaluate `policy` in the shadow stage over a recorded flight's autonomy inputs. / 在已记录飞行的自主层输入上以影子阶段评估策略。"""
    from drone_agent.runtime.ledger import read_log
    from drone_agent.runtime.recording import replay as recorded

    journal = read_log(run / "aircraft/guardian.jsonl")
    starts = [row["data"] for row in journal if row["kind"] == "external_start"]
    frame, autonomy = registry_data["frame"], registry_data["autonomy"]
    band = autonomy["flight_altitude_band_m"]
    bounds = {"x": tuple(registry_data["bounds"]["x"]), "y": tuple(registry_data["bounds"]["y"]),
              "z": (max(registry_data["bounds"]["z"][0], band[0]), min(registry_data["bounds"]["z"][1], band[1]))}
    evaluator = ShadowEvaluator(policy, bounds, registry_data.get("obstacles", {}), FilterSettings(**autonomy["cbf"]))
    tasks = {}
    for start in starts:
        goal = start["goal"]
        now = utcnow()
        tasks[start["task_id"]] = LocalTask(
            robot_id="uav_01", mission_id=start["mission_id"], mission_version=1, step_id=start["step_id"],
            lease_epoch=start["epoch"], task_id=start["task_id"], frame_id=frame["frame_id"],
            map_version=frame["map_version"], goal=Vector3(x=goal[0], y=goal[1], z=goal[2]), goal_tolerance_m=0.8,
            max_speed_mps=2.0, scope_min=Vector3(x=bounds["x"][0], y=bounds["y"][0], z=bounds["z"][0]),
            scope_max=Vector3(x=bounds["x"][1], y=bounds["y"][1], z=bounds["z"][1]), issued_at=now,
            deadline=now + timedelta(minutes=5))
    position, velocity, obstacles = None, (0.0, 0.0, 0.0), None
    for topic, data in recorded(run / "aircraft/guardian.mcap"):
        if topic == "flight/observation" and data.get("pose"):
            p = data["pose"]["position"]
            position = (p["x"], p["y"], p["z"])
            velocity = tuple(data.get("velocity_enu_mps") or (0.0, 0.0, 0.0))
        elif topic == "autonomy/obstacle_set":
            obstacles = ObstacleSet.model_validate(data)
        elif topic == "autonomy/trajectory_segment" and position is not None:
            segment = TrajectorySegment.model_validate(data)
            task = tasks.get(segment.task_id)
            if task is None or segment.status is SegmentStatus.REACHED:
                continue
            evaluator.evaluate(PlanningInput(task=task, position_enu=position, velocity_enu=velocity,
                                             obstacles=obstacles, stamp_monotonic=0.0), segment)
    return evaluator.report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--policy", choices=sorted(POLICIES), default="goal_direct_baseline")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import yaml

    registry = yaml.safe_load((args.root / "configs/scenarios/m3_campus_v3.yaml").read_text(encoding="utf-8"))
    report = replay(args.run, registry, POLICIES[args.policy]())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("cycles", "envelope_violation_rate", "deviation_m", "executed")}))


if __name__ == "__main__":
    main()
