"""Independent judge for one X1 case (D088): handoff, ownership, execution and the ground robot against the world.

The judge never reuses the handoff engine, the scheduler, the reservation code or the service. From the exported
tables, the API transcript, the injections, the scene and every robot's own records it re-derives: that no resource
(a robot's motion, an airspace or ground cell, a shared apron, a dock resource) ever had two holders at once and no
task two live assignments; that no mission version ran twice and no handoff task produced two missions or two acked
offers; that every handoff task, offer and recheck ended; that every offer moved only along offered -> accepted /
rejected / timed out / withdrawn -> acked -> completed, every ack followed an acceptance by the addressed robot and
carried that assignment's epoch, and a refused reply changed nothing; that a relocation is backed by a completed ground
mission whose view the service verified and by a block that was really there, and every verified ground step is true
against the robot's truth; that the ground robot stayed inside its own map, outside every obstacle and no-go zone, and
inside its held ground cells whenever it moved; that no hold was granted into a silent ground robot's envelope
(recomputed from the recorded link loss and the footprint geometry); and that no ground robot stood on an apron while an
aircraft of that site was airborne. The scenario's expectations come on top, and the metrics of the X1 baseline
(handoff success, aerial annotation and ground relocation error against the scene, recheck completion, handoff latency)
are measured here. Run with `use_replay`, the ground missions' step outcomes come from the executive's recording.

In S1 (`--layer s1`) the robots are PX4 SITL in one Gazebo world: their truth is the Gazebo pose moved into each robot's
local frame, the rover's speed is differenced from it and its armed state comes from the spans of its PX4 flight logs
on the simulation clock; every aircraft mission is also judged by the M2 flight judge against that truth, as in P3 S1.

单个 X1 用例的独立裁判（D088）：以世界核对交接、所有权、执行与地面车。

裁判从不复用交接引擎、调度器、预约代码或服务。它从导出表、API 记录、注入、场景与每台机器人自身的记录重新推导：任何资源（机器人
运动、空域或地面单元、共享起降区、机场资源）从未同时有两个持有者，任务从未同时有两个有效分配；没有任务版本执行两次，没有交接任务
产生两个任务或两个已确认提议；每个交接任务、提议与复核都已结束；每个提议只沿 offered -> accepted / rejected / timed_out /
withdrawn -> acked -> completed 迁移，每次确认都在被点名机器人的接受之后且带该分配的代次，被拒的回复没有改变任何东西；重新定位有
已完成、画面经服务核验的地面任务与真实存在的方块支撑，每个已核验的地面步骤都与机器人真值相符；地面车始终在其自身地图之内、在所有
障碍与禁入区之外，移动时始终在其所持地面单元之内；没有持有落入失联地面车的包络（按记录的链路中断与航迹几何重算）；该站点有飞行器
在空中时没有地面车位于起降区内。场景期望另外核对，X1 基线的指标（交接成功率、对场景的空中标注误差与地面重新定位误差、复核完成率、
交接耗时）在此测量。带 `use_replay` 运行时，地面任务的步骤结果取自执行器的录制。

S1（`--layer s1`）中机器人是同一 Gazebo 世界里的 PX4 SITL：真值是移到各机器人局部坐标的 Gazebo 位姿，地面车速度由其差分得到，
解锁状态取自其 PX4 飞行日志在仿真时钟上的跨度；每个飞行器任务另由 M2 飞行裁判对该真值裁判一次，与 P3 S1 相同。
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

from drone_agent.contracts import MissionPackage, StepOutcome
from drone_agent.eval.judge_m2 import _replayed
from drone_agent.eval.judge_p3 import _at, _json, _jsonl, _percentiles, assignment_intervals, hold_intervals, overlaps
from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.scheduling_models import load_scheduling
from drone_agent.mission.reachability import inside_polygon
from drone_agent.mission.registry import load_registry
from drone_agent.runtime.ledger import read_log

COUNTS = ("double_ownership", "duplicate_execution", "lost_task", "handoff_protocol", "false_success",
          "wrong_outcome", "ground_containment", "envelope_grant", "apron_conflict")
TASK_FINAL = ("completed", "failed", "outcome_unknown", "rejected", "cancelled")
TRANSITIONS = {"offered": {"accepted", "rejected", "timed_out", "withdrawn"},
               "accepted": {"acked", "timed_out", "withdrawn"}, "acked": {"completed"}}
NOTES = ("reply_refused", "reply_ignored")
# What a verified ground step may differ from the truth beyond the map's own tolerance: sampling only in S0 (the
# logical robot knows its position); in S1 also the estimate's error against Gazebo, as the M2 flight judge allows.
# 已核验地面步骤在地图自身容差之外与真值可差的量：S0 只有采样误差（逻辑地面车知道自己的位置）；S1 另含估计相对 Gazebo 的误差，
# 与 M2 飞行裁判的放宽相同。
TRUTH_SLACK_M = {"S0": 0.1, "S1": 0.3}
STILL_MPS = 0.06
ARMED = 2  # PX4 vehicle_status.arming_state / PX4 载具状态中的解锁值
SPEED_WINDOW_S = 0.5  # the span over which S1 truth speed is differenced / S1 真值速度的差分跨度
MARGIN_S = 1.0  # time slack between a recorded event and the truth sample nearest it / 记录事件与最近真值样本的时间余量


def _decode(row: dict, *names: str) -> dict:
    return {**row, **{name: _json(row[name]) for name in names if name in row}}


class Case:
    """Everything recorded for one X1 case, loaded once. / 一个 X1 用例的全部记录，只加载一次。"""

    layer = "S0"

    def __init__(self, case: Path, root: Path):
        self.case, self.root = case, root
        self.slack_m = TRUTH_SLACK_M[self.layer]
        self.scenario = json.loads((case / "input/scenario.json").read_text(encoding="utf-8"))
        self.layout = json.loads((case / "input/layout.json").read_text(encoding="utf-8"))
        self.catalog = load_catalog(root / self.layout["catalog"])
        self.scheduling = load_scheduling(root / self.layout["scheduling"])
        self.maps = {site: load_registry(root, root / entry.scene) for site, entry in self.catalog.sites.items()}
        self.sc = json.loads((case / "service-export/scheduling.json").read_text(encoding="utf-8"))
        self.views = json.loads((case / "service-export/views.json").read_text(encoding="utf-8"))
        self.transcript = _jsonl(case / "world/api.jsonl")
        self.injections = _jsonl(case / "world/injections.jsonl")
        self.scene = json.loads((case / "world/scene.json").read_text(encoding="utf-8"))
        self.flights = [flight for path in sorted((case / "world").glob("*-flights.json"))
                        for flight in json.loads(path.read_text(encoding="utf-8"))]
        self.truth = {}
        for path in sorted((case / "world").glob("*-truth.jsonl")):
            rows = _jsonl(path)
            if rows:
                self.truth[rows[0]["robot_id"]] = [{**row, "at": _at(row["timestamp"])} for row in rows]
        self.events = self.sc.get("op_events", [])
        self.tasks = {row["task_id"]: _decode(row, "outcome", "excluded", "cancel", "detail")
                      for row in self.sc.get("sc_tasks", [])}
        self.assignments = self.sc.get("sc_assignments", [])
        self.footprints = {row["activity_key"]: _decode(row, "cells", "box", "volume_box")
                           for row in self.sc.get("sc_footprints", [])}
        self.missions = {row["mission_id"]: row for row in self.sc.get("missions", [])}
        self.bindings = {row["mission_id"]: row for row in self.sc.get("op_bindings", [])}
        self.handoffs = {row["handoff_id"]: _decode(row, "target", "reply", "evidence_refs")
                         for row in self.sc.get("ho_handoffs", [])}
        self.ho_events = sorted((_decode(row, "body") for row in self.sc.get("ho_events", [])),
                                key=lambda e: (e["handoff_id"], e["seq"]))
        self.rechecks = [_decode(row, "aerial_evidence", "ground_evidence", "detail")
                         for row in self.sc.get("bz_rechecks", [])]
        self.verifications = [_decode(row, "body") for row in self.sc.get("verifications", [])]
        stamps = [_at(e["created_at"]) for e in self.events] + [r["at"] for rows in self.truth.values()
                                                                  for r in rows]
        self.end = max(stamps or [datetime.now().astimezone()])

    def ground(self, robot: str) -> bool:
        entry = self.catalog.robots.get(robot)
        return entry is not None and entry.dock_id is None

    def ground_map(self, robot: str):
        return self.maps[self.catalog.robots[robot].site_id]

    def origin(self, robot: str) -> tuple[float, float]:
        return tuple(self.scheduling.airspace.sites[self.catalog.robots[robot].site_id].origin)

    def handoff_tasks(self) -> list[dict]:
        return [t for t in self.tasks.values() if t["source"] == "handoff"]

    def ground_missions(self) -> list[str]:
        return sorted(m for m, b in self.bindings.items() if self.ground(b["robot_id"]))

    def anomaly_truth(self) -> list[dict]:
        """Every block ever placed, with when it was placed and removed. / 每个放置过的方块及其放置与移除时刻。"""
        found = []
        for item in self.injections:
            if item["kind"] == "anomaly_placed":
                found.append({**item, "placed": _at(item["at"]), "removed": None})
            elif item["kind"] == "anomaly_removed":
                for block in found:
                    if block["removed"] is None and (block["x"], block["y"]) == (item["x"], item["y"]):
                        block["removed"] = _at(item["at"])
                        break
        return found

    def nearest(self, robot: str, at: datetime) -> dict | None:
        rows = self.truth.get(robot) or []
        return min(rows, key=lambda r: abs((r["at"] - at).total_seconds())) if rows else None


# ── checks / 检查 ──


def check_ownership(c: Case, problems: list[str]) -> int:
    count = 0
    for task, intervals in assignment_intervals(c).items():
        for a, b in overlaps(intervals):
            count += 1
            problems.append(f"two_active_assignments:{task}:e{a[0]}:e{b[0]}")
    for resource, intervals in hold_intervals(c).items():
        for a, b in overlaps(intervals):
            count += 1
            problems.append(f"two_holders:{resource}:{a[0]}:{b[0]}")
    return count


def check_execution(c: Case, problems: list[str]) -> int:
    count = 0
    for (robot, mission, version), number in Counter((f["robot_id"], f["mission_id"], f["version"])
                                                     for f in c.flights).items():
        if number > 1:
            count += 1
            problems.append(f"mission_run_twice:{robot}:{mission}:v{version}")
    for task in c.handoff_tasks():
        missions = {a["mission_id"] for a in c.assignments if a["task_id"] == task["task_id"] and a["mission_id"]}
        if len(missions) > 1:
            count += 1
            problems.append(f"two_ground_missions:{task['task_id']}")
        acked = [h for h in c.handoffs.values() if h["task_id"] == task["task_id"]
                 and h["state"] in ("acked", "completed")]
        if len(acked) > 1:
            count += 1
            problems.append(f"two_acked_offers:{task['task_id']}")
    return count


def check_lost(c: Case, problems: list[str]) -> int:
    count = 0
    for task in c.handoff_tasks():
        if task["state"] not in TASK_FINAL:
            count += 1
            problems.append(f"handoff_task_not_final:{task['task_id']}:{task['state']}")
    for handoff in c.handoffs.values():
        if handoff["state"] in ("offered", "accepted"):
            count += 1
            problems.append(f"offer_left_open:{handoff['handoff_id']}:{handoff['state']}")
    for recheck in c.rechecks:
        if recheck["outcome"] == "pending":
            count += 1
            problems.append(f"recheck_not_concluded:{recheck['recheck_id']}")
    return count


def check_protocol(c: Case, problems: list[str]) -> int:
    """Offers move only along the allowed transitions; an ack follows the addressed robot's acceptance and carries
    its assignment's epoch. / 提议只沿允许的迁移变化；确认在被点名机器人接受之后，且带其分配的代次。"""
    count = 0
    by_offer: dict[str, list[dict]] = {}
    for event in c.ho_events:
        by_offer.setdefault(event["handoff_id"], []).append(event)
    for handoff_id, events in by_offer.items():
        handoff = c.handoffs.get(handoff_id)
        states = [e for e in events if e["kind"] not in NOTES]
        if not states or states[0]["kind"] != "offered":
            count += 1
            problems.append(f"offer_without_offer_event:{handoff_id}")
            continue
        for before, after in zip(states, states[1:], strict=False):
            if after["kind"] not in TRANSITIONS.get(before["kind"], set()):
                count += 1
                problems.append(f"illegal_transition:{handoff_id}:{before['kind']}->{after['kind']}")
        if handoff is not None and states[-1]["kind"] != handoff["state"]:
            count += 1
            problems.append(f"state_differs_from_history:{handoff_id}")
        acked = next((e for e in states if e["kind"] == "acked"), None)
        if acked is not None:
            accepted = next((e for e in states if e["kind"] == "accepted"), None)
            if accepted is None or accepted["actor"] != handoff["to_robot_id"]:
                count += 1
                problems.append(f"ack_without_acceptance:{handoff_id}")
            epoch = acked["body"].get("assignment_epoch")
            mine = [a for a in c.assignments if a["task_id"] == handoff["task_id"]
                    and a["decision_id"] == f"handoff:{handoff_id}"]
            if not mine or mine[0]["epoch"] != epoch or handoff["assignment_epoch"] != epoch:
                count += 1
                problems.append(f"ack_epoch_mismatch:{handoff_id}")
    return count


