"""Dispatch gate for P1: soft reservation, dock preparation, the delivery claim and release by reconciliation (D055).

An approved package is still only queued. Before the robot may take it, the service prepares the dock (requests the
lid to open once the same judgment passes at the prepare stage), and when the robot's uplink pulls deliveries the
claim gate re-checks, in one transaction, the cancel intent, the approval's expiry, the execution backend, the
claim-stage eligibility and this activity's reservation. Only then is the delivery handed out and the reservation
occupied. A reservation is released only by reconciliation: an authoritative terminal outcome of this activity, a
grounded status after it and a fresh dock observation of the aircraft on the pad, exactly once.

P1 派遣闸门：软预约、机场准备、交付领取与对账释放（D055）。

已批准的任务包仍然只是排队。机器人取走之前，服务先准备机场（同一判定在准备阶段通过后请求开盖）；机器人的
uplink 拉取投递时，领取闸门在一个事务内重查取消意图、审批有效期、执行后端、领取阶段的可派遣判定与本活动的
预约，全部通过才交付并把预约转为占用。预约只由对账释放：本活动的权威终态、其后的地面状态，以及机场对机位上
飞行器的新鲜观测，恰好释放一次。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from drone_agent.contracts import ApprovalRecord, CapabilityDescriptor, MissionPackage, utcnow
from drone_agent.fleet.catalog import Catalog
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import OperationsStore, migrate, soft_expiry
from drone_agent.fleet.provenance import project
from drone_agent.fleet.resources import (
    DispatchEligibility,
    DispatchNeeds,
    EnergyPhase,
    LidState,
    MemberList,
    OperationsCatalog,
    Presence,
    ProjectDirectory,
    ReservationState,
    SessionState,
    Stage,
    Verdict,
    activity_key,
    evaluate,
    load_catalog,
    load_members,
)
from drone_agent.mission.registry import Registry

OCCUPYING = (ReservationState.OCCUPIED.value, ReservationState.UNCERTAIN.value)


@dataclass
class Operations:
    """Everything the service needs in catalog mode. / 目录模式下服务需要的全部内容。"""

    catalog: OperationsCatalog
    directory: ProjectDirectory
    store: OperationsStore
    registries: dict[str, Registry]
    capabilities: dict[str, CapabilityDescriptor]
    migration: dict = field(default_factory=dict)

    def registry(self, robot_id: str) -> Registry:
        return self.registries[self.catalog.robots[robot_id].site_id]


def static_capability(root: Path, platform: str, robot_id: str) -> CapabilityDescriptor:
    """The platform's static capability for one robot, flagged `static` by the catalog. / 平台静态能力，目录标为 static。"""
    data = yaml.safe_load((root / platform).read_text(encoding="utf-8"))["capability"]
    base = data["robot_id"]
    for sensor in data.get("sensors", []):
        if sensor.get("frame_id", "").startswith(base + "/"):
            sensor["frame_id"] = robot_id + sensor["frame_id"][len(base):]
    return CapabilityDescriptor.model_validate({**data, "robot_id": robot_id})


def build_operations(root: Path, ledger: BusinessLedger, catalog_path: Path, members_path: Path | None, *,
                     backups: Path | None, clock=utcnow) -> Operations:
    """Load and check the catalog, migrate the ledger (D056) and open the store. / 加载并核对目录、迁移账本并打开存储。"""
    catalog = load_catalog(catalog_path)
    catalog.check_files(root)
    members = load_members(members_path) if members_path else MemberList(format="drone.project-members/v1")
    directory = ProjectDirectory(catalog, members)
    migration = migrate(ledger, backups=backups, clock=clock)
    store = OperationsStore(ledger, catalog, clock=clock)
    registries = {site_id: Registry(root, scene=root / site.scene) for site_id, site in catalog.sites.items()}
    capabilities = {robot_id: static_capability(root, robot.platform, robot_id)
                    for robot_id, robot in catalog.robots.items()}
    return Operations(catalog, directory, store, registries, capabilities, migration)


