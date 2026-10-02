"""P5 vendor-managed docks (D072): the pinned profile, the derived task, the envelope, the journal summary and the
catalog rules that keep earlier digests and the P1 lid rule intact.

P5 厂商托管机场（D072）：固定档案、推导出的任务、信封、账本摘要，以及保持既有摘要与 P1 开盖规则不变的目录规则。
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from drone_agent.admission.admission import AdmissionContext
from drone_agent.admission.airspace import SimulatedAirspaceProvider
from drone_agent.admission.compiler import CompileContext
from drone_agent.admission.models import MissionRequest, RequestChannel
from drone_agent.admission.pipeline import evaluate as admit
from drone_agent.contracts import EffectVerdict, ExecutionStatus, Provenance, utcnow
from drone_agent.eval.p1_world import draft
from drone_agent.fleet.dispatch import static_capability
from drone_agent.fleet.resources import (
    DispatchNeeds,
    DockStatus,
    DockStatusReport,
    OperationsCatalog,
    SessionState,
    Stage,
    evaluate,
    load_catalog,
)
from drone_agent.fleet.vendor import (
    COMMANDS,
    VendorError,
    command,
    compile_task,
    flight_id,
    load_profile,
    transaction_id,
)
from drone_agent.fleet.vendor_gateway import summarize, vendor_outcomes
from drone_agent.mission.registry import Registry
from drone_agent.planner.draft import draft_to_spec
from drone_agent.runtime.ledger import content_hash
from drone_agent.runtime.permission import TrustLevel

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "configs/vendors/dock_task_v1.yaml"
SCENE = ROOT / "configs/scenarios/p5_site_vd_v1.yaml"
# Catalog digests recorded by the earlier phases; the P5 model extension must not move them.
# 前几个阶段记录的目录摘要；P5 的模型扩展不能改变它们。
DIGESTS = {"p1_campus_v1": "7ea9f6ef75c888418de1a0efa2cb7ab639f89ba73d0eee88aea19c5073245edb",
           "p1_s1_v1": "c5a6821e937cc34c199e0e40555b915a7961af325d188991ef86969c70f2cbeb",
           "p3_campus_v1": "a6c957c2c6c880ea67022dd566c43251232415420d193e8faea9f2c8782d7337",
           "p3_s1_v1": "1e48d0b35e7b3dda5f5ab8027f73f16a22eace71dae910800b24495f924e28d7"}


def package(asset: str = "asset_red", robot: str = "uav_v1"):
    registry = Registry(ROOT, scene=SCENE)
    now = utcnow()
    request = MissionRequest(request_id="req-00000001", text="inspect", requested_by="harness:test",
                             trust_level=TrustLevel.FIRST_PARTY, channel=RequestChannel.HARNESS,
                             approved_volume_id="campus_training", asset_ids=[asset], idempotency_key="k-0001",
                             received_at=now)
    spec = draft_to_spec(draft(asset), request, registry, mission_id="m-000000000001", mission_version=1,
                         provenance=Provenance(model_id="x", prompt_version="x", input_hash="0" * 64, generated_at=now),
                         now=now)
    capability = static_capability(ROOT, "configs/platforms/vendor_dock_sim.yaml", robot)
    result = admit(spec, CompileContext(registry=registry, robot_id=robot),
                   AdmissionContext(registry=registry, capability=capability, airspace=SimulatedAirspaceProvider(),
                                    now=now, request=request))
    assert result.blocked_at is None, result.codes
    return result.compile.package, registry


def test_the_profile_pins_four_whitelisted_commands_and_declares_what_is_invisible():
    profile, digest = load_profile(ROOT, PROFILE)
    assert {role: spec.method for role, spec in profile.commands.items()} == COMMANDS
    assert profile.low_level_control == "none" and profile.safety_responsibility == "vendor"
    assert profile.visibility.logs == "none" and profile.visibility.safety_events == "none"
    assert profile.protocol.verified_against_firmware is False and len(digest) == 64


@pytest.mark.parametrize("change", [
    {"low_level_control": "stick"},
    {"commands": {"fly": {"method": "drc_stick_control"}}},
    {"commands": {"prepare": {"method": "velocity_setpoint"}}},
    {"protocol": {"verified_against_firmware": True}},
    {"cross_dock": True},
    {"safety_responsibility": "platform"},
])
def test_profiles_claiming_control_or_firmware_do_not_load(tmp_path, change):
    data = yaml.safe_load((ROOT / PROFILE).read_text(encoding="utf-8"))
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(VendorError):
        load_profile(tmp_path, "profile.yaml")


def test_only_whitelisted_commands_can_be_built_and_each_transaction_is_fixed():
    message = command("flighttask_execute", flight="f-1", gateway="dock_vd", data={}, ts="2026-10-02T00:00:00+00:00")
    assert message["tid"] == transaction_id("f-1", "flighttask_execute") == command(
        "flighttask_execute", flight="f-1", gateway="dock_vd", data={}, ts="later")["tid"]
    assert transaction_id("f-1", "flighttask_prepare") != message["tid"]
    for method in ("drc_stick_control", "velocity_setpoint", "attitude", "arm"):
        with pytest.raises(VendorError):
            command(method, flight="f-1", gateway="dock_vd", data={}, ts="now")


def test_the_task_follows_the_approved_package_and_is_deterministic():
    profile, digest = load_profile(ROOT, PROFILE)
    pkg, registry = package()
    task = compile_task(pkg, registry, profile, digest, robot_id="uav_v1", dock_id="dock_vd")
    again = compile_task(pkg, registry, profile, digest, robot_id="uav_v1", dock_id="dock_vd")
    assert task.sha256 == again.sha256 and task.flight_id == flight_id(pkg.mission_id, 1, pkg.package_hash)
    kinds = [step.kind for step in task.steps]
    assert kinds[0] == "takeoff" and kinds[-1] == "land" and kinds.count("photo") == 1
    photo = next(step for step in task.steps if step.kind == "photo")
    assert photo.asset_id == "asset_red" and photo.node == "inspect_asset_red"
    assert set(task.nodes()) == {node.task_id for node in pkg.nodes}
    other = compile_task(package("asset_blue")[0], registry, profile, digest, robot_id="uav_v1", dock_id="dock_vd")
    assert other.sha256 != task.sha256


def test_a_node_without_a_vendor_form_is_refused():
    profile, digest = load_profile(ROOT, PROFILE)
    pkg, registry = package()
    nodes = [node.model_copy(update={"skill_id": "skill.flight.goto_local"}) if node.task_id == "takeoff" else node
             for node in pkg.nodes]
    with pytest.raises(VendorError):
        compile_task(pkg.model_copy(update={"nodes": nodes}), registry, profile, digest, robot_id="uav_v1",
                     dock_id="dock_vd")


def rows(*items):
    found, previous = [], "0" * 64
    for seq, (kind, data) in enumerate(items):
        body = {"seq": seq, "previous": previous, "timestamp": (utcnow() + timedelta(seconds=seq)).isoformat(),
                "monotonic_ns": seq, "kind": kind, "data": data}
        row = {**body, "sha256": content_hash(body)}
        found.append(row)
        previous = row["sha256"]
    return found


def progress(seq, status, step, applied=True):
    return ("event", {"method": "flighttask_progress", "seq": seq, "status": status, "current_step": step,
                      "percent": 0, "applied": applied})


def test_the_summary_only_moves_forward_and_keeps_the_terminal():
    summary = summarize(rows(("task", {"flight_id": "f", "task_sha256": "t", "delivery_id": "d"}),
                             ("command", {"method": "flighttask_prepare", "tid": "a"}),
                             ("reply", {"method": "flighttask_prepare", "tid": "a", "result": 0}),
                             progress(3, "in_progress", 3), progress(2, "in_progress", 2, applied=False),
                             progress(7, "ok", 7), ("terminal", {"status": "ok", "physical": True}),
                             ("terminal", {"status": "failed", "physical": True})))
    assert summary.reached == 7 and summary.status == "ok" and summary.terminal["status"] == "ok"
    assert summary.sent["flighttask_prepare"]["attempts"] == 1 and summary.executing


def test_unstarted_nodes_have_no_outcome_and_only_verified_media_completes_the_inspection():
    profile, digest = load_profile(ROOT, PROFILE)
    pkg, registry = package()
    task = compile_task(pkg, registry, profile, digest, robot_id="uav_v1", dock_id="dock_vd")
    waiting = summarize(rows(("task", {"flight_id": task.flight_id, "task_sha256": task.sha256, "delivery_id": "d"})))
    assert vendor_outcomes(task, waiting, "uav_v1", {}) == {}
    done = summarize(rows(("task", {"flight_id": task.flight_id, "task_sha256": task.sha256, "delivery_id": "d"}),
                          progress(0, "ok", len(task.steps)), ("terminal", {"status": "ok", "physical": True})))
    without = vendor_outcomes(task, done, "uav_v1", {})
    assert without["inspect_asset_red"].execution_status is ExecutionStatus.SUCCEEDED
    assert not without["inspect_asset_red"].counts_as_completed
    assert all(o.effect_verdict is EffectVerdict.UNKNOWN for o in without.values())
    verified = vendor_outcomes(task, done, "uav_v1", {"inspect_asset_red": EffectVerdict.VERIFIED})
    assert verified["inspect_asset_red"].counts_as_completed
    assert not verified["land"].counts_as_completed  # landing is the vendor's and not observable / 降落属于厂商且不可观测
    cancelled = summarize(rows(("task", {"flight_id": task.flight_id, "task_sha256": task.sha256, "delivery_id": "d"}),
                               progress(0, "in_progress", 1), progress(1, "canceled", 1),
                               ("terminal", {"status": "canceled", "physical": True})))
    outcomes = vendor_outcomes(task, cancelled, "uav_v1", {})
    assert outcomes["takeoff"].execution_status is ExecutionStatus.SUCCEEDED
    assert outcomes["inspect_asset_red"].execution_status is ExecutionStatus.CANCELLED


def test_earlier_catalog_digests_are_unchanged():
    for name, digest in DIGESTS.items():
        assert load_catalog(ROOT / "configs/sites" / f"{name}.yaml").sha256 == digest


def vendor_catalog() -> dict:
    return yaml.safe_load((ROOT / "configs/sites/p5_campus_v1.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate", [
    lambda d: d["docks"]["dock_vd"].update(actions=["open_lid"]),
    lambda d: d["docks"]["dock_vd"]["backend"].pop("profile"),
    lambda d: d["docks"]["dock_fa"]["backend"].update(profile="configs/vendors/dock_task_v1.yaml"),
    lambda d: d["robots"]["uav_v1"].update(execution_backend="logical_sim"),
    lambda d: d["robots"]["uav_fa"].update(execution_backend="vendor_protocol_sim"),
    lambda d: d["robots"]["uav_fa"].update(execution_backend="real_device"),
    lambda d: d["docks"]["dock_vd"]["backend"].update(kind="real_device"),
])
def test_vendor_docks_take_no_actions_need_their_profile_and_serve_only_vendor_aircraft(mutate):
    data = copy.deepcopy(vendor_catalog())
    mutate(data)
    with pytest.raises(ValidationError):
        OperationsCatalog.model_validate(data)


def dock_report(now, lid: str) -> DockStatus:
    report = DockStatusReport.model_validate({
        "dock_id": "dock_vd", "boot_id": "vendor-0007-abcdef", "seq": 4, "observed_at": now.isoformat(),
        "link": "online", "lid": lid, "aircraft": "present", "energy": {"state": "ready", "charge_fraction": 1.0},
        "environment": {"state": "permitted", "wind_mps": 2.0}, "upkeep": "normal", "actions": []})
    return DockStatus(dock_id="dock_vd", source="vendor_protocol_sim", session=SessionState.ACTIVE,
                      complete_reports=3, report=report, received_at=now)


def test_a_vendor_dock_opens_its_own_lid_but_a_jammed_one_still_blocks():
    catalog = OperationsCatalog.model_validate(vendor_catalog())
    capability = static_capability(ROOT, "configs/platforms/vendor_dock_sim.yaml", "uav_v1")
    now = utcnow()
    holders = dict.fromkeys(catalog.resources("uav_v1"), "mission:m-000000000001:v1")
    needs = DispatchNeeds(skills=("skill.inspect.asset",), energy_fraction=0.98, not_after=now + timedelta(minutes=9))

    def judge(lid):
        return evaluate(catalog, "uav_v1", stage=Stage.CLAIM, now=now, needs=needs, capability=capability,
                        dock=dock_report(now, lid), holders=holders, activity="mission:m-000000000001:v1")

    assert judge("closed").verdict.value == "eligible"
    assert "dock.lid_jammed" in judge("jammed").reasons and "dock.lid_unknown" in judge("unknown").reasons