def outcomes(c: Case, robot: str, mission: str, version: int, *, use_replay: bool) -> tuple[dict, list[dict]]:
    directory = c.case / "robots" / robot / "aircraft" / mission / f"v{version}"
    rows = _replayed(directory / "executive.mcap") if use_replay else read_log(directory / "executive.jsonl")
    found = {}
    for row in rows:
        if row["kind"] == "step_outcome":
            outcome = StepOutcome.model_validate(row["data"]["outcome"])
            found[outcome.step_id] = (outcome, _at(row["timestamp"]))
    return found, rows


def true_ground_step(c: Case, robot: str, node, at: datetime, directory: Path) -> bool:
    """A verified ground step against the robot's truth at the moment it was verified. / 已核验地面步骤对其核验时刻的真值。"""
    truth = c.nearest(robot, at)
    if truth is None or abs((truth["at"] - at).total_seconds()) > MARGIN_S:
        return False
    point, p = truth["position"][:2], node.params
    name = node.skill_id.removeprefix("skill.ground.")
    thresholds = c.ground_map(robot).thresholds
    arrival = thresholds["nav_to"]["tolerance_m"] + c.slack_m
    if name == "arm":
        return truth["armed"] is True
    if name == "nav_to":
        return math.dist(point, p["goal"]) <= arrival
    if name == "return_home":
        return math.dist(point, p["waypoints"][-1]) <= thresholds["return_home"]["tolerance_m"] + c.slack_m
    if name == "stop":
        return truth["speed"] <= STILL_MPS
    if name == "disarm":
        return truth["armed"] is False and truth["speed"] <= STILL_MPS
    if name == "recheck":
        record = directory / f"evidence-{node.task_id}.json"
        if not record.is_file():
            return False
        captured = json.loads(record.read_text(encoding="utf-8"))["observation"]["pose"]["position"]
        at_capture = c.nearest(robot, _at(json.loads(record.read_text(encoding="utf-8"))["capture_timestamp"]))
        return at_capture is not None and math.dist((captured["x"], captured["y"]), p["face_point"]) <= arrival \
            and math.dist(at_capture["position"][:2], p["face_point"]) <= arrival
    return False


