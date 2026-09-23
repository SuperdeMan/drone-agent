"""Robot catalog: published capabilities and the latest status per robot (WP-M2-12).

The catalog answers "who can do this skill" and "is this robot on the ground" from what robots published.
A robot that never published a capability falls back to the scene's static descriptor, flagged as such;
a missing or stale status is unknown, never grounded.

机器人目录：各机器人发布的能力与最新状态（WP-M2-12）。

目录根据机器人发布的内容回答「谁能执行此技能」与「此机器人是否在地面」。从未发布能力的机器人回退到
场景的静态描述并注明来源；缺失或过时的状态为未知，绝不视为在地面。
"""

from __future__ import annotations

from datetime import datetime

from drone_agent.contracts import CapabilityDescriptor, RobotStatus
from drone_agent.fleet.ledger import BusinessLedger

GROUNDED = "grounded"


class Catalog:
    def __init__(self, ledger: BusinessLedger, static: CapabilityDescriptor | None = None):
        self.ledger, self.static = ledger, static

    def capability(self, robot_id: str) -> tuple[CapabilityDescriptor | None, str]:
        """The capability and where it came from: `published`, `static` or `missing`. / 能力及其来源。"""
        row = self.ledger.robot(robot_id)
        if row and row["capability"]:
            return CapabilityDescriptor.model_validate(row["capability"]), "published"
        if self.static is not None and self.static.robot_id == robot_id:
            return self.static, "static"
        return None, "missing"

    def status(self, robot_id: str) -> RobotStatus | None:
        row = self.ledger.robot(robot_id)
        return RobotStatus.model_validate(row["status"]) if row and row["status"] else None

    def grounded_since(self, robot_id: str, since: datetime) -> bool:
        """True only if a status observed at or after `since` says grounded. / 只有 `since` 之后观测到的状态为在地面时为真。"""
        status = self.status(robot_id)
        return status is not None and status.flight_phase == GROUNDED and status.timestamp >= since

    def capable(self, skills: list[str]) -> list[str]:
        ids = {row["robot_id"] for row in self.ledger.robots()}
        if self.static is not None:
            ids.add(self.static.robot_id)
        capable = []
        for robot_id in sorted(ids):
            capability, _ = self.capability(robot_id)
            if capability is not None and not capability.missing_for(skills=skills):
                capable.append(robot_id)
        return capable

    def view(self) -> list[dict]:
        rows = []
        ids = {row["robot_id"] for row in self.ledger.robots()} | ({self.static.robot_id} if self.static else set())
        for robot_id in sorted(ids):
            capability, source = self.capability(robot_id)
            status = self.status(robot_id)
            rows.append({"robot_id": robot_id, "capability_source": source,
                         "skills": sorted(s.skill_id for s in capability.skills) if capability else [],
                         "status": status.model_dump(mode="json") if status else None})
        return rows
