"""P1 operations resources: catalog, project directory, dock status, eligibility and reservations (D055).

The catalog is versioned configuration: projects, sites (each with its own onboard map), docks bound to a backend
principal, and robots bound to one dock. Memberships are a separate trusted input. Dock status is what a bound
backend reported, projected by the service; its source label comes from the catalog binding, never from the report.
`evaluate` is a pure function of one snapshot: it returns every reason, keeps blocked and unknown apart, and never
grants anything, because only a signed approval authorizes a flight (D032).

P1 运营资源：目录、项目目录、机场状态、可派遣判定与预约（D055）。

目录是版本化配置：项目、站点（各自有机载地图）、绑定后端身份的机场、绑定到一个机场的机器人。成员资格是单独
的受信输入。机场状态是绑定后端报告、经服务投影的内容；来源标签来自目录绑定，从不来自报告本身。`evaluate`
是单个快照的纯函数：返回全部原因、区分阻断与未知，且从不授予任何东西，因为只有签名审批才授权飞行（D032）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from drone_agent.contracts import CapabilityDescriptor, RobotStatus
from drone_agent.contracts.common import ContractModel
from drone_agent.fleet.provenance import digest
from drone_agent.runtime.permission import TRUST_LEVEL_CAPS, Caller, Role, TrustLevel, role_scopes

CATALOG_FORMAT = "drone.operations-catalog/v1"
MEMBERS_FORMAT = "drone.project-members/v1"
ID = r"^[a-z][a-z0-9_]{0,39}$"
PRINCIPAL = r"^(tailnet|local|a2a|harness):\S{1,200}$"
DOCK_PRINCIPAL = r"^dock:[A-Za-z0-9_.-]{1,80}$"


class ResourceModel(ContractModel):
    """Service-side resource record: unknown fields rejected, instances frozen. / 服务侧资源记录：拒绝未知字段、实例冻结。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["0.1.0"] = "0.1.0"


class LinkState(StrEnum):
    """Dock link as its backend sees it. / 后端所见的机场链路。"""

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class LidState(StrEnum):
    """Dock lid; `open` without a completed action result is not launch-ready. / 舱盖；没有完成回执的 open 不算可起飞。"""

    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    JAMMED = "jammed"
    UNKNOWN = "unknown"


class Presence(StrEnum):
    """What the pad sensor observes. / 机位传感器的观测。"""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class EnergyPhase(StrEnum):
    """Replenishment phase of the docked aircraft; `idle` is neither replenishing nor ready.

    在舱飞行器的补能阶段；`idle` 表示既未补能也未就绪。
    """

    IDLE = "idle"
    CHARGING = "charging"
    COOLING = "cooling"
    READY = "ready"
    FAULT = "fault"
    UNKNOWN = "unknown"


class EnvironmentState(StrEnum):
    """Site weather permission reported by the dock. / 机场报告的站点环境许可。"""

    PERMITTED = "permitted"
    DEFERRED = "deferred"
    UNKNOWN = "unknown"


class Upkeep(StrEnum):
    """Maintenance dimension; telemetry may set a lock but never clear it. / 运维维度；遥测可以加锁，不能解锁。"""

    NORMAL = "normal"
    MAINTENANCE = "maintenance"
    FAULT = "fault"


class SessionState(StrEnum):
    """A new boot is reconciled before its reports count. / 新 boot 会话先对账，其报告才计入。"""

    ACTIVE = "active"
    RECONCILING = "reconciling"


class DockAction(StrEnum):
    """Dock actions the service may request; an ACK is receipt, not completion. / 服务可请求的机场动作；ACK 只是受理。"""

    OPEN_LID = "open_lid"
    CLOSE_LID = "close_lid"
    START_CHARGE = "start_charge"


class ActionResult(StrEnum):
    """Outcome a later status report gives for a requested action. / 后续状态报告给出的动作结果。"""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class Verdict(StrEnum):
    """Eligibility verdict; unknown never dispatches. / 可派遣判定；unknown 永不派遣。"""

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Stage(StrEnum):
    """Where the same judgment is used: preview, before the lid opens, at the delivery claim.

    同一判定的使用点：预览、请求开盖前、交付领取时。
    """

    PREVIEW = "preview"
    PREPARE = "prepare"
    CLAIM = "claim"


