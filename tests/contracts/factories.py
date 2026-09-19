"""Small builders shared by the contract tests. Not fixtures: plain functions keep intent visible."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from drone_agent.contracts import (
    ApprovalRecord,
    CancelSpec,
    CapabilityDescriptor,
    ControlCommandEnvelope,
    ControlMode,
    Embodiment,
    EnergyBudget,
    Frame,
    GoalType,
    IdempotencyKey,
    Limits,
    MissionPackage,
    MissionSpec,
    PackageNode,
    Provenance,
    RecoveryBehavior,
    ResourceClaim,
    ResourceMode,
    SkillManifest,
    SkillRef,
    SpatialScope,
    TargetRef,
    TaskLease,
    TaskNode,
    TemporalWindow,
)

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


def frame() -> Frame:
    return Frame(frame_id="campus_enu", map_version="2026-09-01")


def provenance() -> Provenance:
    return Provenance(model_id="claude-opus-5", prompt_version="p1", input_hash="deadbeef", generated_at=NOW)


def mission_spec(**overrides) -> MissionSpec:
    base = dict(
        mission_id="m-001",
        mission_version=1,
        goal="inspect three assets in zone A",
        goal_type=GoalType.INSPECT,
        targets=[TargetRef(asset_id="asset_01")],
        spatial_scope=SpatialScope(approved_volume_id="inspection_volume_A", frame=frame()),
        temporal_window=TemporalWindow(not_before=NOW, not_after=NOW + timedelta(hours=1)),
        tasks=[
            TaskNode(task_id="t1", skill_id="skill.flight.takeoff", params={"altitude_m_agl": 20}),
            TaskNode(task_id="t2", skill_id="skill.inspect.asset", params={"asset_id": "asset_01"}, depends_on=["t1"]),
            TaskNode(task_id="t3", skill_id="skill.flight.land", depends_on=["t2"]),
        ],
        energy_budget=EnergyBudget(max_consumption_fraction=0.6, reserve_fraction=0.25),
        recovery_policy_ref="multirotor_campus@v1",
        provenance=provenance(),
    )
    base.update(overrides)
    return MissionSpec(**base)


def capability(
    *,
    robot_id: str = "uav_01",
    control_modes: set[ControlMode] | None = None,
    skills: list[str] | None = None,
    embodiment: Embodiment = Embodiment.AERIAL_MULTIROTOR,
    can_hover: bool = True,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        robot_id=robot_id,
        embodiment=embodiment,
        interface_version="0.1.0",
        control_modes=control_modes if control_modes is not None else {ControlMode.MISSION_UPLOAD},
        skills=[SkillRef(skill_id=s, version="0.1.0") for s in (skills or ["skill.flight.takeoff"])],
        limits=Limits(max_speed_mps=8.0, endurance_s=1500, can_hover=can_hover),
        recovery_behaviors={
            RecoveryBehavior.HOLD,
            RecoveryBehavior.RTL,
            RecoveryBehavior.LAND_HERE,
            RecoveryBehavior.LAND_AT,
            RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE,
        },
    )


def lease(*, epoch: int = 1, robot_id: str = "uav_01", ttl_s: float = 60.0) -> TaskLease:
    return TaskLease(
        robot_id=robot_id,
        mission_id="m-001",
        mission_version=1,
        lease_epoch=epoch,
        holder="executive@uav_01",
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=ttl_s),
    )


def envelope(*, epoch: int = 1, seq: int = 0, command_id: str = "c-1", robot_id: str = "uav_01") -> ControlCommandEnvelope:
    return ControlCommandEnvelope(
        key=IdempotencyKey(
            mission_id="m-001",
            mission_version=1,
            step_id="t1",
            command_id=command_id,
            robot_id=robot_id,
            lease_epoch=epoch,
        ),
        command_seq=seq,
        issued_at=NOW,
        valid_until=NOW + timedelta(seconds=1),
        intent_kind="mode_request",
        payload={"mode": "takeoff"},
    )


def motion_skill(skill_id: str = "skill.flight.fly_route") -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="0.1.0",
        embodiments=[Embodiment.AERIAL_MULTIROTOR],
        resources=[ResourceClaim(resource_id="uav_01.motion", mode=ResourceMode.EXCLUSIVE)],
        cancel=CancelSpec(cancel_safe_states=["running"], cancel_procedure="hold position, hand back to executive"),
        timeout_s=300,
    )


def package(*, approved: bool = True, hash_override: str | None = None) -> MissionPackage:
    pkg = MissionPackage(
        mission_id="m-001",
        mission_version=1,
        recovery_policy_ref="multirotor_campus@v1",
        nodes=[
            PackageNode(
                task_id="t1",
                skill_id="skill.flight.takeoff",
                skill_version="0.1.0",
                robot_id="uav_01",
                params={"altitude_m_agl": 20},
                timeout_s=60,
            )
        ],
    )
    pkg.package_hash = pkg.compute_hash()
    if approved:
        pkg.approval = ApprovalRecord(
            approver="operator@site",
            approved_at=NOW,
            mission_id="m-001",
            mission_version=1,
            package_hash=hash_override or pkg.package_hash,
            expires_at=NOW + timedelta(hours=2),
            allowed_robots=["uav_01"],
        )
    return pkg
