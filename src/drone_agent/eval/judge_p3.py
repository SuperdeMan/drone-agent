"""Independent judge for one P3 case (WP-P3-05): task ownership, execution, airspace and epochs against the world.

The judge never reuses the scheduler, the reservation code or the service. From the exported tables, the audit events,
the API transcript, the injections and every aircraft's own records it re-derives: that a task never had two active
assignments at once and a robot resource never had two holders at once; that a task was completed at most once, no
(task, epoch) produced two missions and no withdrawn or superseded assignment was ever claimed, approved or flown; that
every accepted task ended or waits with a recorded reason; that no two activities held one airspace cell or dock
resource at the same time, every claim held its whole footprint, no hold was granted into a silent robot's envelope
(recomputed here from the recorded link loss and the footprint geometry), and airborne aircraft always kept their
separation in the shared frame; that a completed task has a completed, service-verified inspection that the logical
truth confirms; and that every recorded decision replays to the same conclusion from its recorded snapshot. The P1
claim, release, flight and probe checks run on the same case, the scenario's expectations come on top, and ladder
rungs also get their latency, wait and rejection distributions.

单个 P3 用例的独立裁判（WP-P3-05）：以世界核对任务所有权、执行、空域与代次。

裁判从不复用调度器、预约代码或服务。它从导出表、审计事件、API 记录、注入与每架飞行器自身的记录重新推导：一个任务从未
同时有两个有效分配，一项机器人资源从未同时有两个持有者；一个任务至多完成一次，没有（任务，代次）产生两个任务，被撤回或
被替代的分配从未被领取、审批或飞行；每个受理的任务都已结束或带记录原因地等待；没有两个活动同时持有同一空域单元或机场资源，
每次领取都持有其全部航迹单元，没有持有落入失联机器人的包络（此处按记录的链路中断与航迹几何重算），空中的飞行器在共享坐标中
始终保持间隔；已完成的任务有经服务复核并被逻辑真值证实的巡检；每个记录的判定从其记录的快照重放得到相同结论。P1 的领取、
释放、飞行与探测检查在同一用例上运行，场景期望另外核对，阶梯档另给出延迟、等待与拒绝分布。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.eval.judge_p1 import Case as P1Case
from drone_agent.eval.judge_p1 import check_claims, check_dispatch_counts, check_flights, check_releases
from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.scheduling_models import load_scheduling
from drone_agent.runtime.ledger import read_log

COUNTS = ("double_ownership", "duplicate_execution", "lost_task", "conflicting_reservation", "false_success",
          "stale_epoch_effect", "decision_mismatch", "project_escape", "wrong_dispatch", "duplicate_dispatch",
          "wrong_release")
REFUSED = ("service.not_found", "auth.project_denied", "auth.method_not_allowed", "auth.scope_missing",
           "auth.identity_missing", "service.invalid_request", "task.candidate_outside_project",
           "task.invalid_request", "dispatch.cancelled", "approval.stale_version", "task.not_cancellable")
TASK_FINAL = ("completed", "failed", "outcome_unknown", "rejected", "cancelled")
MIN_SEPARATION_M = 3.0
MAX_SPEED_MPS = 8.0


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _body(row: dict) -> dict:
    return json.loads(row["body"]) if isinstance(row["body"], str) else row["body"]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if path.is_file() else []


def _percentiles(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)

    def pick(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)], 3)

    return {"n": len(ordered), "p50": pick(0.5), "p95": pick(0.95), "p99": pick(0.99), "max": round(ordered[-1], 3)}


class Case(P1Case):
    """The P1 records plus the scheduling tables and the layout the case ran with. / P1 记录加上调度表与用例布局。"""

    def __init__(self, case: Path, root: Path):
        super().__init__(case, root)
        self._load_scheduling(case, root)
        self.end = max([_at(e["created_at"]) for e in self.events] or [datetime.now().astimezone()])

    def _load_scheduling(self, case: Path, root: Path) -> None:
        layout = json.loads((case / "input/layout.json").read_text(encoding="utf-8"))
        self.layout = layout
        self.catalog = load_catalog(root / layout["catalog"])
        self.scheduling = load_scheduling(root / layout["scheduling"])
        self.sc = json.loads((case / "service-export/scheduling.json").read_text(encoding="utf-8"))
        self.tasks = {row["task_id"]: {**row, "outcome": _json(row["outcome"]), "excluded": _json(row["excluded"]),
                                       "cancel": _json(row["cancel"])} for row in self.sc["sc_tasks"]}
        self.assignments = self.sc["sc_assignments"]
        self.decisions = [{**row, "body": _body(row)} for row in self.sc["sc_decisions"]]
        self.footprints = {row["activity_key"]: {**row, "cells": _json(row["cells"]), "box": _json(row["box"]),
                                                 "volume_box": _json(row["volume_box"])}
                           for row in self.sc["sc_footprints"]}
        self.missions = {row["mission_id"]: row for row in self.sc["missions"]}
        self.requests = {row["mission_id"]: row for row in self.sc["requests"]}

    def origin(self, robot: str) -> tuple[float, float]:
        return tuple(self.scheduling.airspace.sites[self.catalog.robots[robot].site_id].origin)

    def task_missions(self, task_id: str) -> list[str]:
        return [a["mission_id"] for a in self.assignments if a["task_id"] == task_id and a["mission_id"]]


# ── intervals from the audit stream / 由审计流得到的区间 ──


def assignment_intervals(c: Case) -> dict[str, list[tuple[int, datetime, datetime]]]:
    """task -> [(epoch, became active, stopped being active)]. / 任务 -> [（代次，生效时刻，失效时刻）]。"""
    found: dict[str, list[tuple[int, datetime, datetime]]] = {}
    for row in c.assignments:
        task = row["task_id"]
        created = [e for e in c.events if e["subject"] == f"task:{task}" and e["kind"] == "assignment.created"
                   and _body(e)["epoch"] == row["epoch"]]
        if not created or _body(created[0])["state"] != "active":
            continue
        start = _at(created[0]["created_at"])
        ended = [e for e in c.events if e["subject"] == f"task:{task}" and e["kind"] in
                 ("assignment.withdrawn", "assignment.ended") and _body(e)["epoch"] == row["epoch"]]
        stop = _at(ended[0]["created_at"]) if ended else c.end + timedelta(days=1)
        found.setdefault(task, []).append((row["epoch"], start, stop))
    return found


def hold_intervals(c: Case) -> dict[str, list[tuple[str, datetime, datetime]]]:
    """resource -> [(activity, held from, held until)] from the reservation events. / 资源 -> [（活动，起，止）]。"""
    found: dict[str, list[tuple[str, datetime, datetime]]] = {}
    open_holds: dict[str, tuple[list[str], datetime]] = {}
    for event in sorted(c.events, key=lambda e: e["id"]):
        subject, kind = event["subject"], event["kind"]
        if not subject.startswith("mission:"):
            continue
        body = _body(event)
        if kind == "reservation.reserved":
            if subject in open_holds:
                continue  # a refresh of the same hold / 同一持有的刷新
            open_holds[subject] = (list(body.get("resources") or []), _at(event["created_at"]))
        elif kind == "reservation.released" and subject in open_holds:
            resources, start = open_holds.pop(subject)
            for resource in resources:
                found.setdefault(resource, []).append((subject, start, _at(event["created_at"])))
    for subject, (resources, start) in open_holds.items():
        for resource in resources:
            found.setdefault(resource, []).append((subject, start, c.end + timedelta(days=1)))
    return found


def overlaps(intervals: list[tuple]) -> list[tuple]:
    ordered = sorted(intervals, key=lambda item: item[1])
    return [(a, b) for index, a in enumerate(ordered) for b in ordered[index + 1:]
            if a[0] != b[0] and b[1] < a[2]]


# ── checks / 检查 ──


def check_ownership(c: Case, problems: list[str]) -> int:
    """One active assignment per task and one holder per robot motion at any moment. / 任一时刻每任务一个有效分配、每机一个持有者。"""
    count = 0
    for task, intervals in assignment_intervals(c).items():
        for a, b in overlaps(intervals):
            count += 1
            problems.append(f"two_active_assignments:{task}:e{a[0]}:e{b[0]}")
    for resource, intervals in hold_intervals(c).items():
        if resource.endswith(".motion"):
            for a, b in overlaps(intervals):
                count += 1
                problems.append(f"two_motion_holders:{resource}:{a[0]}:{b[0]}")
    active = Counter(a["task_id"] for a in c.assignments if a["state"] == "active")
    for task, number in active.items():
        if number > 1:
            count += 1
            problems.append(f"active_assignments_at_end:{task}:{number}")
    return count


def check_execution(c: Case, problems: list[str]) -> int:
    """At most one completion per task, one mission per (task, epoch), no flight or claim of a withdrawn assignment.

    每任务至多完成一次，每（任务，代次）一个任务，被撤回的分配没有飞行或领取。
    """
    count = 0
    flights = Counter(f["mission_id"] for f in c.flights)
    claims = {row["mission_id"] for row in c.ops["op_claims"] if row["state"] == "claimed"}
    for task_id in c.tasks:
        epochs = Counter(a["epoch"] for a in c.assignments if a["task_id"] == task_id)
        for epoch, number in epochs.items():
            if number > 1:
                count += 1
                problems.append(f"two_missions_one_epoch:{task_id}:e{epoch}")
        missions = c.task_missions(task_id)
        completed = [m for m in missions if (c.missions.get(m) or {}).get("status") == "completed"]
        if len(completed) > 1:
            count += len(completed) - 1
            problems.append(f"task_completed_twice:{task_id}")
    for row in c.assignments:
        if row["state"] != "withdrawn" or not row["mission_id"]:
            continue
        if flights.get(row["mission_id"]):
            count += 1
            problems.append(f"withdrawn_assignment_flew:{row['task_id']}:e{row['epoch']}")
        if row["mission_id"] in claims:
            count += 1
            problems.append(f"withdrawn_assignment_claimed:{row['task_id']}:e{row['epoch']}")
    return count


def check_lost(c: Case, problems: list[str]) -> int:
    """Every accepted task ended, or waits with a recorded reason; every created task is in the table.

    每个受理的任务都已结束或带记录原因地等待；每个创建事件都有任务行。
    """
    count = 0
    for event in c.events:
        if event["kind"] == "task.created":
            task = event["subject"].split(":", 1)[1]
            if task not in c.tasks:
                count += 1
                problems.append(f"created_task_missing:{task}")
    decided = {d["task_id"] for d in c.decisions}
    allowed = set(c.scenario.get("expected", {}).get("open_tasks", []))
    for task_id, task in c.tasks.items():
        if task["state"] in TASK_FINAL or task["asset_id"] in allowed:
            continue
        if task_id not in decided:
            count += 1
            problems.append(f"task_never_decided:{task_id}")
            continue
        if task["state"] == "assigned":
            mission = c.missions.get(task["mission_id"]) or {}
            holds = [r for r in c.ops["op_reservations"] if r["mission_id"] == task["mission_id"]
                     and r["state"] in ("reserved", "occupied", "uncertain")]
            if mission.get("status") in ("completed", "incomplete", "declined", "rejected", "cancelled",
                                         "dispatch_expired", "delivery_rejected") and not holds:
                count += 1
                problems.append(f"settled_task_not_settled:{task_id}")
        problems.append(f"task_open_at_end:{task_id}:{task['state']}")
    return count


def envelope_cells(c: Case, footprint: dict, radius: float) -> set[str]:
    """The judge's own envelope: the footprint box grown by `radius`, bounded by the volume box. / 裁判自己的包络。"""
    grid = c.scheduling.airspace
    box = footprint["box"]
    grown = [box[0] - radius, box[1] - radius, box[2] + radius, box[3] + radius]
    volume = footprint["volume_box"]
    if volume:
        clipped = [max(grown[0], volume[0]), max(grown[1], volume[1]), min(grown[2], volume[2]),
                   min(grown[3], volume[3])]
        grown = clipped if clipped[0] <= clipped[2] and clipped[1] <= clipped[3] else list(box)
    size = grid.cell_m
    cells = set(footprint["cells"])
    for i in range(math.floor(grown[0] / size), max(math.floor(grown[0] / size), math.ceil(grown[2] / size) - 1) + 1):
        for j in range(math.floor(grown[1] / size),
                       max(math.floor(grown[1] / size), math.ceil(grown[3] / size) - 1) + 1):
            cells.add(f"air.{grid.frame}.{i}.{j}")
    return cells