class ReservationState(StrEnum):
    """Reservation lifecycle; only reconciliation releases a claimed hold. / 预约生命周期；已领取的持有只由对账释放。"""

    RESERVED = "reserved"
    OCCUPIED = "occupied"
    UNCERTAIN = "uncertain"
    RELEASED = "released"


# ── catalog / 目录 ──


class ProjectEntry(ResourceModel):
    """A project and its sites. / 项目及其站点。"""

    name: str = Field(min_length=1, max_length=120)
    sites: tuple[str, ...] = Field(min_length=1)


class LegacyProject(ResourceModel):
    """Owner of missions without a binding row, with the fixed first-party roles there.

    无绑定行任务的归属项目，以及第一方身份在该项目的固定角色。
    """

    project_id: str = Field(pattern=ID)
    first_party_roles: tuple[Role, ...] = ()


class DefaultBinding(ResourceModel):
    """Where the unscoped `submit` goes; membership is still checked. / 未带项目的 `submit` 的去向；仍检查成员资格。"""

    project_id: str = Field(pattern=ID)
    robot_id: str = Field(pattern=ID)


class SiteEntry(ResourceModel):
    """A site: its project, its onboard map and its weather limit. / 站点：所属项目、机载地图与风速上限。"""

    project_id: str = Field(pattern=ID)
    scene: str = Field(min_length=1, description="repository-relative map file / 仓库内相对路径的地图文件")
    max_wind_mps: float = Field(gt=0)


class DockBackendBinding(ResourceModel):
    """The only backend kind P1 accepts is a logical simulator (D055). / P1 只接受逻辑模拟后端（D055）。"""

    kind: Literal["logical_sim"]
    principal: str = Field(pattern=DOCK_PRINCIPAL)


class DockEntry(ResourceModel):
    """A dock, the robots it serves and the backend bound to report for it. / 机场、其服务的机器人与绑定的报告后端。"""

    site_id: str = Field(pattern=ID)
    vendor: str = Field(min_length=1)
    model: str = Field(min_length=1)
    serves: tuple[str, ...] = Field(min_length=1)
    actions: tuple[DockAction, ...] = Field(min_length=1)
    backend: DockBackendBinding


class RobotEntry(ResourceModel):
    """A robot fixed to one dock, with the execution backend of this deployment. / 固定于一个机场的机器人及其执行后端。"""

    site_id: str = Field(pattern=ID)
    dock_id: str = Field(pattern=ID)
    platform: str = Field(min_length=1)
    execution_backend: Literal["logical_sim", "px4_sitl"]


class DispatchPolicy(ResourceModel):
    """Versioned dispatch limits (09-operations §3). / 版本化派遣限值（09-operations §3）。"""

    version: str = Field(min_length=1)
    freshness_s: float = Field(default=3.0, gt=0)
    future_skew_s: float = Field(default=1.0, ge=0)
    reconcile_reports: int = Field(default=2, ge=1)
    soft_reservation_s: float = Field(default=900.0, gt=0)
    claim_ack_timeout_s: float = Field(default=30.0, gt=0)
    # A landing without the activity's result turns the hold uncertain only after this grace.
    # 落地后若迟迟没有本活动的结果，超过此宽限才把持有转为不确定。
    result_grace_s: float = Field(default=5.0, gt=0)


