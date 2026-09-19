"""Build the explicitly authorized M1 simulation package; no planner or production issuer.

构建明确授权的 M1 仿真任务包；不作为规划器或生产签发者。
"""

import random
from datetime import timedelta

from drone_agent.contracts import (
    ApprovalRecord,
    EnergyBudget,
    Frame,
    MissionPackage,
    PackageNode,
    SpatialScope,
    TemporalWindow,
    utcnow,
)


def make_package(registry, seed: int, mission_id: str):
    rng = random.Random(seed)
    speed = round(rng.uniform(1.5, 2.5), 3)
    params = {
        "takeoff": {"altitude_m_agl": 4},
        "fly_route": {"route_id": "inspection", **registry.data["frame"], "speed_mps": speed},
        "capture_image": {"asset_id": "asset_red", "camera_id": "cam_0", "quality_profile_ref": "m1_campus_v1.image"},
        "return_home": {"home_ref": "home_v1", "return_route_id": "return"},
        "land": {"landing_site_id": "home_pad"},
    }
    nodes = []
    for action, values in params.items():
        manifest = registry.manifests["skill.flight." + action]
        nodes.append(
            PackageNode(
                task_id=action,
                skill_id=manifest.skill_id,
                skill_version=manifest.version,
                robot_id=registry.capability.robot_id,
                params=values,
                resources=manifest.resources,
                timeout_s=manifest.timeout_s,
                depends_on=[nodes[-1].task_id] if nodes else [],
                completion_evidence=[e.evidence_type for e in manifest.completion_evidence],
            )
        )
    now = utcnow()
    package = MissionPackage(
        mission_id=mission_id,
        mission_version=1,
        nodes=nodes,
        recovery_policy_ref=registry.data["simulation"]["policy_ref"],
        spatial_scope=SpatialScope(
            approved_volume_id=registry.data["approved_volume_id"], frame=Frame(**registry.data["frame"])
        ),
        temporal_window=TemporalWindow(not_before=now - timedelta(seconds=1), not_after=now + timedelta(hours=1)),
        energy_budget=EnergyBudget(max_consumption_fraction=0.6, reserve_fraction=0.2),
    )
    package.package_hash = package.compute_hash()
    package.approval = ApprovalRecord(
        approver="authorized_m1_simulation_runner",
        approved_at=now,
        mission_id=mission_id,
        mission_version=1,
        package_hash=package.package_hash,
        expires_at=now + timedelta(hours=1),
        allowed_robots=[registry.capability.robot_id],
    )
    return package