def link_losses(c: Case) -> dict[str, list[tuple[datetime, datetime]]]:
    """robot -> [(link lost, link restored)] from the harness injections. / 机器人 -> [（链路中断，恢复）]。"""
    found: dict[str, list[tuple[datetime, datetime]]] = {}
    lost: dict[str, datetime] = {}
    for item in c.injections:
        if item["kind"] == "link_lost":
            lost[item["robot_id"]] = _at(item["at"])
        elif item["kind"] == "link_restored" and item["robot_id"] in lost:
            found.setdefault(item["robot_id"], []).append((lost.pop(item["robot_id"]), _at(item["at"])))
    for robot, start in lost.items():
        found.setdefault(robot, []).append((start, c.end + timedelta(days=1)))
    return found


def shared_truth(c: Case) -> dict[str, list[tuple[datetime, float, float, bool]]]:
    found = {}
    for robot, rows in c.truth.items():
        ox, oy = c.origin(robot)
        found[robot] = [(_at(r["timestamp"]), ox + r["position"][0], oy + r["position"][1], bool(r["in_air"]))
                        for r in rows]
    return found


def check_airspace(c: Case, problems: list[str]) -> int:
    """No shared cell or dock resource, whole footprints at the claim, nothing inside an envelope, true separation.

    无共用单元或机场资源、领取时持有完整航迹、不落入包络、真实间隔。
    """
    count = 0
    holds = hold_intervals(c)
    for resource, intervals in holds.items():
        if resource.endswith(".motion"):
            continue
        for a, b in overlaps(intervals):
            count += 1
            problems.append(f"shared_hold:{resource}:{a[0]}:{b[0]}")
    for claim in c.ops["op_claims"]:
        if claim["state"] != "claimed":
            continue
        activity = f"mission:{claim['mission_id']}:v{claim['mission_version']}"
        footprint = c.footprints.get(activity)
        at = _at(claim["decided_at"])
        if footprint is None:
            count += 1
            problems.append(f"claim_without_footprint:{activity}")
            continue
        for cell in footprint["cells"]:
            holders = [a for a, start, stop in holds.get(cell, []) if start <= at < stop]
            if holders != [activity]:
                count += 1
                problems.append(f"claim_without_whole_footprint:{activity}:{cell}:{holders}")
                break
    # Envelopes of silent robots, recomputed from the link losses the harness caused. / 由编排造成的链路中断重算包络。
    grid = c.scheduling.airspace
    losses = link_losses(c)
    for event in c.events:
        if event["kind"] != "reservation.reserved":
            continue
        at, cells = _at(event["created_at"]), [r for r in _body(event).get("resources", []) if r.startswith("air.")]
        for other, footprint in c.footprints.items():
            if other == event["subject"]:
                continue
            robot = footprint["robot_id"]
            for lost, restored in losses.get(robot, []):
                claimed = [_at(row["decided_at"]) for row in c.ops["op_claims"] if row["state"] == "claimed"
                           and f"mission:{row['mission_id']}:v{row['mission_version']}" == other]
                released = [start for a, start, stop in holds.get(footprint["cells"][0], []) if a == other]
                if not claimed or not (lost <= at < restored) or claimed[0] > at:
                    continue
                if released and [stop for a, start, stop in holds.get(footprint["cells"][0], []) if a == other][0] <= at:
                    continue
                silence = (at - max(lost, claimed[0])).total_seconds()
                if silence <= grid.contact_timeout_s + 1.0:
                    continue
                radius = MAX_SPEED_MPS * (silence - 1.0) + grid.envelope_margin_m
                inside = sorted(set(cells) & envelope_cells(c, footprint, radius))
                if inside:
                    count += 1
                    problems.append(f"hold_inside_envelope:{event['subject']}:{other}:{inside[0]}")
    # True separation of airborne aircraft in the shared frame. / 空中飞行器在共享坐标中的真实间隔。
    truth = shared_truth(c)
    robots = sorted(truth)
    for index, first in enumerate(robots):
        for second in robots[index + 1:]:
            closest = None
            rows = truth[second]
            cursor = 0
            for at, x, y, air in truth[first]:
                while cursor + 1 < len(rows) and rows[cursor + 1][0] <= at:
                    cursor += 1
                if not rows:
                    break
                other = rows[cursor]
                if not (air and other[3]) or abs((other[0] - at).total_seconds()) > 0.5:
                    continue
                distance = math.hypot(x - other[1], y - other[2])
                closest = distance if closest is None else min(closest, distance)
            if closest is not None and closest < MIN_SEPARATION_M:
                count += 1
                problems.append(f"separation_lost:{first}:{second}:{closest:.2f}m")
    return count


