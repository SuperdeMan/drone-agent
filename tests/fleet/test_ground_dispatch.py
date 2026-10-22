"""X1 ground robots in P1 dispatch (D088): the catalog places a dockless ground robot on a ground site, eligibility
reads the robot's own fresh status instead of a dock, and a hold ends only on a fresh parked status after the end.

X1 地面车接入 P1 派遣（D088）：目录把没有机场的地面车放在地面站点；可派遣判定读取地面车自己的新鲜状态而不是机场；
持有只在任务结束后有新鲜状态显示已停回停车位时结束。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml
from pydantic import ValidationError

from drone_agent.contracts import EnergyState, LocalizationHealth, RobotStatus, utcnow
from drone_agent.contracts.capability import CommsState
from drone_agent.fleet.dispatch import Dispatch, check_ground, static_capability
from drone_agent.fleet.resources import DispatchNeeds, OperationsCatalog, Stage, Verdict, evaluate, load_catalog
from drone_agent.fleet.scheduling_models import SchedulingCatalog, load_scheduling
from drone_agent.mission.registry import load_registry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "configs/sites/x1_campus_v1.yaml"
SCHEDULING = ROOT / "configs/scheduling/x1_campus_v1.yaml"


def raw(path=CATALOG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def placed(data: dict) -> None:
    """Load and check a catalog the way the service does. / 按服务的方式加载并核对目录。"""
    catalog = OperationsCatalog.model_validate(data)
    registries = {s: load_registry(ROOT, ROOT / e.scene) for s, e in catalog.sites.items()}
    capabilities = {r: static_capability(ROOT, e.platform, r) for r, e in catalog.robots.items()}
    check_ground(catalog, registries, capabilities)
    return catalog, registries


def status(*, phase="parked", age_s=0.0, energy=0.9, failsafe=False) -> RobotStatus:
    now = utcnow()
    return RobotStatus(robot_id="ugv_01", timestamp=now - timedelta(seconds=age_s), flight_phase="unknown",
                       ground_phase=phase, energy=EnergyState(remaining_fraction=energy),
                       localization=LocalizationHealth(fix_type="estimated", healthy=True),
                       comms=CommsState(uplink_ok=True, last_seen=now), fc_failsafe_active=failsafe)


def test_the_x1_catalogs_place_a_dockless_ground_robot_on_its_ground_site():
    catalog, registries = placed(raw())
    catalog.check_files(ROOT)
    load_scheduling(SCHEDULING).check(catalog, registries)
    assert catalog.robots["ugv_01"].dock_id is None
    assert catalog.resources("ugv_01") == ("ugv_01.motion",)
    assert catalog.resources("uav_a") == ("uav_a.motion", "dock_a.pad", "dock_a.charger")
    assert registries["site_g"].kind == "ground_site" and registries["site_a"].kind == "aerial_site"


def test_robots_without_docks_never_run_vendor_tasks():
    data = raw()
    data["robots"]["ugv_01"]["execution_backend"] = "vendor_protocol_sim"
    with pytest.raises(ValidationError):
        OperationsCatalog.model_validate(data)


@pytest.mark.parametrize("mutate", [
    # a dock on the ground site / 地面站点上放机场
    lambda d: d["docks"]["dock_a"].update(site_id="site_g") or d["robots"]["uav_a"].update(site_id="site_g"),
    # a ground robot on an aerial site / 地面车放在空中站点
    lambda d: d["robots"]["ugv_01"].update(site_id="site_b"),
    # an aircraft without a dock / 没有机场的飞行器
    lambda d: d["docks"].pop("dock_b") and d["robots"]["uav_b"].pop("dock_id"),
], ids=["dock_on_ground_site", "rover_on_aerial_site", "dockless_aircraft"])
def test_ground_placement_rules_are_enforced(mutate):
    data = raw()
    mutate(data)
    with pytest.raises(ValueError):
        placed(data)


def test_a_ground_site_must_lie_where_the_ground_layer_places_its_map():
    catalog, registries = placed(raw())
    data = raw(SCHEDULING)
    data["airspace"]["sites"]["site_g"]["origin"] = [3, -4]
    with pytest.raises(ValueError):
        SchedulingCatalog.model_validate(data).check(catalog, registries)
    data = raw(SCHEDULING)
    data.pop("ground")
    with pytest.raises(ValueError):
        SchedulingCatalog.model_validate(data).check(catalog, registries)


def judge(robot_status, *, stage=Stage.PREVIEW, holders=None, activity=None, needs=None):
    catalog = load_catalog(CATALOG)
    capability = static_capability(ROOT, catalog.robots["ugv_01"].platform, "ugv_01")
    return evaluate(catalog, "ugv_01", stage=stage, now=utcnow(), needs=needs, capability=capability,
                    robot_status=robot_status, dock=None, holders=holders, activity=activity)


def test_a_fresh_parked_charged_ground_robot_is_eligible_for_its_status_freshness():
    value = status()
    eligibility = judge(value, needs=DispatchNeeds(skills=("skill.ground.recheck",), energy_fraction=0.6))
    assert (eligibility.verdict, eligibility.reasons, eligibility.dock_id) == (Verdict.ELIGIBLE, (), None)
    assert eligibility.valid_until == value.timestamp + timedelta(seconds=3)


@pytest.mark.parametrize(("value", "reason", "verdict"), [
    (None, "robot.status_missing", Verdict.UNKNOWN),
    (status(age_s=10), "robot.status_stale", Verdict.UNKNOWN),
    (status(phase="unknown"), "robot.phase_unknown", Verdict.UNKNOWN),
    (status(phase="moving"), "robot.moving", Verdict.BLOCKED),
    (status(phase="stopped"), "robot.not_parked", Verdict.BLOCKED),
    (status(energy=0.5), "energy.insufficient", Verdict.BLOCKED),
    (status(failsafe=True), "robot.failsafe", Verdict.BLOCKED),
])
def test_every_ground_condition_has_its_own_reason_and_no_dock_reason_appears(value, reason, verdict):
    eligibility = judge(value, needs=DispatchNeeds(energy_fraction=0.6))
    assert eligibility.verdict is verdict and reason in eligibility.reasons
    assert not any(code.startswith(("dock.", "environment.")) for code in eligibility.reasons)


def test_the_claim_needs_this_activity_to_hold_the_ground_robots_motion():
    activity = "mission:m-1:v1"
    assert "reservation.missing" in judge(status(), stage=Stage.CLAIM, activity=activity).reasons
    held = judge(status(), stage=Stage.CLAIM, activity=activity, holders={"ugv_01.motion": activity})
    assert held.verdict is Verdict.ELIGIBLE
    other = judge(status(), stage=Stage.PREVIEW, activity=activity, holders={"ugv_01.motion": "mission:m-2:v1"})
    assert "reservation.conflict" in other.reasons


def release(terminal, value, claim=None):
    owner = NS(clock=utcnow, catalog=load_catalog(CATALOG), ledger=NS(delivery=lambda _: {"acked_at": "x"}))
    return Dispatch._ground_release(owner, None, claim, terminal, value)


def test_a_ground_hold_ends_only_on_a_fresh_parked_status_after_the_end():
    ended = ("mission_result", utcnow() - timedelta(seconds=1))
    decision, reason, evidence = release(ended, status())
    assert (decision, reason, evidence["ground_phase"]) == ("release", "reconciled", "parked")
    assert release(ended, status(phase="stopped"))[:2] == ("uncertain", "robot_not_parked")
    assert release(ended, status(phase="moving"))[:2] == ("uncertain", "robot_not_parked")
    assert release(ended, status(age_s=10))[:2] == ("uncertain", "robot_status_stale,robot_status_before_terminal")
    assert release(ended, None)[:2] == ("uncertain", "robot_status_missing")
    assert release(None, status())[:2] == ("hold", "unclaimed")
    claim = {"delivery_id": "d-1", "decided_at": utcnow().isoformat()}
    assert release(None, status(phase="moving"), claim)[:2] == ("hold", "in_progress")