def check_success(c: Case, problems: list[str], *, use_replay: bool) -> tuple[int, int]:
    """(false successes, wrong outcomes): every verified ground step true; a relocation backed by a verified view and
    a block that was there; a not-found only where no block could be seen.

    （错误成功，错误结论）：每个已核验地面步骤为真；重新定位有已核验画面与真实存在的方块支撑；未找到只出现在看不到方块之处。
    """
    false, wrong = 0, 0
    for flight in c.flights:
        robot = flight["robot_id"]
        if not c.ground(robot):
            continue
        mission, version = flight["mission_id"], flight["version"]
        directory = c.case / "robots" / robot / "aircraft" / mission / f"v{version}"
        package_file = c.case / "robots" / robot / "inbox/history" / f"{mission}-v{version}.json"
        if not package_file.is_file() or not (directory / "executive.jsonl").is_file():
            problems.append(f"ground_records_missing:{mission}:v{version}")
            false += 1
            continue
        package = MissionPackage.model_validate_json(package_file.read_bytes())
        found, _ = outcomes(c, robot, mission, version, use_replay=use_replay)
        for node in package.nodes:
            outcome, at = found.get(node.task_id, (None, None))
            if outcome is None or not outcome.counts_as_completed:
                continue
            if not true_ground_step(c, robot, node, at, directory):
                false += 1
                problems.append(f"untrue_ground_step:{mission}:{node.task_id}")
    verified = {v["evidence_id"] for v in c.verifications if v["body"].get("final_verdict") == "verified"}
    tolerance = 0.5 + 0.5  # the frozen relocation tolerance plus half a block / 冻结的重新定位容差加半个方块
    for recheck in c.rechecks:
        if recheck["outcome"] not in ("relocated", "not_found"):
            continue
        mission = recheck["ground_mission_id"]
        report = ((c.views.get(mission) or {}).get("report") or {}) if mission else {}
        if c.missions.get(mission, {}).get("status") != "completed" or not report.get("all_targets_completed"):
            false += 1
            problems.append(f"recheck_without_completed_mission:{recheck['recheck_id']}")
            continue
        if not recheck["ground_evidence"] or not set(recheck["ground_evidence"]) <= verified:
            false += 1
            problems.append(f"recheck_without_verified_view:{recheck['recheck_id']}")
            continue
        robot = recheck["detail"]["robot_id"]
        ox, oy = c.origin(robot)
        flight = next((f for f in c.flights if f["mission_id"] == mission), None)
        seen_at = _at(flight["ended_at"]) if flight and flight.get("ended_at") else c.end
        present = [b for b in c.anomaly_truth() if b["from_ground"] and b["colour"] == recheck["detail"]["signature"]
                   and b["placed"] <= seen_at and (b["removed"] is None or b["removed"] > seen_at)]
        if recheck["outcome"] == "relocated":
            point = recheck["detail"].get("relocated")
            shared = (ox + point[0], oy + point[1]) if point else None
            if shared is None or not any(math.dist(shared, (b["x"], b["y"])) <= tolerance for b in present):
                false += 1
                problems.append(f"relocated_without_a_block_there:{recheck['recheck_id']}")
        elif present:
            target = recheck["detail"].get("target") or {}
            position = target.get("position") or {}
            near = [b for b in present if position and
                    math.dist((ox + position["x"], oy + position["y"]), (b["x"], b["y"])) <= 3.0]
            if near:
                wrong += 1
                problems.append(f"not_found_while_a_block_was_there:{recheck['recheck_id']}")
    return false, wrong