def check_success(c: Case, problems: list[str]) -> int:
    """A completed task has a completed mission whose report target and verification say verified.

    已完成的任务有已完成的任务，其报告目标与复核均为已证实。
    """
    count = 0
    verdicts = {row["evidence_id"]: _json(row["body"])["final_verdict"] for row in c.sc["verifications"]}
    reports = {row["mission_id"]: _json(row["body"]) for row in c.sc["reports"]}
    for task_id, task in c.tasks.items():
        if task["state"] != "completed":
            continue
        outcome = task["outcome"] or {}
        mission = outcome.get("mission_id")
        report = reports.get(mission) or {}
        if (c.missions.get(mission) or {}).get("status") != "completed" or \
                (report.get("targets") or {}).get(task["asset_id"]) != "completed" or \
                verdicts.get(outcome.get("evidence_id")) != "verified" or mission not in c.task_missions(task_id):
            count += 1
            problems.append(f"task_completed_without_verified_inspection:{task_id}")
    for run in c.sc.get("wf_runs", []):
        if run["state"] != "completed":
            continue
        for node in c.sc.get("wf_nodes", []):
            if node["run_id"] == run["run_id"] and node["activity"] == "await_mission" and node["state"] != "completed":
                count += 1
                problems.append(f"run_completed_with_unverified_inspection:{run['run_id']}:{node['node_id']}")
    return count