class OperationsCatalog(ResourceModel):
    """The versioned P1 catalog; every cross reference is checked on load. / 版本化的 P1 目录；加载时核对全部交叉引用。"""

    format: Literal["drone.operations-catalog/v1"]
    catalog_id: str = Field(pattern=ID)
    legacy_project: LegacyProject
    default_binding: DefaultBinding | None = None
    projects: dict[str, ProjectEntry]
    sites: dict[str, SiteEntry]
    docks: dict[str, DockEntry]
    robots: dict[str, RobotEntry]
    policy: DispatchPolicy

    @model_validator(mode="after")
    def _references(self):
        import re

        for group in (self.projects, self.sites, self.docks, self.robots):
            if any(not re.fullmatch(ID, key) for key in group):
                raise ValueError("catalog identifiers must be lower-case slugs")
        if self.legacy_project.project_id in self.projects:
            raise ValueError("the legacy project cannot also be a catalog project")
        listed = [site for project in self.projects.values() for site in project.sites]
        if len(listed) != len(set(listed)) or set(listed) != set(self.sites):
            raise ValueError("every site belongs to exactly one listed project")
        for project_id, project in self.projects.items():
            if any(self.sites[site].project_id != project_id for site in project.sites):
                raise ValueError("site project does not match its project listing")
        for dock_id, dock in self.docks.items():
            if dock.site_id not in self.sites:
                raise ValueError(f"{dock_id} names an unknown site")
            for robot_id in dock.serves:
                robot = self.robots.get(robot_id)
                if robot is None or robot.dock_id != dock_id or robot.site_id != dock.site_id:
                    raise ValueError(f"{dock_id} serves {robot_id} without a matching robot binding")
        for robot_id, robot in self.robots.items():
            dock = self.docks.get(robot.dock_id)
            if dock is None or robot_id not in dock.serves:
                raise ValueError(f"{robot_id} is not served by its dock")
        if set(self.robots) & set(self.docks):
            raise ValueError("robot and dock identifiers must be distinct")
        binding = self.default_binding
        if binding is not None and (binding.project_id not in self.projects
                                    or self.project_of(binding.robot_id) != binding.project_id):
            raise ValueError("the default binding must name a robot of its project")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    def project_of(self, robot_id: str) -> str | None:
        robot = self.robots.get(robot_id)
        return self.sites[robot.site_id].project_id if robot else None

    def resources(self, robot_id: str) -> tuple[str, str, str]:
        """The exclusive P1 resources of one activity on `robot_id`. / 在 `robot_id` 上一次活动的 P1 独占资源。"""
        dock = self.robots[robot_id].dock_id
        return f"{robot_id}.motion", f"{dock}.pad", f"{dock}.charger"

    def docks_of(self, principal: str) -> list[str]:
        """Docks this backend principal may report for. / 该后端身份可以报告的机场。"""
        return sorted(dock_id for dock_id, dock in self.docks.items() if dock.backend.principal == principal)

    def check_files(self, root: Path) -> None:
        """Every site map and platform file must exist in the checkout. / 每个站点地图与平台文件都必须存在。"""
        for path in [site.scene for site in self.sites.values()] + [r.platform for r in self.robots.values()]:
            if Path(path).is_absolute() or ".." in Path(path).parts or not (root / path).is_file():
                raise ValueError(f"catalog file {path} is missing or outside the repository")


def load_catalog(path: Path) -> OperationsCatalog:
    return OperationsCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# ── project directory / 项目目录 ──


class Membership(ResourceModel):
    """Roles of one principal in one project. / 一个身份在一个项目中的角色。"""

    principal: str = Field(pattern=PRINCIPAL)
    project_id: str = Field(pattern=ID)
    roles: tuple[Role, ...] = Field(min_length=1)


class MemberList(ResourceModel):
    """A trusted deployment input; never editable through the API. / 受信部署输入；不能经 API 修改。"""

    format: Literal["drone.project-members/v1"]
    members: tuple[Membership, ...] = ()


