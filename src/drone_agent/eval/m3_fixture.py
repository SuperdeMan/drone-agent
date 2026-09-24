"""Build the explicitly authorized M3 simulation packages; no planner or production issuer (D042).

Three mission shapes over the m3_campus_v3 registry, all under recovery policy multirotor_m3@v2:
`inspect_external` (takeoff, obstacle-aware approach and capture of the green asset in the external mode, return
along the registered detour, land), `goto_external` (two external-mode legs through the wall gap and back) and
`inspect_route` (the mission_upload fallback of the same inspection along the registered detour route). The seed
varies only the cruise speed.

构建明确授权的 M3 仿真任务包；不作为规划器或生产签发者（D042）。

基于 m3_campus_v3 登记表的三种任务，均使用恢复策略 multirotor_m3@v2：`inspect_external`（起飞，在外部模式下避障接近
并拍摄绿色资产，沿登记绕行航线返航，降落）、`goto_external`（两段外部模式飞行，绕过墙体再返回）与 `inspect_route`
（同一巡检沿登记绕行航线的 mission_upload 回退版本）。随机种子只改变巡航速度。
"""

from __future__ import annotations

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

SHAPES = ("inspect_external", "goto_external", "inspect_route")


def _steps(registry, shape: str, speed: float) -> list[tuple[str, str, dict]]:
    frame = registry.data["frame"]
    quality = f"{registry.registry_id}.image"
    takeoff = ("takeoff", "skill.flight.takeoff", {"altitude_m_agl": 4})
    land = ("land", "skill.flight.land", {"landing_site_id": "home_pad"})
    back = ("return_home", "skill.flight.return_home", {"home_ref": "home_v1", "return_route_id": "return_green"})
    if shape == "inspect_external":
        inspect = ("inspect_green", "skill.inspect.asset_local", {
            "asset_id": "asset_green", "approach_goal_id": "green_observe", "camera_id": "cam_0",
            "quality_profile_ref": quality, "speed_mps": speed, "max_captures": 3})
        return [takeoff, inspect, back, land]
    if shape == "goto_external":
        out = ("goto_green", "skill.flight.goto_local", {"goal_id": "green_observe", **frame, "speed_mps": speed})
        home = ("goto_home", "skill.flight.goto_local", {"goal_id": "home_hold", **frame, "speed_mps": speed})
        return [takeoff, out, home, land]
    if shape == "inspect_route":
        inspect = ("inspect_green", "skill.inspect.asset", {
            "asset_id": "asset_green", "approach_route_id": "observe_green", "camera_id": "cam_0",
            "quality_profile_ref": quality, "speed_mps": speed, "max_captures": 3})
        return [takeoff, inspect, back, land]
    raise ValueError(f"unknown M3 mission shape {shape!r}")


def make_m3_package(registry, seed: int, mission_id: str, shape: str = "inspect_external") -> MissionPackage:
    rng = random.Random(seed)
    speed = round(rng.uniform(1.5, 2.2), 3)
    nodes: list[PackageNode] = []
    for task_id, skill_id, params in _steps(registry, shape, speed):
        manifest = registry.manifests[skill_id]
        nodes.append(
            PackageNode(
                task_id=task_id,
                skill_id=manifest.skill_id,
                skill_version=manifest.version,
                robot_id=registry.capability.robot_id,
                params=params,
                resources=manifest.resources,
                timeout_s=manifest.timeout_s,
                depends_on=[nodes[-1].task_id] if nodes else [],
                completion_evidence=[e.evidence_type for e in manifest.completion_evidence],
            )
        )
    now = utcnow()
    budget = registry.data["mission_defaults"]["energy_budget"]
    package = MissionPackage(
        mission_id=mission_id,
        mission_version=1,
        nodes=nodes,
        recovery_policy_ref=registry.data["simulation"]["policy_ref"],
        spatial_scope=SpatialScope(
            approved_volume_id=registry.data["approved_volume_id"], frame=Frame(**registry.data["frame"])
        ),
        temporal_window=TemporalWindow(not_before=now - timedelta(seconds=1), not_after=now + timedelta(hours=1)),
        energy_budget=EnergyBudget(**budget),
    )
    package.package_hash = package.compute_hash()
    package.approval = ApprovalRecord(
        approver="authorized_m3_simulation_runner",
        approved_at=now,
        mission_id=mission_id,
        mission_version=1,
        package_hash=package.package_hash,
        expires_at=now + timedelta(hours=1),
        allowed_robots=[registry.capability.robot_id],
    )
    return package