def needs_of(record: dict) -> DispatchNeeds:
    """What a mission version needs: its skills, its energy budget and the end of its window.

    任务版本的需求：技能、能源预算与窗口终点。
    """
    package = MissionPackage.model_validate(record["package"])
    not_after = package.temporal_window.not_after
    if record.get("approval"):
        not_after = min(not_after, ApprovalRecord.model_validate(record["approval"]).expires_at)
    budget = package.energy_budget
    return DispatchNeeds(skills=tuple(sorted({node.skill_id for node in package.nodes})),
                         energy_fraction=min(1.0, budget.max_consumption_fraction + budget.reserve_fraction),
                         not_after=not_after)


class Dispatch:
    """The stateful half of P1 dispatch; `terminal` reports an activity's authoritative end from the service.

    P1 派遣的有状态部分；`terminal` 由服务报告活动的权威结束。
    """

    def __init__(self, ops: Operations, ledger: BusinessLedger, robots: Catalog, *, clock=utcnow,
                 terminal: Callable[[str, int], tuple[str, datetime] | None] = lambda m, v: None):
        self.ops, self.ledger, self.robots, self.clock, self.terminal = ops, ledger, robots, clock, terminal
        self.store, self.catalog = ops.store, ops.catalog
        self.touched: set[str] = set()

    # ── judgments / 判定 ──

    def judge(self, robot_id: str, *, stage: Stage, needs: DispatchNeeds | None = None,
              activity: str | None = None) -> DispatchEligibility:
        capability, _ = self.robots.capability(robot_id)
        dock_id = self.catalog.robots[robot_id].dock_id if robot_id in self.catalog.robots else None
        opened = self.store.action_for(activity, "open_lid") if activity else None
        return evaluate(self.catalog, robot_id, stage=stage, now=self.clock(), needs=needs, capability=capability,
                        robot_status=self.robots.status(robot_id),
                        dock=self.store.dock_status(dock_id) if dock_id else None,
                        holders=self.store.holders(self.catalog.resources(robot_id)) if dock_id else {},
                        activity=activity, lid_confirmed=bool(opened and opened["state"] == "completed"))

    def record(self, eligibility: DispatchEligibility, mission_id: str, version: int) -> str:
        """Append a decision only when its verdict or reasons changed. / 只在判定或原因变化时追加记录。"""
        last = self.store.last_decision(mission_id, version, eligibility.stage.value)
        if last and last["verdict"] == eligibility.verdict.value and \
                tuple(last["body"]["reasons"]) == eligibility.reasons:
            return last["decision_id"]
        self.touched.add(mission_id)
        return self.store.record_decision(eligibility, mission_id=mission_id, version=version)

    def preview(self, mission_id: str, version: int) -> DispatchEligibility | None:
        binding, record = self.store.binding(mission_id), self.ledger.version(mission_id, version)
        if binding is None or record is None or not record["package"]:
            return None
        eligibility = self.judge(binding["robot_id"], stage=Stage.PREVIEW, needs=needs_of(record),
                                 activity=activity_key(mission_id, version))
        self.record(eligibility, mission_id, version)
        return eligibility

    # ── reservations / 预约 ──

    def reserve(self, mission_id: str, version: int) -> tuple[object | None, dict[str, str]]:
        binding, record = self.store.binding(mission_id), self.ledger.version(mission_id, version)
        not_after = needs_of(record).not_after if record and record["package"] else None
        return self.store.reserve(activity=activity_key(mission_id, version), project_id=binding["project_id"],
                                  mission_id=mission_id, version=version, robot_id=binding["robot_id"],
                                  expires_at=soft_expiry(self.catalog, self.clock(), not_after))

    def release_unclaimed(self, mission_id: str, version: int, reason: str) -> bool:
        """Nothing physical happened before a claim, so an unclaimed hold may be released. / 领取前无物理动作，可释放。"""
        released = self.store.transition(activity_key(mission_id, version), ("reserved",), "released", reason)
        if released:
            self.touched.add(mission_id)
        return released

    # ── the claim gate / 领取闸门 ──

    def _package_deliveries(self) -> list[dict]:
        return [row for row in self.ledger._rows(
            "SELECT * FROM deliveries WHERE kind='mission_package' AND acked_at IS NULL ORDER BY cursor")]

    def _void(self, delivery: dict, reason: str, decision_id: str | None = None) -> None:
        mission_id, version = delivery["mission_id"], delivery["version"]
        with self.store.transaction():
            if self.store.record_claim(delivery["delivery_id"], mission_id, version, "void", reason, decision_id):
                self.store.transition(activity_key(mission_id, version), ("reserved",), "released", f"void:{reason}")
                self.store.event(f"mission:{mission_id}", "delivery.void", "service",
                                 {"delivery_id": delivery["delivery_id"], "version": version, "reason": reason})
        self.touched.add(mission_id)

    def _terminal_problem(self, delivery: dict) -> str | None:
        """Cancel, lapsed approval or a backend mismatch void a delivery for good. / 取消、审批过期或后端不符永久作废。"""
        mission_id, version = delivery["mission_id"], delivery["version"]
        if self.store.cancel_intent(mission_id) is not None:
            return "cancelled"
        record = self.ledger.version(mission_id, version)
        approval = ApprovalRecord.model_validate(record["approval"]) if record and record["approval"] else None
        if approval is None or approval.expires_at <= self.clock():
            return "approval_expired"
        run = project(record).run
        binding = self.store.binding(mission_id)
        if run is None or run.execution_backend != binding["execution_backend"]:
            return "backend_mismatch"
        return None

    def gate(self, robot_id: str, delivery: dict) -> bool:
        """Whether this pull may hand the delivery out; legacy missions keep the M2 path. / 本次拉取能否交付；legacy 任务沿用 M2。"""
        if delivery["kind"] != "mission_package":
            return True
        mission_id, version = delivery["mission_id"], delivery["version"]
        binding = self.store.binding(mission_id)
        if binding is None:
            return True
        with self.store.transaction():
            claim = self.store.claim(delivery["delivery_id"])
            if claim is not None:
                return claim["state"] == "claimed"
            problem = self._terminal_problem(delivery)
            if problem is not None:
                self._void(delivery, problem)
                return False
            activity = activity_key(mission_id, version)
            reservation = self.store.reservation(activity)
            if reservation is None or reservation.state is ReservationState.RELEASED:
                self.reserve(mission_id, version)
            record = self.ledger.version(mission_id, version)
            eligibility = self.judge(robot_id, stage=Stage.CLAIM, needs=needs_of(record), activity=activity)
            decision = self.record(eligibility, mission_id, version)
            if eligibility.verdict is not Verdict.ELIGIBLE:
                return False
            if not self.store.transition(activity, ("reserved",), "occupied", "claimed",
                                         {"delivery_id": delivery["delivery_id"], "decision_id": decision,
                                          "snapshot_sha256": eligibility.snapshot_sha256}):
                return False
            self.store.record_claim(delivery["delivery_id"], mission_id, version, "claimed", "eligible", decision)
            self.store.event(f"mission:{mission_id}", "delivery.claimed", "service",
                             {"delivery_id": delivery["delivery_id"], "version": version, "decision_id": decision})
        self.touched.add(mission_id)
        return True

    # ── background work / 后台工作 ──

    def prepare(self) -> None:
        """Void what can never fly, keep holds fresh and ask the dock to open for eligible work.

        作废永远不能飞的投递，保持持有有效，并为可派遣的工作请求开盖。
        """
        for delivery in self._package_deliveries():
            mission_id, version = delivery["mission_id"], delivery["version"]
            binding = self.store.binding(mission_id)
            if binding is None or self.store.claim(delivery["delivery_id"]) is not None:
                continue
            problem = self._terminal_problem(delivery)
            if problem is not None:
                self._void(delivery, problem)
                continue
            activity = activity_key(mission_id, version)
            reservation = self.store.reservation(activity)
            if reservation is None or reservation.state is ReservationState.RELEASED or (
                    reservation.expires_at and reservation.expires_at <= self.clock()):
                self.reserve(mission_id, version)
            record = self.ledger.version(mission_id, version)
            eligibility = self.judge(binding["robot_id"], stage=Stage.PREPARE, needs=needs_of(record),
                                     activity=activity)
            self.record(eligibility, mission_id, version)
            if eligibility.verdict is Verdict.ELIGIBLE and self.store.action_for(activity, "open_lid") is None:
                action = self.store.request_action(binding["dock_id"], "open_lid", activity)
                self.store.event(f"dock:{binding['dock_id']}", "action.requested", "service",
                                 {"action_id": action["action_id"], "kind": "open_lid", "activity": activity})
                self.touched.add(mission_id)

    def release_evidence(self, reservation) -> tuple[str, str, dict | None]:
        """("release" | "uncertain" | "hold", reason, evidence) for one occupying reservation.

        对一个占用中的预约给出（释放 | 不确定 | 保持，原因，证据）。
        """
        now, policy = self.clock(), self.catalog.policy
        claim = next((c for c in self.store.claims(reservation.mission_id)
                      if c["mission_version"] == reservation.mission_version and c["state"] == "claimed"), None)
        terminal = self.terminal(reservation.mission_id, reservation.mission_version)
        status = self.robots.status(reservation.robot_id)
        dock = self.store.dock_status(reservation.dock_id)
        fresh = dock is not None and abs((now - dock.report.observed_at).total_seconds()) <= policy.freshness_s
        if terminal is None:
            if claim is None:
                return "hold", "unclaimed", None
            claimed_at = datetime.fromisoformat(claim["decided_at"])
            delivery = self.ledger.delivery(claim["delivery_id"])
            if delivery and delivery["acked_at"] is None and now - claimed_at > timedelta(
                    seconds=policy.claim_ack_timeout_s):
                return "uncertain", "ack_missing", None
            landed = fresh and dock.report.aircraft is Presence.PRESENT and status is not None and \
                status.flight_phase == "grounded" and status.timestamp > claimed_at and self._departed(reservation)
            if landed and (now - status.timestamp).total_seconds() > policy.result_grace_s:
                return "uncertain", "landed_without_result", None
            return "hold", "in_progress", None
        kind, at = terminal
        problems = []
        if status is not None and status.flight_phase == "airborne":
            problems.append("robot_airborne")
        elif kind == "mission_result" and (status is None or status.flight_phase != "grounded" or status.timestamp < at):
            problems.append("awaiting_grounded_status")
        if dock is None:
            problems.append("dock_status_missing")
        else:
            if not fresh:
                problems.append("dock_status_stale")
            if dock.session is SessionState.RECONCILING:
                problems.append("dock_session_reconciling")
            if dock.report.aircraft is not Presence.PRESENT:
                problems.append("aircraft_not_present")
            if dock.report.observed_at < at:
                problems.append("dock_observation_before_terminal")
            if status is not None and status.flight_phase == "airborne" and dock.report.aircraft is Presence.PRESENT:
                problems.append("presence_conflict")
        if problems:
            return "uncertain", ",".join(problems), None
        return "release", "reconciled", {
            "terminal": {"kind": kind, "at": at.isoformat()},
            "robot_status_at": status.timestamp.isoformat() if status else None,
            "dock": {"boot_id": dock.report.boot_id, "seq": dock.report.seq,
                     "observed_at": dock.report.observed_at.isoformat()}}

    def _departed(self, reservation) -> bool:
        """This activity was seen leaving the pad after its claim. / 领取后曾看到本活动离开机位。"""
        return any(event["kind"] == "activity.departed" for event in self.store.events(reservation.activity_key))

    def observe(self) -> None:
        """Record, per claimed activity, the first sign that the aircraft left: an airborne status or an empty pad.

        按已领取活动记录飞行器离开的首个迹象：空中状态或空机位。
        """
        for reservation in self.store.reservations(states=OCCUPYING):
            claim = next((c for c in self.store.claims(reservation.mission_id)
                          if c["mission_version"] == reservation.mission_version and c["state"] == "claimed"), None)
            if claim is None or self._departed(reservation):
                continue
            claimed_at = datetime.fromisoformat(claim["decided_at"])
            status = self.robots.status(reservation.robot_id)
            dock = self.store.dock_status(reservation.dock_id)
            seen = None
            if status is not None and status.flight_phase == "airborne" and status.timestamp >= claimed_at:
                seen = {"robot_status_at": status.timestamp.isoformat()}
            elif dock is not None and dock.report.aircraft is Presence.ABSENT and dock.report.observed_at >= claimed_at:
                seen = {"dock_observed_at": dock.report.observed_at.isoformat(), "seq": dock.report.seq}
            if seen is not None:
                self.store.event(reservation.activity_key, "activity.departed", "service", seen)

    def reconcile(self) -> None:
        """Release with evidence or mark uncertain; never release on silence or restart. / 有证据才释放，沉默或重启从不释放。"""
        for reservation in self.store.reservations(states=OCCUPYING):
            decision, reason, evidence = self.release_evidence(reservation)
            activity = reservation.activity_key
            if decision == "release":
                if self.store.transition(activity, OCCUPYING, "released", reason, evidence):
                    self.touched.add(reservation.mission_id)
            elif decision == "uncertain" and (reservation.state is ReservationState.OCCUPIED
                                              or reservation.reason != reason):
                if self.store.transition(activity, OCCUPYING, "uncertain", reason):
                    self.touched.add(reservation.mission_id)
            elif decision == "hold" and reservation.state is ReservationState.UNCERTAIN and \
                    reservation.reason == "ack_missing" and reason == "in_progress":
                self.store.transition(activity, ("uncertain",), "occupied", "ack_received")

    def chores(self) -> None:
        """After the last activity of a dock ended, close the lid and start charging. / 机场最近活动结束后关盖并开始充电。"""
        for dock_id in self.catalog.docks:
            dock = self.store.dock_status(dock_id)
            if dock is None or self.store.holders([f"{dock_id}.pad"]):
                continue
            if abs((self.clock() - dock.report.observed_at).total_seconds()) > self.catalog.policy.freshness_s:
                continue
            ended = self.store.reservations(dock_id=dock_id, states=("released",))
            if not ended:
                continue
            activity = max(ended, key=lambda r: r.updated_at).activity_key
            report = dock.report
            kind = None
            if report.lid in (LidState.OPEN, LidState.OPENING):
                kind = "close_lid"
            elif report.lid is LidState.CLOSED and report.aircraft is Presence.PRESENT and \
                    report.energy.state is EnergyPhase.IDLE:
                kind = "start_charge"
            if kind and self.store.action_for(activity, kind) is None:
                action = self.store.request_action(dock_id, kind, activity)
                self.store.event(f"dock:{dock_id}", "action.requested", "service",
                                 {"action_id": action["action_id"], "kind": kind, "activity": activity})

    def tick(self) -> set[str]:
        """One background pass; returns missions whose dispatch state changed. / 一次后台处理；返回派遣状态有变化的任务。"""
        self.observe()
        self.prepare()
        self.reconcile()
        self.chores()
        touched, self.touched = self.touched, set()
        return touched