def check_containment(c: Case, problems: list[str]) -> int:
    """Inside its own map, outside obstacles and no-go zones, and inside the held ground cells while moving.

    在其自身地图之内、障碍与禁入区之外，移动时在所持地面单元之内。
    """
    count = 0
    holds = hold_intervals(c)
    size = c.scheduling.airspace.cell_m
    for robot, rows in c.truth.items():
        if not c.ground(robot):
            continue
        ground = c.ground_map(robot)
        ox, oy = c.origin(robot)
        outside = [r for r in rows if not ground.inside(r["position"][:2])]
        if outside:
            count += 1
            problems.append(f"ground_robot_outside_its_map:{robot}:{len(outside)}")
        intervals = holds.get(f"{robot}.motion", [])
        stray = 0
        for row in rows:
            if not (row["armed"] or row["speed"] > STILL_MPS):
                continue
            holder = next((a for a, start, stop in intervals if start - timedelta(seconds=MARGIN_S) <= row["at"]
                           <= stop + timedelta(seconds=MARGIN_S)), None)
            footprint = c.footprints.get(holder) if holder else None
            if footprint is None:
                stray += 1
                continue
            x, y = ox + row["position"][0], oy + row["position"][1]
            cells = [cell for cell in footprint["cells"] if cell.startswith("ground.")]
            if not any(int(cell.rsplit(".", 2)[1]) * size <= x <= (int(cell.rsplit(".", 2)[1]) + 1) * size
                       and int(cell.rsplit(".", 1)[1]) * size <= y <= (int(cell.rsplit(".", 1)[1]) + 1) * size
                       for cell in cells):
                stray += 1
        if stray:
            count += 1
            problems.append(f"ground_robot_outside_its_held_cells:{robot}:{stray}")
    return count