def check_epochs(c: Case, problems: list[str]) -> int:
    """Nothing effective happens for a superseded assignment after it was superseded. / 被替代的分配此后不产生任何效果。"""
    count = 0
    claims = {row["mission_id"]: _at(row["decided_at"]) for row in c.ops["op_claims"] if row["state"] == "claimed"}
    for row in c.assignments:
        if row["state"] == "active" or not row["mission_id"]:
            continue
        ended = [e for e in c.events if e["subject"] == f"task:{row['task_id']}" and e["kind"] in
                 ("assignment.withdrawn", "assignment.ended") and _body(e)["epoch"] == row["epoch"]]
        if not ended:
            continue
        at = _at(ended[0]["created_at"])
        if row["state"] == "withdrawn":
            if row["mission_id"] in claims and claims[row["mission_id"]] >= at:
                count += 1
                problems.append(f"claim_after_withdrawal:{row['mission_id']}")
            for call in c.transcript:
                if call["method"] == "approve" and call["ok"] and call["params"].get("mission_id") == row["mission_id"] \
                        and _at(call["at"]) >= at:
                    count += 1
                    problems.append(f"approval_after_withdrawal:{row['mission_id']}")
    for item in c.injections:
        if item["kind"] == "stale_probes" and item.get("superseded") != "assignment_superseded":
            count += 1
            problems.append("superseded_assignment_not_refused_at_claim")
    return count