def load_members(path: Path) -> MemberList:
    return MemberList.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class ProjectDirectory:
    """Project scopes of a caller: roles from the member list, clipped by the trust cap.

    调用方的项目 scope：成员列表中的角色，再经信任上限裁剪。
    """

    def __init__(self, catalog: OperationsCatalog, members: MemberList):
        self.catalog = catalog
        known = set(catalog.projects) | {catalog.legacy_project.project_id}
        self._roles: dict[tuple[str, str], frozenset[Role]] = {}
        for entry in members.members:
            if entry.project_id not in known:
                raise ValueError(f"membership names an unknown project {entry.project_id}")
            key = (entry.principal, entry.project_id)
            self._roles[key] = self._roles.get(key, frozenset()) | frozenset(entry.roles)

    def roles(self, caller: Caller, project_id: str) -> frozenset[Role]:
        roles = set(self._roles.get((caller.identity, project_id), ()))
        legacy = self.catalog.legacy_project
        if project_id == legacy.project_id and caller.trust_level is TrustLevel.FIRST_PARTY:
            roles |= set(legacy.first_party_roles)
        return frozenset(roles)

    def scopes(self, caller: Caller, project_id: str) -> frozenset[str]:
        return role_scopes(self.roles(caller, project_id)) & TRUST_LEVEL_CAPS[caller.trust_level]

    def allows(self, caller: Caller, project_id: str, scope: str) -> bool:
        return scope in self.scopes(caller, project_id)

    def projects(self, caller: Caller) -> list[str]:
        """Projects where the caller holds any scope. / 调用方持有任意 scope 的项目。"""
        every = [*self.catalog.projects, self.catalog.legacy_project.project_id]
        return [project for project in every if self.scopes(caller, project)]


# ── dock status / 机场状态 ──


class EnergyReading(ResourceModel):
    """Replenishment phase and the measured charge of the docked aircraft. / 补能阶段与在舱飞行器的实测电量。"""

    state: EnergyPhase
    charge_fraction: float | None = Field(ge=0, le=1)


class EnvironmentReading(ResourceModel):
    state: EnvironmentState
    wind_mps: float | None = Field(ge=0)


class ActionProgress(ResourceModel):
    """What the dock says about one requested action. / 机场对一个已请求动作的说明。"""

    action_id: str = Field(min_length=8, max_length=80)
    result: ActionResult


class DockStatusReport(ResourceModel):
    """One backend report: all six dimensions plus the pad sensor are required. / 一份后端报告：六维与机位传感器都必须给出。"""

    dock_id: str = Field(pattern=ID)
    boot_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    seq: int = Field(ge=0)
    observed_at: AwareDatetime
    link: LinkState
    lid: LidState
    aircraft: Presence
    energy: EnergyReading
    environment: EnvironmentReading
    upkeep: Upkeep
    actions: tuple[ActionProgress, ...] = ()


class DockLock(ResourceModel):
    """A sticky maintenance or fault lock. / 粘滞的维护或故障锁。"""

    state: Literal["maintenance", "fault"]
    reason: str
    set_by: str
    set_at: AwareDatetime


class DockStatus(ResourceModel):
    """The service projection of the last accepted report. / 最新接受报告的服务投影。"""

    dock_id: str
    source: Literal["logical_sim"]
    session: SessionState
    complete_reports: int = Field(ge=0)
    report: DockStatusReport
    received_at: AwareDatetime
    lock: DockLock | None = None


# ── eligibility / 可派遣判定 ──

BLOCKED, UNKNOWN = Verdict.BLOCKED, Verdict.UNKNOWN
# Reason code -> the verdict it forces; a new code is a contract change. / 原因码 -> 其导致的判定；新增码属契约变更。
REASONS: dict[str, Verdict] = {
    "dock.status_missing": UNKNOWN,
    "dock.status_stale": UNKNOWN,
    "dock.session_reconciling": UNKNOWN,
    "dock.link_unknown": UNKNOWN,
    "dock.lid_unknown": UNKNOWN,
    "dock.presence_unknown": UNKNOWN,
    "energy.unknown": UNKNOWN,
    "environment.unknown": UNKNOWN,
    "robot.capability_unknown": UNKNOWN,
    "robot.phase_unknown": UNKNOWN,
    "dock.offline": BLOCKED,
    "dock.maintenance": BLOCKED,
    "dock.fault": BLOCKED,
    "dock.lid_jammed": BLOCKED,
    "dock.lid_not_open": BLOCKED,
    "dock.lid_open_unconfirmed": BLOCKED,
    "dock.aircraft_absent": BLOCKED,
    "presence.conflict": BLOCKED,
    "energy.idle": BLOCKED,
    "energy.charging": BLOCKED,
    "energy.cooling": BLOCKED,
    "energy.fault": BLOCKED,
    "energy.insufficient": BLOCKED,
    "environment.deferred": BLOCKED,
    "environment.wind": BLOCKED,
    "robot.airborne": BLOCKED,
    "robot.failsafe": BLOCKED,
    "robot.unbound": BLOCKED,
    "capability.missing_skill": BLOCKED,
    "reservation.conflict": BLOCKED,
    "reservation.missing": BLOCKED,
    "window.expired": BLOCKED,
}