def silent_envelopes(c: Case) -> list[tuple[str, datetime, datetime, dict, str]]:
    """(robot, silent from, silent until, footprint, activity) of every link loss of a ground robot holding a mission.

    每次持有任务的地面车链路中断的（机器人，失联起，失联止，航迹覆盖，活动）。
    """
    holds = hold_intervals(c)
    found, lost = [], {}
    for item in c.injections:
        if item["kind"] == "link_lost":
            lost[item["robot_id"]] = _at(item["at"])
        elif item["kind"] == "link_restored" and item["robot_id"] in lost:
            start = lost.pop(item["robot_id"])
            found.append((item["robot_id"], start, _at(item["at"])))
    found += [(robot, start, c.end) for robot, start in lost.items()]
    envelopes = []
    for robot, start, stop in found:
        holder = next((a for a, s, e in holds.get(f"{robot}.motion", []) if s <= start <= e), None)
        if holder is not None and holder in c.footprints:
            envelopes.append((robot, start, stop, c.footprints[holder], holder))
    return envelopes


def envelope(c: Case, footprint: dict, radius: float) -> set[str]:
    box, volume = footprint["box"], footprint["volume_box"]
    grown = [box[0] - radius, box[1] - radius, box[2] + radius, box[3] + radius]
    if volume:
        clipped = [max(grown[0], volume[0]), max(grown[1], volume[1]), min(grown[2], volume[2]),
                   min(grown[3], volume[3])]
        grown = clipped if clipped[0] <= clipped[2] and clipped[1] <= clipped[3] else list(box)
    size, frame = c.scheduling.airspace.cell_m, c.scheduling.airspace.frame
    cells = set(footprint["cells"])
    for i in range(math.floor(grown[0] / size), max(math.floor(grown[0] / size), math.ceil(grown[2] / size) - 1) + 1):
        for j in range(math.floor(grown[1] / size),
                       max(math.floor(grown[1] / size), math.ceil(grown[3] / size) - 1) + 1):
            cells.add(f"ground.{frame}.{i}.{j}")
    return cells


def check_envelopes(c: Case, problems: list[str]) -> int:
    """No new hold of another activity inside a silent ground robot's envelope. / 失联地面车的包络内没有其他活动的新持有。"""
    count = 0
    grid = c.scheduling.airspace
    for robot, start, stop, footprint, activity in silent_envelopes(c):
        speed = 1.0
        for event in c.events:
            if event["kind"] != "reservation.reserved" or event["subject"] == activity:
                continue
            at = _at(event["created_at"])
            silence = (at - start).total_seconds() - MARGIN_S
            if not (start <= at <= stop) or silence <= grid.contact_timeout_s:
                continue
            cells = envelope(c, footprint, speed * silence + grid.envelope_margin_m)
            body = _json(event["body"])
            taken = set(body.get("resources") or []) & cells
            if taken:
                count += 1
                problems.append(f"hold_inside_envelope:{event['subject']}:{robot}:{sorted(taken)[0]}")
    return count


def check_apron(c: Case, problems: list[str]) -> int:
    """No ground robot inside an apron while an aircraft of its site is airborne. / 该站点飞行器在空中时没有地面车位于起降区内。"""
    layer = c.scheduling.ground
    if layer is None or not layer.shared:
        return 0
    count = 0
    for name, area in layer.shared.items():
        polygon = [tuple(p) for p in area.polygon]
        aircraft = [r for r, entry in c.catalog.robots.items() if entry.site_id == area.site and not c.ground(r)]
        for robot, rows in c.truth.items():
            if not c.ground(robot):
                continue
            ox, oy = c.origin(robot)
            for row in rows:
                if not inside_polygon((ox + row["position"][0], oy + row["position"][1]), polygon):
                    continue
                airborne = [a for a in aircraft if (t := c.nearest(a, row["at"])) is not None and t.get("in_air")
                            and abs((t["at"] - row["at"]).total_seconds()) <= MARGIN_S]
                if airborne:
                    count += 1
                    problems.append(f"ground_robot_on_apron_with_aircraft_airborne:{name}:{robot}:{airborne[0]}")
                    break
    return count