def check_decisions(c: Case, problems: list[str]) -> int:
    """Each decision replays from its snapshot, picks the first eligible by its keys and explains every exclusion.

    每个判定都能从其快照重放、按排序键选第一个可派遣候选，并解释每个排除。
    """
    from drone_agent.fleet.coordinator import decide, same_decision

    count = 0
    for row in c.decisions:
        body = row["body"]
        snapshot = body.get("snapshot")
        if snapshot is None:
            count += 1
            problems.append(f"decision_without_snapshot:{row['decision_id']}")
            continue
        try:
            replay = decide(c.catalog, snapshot)
        except Exception as error:  # a snapshot that cannot replay is a finding / 无法重放的快照本身即发现
            count += 1
            problems.append(f"decision_replay_failed:{row['decision_id']}:{type(error).__name__}")
            continue
        if not same_decision(body, replay) or replay["snapshot_sha256"] != body["snapshot_sha256"]:
            count += 1
            problems.append(f"decision_not_replayable:{row['decision_id']}")
        eligible = [cand for cand in body["candidates"] if cand["verdict"] == "eligible"]
        for cand in body["candidates"]:
            if cand["verdict"] != "eligible" and not cand["reasons"]:
                count += 1
                problems.append(f"exclusion_without_reason:{row['decision_id']}:{cand['robot_id']}")
            # Independent ETA: pad-to-asset distance at cruise speed plus set-up. / 独立的预计到场。
            snap = next(s for s in snapshot["candidates"] if s["robot_id"] == cand["robot_id"])
            if snap.get("asset"):
                eta = round(math.dist(snap["pad"], snap["asset"]) / snapshot["ranking"]["cruise_mps"]
                            + snapshot["ranking"]["setup_s"], 1)
                if eta != cand["eta_s"]:
                    count += 1
                    problems.append(f"eta_mismatch:{row['decision_id']}:{cand['robot_id']}")
        if body["verdict"] == "assign":
            best = min(eligible, key=lambda cand: (cand["eta_s"], cand["usage"], cand["robot_id"])) if eligible else None
            if best is None or best["robot_id"] != body["robot_id"]:
                count += 1
                problems.append(f"not_the_first_eligible:{row['decision_id']}")
        elif eligible:
            count += 1
            problems.append(f"eligible_candidate_not_assigned:{row['decision_id']}")
    return count


def check_probes(c: Case, problems: list[str]) -> int:
    escapes = 0
    for row in c.transcript:
        if not row["probe"]:
            continue
        refused = not row["ok"] and row["code"] in REFUSED and row["result_sha256"] is None
        if not refused:
            escapes += 1
            problems.append(f"probe_not_refused:{row['actor']}:{row['method']}:{row['code']}")
    return escapes


def metrics(c: Case) -> dict:
    """Latency, wait and rejection distributions of the case (reported, never gated here). / 用例的延迟、等待与拒绝分布。"""
    decision_ms = [row["body"].get("compute_ms") for row in c.decisions if row["body"].get("compute_ms") is not None]
    assign_s, complete_s = [], []
    for task_id, task in c.tasks.items():
        created = _at(task["created_at"])
        first = sorted(_at(e["created_at"]) for e in c.events if e["subject"] == f"task:{task_id}"
                       and e["kind"] == "assignment.created" and _body(e)["state"] == "active")
        if first:
            assign_s.append((first[0] - created).total_seconds())
        if task["state"] == "completed":
            complete_s.append((_at(task["updated_at"]) - created).total_seconds())
    waits: Counter = Counter()
    for row in c.decisions:
        if row["body"]["verdict"] == "wait":
            for cand in row["body"]["candidates"]:
                waits.update(cand["reasons"])
    rejects: Counter = Counter()
    for task in c.tasks.values():
        if task["state"] == "rejected":
            rejects.update((task["reason"] or "").split(","))
    ladder = c.scenario.get("ladder") or {}
    samples = ladder.get("samples") or []
    return {"tasks": dict(Counter(t["state"] for t in c.tasks.values())), "decisions": len(c.decisions),
            "decision_ms": _percentiles(decision_ms), "assignment_latency_s": _percentiles(assign_s),
            "completion_s": _percentiles(complete_s), "wait_reasons": dict(sorted(waits.items())),
            "reject_reasons": dict(sorted(rejects.items())),
            "live_tasks_max": max((s["live"] for s in samples), default=None),
            "airborne_max": max((s["airborne"] for s in samples), default=None),
            "flights": len(c.flights), "ladder": {k: ladder.get(k) for k in ("nodes", "budget", "total", "duration_s",
                                                                             "layout_sha256", "host", "tick_ms")}
            if ladder else None}


