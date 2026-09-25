"""P1 resource contracts: catalog and member validation, project scopes, report strictness and eligibility (D055).

P1 资源契约：目录与成员校验、项目 scope、报告严格性与可派遣判定（D055）。
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from drone_agent.contracts import EnergyState, LocalizationHealth, RobotStatus, utcnow
from drone_agent.contracts.capability import CommsState
from drone_agent.fleet.dispatch import static_capability
from drone_agent.fleet.resources import (
    REASONS,
    DispatchNeeds,
    DockLock,
    DockStatus,
    DockStatusReport,
    MemberList,
    OperationsCatalog,
    ProjectDirectory,
    SessionState,
    Stage,
    Verdict,
    evaluate,
    load_catalog,
    load_members,
)
from drone_agent.runtime.permission import (
    MISSION_APPROVE,
    MISSION_OPERATE,
    MISSION_READ,
    MISSION_SUBMIT,
    RESOURCE_MAINTAIN,
    Caller,
    TrustLevel,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "configs/sites/p1_campus_v1.yaml"


def raw() -> dict:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


def catalog() -> OperationsCatalog:
    return load_catalog(CATALOG)


def test_the_versioned_catalogs_load_and_name_existing_files():
    for name in ("p1_campus_v1", "p1_s1_v1"):
        value = load_catalog(ROOT / f"configs/sites/{name}.yaml")
        value.check_files(ROOT)
        assert value.sha256 == load_catalog(ROOT / f"configs/sites/{name}.yaml").sha256
    assert catalog().resources("uav_b") == ("uav_b.motion", "dock_b.pad", "dock_b.charger")
    assert catalog().docks_of("dock:p1-s0-sim") == ["dock_a", "dock_b", "dock_c"]


@pytest.mark.parametrize("mutate", [
    lambda d: d["docks"]["dock_a"]["backend"].update(kind="real_device"),
    lambda d: d["docks"]["dock_a"].update(source="real_device"),
    lambda d: d["projects"]["harbor_ops"]["sites"].append("site_a"),
    lambda d: d["robots"]["uav_a"].update(dock_id="dock_b"),
    lambda d: d["robots"]["uav_a"].update(execution_backend="real_device"),
    lambda d: d.update(default_binding={"project_id": "harbor_ops", "robot_id": "uav_a"}),
    lambda d: d["projects"].update(legacy_m2={"name": "clash", "sites": ["site_h"]}),
    lambda d: d["sites"].update({"Site-X": d["sites"]["site_a"]}),
])
def test_catalogs_with_real_backends_or_broken_references_are_refused(mutate):
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError):
        OperationsCatalog.model_validate(data)


def test_catalog_files_must_stay_inside_the_repository(tmp_path):
    data = raw()
    data["sites"]["site_a"]["scene"] = "../outside.yaml"
    with pytest.raises(ValueError):
        OperationsCatalog.model_validate(data).check_files(ROOT)


def test_site_maps_differ_from_m2_only_in_identity_and_pad_owner():
    m2 = yaml.safe_load((ROOT / "configs/scenarios/m2_campus_v2.yaml").read_text(encoding="utf-8"))
    for site, robot in (("a", "uav_a"), ("b", "uav_b"), ("c", "uav_c"), ("h", "uav_h")):
        derived = yaml.safe_load((ROOT / f"configs/scenarios/p1_site_{site}_v1.yaml").read_text(encoding="utf-8"))
        expected = copy.deepcopy(m2)
        expected["registry_id"] = f"p1_site_{site}_v1"
        expected["landing_sites"]["home_pad"]["reserved_for"] = robot
        assert derived == expected


def test_static_capability_is_the_platform_with_the_robot_identity():
    capability = static_capability(ROOT, "configs/platforms/px4_sitl_multirotor.yaml", "uav_c")
    assert capability.robot_id == "uav_c"
    assert {s.frame_id for s in capability.sensors} == {"uav_c/base_link", "uav_c/cam_0"}


def members(*entries) -> MemberList:
    return MemberList(format="drone.project-members/v1", members=[
        {"principal": p, "project_id": project, "roles": roles} for p, project, roles in entries])


def test_roles_map_to_scopes_inside_their_project_only():
    directory = ProjectDirectory(catalog(), members(("tailnet:ops@x", "campus_ops", ["operator"]),
                                                    ("tailnet:ops@x", "harbor_ops", ["viewer"]),
                                                    ("tailnet:boss@x", "campus_ops", ["admin"])))
    operator = Caller("tailnet:ops@x", TrustLevel.FIRST_PARTY)
    assert {MISSION_SUBMIT, MISSION_OPERATE, MISSION_READ} <= directory.scopes(operator, "campus_ops")
    assert MISSION_APPROVE not in directory.scopes(operator, "campus_ops")
    assert directory.scopes(operator, "harbor_ops") >= {MISSION_READ}
    assert MISSION_SUBMIT not in directory.scopes(operator, "harbor_ops")
    admin = Caller("tailnet:boss@x", TrustLevel.FIRST_PARTY)
    assert RESOURCE_MAINTAIN in directory.scopes(admin, "campus_ops")
    assert MISSION_APPROVE not in directory.scopes(admin, "campus_ops"), "admin never approves flights"


def test_the_trust_cap_clips_roles_and_legacy_rights_are_first_party_only():
    directory = ProjectDirectory(catalog(), members(("a2a:client", "campus_ops", ["operator", "approver"])))
    agent = Caller("a2a:client", TrustLevel.THIRD_PARTY)
    assert directory.scopes(agent, "campus_ops") == {MISSION_SUBMIT, MISSION_READ}
    person = Caller("tailnet:anyone@x", TrustLevel.FIRST_PARTY)
    assert directory.scopes(person, "legacy_m2") >= {MISSION_READ}
    assert MISSION_SUBMIT not in directory.scopes(person, "legacy_m2")
    assert directory.projects(person) == ["legacy_m2"]
    assert directory.projects(Caller("anonymous", TrustLevel.ANONYMOUS)) == []


def test_memberships_for_unknown_projects_are_refused():
    with pytest.raises(ValueError):
        ProjectDirectory(catalog(), members(("tailnet:ops@x", "campus_s1", ["viewer"])))
    load_members(ROOT / "configs/sites/p1_members_s0.yaml")
    ProjectDirectory(load_catalog(ROOT / "configs/sites/p1_s1_v1.yaml"),
                     load_members(ROOT / "configs/sites/p1_members_s1.yaml"))


def report(**overrides) -> dict:
    now = overrides.pop("now", utcnow())
    value = {"dock_id": "dock_a", "boot_id": "boot-dock_a-0001", "seq": 3, "observed_at": now.isoformat(),
             "link": "online", "lid": "closed", "aircraft": "present",
             "energy": {"state": "ready", "charge_fraction": 1.0},
             "environment": {"state": "permitted", "wind_mps": 2.0}, "upkeep": "normal", "actions": []}
    for key, item in overrides.items():
        value[key] = item
    return value


@pytest.mark.parametrize("change", [
    {"source": "real_device"}, {"lid": "ajar"}, {"energy": {"state": "ready"}},
    {"observed_at": "2026-09-26T00:00:00"}, {"seq": -1},
])
def test_reports_are_complete_typed_and_closed(change):
    with pytest.raises(ValidationError):
        DockStatusReport.model_validate(report(**change))
    missing = report()
    missing.pop("upkeep")
    with pytest.raises(ValidationError):
        DockStatusReport.model_validate(missing)


def dock(now, *, session=SessionState.ACTIVE, lock=None, **overrides) -> DockStatus:
    return DockStatus(dock_id="dock_a", source="logical_sim", session=session, complete_reports=3,
                      report=report(now=now, **overrides), received_at=now, lock=lock)


def status(now, phase="grounded", *, failsafe=False, age=0.0) -> RobotStatus:
    return RobotStatus(robot_id="uav_a", timestamp=now - timedelta(seconds=age), flight_phase=phase,
                       energy=EnergyState(remaining_fraction=1.0), localization=LocalizationHealth(healthy=True),
                       comms=CommsState(uplink_ok=True), fc_failsafe_active=failsafe)


def judge(stage=Stage.PREVIEW, *, now=None, needs=None, dock_status="default", robot=None, holders=None,
          activity=None, lid_confirmed=False, capability="default"):
    now = now or utcnow()
    value = catalog()
    return evaluate(value, "uav_a", stage=stage, now=now,
                    needs=needs or DispatchNeeds(skills=("skill.inspect.asset",), energy_fraction=0.98,
                                                 not_after=now + timedelta(minutes=10)),
                    capability=static_capability(ROOT, "configs/platforms/px4_sitl_multirotor.yaml", "uav_a")
                    if capability == "default" else capability,
                    robot_status=robot, dock=dock(now) if dock_status == "default" else dock_status,
                    holders=holders, activity=activity, lid_confirmed=lid_confirmed)


def test_a_healthy_snapshot_is_eligible_but_only_for_its_freshness_window():
    now = utcnow()
    result = judge(now=now)
    assert (result.verdict, result.reasons) == (Verdict.ELIGIBLE, ())
    assert result.valid_until == now + timedelta(seconds=3)
    assert judge(now=now).snapshot_sha256 == result.snapshot_sha256
    assert judge(now=now, robot=status(now)).snapshot_sha256 != result.snapshot_sha256


NOW = utcnow()


@pytest.mark.parametrize("kwargs,reason", [
    ({"dock_status": dock(NOW, link="offline")}, "dock.offline"),
    ({"dock_status": dock(NOW, upkeep="maintenance")}, "dock.maintenance"),
    ({"dock_status": dock(NOW, lock=DockLock(state="maintenance", reason="r", set_by="dock:x", set_at=NOW))},
     "dock.maintenance"),
    ({"dock_status": dock(NOW, upkeep="fault")}, "dock.fault"),
    ({"dock_status": dock(NOW, lid="jammed")}, "dock.lid_jammed"),
    ({"stage": Stage.CLAIM, "holders": {r: "a" for r in ("uav_a.motion", "dock_a.pad", "dock_a.charger")},
      "activity": "a"}, "dock.lid_not_open"),
    ({"stage": Stage.CLAIM, "dock_status": dock(NOW, lid="open"), "activity": "a",
      "holders": {r: "a" for r in ("uav_a.motion", "dock_a.pad", "dock_a.charger")}}, "dock.lid_open_unconfirmed"),
    ({"dock_status": dock(NOW, aircraft="absent")}, "dock.aircraft_absent"),
    ({"dock_status": dock(NOW, energy={"state": "charging", "charge_fraction": 0.5})}, "energy.charging"),
    ({"dock_status": dock(NOW, energy={"state": "cooling", "charge_fraction": 1.0})}, "energy.cooling"),
    ({"dock_status": dock(NOW, energy={"state": "idle", "charge_fraction": 0.4})}, "energy.idle"),
    ({"dock_status": dock(NOW, energy={"state": "fault", "charge_fraction": 0.4})}, "energy.fault"),
    ({"dock_status": dock(NOW, energy={"state": "ready", "charge_fraction": 0.6})}, "energy.insufficient"),
    ({"dock_status": dock(NOW, environment={"state": "deferred", "wind_mps": 2.0})}, "environment.deferred"),
    ({"dock_status": dock(NOW, environment={"state": "permitted", "wind_mps": 9.0})}, "environment.wind"),
    ({"robot": status(NOW, "airborne", age=120)}, "robot.airborne"),
    ({"robot": status(NOW, "airborne")}, "presence.conflict"),
    ({"robot": status(NOW, failsafe=True)}, "robot.failsafe"),
    ({"needs": DispatchNeeds(skills=("skill.inspect.thermal",))}, "capability.missing_skill"),
    ({"holders": {"dock_a.pad": "mission:m-other:v1"}, "activity": "mission:m-this:v1"}, "reservation.conflict"),
    ({"stage": Stage.CLAIM, "dock_status": dock(NOW, lid="open"), "lid_confirmed": True}, "reservation.missing"),
    ({"needs": DispatchNeeds(not_after=NOW - timedelta(seconds=1))}, "window.expired"),
])
def test_every_blocking_condition_blocks_with_its_own_reason(kwargs, reason):
    result = judge(now=NOW, **kwargs)
    assert reason in result.reasons and REASONS[reason] is Verdict.BLOCKED
    assert result.verdict is Verdict.BLOCKED
    assert result.valid_until == result.evaluated_at


@pytest.mark.parametrize("kwargs,reason", [
    ({"dock_status": None}, "dock.status_missing"),
    ({"dock_status": dock(NOW - timedelta(seconds=3.5))}, "dock.status_stale"),
    ({"dock_status": dock(NOW + timedelta(seconds=1.5))}, "dock.status_stale"),
    ({"dock_status": dock(NOW, session=SessionState.RECONCILING)}, "dock.session_reconciling"),
    ({"dock_status": dock(NOW, link="unknown")}, "dock.link_unknown"),
    ({"dock_status": dock(NOW, lid="unknown")}, "dock.lid_unknown"),
    ({"dock_status": dock(NOW, aircraft="unknown")}, "dock.presence_unknown"),
    ({"dock_status": dock(NOW, energy={"state": "ready", "charge_fraction": None})}, "energy.unknown"),
    ({"dock_status": dock(NOW, environment={"state": "permitted", "wind_mps": None})}, "environment.unknown"),
    ({"capability": None}, "robot.capability_unknown"),
    ({"robot": status(NOW, "unknown")}, "robot.phase_unknown"),
])
def test_unknown_never_dispatches_and_stays_distinct_from_blocked(kwargs, reason):
    result = judge(now=NOW, **kwargs)
    assert reason in result.reasons and REASONS[reason] is Verdict.UNKNOWN
    assert result.verdict is Verdict.UNKNOWN


def test_blocked_wins_over_unknown_and_all_reasons_are_kept():
    result = judge(now=NOW, dock_status=dock(NOW, lid="unknown", link="offline"))
    assert result.verdict is Verdict.BLOCKED
    assert {"dock.lid_unknown", "dock.offline"} <= set(result.reasons)


def test_the_claim_stage_needs_an_open_confirmed_lid_and_this_activitys_holds():
    holders = {r: "mission:m:v1" for r in ("uav_a.motion", "dock_a.pad", "dock_a.charger")}
    result = judge(Stage.CLAIM, now=NOW, dock_status=dock(NOW, lid="open"), holders=holders,
                   activity="mission:m:v1", lid_confirmed=True)
    assert result.verdict is Verdict.ELIGIBLE
    assert judge(Stage.PREPARE, now=NOW, holders=holders, activity="mission:m:v1").verdict is Verdict.ELIGIBLE
