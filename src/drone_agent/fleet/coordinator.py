"""Passthrough coordinator for M2: one robot, collaboration rules recorded but never executed (WP-M2-12).

M2 flies a single robot (`uav_01`). The coordinator checks that it can perform every skill of a spec and
returns it as the assignee; otherwise the mission is not compiled. Collaboration rules are parsed and
validated against the robot catalog and recorded with the status `recorded_not_executed`: handoff and
task allocation across robots belong to M4, so nothing here offers, accepts or transfers a task.

M2 的直通协调器：单机器人，协作规则只记录不执行（WP-M2-12）。

M2 只飞一台机器人（`uav_01`）。协调器检查它能否执行规格中的每个技能，能则作为执行者返回，否则任务
不进入编译。协作规则被解析、按机器人目录校验，并以 `recorded_not_executed` 状态入账：跨机器人的交接
与任务分配属于 M4，这里不提议、不接受、也不转移任何任务。
"""

from __future__ import annotations

from drone_agent.contracts import MissionSpec
from drone_agent.fleet.catalog import Catalog
from drone_agent.runtime.issues import Issue, issue


class PassthroughCoordinator:
    def __init__(self, robot_id: str, catalog: Catalog):
        self.robot_id, self.catalog = robot_id, catalog

    def assign(self, spec: MissionSpec) -> tuple[str | None, list[Issue]]:
        """The robot that takes the whole mission, or the reasons none can. / 承担整个任务的机器人，或无法分配的原因。"""
        capability, _ = self.catalog.capability(self.robot_id)
        if capability is None:
            return None, [issue("compile.no_capable_robot", f"{self.robot_id} has no known capability")]
        gaps = capability.missing_for(skills=[task.skill_id for task in spec.tasks])
        if gaps:
            return None, [issue("compile.no_capable_robot", f"{self.robot_id} lacks {', '.join(gaps)}",
                                affected=gaps)]
        return self.robot_id, []

    def collaboration(self, spec: MissionSpec) -> list[dict]:
        """Validated collaboration rules as ledger records; none is executed in M2. / 校验后的协作规则记录；M2 不执行。"""
        records = []
        for rule in spec.collaboration_rules:
            capable = [r for r in self.catalog.capable([rule.required_skill]) if r != self.robot_id]
            records.append({**rule.model_dump(mode="json"), "status": "recorded_not_executed",
                            "capable_robots": capable})
        return records