def overlapping(c: Case) -> bool:
    flights = [f for f in c.flights if f.get("ended_at")]
    return any(a["robot_id"] != b["robot_id"] and _at(a["started_at"]) < _at(b["ended_at"]) and
               _at(b["started_at"]) < _at(a["ended_at"]) for i, a in enumerate(flights) for b in flights[i + 1:])


def check_expected(c: Case, problems: list[str]) -> None:
    expected = c.scenario.get("expected", {})
    if c.scenario.get("harness_error"):
        problems.append(f"harness_error:{c.scenario['harness_error']}")
    if "flights" in expected and len(c.flights) != expected["flights"]:
        problems.append(f"flights {len(c.flights)} != {expected['flights']}")
    if "tasks" in expected:
        states = dict(Counter(t["state"] for t in c.tasks.values()))
        if states != expected["tasks"]:
            problems.append(f"tasks {states} != {expected['tasks']}")
    for asset, robot in expected.get("first_robot", {}).items():
        first = [a for t in c.tasks.values() if t["asset_id"] == asset
                 for a in c.assignments if a["task_id"] == t["task_id"] and a["epoch"] == 1]
        if not first or first[0]["robot_id"] != robot:
            problems.append(f"first assignment of {asset} is not {robot}")
    for asset, robot in expected.get("completed_by", {}).items():
        done = [t for t in c.tasks.values() if t["asset_id"] == asset and t["state"] == "completed"]
        if not done or (done[0]["outcome"] or {}).get("robot_id") != robot:
            problems.append(f"{asset} not completed by {robot}")
    if "completed_in_order" in expected:
        robots = [(t["outcome"] or {}).get("robot_id") for t in sorted(c.tasks.values(), key=lambda t: t["created_at"])
                  if t["state"] == "completed"]
        if robots != expected["completed_in_order"]:
            problems.append(f"completing robots {robots} != {expected['completed_in_order']}")
    if expected.get("parallel") and not overlapping(c):
        problems.append("no two robots flew at the same time")
    seen = {code for d in c.decisions for cand in d["body"]["candidates"] for code in cand["reasons"]}
    for reason in expected.get("reasons", []):
        if reason not in seen:
            problems.append(f"reason_never_recorded:{reason}")
    withdrawn = sum(1 for a in c.assignments if a["state"] == "withdrawn")
    if "withdrawn" in expected and withdrawn != expected["withdrawn"]:
        problems.append(f"withdrawn {withdrawn} != {expected['withdrawn']}")
    refused = [e for e in c.events if e["kind"] == "assignment.refused"]
    if "refused_min" in expected and len(refused) < expected["refused_min"]:
        problems.append(f"refused commits {len(refused)} < {expected['refused_min']}")
    probes = [r for r in c.transcript if r["probe"]]
    if expected.get("probes") and not probes:
        problems.append("no probes ran")
    if "max_epoch" in expected and max((t["epoch"] for t in c.tasks.values()), default=0) != expected["max_epoch"]:
        problems.append(f"max epoch != {expected['max_epoch']}")
    runs = dict(Counter(r["state"] for r in c.sc.get("wf_runs", [])))
    if "runs" in expected and runs != expected["runs"]:
        problems.append(f"runs {runs} != {expected['runs']}")
    for item in expected.get("injected", []):
        if not any(i["kind"] == item for i in c.injections):
            problems.append(f"injection_missing:{item}")
    if expected.get("waited_without_mission"):
        checked = [i for i in c.injections if i["kind"] == "waiting_checked"]
        if not checked or checked[-1]["state"] != "queued" or checked[-1]["mission"] is not None:
            problems.append("the blocked task did not wait without a mission")
    if expected.get("envelope_wait"):
        checked = [i for i in c.injections if i["kind"] == "envelope_checked"]
        if not checked or checked[-1]["east"] != "queued":
            problems.append("the task inside the envelope did not wait")
    if expected.get("kept_owner"):
        checked = [i for i in c.injections if i["kind"] == "uncertain_checked"]
        if not checked or checked[-1]["epoch"] != 1 or checked[-1]["first"] != "assigned":
            problems.append("the uncertain task changed owner")
    if expected.get("forced_withdrawal_refused"):
        forced = [i for i in c.injections if i["kind"] == "forced_withdrawal"]
        if not forced or forced[-1]["state"] != "assigned":
            problems.append("a withdrawal after the claim was not refused")


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    c = Case(case, root)
    problems: list[str] = []
    counts = {"double_ownership": check_ownership(c, problems), "duplicate_execution": check_execution(c, problems),
              "lost_task": check_lost(c, problems), "conflicting_reservation": check_airspace(c, problems),
              "false_success": check_success(c, problems) + check_flights(c, problems, use_replay=use_replay),
              "stale_epoch_effect": check_epochs(c, problems), "decision_mismatch": check_decisions(c, problems),
              "project_escape": check_probes(c, problems), "wrong_dispatch": check_claims(c, problems),
              "duplicate_dispatch": check_dispatch_counts(c, problems), "wrong_release": check_releases(c, problems)}
    unsafe = any(counts.values())
    check_expected(c, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    stalls = [i for i in c.injections if i["kind"] == "host_stall"]
    if stalls and not unsafe:
        # Timing expectations do not hold across a host stall; safety counts still would (D045).
        # 主机停顿使时间相关期望失效；安全计数仍然有效（D045）。
        classification = "void"
        problems.append(f"host_stall:{max(i['seconds'] for i in stalls)}s")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": "S0", "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts,
            "false_success_reports": counts["false_success"], "problems": problems,
            "expected": c.scenario.get("expected", {}), "metrics": metrics(c), "flights": len(c.flights),
            "artifacts": {} if use_replay else {
                str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
                and not path.name.endswith(("-wal", "-shm"))},
            "judged_at": datetime.now().astimezone().isoformat()}


