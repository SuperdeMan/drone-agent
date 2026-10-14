"""Builders for X1 ground tests (D088): recheck specs, compiled and approved ground packages on x1_ground_a_v1.

X1 地面测试的构造器（D088）：基于 x1_ground_a_v1 的复核规格，以及编译并审批后的地面任务包。
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from drone_agent.admission.admission import AdmissionContext
from drone_agent.admission.airspace import SimulatedAirspaceProvider
from drone_agent.admission.ground import compile_ground
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
from drone_agent.mission.ground import GroundRegistry
from drone_agent.mission.registry import load_registry

ROOT = Path(__file__).resolve().parents[2]
SCENE = ROOT / "configs/scenarios/x1_ground_a_v1.yaml"
POLICY = "rover_campus@v1"
# Site a's asset_mid (5, 4) seen from the ground map, whose origin is site a's (2, -4). / 地面地图中的站点 a asset_mid。
MID = [3.0, 8.0]
COMPOUND = [8.5, 18.0]  # deep inside the fenced compound / 院子深处


@lru_cache(maxsize=1)
def ground() -> GroundRegistry:
    return load_registry(ROOT, SCENE)


def frame() -> Frame:
    return Frame(frame_id="map_enu", map_version="x1_ground_a_v1")


def recheck_spec(target=MID, *, kind: str = "point", radius_m: float = 0.0, **overrides) -> MissionSpec:
    now = utcnow()
    values = dict(
        mission_id="x1-test",
        mission_version=1,
        goal="recheck the anomaly the aircraft reported",
        goal_type=GoalType.RECHECK,
        targets=[TargetRef(asset_id="asset_mid", description="orange block")],
        spatial_scope=SpatialScope(approved_volume_id="x1_ground_a_v1", frame=frame()),
        temporal_window=TemporalWindow(not_before=now - timedelta(minutes=1), not_after=now + timedelta(minutes=30)),
        tasks=[TaskNode(task_id="recheck_asset_mid", skill_id="skill.ground.recheck",
                        params={"asset_id": "asset_mid", "target": list(target), "target_kind": kind,
                                "radius_m": radius_m, "signature": "orange"})],
        energy_budget=EnergyBudget(max_consumption_fraction=0.4, reserve_fraction=0.2),
        recovery_policy_ref=POLICY,
        provenance=Provenance(model_id="handoff/ho-test", prompt_version="deterministic", input_hash="0" * 64,
                              generated_at=now),
    )
    values.update(overrides)
    return MissionSpec(**values)


def admission_context(**overrides) -> AdmissionContext:
    values = dict(registry=ground(), capability=ground().capability, airspace=SimulatedAirspaceProvider(),
                  now=utcnow())
    values.update(overrides)
    return AdmissionContext(**values)


def approve(package, *, approver: str = "operator@example.test"):
    now = utcnow()
    package.approval = ApprovalRecord(approver=approver, approved_at=now - timedelta(seconds=1),
                                      mission_id=package.mission_id, mission_version=package.mission_version,
                                      package_hash=package.package_hash, expires_at=now + timedelta(minutes=30),
                                      allowed_robots=["ugv_01"])
    return package


def ground_package(target=MID, **spec_overrides):
    result = compile_ground(recheck_spec(target, **spec_overrides), ground(), robot_id="ugv_01")
    assert result.ok, result.issues
    return approve(result.package)