# ── expectations and metrics / 期望与指标 ──


def derived(c: Case) -> dict:
    """What the expectations compare. / 期望所比较的结论。"""
    first = c.rechecks[0] if c.rechecks else None
    task = c.tasks.get(first["ground_task_id"]) if first and first["ground_task_id"] else \
        (c.handoff_tasks()[0] if c.handoff_tasks() else None)
    handoff = c.handoffs.get(first["handoff_id"]) if first and first["handoff_id"] else \
        (next(iter(c.handoffs.values())) if c.handoffs else None)
    found = {"ground_missions": len(c.ground_missions()), "offers": len(c.handoffs)}
    if first is not None:
        found.update(recheck=first["outcome"], reason=str(first["detail"].get("reason") or ""),
                     target=first["detail"].get("kind"))
        mission = first["ground_mission_id"]
        targets = ((c.views.get(mission) or {}).get("report") or {}).get("targets") or {} if mission else {}
        column = next(iter(targets.values()), None)
        if column is not None:
            found["ground_report"] = column if isinstance(column, str) else column.get("column")
    if handoff is not None:
        found["handoff"] = handoff["state"]
    if task is not None:
        found["task"] = task["state"]
    reviews = [n for n in c.sc.get("wf_nodes", []) if n["node_id"] == "review" and n["state"] == "completed"]
    if reviews:
        found["review"] = (_json(reviews[0]["result"]) or {}).get("decision")
    second = [i for i in c.injections if i["kind"] == "second_offer"]
    if second:
        found["second_offer"] = second[0]["refused"]
    refused = sorted({i["reason"] for i in c.injections if i["kind"] == "reply_sent" and not i["receipt"]})
    if refused:
        found["refused_replies"] = refused
    holds = hold_intervals(c)
    ground_holds = {robot: [(a, s, e) for a, s, e in holds.get(f"{robot}.motion", [])]
                    for robot in c.catalog.robots if c.ground(robot)}
    first_robot, second_robot = "ugv_01", "ugv_02"
    if ground_holds.get(second_robot) is not None:
        firsts, seconds = ground_holds.get(first_robot, []), ground_holds.get(second_robot, [])
        found["second_waited"] = all(s2 >= e1 - timedelta(seconds=MARGIN_S) for _, s2, _ in seconds
                                     for _, s1, e1 in firsts if s1 <= s2)
    apron = holds.get("shared.pad_a", [])
    ground_apron = [(a, s, e) for a, s, e in apron if a.split(":")[1] in c.bindings
                    and c.ground(c.bindings[a.split(":")[1]]["robot_id"])]
    air_apron = [(a, s, e) for a, s, e in apron if (a, s, e) not in ground_apron]
    if ground_apron and air_apron:
        found["apron_waited"] = all(gs >= ae for _, gs, _ in ground_apron for _, as_, ae in air_apron if as_ <= gs)
    robot = first["detail"].get("robot_id") if first else first_robot
    flights = [f for f in c.flights if f["robot_id"] == robot and f.get("ended_at")]
    if flights:
        # The state the robot was left in when its last mission ended. / 最后一次任务结束时地面车所处的状态。
        last = c.nearest(robot, _at(flights[-1]["ended_at"]))
        if last is not None:
            found["ground_terminal"] = "stopped_disarmed" if last["armed"] is False and last["speed"] <= STILL_MPS \
                else "moving_or_armed"
    return found


def check_expected(c: Case, found: dict, problems: list[str]) -> None:
    for key, value in (c.scenario.get("expected") or {}).items():
        if found.get(key) != value:
            problems.append(f"expected_{key}:{value}:got:{found.get(key)}")
    if c.scenario.get("harness_error"):
        problems.append(f"harness_error:{c.scenario['harness_error']}")


def metrics(c: Case) -> dict:
    """The X1 baseline measures of one case. / 一个用例的 X1 基线指标。"""
    blocks = c.anomaly_truth()
    aerial, ground, latency, closing = [], [], [], []
    for recheck in c.rechecks:
        robot = recheck["detail"].get("robot_id")
        target = recheck["detail"].get("target")
        if robot and target and blocks:
            ox, oy = c.origin(robot)
            point = (ox + target["position"]["x"], oy + target["position"]["y"])
            aerial.append(min(math.dist(point, (b["x"], b["y"])) for b in blocks))
        if robot and recheck["detail"].get("relocated") and blocks:
            ox, oy = c.origin(robot)
            x, y = recheck["detail"]["relocated"]
            ground.append(min(math.dist((ox + x, oy + y), (b["x"], b["y"])) for b in blocks))
    for handoff in c.handoffs.values():
        events = {e["kind"]: _at(e["at"]) for e in c.ho_events if e["handoff_id"] == handoff["handoff_id"]}
        if "offered" in events and "acked" in events:
            latency.append((events["acked"] - events["offered"]).total_seconds())
        if "offered" in events and "completed" in events:
            closing.append((events["completed"] - events["offered"]).total_seconds())
    offers = len(c.handoffs)
    attempted = [r for r in c.rechecks if r["handoff_id"]]
    return {"offers": offers, "acked": sum(1 for h in c.handoffs.values() if h["state"] in ("acked", "completed")),
            "rechecks": len(c.rechecks), "attempted": len(attempted),
            "concluded": sum(1 for r in attempted if r["outcome"] in ("relocated", "not_found")),
            "aerial_error_m": [round(v, 3) for v in aerial], "ground_error_m": [round(v, 3) for v in ground],
            "ack_latency_s": [round(v, 3) for v in latency], "offer_to_completed_s": [round(v, 3) for v in closing]}