class DispatchNeeds(ResourceModel):
    """What one mission version needs from its robot and dock. / 一个任务版本对机器人与机场的需求。"""

    skills: tuple[str, ...] = ()
    energy_fraction: float | None = Field(default=None, ge=0, le=1)
    not_after: AwareDatetime | None = None


class DispatchEligibility(ResourceModel):
    """A time-limited judgment with every reason and the snapshot it came from; never an authorization.

    带全部原因与输入快照的有时效判断；从不构成授权。
    """

    project_id: str | None
    robot_id: str
    dock_id: str | None
    stage: Stage
    activity_key: str | None = None
    verdict: Verdict
    reasons: tuple[str, ...]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_at: AwareDatetime
    valid_until: AwareDatetime


def evaluate(catalog: OperationsCatalog, robot_id: str, *, stage: Stage, now: datetime,
             needs: DispatchNeeds | None = None, capability: CapabilityDescriptor | None = None,
             robot_status: RobotStatus | None = None, dock: DockStatus | None = None,
             holders: dict[str, str] | None = None, activity: str | None = None,
             lid_confirmed: bool = False) -> DispatchEligibility:
    """Judge one snapshot; the same inputs always give the same verdict and reasons.

    `holders` maps each of the robot's resources to the activity key actively holding it. At the claim stage the
    lid must be open and `lid_confirmed` (this activity's open action completed per a later report), and this
    activity must hold every resource.

    判定一个快照；相同输入总得到相同的判定与原因。`holders` 把机器人的每项资源映射到当前有效持有它的活动键。
    领取阶段要求舱盖为 open 且 `lid_confirmed`（本活动的开盖动作已由后续报告证实完成），并且本活动持有全部资源。
    """
    policy, robot = catalog.policy, catalog.robots.get(robot_id)
    reasons: set[str] = set()
    holders = dict(holders or {})
    dock_id = robot.dock_id if robot else None
    if robot is None:
        reasons.add("robot.unbound")
    if dock is None or dock.dock_id != dock_id:
        reasons.add("dock.status_missing")
    else:
        report = dock.report
        age = (now - report.observed_at).total_seconds()
        if age > policy.freshness_s or age < -policy.future_skew_s:
            reasons.add("dock.status_stale")
        if dock.session is SessionState.RECONCILING:
            reasons.add("dock.session_reconciling")
        reasons |= {LinkState.OFFLINE: {"dock.offline"}, LinkState.UNKNOWN: {"dock.link_unknown"}}.get(report.link, set())
        for upkeep in (dock.lock.state if dock.lock else None, report.upkeep.value):
            if upkeep in ("maintenance", "fault"):
                reasons.add(f"dock.{upkeep}")
        if report.lid is LidState.JAMMED:
            reasons.add("dock.lid_jammed")
        elif report.lid is LidState.UNKNOWN:
            reasons.add("dock.lid_unknown")
        elif stage is Stage.CLAIM and report.lid is not LidState.OPEN:
            reasons.add("dock.lid_not_open")
        elif stage is Stage.CLAIM and not lid_confirmed:
            reasons.add("dock.lid_open_unconfirmed")
        if report.aircraft is Presence.ABSENT:
            reasons.add("dock.aircraft_absent")
        elif report.aircraft is Presence.UNKNOWN:
            reasons.add("dock.presence_unknown")
        energy = report.energy
        if energy.state is not EnergyPhase.READY:
            reasons.add(f"energy.{energy.state.value}")
        if energy.charge_fraction is None:
            reasons.add("energy.unknown")
        elif needs and needs.energy_fraction is not None and energy.charge_fraction < needs.energy_fraction:
            reasons.add("energy.insufficient")
        environment = report.environment
        if environment.state is EnvironmentState.DEFERRED:
            reasons.add("environment.deferred")
        elif environment.state is EnvironmentState.UNKNOWN or environment.wind_mps is None:
            reasons.add("environment.unknown")
        elif robot is not None:
            limits = [catalog.sites[robot.site_id].max_wind_mps]
            if capability is not None and capability.limits.max_wind_mps is not None:
                limits.append(capability.limits.max_wind_mps)
            if environment.wind_mps > min(limits):
                reasons.add("environment.wind")
    if capability is None:
        reasons.add("robot.capability_unknown")
    elif needs and capability.missing_for(skills=needs.skills):
        reasons.add("capability.missing_skill")
    if robot_status is not None:
        age = (now - robot_status.timestamp).total_seconds()
        if robot_status.flight_phase == "airborne":
            # The last word from the aircraft is airborne: never assume it came back. / 最后消息为空中：绝不假设已返回。
            reasons.add("robot.airborne")
            if dock is not None and dock.report.aircraft is Presence.PRESENT:
                reasons.add("presence.conflict")
        elif robot_status.flight_phase != "grounded":
            reasons.add("robot.phase_unknown")
        if robot_status.fc_failsafe_active and age <= policy.freshness_s:
            reasons.add("robot.failsafe")
    if robot is not None:
        owned = catalog.resources(robot_id)
        if any(holder != activity for resource, holder in holders.items() if resource in owned):
            reasons.add("reservation.conflict")
        if stage is Stage.CLAIM and (activity is None or any(holders.get(r) != activity for r in owned)):
            reasons.add("reservation.missing")
    if needs and needs.not_after is not None and needs.not_after <= now:
        reasons.add("window.expired")
    unknown = [code for code in reasons if code not in REASONS]
    if unknown:
        raise ValueError(f"uncontrolled eligibility reason {unknown}")
    ordered = tuple(sorted(reasons))
    verdict = (Verdict.BLOCKED if any(REASONS[c] is Verdict.BLOCKED for c in ordered)
               else Verdict.UNKNOWN if ordered else Verdict.ELIGIBLE)
    snapshot = digest({
        "catalog": catalog.sha256, "robot_id": robot_id, "stage": stage.value, "activity": activity,
        "needs": needs.model_dump(mode="json") if needs else None,
        "capability": capability.model_dump(mode="json") if capability else None,
        "robot_status": robot_status.model_dump(mode="json") if robot_status else None,
        "dock": dock.model_dump(mode="json") if dock else None, "holders": holders, "lid_confirmed": lid_confirmed,
        "now": now.isoformat(), "policy": policy.model_dump(mode="json")})
    valid_until = now
    if verdict is Verdict.ELIGIBLE and dock is not None:
        valid_until = dock.report.observed_at + timedelta(seconds=policy.freshness_s)
        if needs and needs.not_after is not None:
            valid_until = min(valid_until, needs.not_after)
    return DispatchEligibility(
        project_id=catalog.project_of(robot_id), robot_id=robot_id, dock_id=dock_id, stage=stage,
        activity_key=activity, verdict=verdict, reasons=ordered, snapshot_sha256=snapshot,
        policy_version=policy.version, catalog_sha256=catalog.sha256, evaluated_at=now, valid_until=valid_until)


# ── reservations / 预约 ──


class ResourceReservation(ResourceModel):
    """One activity's hold on its resources; release needs reconciled evidence. / 一次活动对其资源的持有；释放需要对账证据。"""

    reservation_id: str
    activity_key: str
    project_id: str
    mission_id: str
    mission_version: int = Field(ge=1)
    robot_id: str
    dock_id: str
    resources: tuple[str, ...]
    state: ReservationState
    expires_at: AwareDatetime | None = None
    reason: str = ""
    evidence: dict | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


def activity_key(mission_id: str, version: int) -> str:
    """Stable activity identity of one mission version. / 一个任务版本的稳定活动身份。"""
    return f"mission:{mission_id}:v{version}"
