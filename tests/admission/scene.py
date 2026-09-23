"""Builders for M2 scene tests: specs, requests and admission contexts on m2_campus_v2.

M2 场景测试的构造器：基于 m2_campus_v2 的规格、请求与准入上下文。
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from drone_agent.admission.admission import AdmissionContext
from drone_agent.admission.airspace import SimulatedAirspaceProvider
from drone_agent.admission.compiler import INSPECT, CompileContext
from drone_agent.admission.models import MissionRequest, RequestChannel
from drone_agent.contracts import (
    ApprovalRecord,
    EnergyBudget,
    Frame,
    GoalType,
    MissionSpec,
    Provenance,
    SpatialScope,
    TargetRef,
    TaskNode,
    TemporalWindow,
    utcnow,
)
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.runtime.permission import TrustLevel

ROOT = Path(__file__).resolve().parents[2]
POLICY = "multirotor_m1@v1"


@lru_cache(maxsize=1)
def registry() -> Registry:
    return Registry(ROOT, scene=ROOT / M2_SCENE)


def frame() -> Frame:
    return Frame(frame_id="map_enu", map_version="campus_v2")


def inspect_task(asset: str = "asset_red", task_id: str | None = None, depends_on=(), **params) -> TaskNode:
    return TaskNode(task_id=task_id or f"inspect_{asset}", skill_id=INSPECT, params={"asset_id": asset, **params},
                    depends_on=list(depends_on))


def spec(**overrides) -> MissionSpec:
    now = utcnow()
    values = dict(
        mission_id="m2-test",
        mission_version=1,
        goal="inspect the red equipment marker",
        goal_type=GoalType.INSPECT,
        targets=[TargetRef(asset_id="asset_red")],
        spatial_scope=SpatialScope(approved_volume_id="campus_training", frame=frame()),
        temporal_window=TemporalWindow(not_before=now - timedelta(minutes=1), not_after=now + timedelta(minutes=30)),
        tasks=[inspect_task()],
        energy_budget=EnergyBudget(max_consumption_fraction=0.78, reserve_fraction=0.2),
        recovery_policy_ref=POLICY,
        provenance=Provenance(model_id="MiniMax-M3", prompt_version="test", input_hash="0" * 64, generated_at=now),
    )
    values.update(overrides)
    return MissionSpec(**values)


def request(**overrides) -> MissionRequest:
    values = dict(
        request_id="req-test-0001",
        text="Inspect the red equipment marker.",
        requested_by="operator@example.test",
        trust_level=TrustLevel.FIRST_PARTY,
        channel=RequestChannel.CONSOLE,
        approved_volume_id="campus_training",
        idempotency_key="idem-0001",
        received_at=utcnow(),
    )
    values.update(overrides)
    return MissionRequest(**values)


def compile_context(**overrides) -> CompileContext:
    values = dict(registry=registry(), robot_id="uav_01")
    values.update(overrides)
    return CompileContext(**values)


def admission_context(**overrides) -> AdmissionContext:
    values = dict(registry=registry(), capability=registry().capability, airspace=SimulatedAirspaceProvider(),
                  now=utcnow(), request=request())
    values.update(overrides)
    return AdmissionContext(**values)


def approve(package, *, approver: str = "operator@example.test"):
    now = utcnow()
    package.approval = ApprovalRecord(approver=approver, approved_at=now - timedelta(seconds=1),
                                      mission_id=package.mission_id, mission_version=package.mission_version,
                                      package_hash=package.package_hash, expires_at=now + timedelta(minutes=30),
                                      allowed_robots=["uav_01"])
    return package