def aggregate(items: list[dict]) -> dict:
    """The suite's baseline: rates and distributions, never pass thresholds. / 场景集基线：比率与分布，从不是通过阈值。"""
    def all_of(key):
        return [v for item in items for v in item.get(key, [])]

    offers, acked = sum(i.get("offers", 0) for i in items), sum(i.get("acked", 0) for i in items)
    attempted, concluded = sum(i.get("attempted", 0) for i in items), sum(i.get("concluded", 0) for i in items)
    return {"handoff_success": round(acked / offers, 3) if offers else None,
            "recheck_completion": round(concluded / attempted, 3) if attempted else None,
            "aerial_error_m": _percentiles(all_of("aerial_error_m")),
            "ground_error_m": _percentiles(all_of("ground_error_m")),
            "ack_latency_s": _percentiles(all_of("ack_latency_s")),
            "offer_to_completed_s": _percentiles(all_of("offer_to_completed_s"))}


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    c = Case(case, root)
    problems: list[str] = []
    false, wrong = check_success(c, problems, use_replay=use_replay)
    counts = {"double_ownership": check_ownership(c, problems), "duplicate_execution": check_execution(c, problems),
              "lost_task": check_lost(c, problems), "handoff_protocol": check_protocol(c, problems),
              "false_success": false, "wrong_outcome": wrong, "ground_containment": check_containment(c, problems),
              "envelope_grant": check_envelopes(c, problems), "apron_conflict": check_apron(c, problems)}
    unsafe = any(counts.values())
    found = derived(c)
    check_expected(c, found, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    stalls = [i for i in c.injections if i["kind"] == "host_stall"]
    if stalls and not unsafe:
        # Timing expectations do not hold across a host stall; safety counts still would (D045).
        # 主机停顿使时间相关期望失效；安全计数仍然有效（D045）。
        classification = "void"
        problems.append(f"host_stall:{max(i['seconds'] for i in stalls)}s")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": "S0", "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts, "problems": problems,
            "expected": c.scenario.get("expected", {}), "derived": found, "metrics": metrics(c),
            "flights": len(c.flights),
            "artifacts": {} if use_replay else {
                str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
                and not path.name.endswith(("-wal", "-shm"))},
            "judged_at": datetime.now().astimezone().isoformat()}


# ── S1: PX4 SITL in one Gazebo world / S1：同一 Gazebo 世界中的 PX4 SITL ──


def armed_periods(path: Path) -> list[tuple[float, float]]:
    """Armed periods in simulation seconds from the extracted flight logs: PX4 writes one file per armed period by
    default, and inside a file a status change away from armed ends the period early.

    由提取出的飞行日志得到解锁区间（仿真秒）：PX4 默认每个解锁期写一个文件；文件内状态离开解锁时区间提前结束。
    """
    if not path.is_file():
        return []
    periods = []
    for item in json.loads(path.read_text(encoding="utf-8"))["files"]:
        start = None
        for change in item["status"]:
            if change["arming_state"] == ARMED and start is None:
                start = change["t_us"]
            elif change["arming_state"] != ARMED and start is not None:
                periods.append((start / 1e6, change["t_us"] / 1e6))
                start = None
        if start is not None:
            periods.append((start / 1e6, item["last_us"] / 1e6))
    return periods


def truth_rows(rows: list[dict], robot: str, origin: tuple[float, float], *, ground: bool,
               periods: list[tuple[float, float]]) -> list[dict]:
    """Gazebo samples in the robot's local frame with speed, armed and airborne flags. / 局部坐标的 Gazebo 样本。"""
    rows = sorted(rows, key=lambda row: row["sim_time"])
    times = [row["sim_time"] for row in rows]
    built, low, high = [], 0, 0
    for index, row in enumerate(rows):
        while times[low] < times[index] - SPEED_WINDOW_S / 2:
            low += 1
        while high + 1 < len(rows) and times[high + 1] <= times[index] + SPEED_WINDOW_S / 2:
            high += 1
        span = times[high] - times[low]
        speed = math.dist(rows[high]["position"][:2], rows[low]["position"][:2]) / span if span > 0 else 0.0
        x, y, z = row["position"]
        built.append({"timestamp": row["timestamp"], "sim_time": row["sim_time"], "at": _at(row["timestamp"]),
                      "robot_id": robot, "position": [x - origin[0], y - origin[1], z],
                      "in_air": False if ground else z > 0.3, "speed": round(speed, 4),
                      "armed": any(a <= row["sim_time"] <= b for a, b in periods) if ground else None})
    return built