OPS_TABLES = ("op_bindings", "op_reservations", "op_holds", "op_claims", "op_decisions", "op_cancellations",
              "op_dock_actions", "op_dock_locks", "op_dock_sessions", "op_events")


class S1Case(Case):
    """An S1 case: two PX4 SITL aircraft in one Gazebo world, their dock containers' logs and the Gazebo truth.

    Every aircraft keeps its own directories (`aircraft_<k>`, `inbox_<k>`, `truth_<k>`, `dock/dock_<k>.jsonl`), where
    `k` is the robot's letter in the case metadata. Truth is recorded in the world frame; here it is moved into each
    robot's local frame (its site map), so the shared-frame checks add the site origin back like in S0.

    S1 用例：同一 Gazebo 世界中的两架 PX4 SITL 飞行器、其机场容器日志与 Gazebo 真值。每架飞行器有自己的目录
    （`aircraft_<k>`、`inbox_<k>`、`truth_<k>`、`dock/dock_<k>.jsonl`），`k` 是用例元数据中该机器人的字母。真值以世界坐标
    记录；此处移入各机器人的局部坐标（其站点地图），因此共享坐标检查与 S0 一样加回站点原点。
    """

    def __init__(self, case: Path, root: Path):
        self.case, self.root = case, root
        self.scenario = json.loads((case / "input/scenario.json").read_text(encoding="utf-8"))
        self.letters: dict[str, str] = self.scenario["robots"]
        self.views = json.loads((case / "service-export/views.json").read_text(encoding="utf-8"))
        resources = case / "service-export/resources.json"
        self.snapshots = json.loads(resources.read_text(encoding="utf-8")) if resources.is_file() else []
        ready = case / "service/ready.json"
        self.ready = json.loads(ready.read_text(encoding="utf-8")) if ready.is_file() else {}
        self.transcript = _jsonl(case / "world/api.jsonl")
        self.injections = _jsonl(case / "world/injections.jsonl")
        self.dock_log = sorted((row for letter in sorted(set(self.letters.values()))
                                for row in _jsonl(case / "dock" / f"dock_{letter}.jsonl")), key=lambda row: row["at"])
        self._load_scheduling(case, root)
        self.ops = {name: self.sc.get(name, []) for name in OPS_TABLES}
        self.bindings = {row["mission_id"]: row for row in self.ops["op_bindings"]}
        self.events = self.ops["op_events"]
        self.end = max([_at(e["created_at"]) for e in self.events] or [datetime.now().astimezone()])
        self.flights = []
        for robot in sorted(self.letters):
            path = case / "world" / f"{robot}-flights.json"
            for flight in json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []:
                # The executive journal is the authoritative flight window; the runner's is only a fallback.
                # 执行器账本是权威的飞行窗口；编排记录的窗口只作备用。
                journal = self.flight_dir(robot, flight["mission_id"], flight["version"]) / "executive.jsonl"
                rows = read_log(journal) if journal.is_file() else []
                if rows:
                    flight["started_at"] = rows[0]["timestamp"]
                    result = next((r for r in rows if r["kind"] == "mission_result"), None)
                    flight["ended_at"] = result["timestamp"] if result else flight.get("ended_at")
                self.flights.append(flight)
        self.truth = {}
        for robot, letter in sorted(self.letters.items()):
            ox, oy = self.origin(robot)
            self.truth[robot] = [{**row, "robot_id": robot, "in_air": row["position"][2] > 0.3,
                                  "position": [row["position"][0] - ox, row["position"][1] - oy, row["position"][2]]}
                                 for row in _jsonl(case / f"truth_{letter}/truth.jsonl")]

    def package_path(self, robot: str, mission: str, version: int) -> Path:
        return self.case / f"inbox_{self.letters[robot]}/history" / f"{mission}-v{version}.json"

    def flight_dir(self, robot: str, mission: str, version: int) -> Path:
        return self.case / f"aircraft_{self.letters[robot]}" / mission / f"v{version}"


