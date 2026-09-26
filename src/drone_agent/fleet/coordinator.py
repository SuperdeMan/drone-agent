"""Coordinators: the M2 passthrough for one fixed robot, and the P3 deterministic decision over candidates (D059).

M2 flies a single robot (`uav_01`). The passthrough coordinator checks that it can perform every skill of a spec and
returns it as the assignee; otherwise the mission is not compiled. Collaboration rules are parsed and validated
against the robot catalog and recorded with the status `recorded_not_executed`; nothing here offers, accepts or
transfers a task. P1/P2 missions with a fixed binding keep this path.

P3 adds `decide`, a pure function of one snapshot: every candidate is judged with the P1 eligibility rules plus the
P3 filters (asset and volume registered at the robot's site, robots excluded for this task, airspace cells held by
another activity or inside a silent robot's envelope); eligible candidates are ranked by estimated arrival, recent use
and robot id; the task is assigned to the first, rejected when every candidate has a permanent reason, and otherwise
waits with every reason recorded. The same snapshot always gives the same decision, so a recorded decision replays.

协调器：M2 的单机直通协调器，以及 P3 对候选的确定性判定（D059）。

M2 只飞一台机器人（`uav_01`）。直通协调器检查它能否执行规格中的每个技能，能则作为执行者返回，否则任务不进入编译。协作
规则被解析、按机器人目录校验，并以 `recorded_not_executed` 状态入账；这里不提议、不接受、也不转移任何任务。固定绑定的
P1/P2 任务沿用此路径。

P3 增加 `decide`：单个快照的纯函数。每个候选按 P1 可派遣规则加 P3 过滤（资产与体积已在该机站点登记、本任务排除的机器人、
被其他活动持有或落入失联机器人包络的空域单元）判定；可派遣候选按预计到场时间、近期使用次数与 robot ID 排序；任务分配给
第一者，全部候选都有永久原因时拒绝，否则等待并记录全部原因。相同快照总得到相同判定，因此记录的判定可以重放。
"""

from __future__ import annotations

import math
from datetime import datetime

from drone_agent.contracts import CapabilityDescriptor, MissionSpec, RobotStatus
from drone_agent.fleet.catalog import Catalog
from drone_agent.fleet.provenance import digest
from drone_agent.fleet.resources import REASONS, DispatchNeeds, DockStatus, Stage, Verdict, evaluate
from drone_agent.fleet.scheduling_models import PERMANENT, TaskVerdict
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


# ── P3 deterministic decision (D059) / P3 确定性判定 ──


def candidate_reasons(catalog, task: dict, candidate: dict, now: datetime) -> tuple[Verdict, tuple[str, ...], str]:
    """(verdict, reasons, P1 snapshot digest) of one candidate; the P3 filters join the P1 rule as extra reasons.

    单个候选的（判定，原因，P1 快照摘要）；P3 过滤作为额外原因并入 P1 规则。
    """
    extra = []
    if candidate["robot_id"] in task.get("excluded", []):
        extra.append("task.robot_excluded")
    if not candidate["volume_registered"]:
        extra.append("volume.unapproved")
    elif not candidate["asset_registered"]:
        extra.append("asset.unregistered")
    if candidate["held"]:
        extra.append("airspace.cell_held")
    if candidate["envelope"]:
        extra.append("airspace.envelope")
    needs = DispatchNeeds.model_validate(candidate["needs"])
    eligibility = evaluate(
        catalog, candidate["robot_id"], stage=Stage.PREVIEW, now=now, needs=needs,
        capability=CapabilityDescriptor.model_validate(candidate["capability"]) if candidate["capability"] else None,
        robot_status=RobotStatus.model_validate(candidate["robot_status"]) if candidate["robot_status"] else None,
        dock=DockStatus.model_validate(candidate["dock"]) if candidate["dock"] else None,
        holders=dict(candidate["holders"]), activity=None, extra=tuple(extra))
    return eligibility.verdict, eligibility.reasons, eligibility.snapshot_sha256


def eta_s(ranking: dict, candidate: dict) -> float | None:
    """Estimated arrival: horizontal pad-to-asset distance at cruise speed plus the fixed set-up time.

    预计到场时间：机位到资产的水平距离按巡航速度计，加固定起降开销。
    """
    if candidate.get("asset") is None:
        return None
    distance = math.dist(candidate["pad"], candidate["asset"])
    return round(distance / ranking["cruise_mps"] + ranking["setup_s"], 1)


def decide(catalog, snapshot: dict) -> dict:
    """The decision for one task over one snapshot; pure and deterministic (D059 §3).

    `catalog` is the operations catalog the snapshot names by digest. Candidates arrive sorted by robot id.

    单个任务在单个快照上的判定；纯函数且确定（D059 §3）。`catalog` 是快照以摘要指明的运营目录。候选按 robot ID 排序传入。
    """
    if catalog.sha256 != snapshot["catalog_sha256"]:
        raise ValueError("the snapshot was taken against another operations catalog")
    now = datetime.fromisoformat(snapshot["now"])
    task, ranking = snapshot["task"], snapshot["ranking"]
    judged = []
    for candidate in sorted(snapshot["candidates"], key=lambda c: c["robot_id"]):
        verdict, reasons, p1_digest = candidate_reasons(catalog, task, candidate, now)
        unknown = [code for code in reasons if code not in REASONS]
        if unknown:
            raise ValueError(f"uncontrolled scheduling reason {unknown}")
        judged.append({"robot_id": candidate["robot_id"], "verdict": verdict.value, "reasons": list(reasons),
                       "permanent": sorted(code for code in reasons if code in PERMANENT),
                       "eta_s": eta_s(ranking, candidate), "usage": candidate["usage"],
                       "cells": list(candidate["cells"]), "held": dict(candidate["held"]),
                       "envelope": dict(candidate["envelope"]), "eligibility_sha256": p1_digest})
    eligible = sorted((c for c in judged if c["verdict"] == Verdict.ELIGIBLE.value),
                      key=lambda c: (c["eta_s"], c["usage"], c["robot_id"]))
    if eligible:
        verdict, robot = TaskVerdict.ASSIGN, eligible[0]["robot_id"]
    elif not judged or all(c["permanent"] for c in judged):
        verdict, robot = TaskVerdict.REJECT, None
    else:
        verdict, robot = TaskVerdict.WAIT, None
    return {"verdict": verdict.value, "robot_id": robot, "order": [c["robot_id"] for c in eligible],
            "candidates": judged, "reason": None if judged else "task.no_candidates",
            "snapshot_sha256": digest(snapshot), "ranking_version": ranking["version"],
            "policy_version": snapshot["policy_version"], "evaluated_at": snapshot["now"]}


def same_decision(first: dict, second: dict) -> bool:
    """Whether two decisions conclude alike: verdict, robot and every candidate's reasons. / 两个判定结论是否相同。"""
    def key(decision: dict):
        return (decision["verdict"], decision["robot_id"],
                [(c["robot_id"], c["verdict"], c["reasons"]) for c in decision["candidates"]])
    return key(first) == key(second)