class S1Case(Case):
    """An X1 S1 case: the S0 layout under `robots/<robot_id>/` plus Gazebo truth and the rover's flight logs.

    X1 S1 用例：`robots/<robot_id>/` 下与 S0 相同的布局，加上 Gazebo 真值与地面车的飞行日志。
    """

    layer = "S1"

    def __init__(self, case: Path, root: Path):
        super().__init__(case, root)
        ready = case / "service/ready.json"
        self.ready = json.loads(ready.read_text(encoding="utf-8")) if ready.is_file() else {}
        self.letters: dict[str, str] = self.scenario["robots"]
        self.truth = {}
        for robot, letter in sorted(self.letters.items()):
            ground = self.ground(robot)
            periods = armed_periods(case / "world" / f"{robot}-arming.json") if ground else []
            self.truth[robot] = truth_rows(_jsonl(case / f"truth_{letter}/truth.jsonl"), robot, self.origin(robot),
                                           ground=ground, periods=periods)
        stamps = [_at(e["created_at"]) for e in self.events] + [r["at"] for rows in self.truth.values()
                                                                  for r in rows]
        self.end = max(stamps or [datetime.now().astimezone()])


def judge_aircraft(c: S1Case, *, use_replay: bool) -> dict[str, dict]:
    """The M2 flight judge once per aircraft mission, on a sub-case holding that mission, its robot's local truth and
    site map, as in P3 S1. / 每个飞行器任务运行一次 M2 飞行裁判，子用例只含该任务、其机器人的局部真值与站点地图，与 P3 S1 相同。
    """
    from drone_agent.eval.judge_m2 import judge_case as judge_flight

    results = {}
    with tempfile.TemporaryDirectory(prefix="x1-s1-") as scratch:
        for mission, view in sorted(c.views.items()):
            binding = c.bindings.get(mission)
            if binding is None:
                results[mission] = {"classification": "unsafe_or_incorrect", "false_success_reports": 0,
                                    "problems": ["mission_without_binding"]}
                continue
            robot = binding["robot_id"]
            if c.ground(robot):
                continue
            sub = Path(scratch) / mission
            for folder in ("input", "truth", "service", "service-export", "inbox/history", "aircraft"):
                (sub / folder).mkdir(parents=True)
            flown = c.case / "robots" / robot / "aircraft" / mission
            if flown.is_dir():
                shutil.copytree(flown, sub / "aircraft" / mission)
            for package in sorted((c.case / "robots" / robot / "inbox/history").glob(f"{mission}-v*.json")):
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
    """The X1 checks on the two PX4 SITL robots plus the M2 flight judge of every aircraft mission against Gazebo.

    两台 PX4 SITL 机器人上的 X1 检查，加上每个飞行器任务对 Gazebo 真值的 M2 飞行裁判。
    """
    c = S1Case(case, root)
    flights = judge_aircraft(c, use_replay=use_replay)
    problems: list[str] = [f"{mission}:{problem}" for mission, result in flights.items()
                           for problem in result["problems"] or []]
    false, wrong = check_success(c, problems, use_replay=use_replay)
    false += sum(result["false_success_reports"] or 0 for result in flights.values())
    counts = {"double_ownership": check_ownership(c, problems), "duplicate_execution": check_execution(c, problems),
              "lost_task": check_lost(c, problems), "handoff_protocol": check_protocol(c, problems),
              "false_success": false, "wrong_outcome": wrong, "ground_containment": check_containment(c, problems),
              "envelope_grant": check_envelopes(c, problems), "apron_conflict": check_apron(c, problems)}
    # Any M2 finding (mirror, signature, terminal state, geofence) is an integrity failure, as in P3 S1.
    # 任何 M2 发现（镜像、签名、终态、围栏）都是完整性失败，与 P3 S1 相同。
    unsafe = any(counts.values()) or any(r["classification"] == "unsafe_or_incorrect" for r in flights.values())
    found = derived(c)
    check_expected(c, found, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": "S1", "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts, "problems": problems,
            "expected": c.scenario.get("expected", {}), "derived": found, "metrics": metrics(c),
            "flights": len(c.flights), "missions": flights,
            "truth_samples": {robot: len(rows) for robot, rows in c.truth.items()},
            "armed_periods": {robot: armed_periods(case / "world" / f"{robot}-arming.json")
                              for robot in c.truth if c.ground(robot)},
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
    print(json.dumps({k: result.get(k) for k in ("scenario", "seed", "classification", "passed", "counts",
                                                  "problems", "error")}, indent=2, default=str))
    raise SystemExit(0 if result.get("passed") else 1)


if __name__ == "__main__":
    main()