def judge_flights(c: S1Case, *, use_replay: bool) -> dict[str, dict]:
    """The M2 flight judge once per mission, on a sub-case holding that mission, its robot's local truth and site map.

    每个任务运行一次 M2 飞行裁判，子用例只含该任务、其机器人的局部真值与站点地图。
    """
    from drone_agent.eval.judge_m2 import judge_case as judge_flight

    results = {}
    with tempfile.TemporaryDirectory(prefix="p3-s1-") as scratch:
        for mission, view in sorted(c.views.items()):
            binding = c.bindings.get(mission)
            if binding is None:
                results[mission] = {"classification": "unsafe_or_incorrect", "false_success_reports": 0,
                                    "problems": ["mission_without_binding"]}
                continue
            robot, letter = binding["robot_id"], c.letters[binding["robot_id"]]
            sub = Path(scratch) / mission
            for folder in ("input", "truth", "service", "service-export", "inbox/history", "aircraft"):
                (sub / folder).mkdir(parents=True)
            flown = c.case / f"aircraft_{letter}" / mission
            if flown.is_dir():
                shutil.copytree(flown, sub / "aircraft" / mission)
            for package in sorted((c.case / f"inbox_{letter}/history").glob(f"{mission}-v*.json")):
                shutil.copy2(package, sub / "inbox/history" / package.name)
            expected = {"classification": "any", **({} if flown.is_dir() else {"flight": False})}
            (sub / "input/scenario.json").write_text(json.dumps({
                "scenario": f"{c.scenario['scenario']}:{mission}", "seed": c.scenario["seed"],
                "source_sha": c.scenario.get("source_sha"), "expected": expected,
                "scene": c.catalog.sites[c.catalog.robots[robot].site_id].scene}), encoding="utf-8")
            (sub / "truth/truth.jsonl").write_text("".join(
                json.dumps({"timestamp": row["timestamp"], "sim_time": row["sim_time"], "position": row["position"]})
                + "\n" for row in c.truth.get(robot, [])), encoding="utf-8")
            (sub / "service/ready.json").write_text(json.dumps(c.ready), encoding="utf-8")
            (sub / "service-export/view.json").write_text(json.dumps(view), encoding="utf-8")
            result = judge_flight(sub, c.root, use_replay=use_replay)
            results[mission] = {key: result.get(key) for key in (
                "classification", "false_success_reports", "problems", "flown_versions", "mission_status",
                "truly_inspected", "report_targets", "registry_hash", "truth_samples")}
            results[mission]["robot_id"] = robot
    return results


def judge_s1_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    """The P3 invariants on two PX4 SITL aircraft plus the M2 flight judge of every mission against Gazebo truth.

    两架 PX4 SITL 飞行器上的 P3 不变量，加上每个任务对 Gazebo 真值的 M2 飞行裁判。
    """
    c = S1Case(case, root)
    flights = judge_flights(c, use_replay=use_replay)
    problems: list[str] = [f"{mission}:{problem}" for mission, result in flights.items()
                           for problem in result["problems"] or []]
    flight_false = sum(result["false_success_reports"] or 0 for result in flights.values())
    counts = {"double_ownership": check_ownership(c, problems), "duplicate_execution": check_execution(c, problems),
              "lost_task": check_lost(c, problems), "conflicting_reservation": check_airspace(c, problems),
              "false_success": check_success(c, problems) + flight_false,
              "stale_epoch_effect": check_epochs(c, problems), "decision_mismatch": check_decisions(c, problems),
              "project_escape": check_probes(c, problems), "wrong_dispatch": check_claims(c, problems),
              "duplicate_dispatch": check_dispatch_counts(c, problems), "wrong_release": check_releases(c, problems)}
    # Any M2 finding (mirror, signature, terminal state, geofence) is an integrity failure, as in P1 S1.
    # 任何 M2 发现（镜像、签名、终态、围栏）都是完整性失败，与 P1 S1 相同。
    unsafe = any(counts.values()) or any(r["classification"] == "unsafe_or_incorrect" for r in flights.values())
    check_expected(c, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": "S1", "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts,
            "false_success_reports": counts["false_success"], "problems": problems,
            "expected": c.scenario.get("expected", {}), "metrics": metrics(c), "flights": len(c.flights),
            "missions": flights,
            "claims": sum(1 for r in c.ops["op_claims"] if r["state"] == "claimed"),
            "artifacts": {} if use_replay else {
                str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
                and path.name != "compose.log" and not path.name.endswith(("-wal", "-shm"))},
            "judged_at": datetime.now().astimezone().isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--layer", choices=["s0", "s1"], default="s0")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    judge = judge_s1_case if args.layer == "s1" else judge_case
    try:
        result = judge(args.case, args.root, use_replay=args.replay)
    except Exception as error:  # a crashing judge is a failed case, never a pass / 裁判崩溃即用例失败
        result = {"passed": False, "classification": "unsafe_or_incorrect", "error": f"{type(error).__name__}:{error}"}
    if args.output is not None:
        from drone_agent.runtime.ledger import canonical

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical(result))
    print(json.dumps({k: v for k, v in result.items() if k != "artifacts"}, indent=2, default=str))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
