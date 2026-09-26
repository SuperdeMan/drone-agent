"""WP-P2-02–05: the workflow engine's crash windows, trigger deduplication, schedules and cancellation.

WP-P2-02–05：工作流引擎的崩溃窗口、触发去重、排班与取消。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from drone_agent.contracts import utcnow
from drone_agent.fleet.api import dispatch
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import source_context
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.transport import FleetHub
from drone_agent.fleet.workflow import WorkflowEngine, build_workflows
from drone_agent.fleet.workflow_models import run_principal
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.runtime.signing import SigningKey
from tests.fleet.harness import scripted_planner

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "configs/sites/p1_campus_v1.yaml"
MEMBERS = ROOT / "configs/sites/p2_members_s0.yaml"
WORKFLOWS = ROOT / "configs/workflows/p2_campus_v1.yaml"
OPERATOR, REVIEWER, VIEWER, ALARM = "harness:p2-operator", "harness:p2-reviewer", "harness:p2-viewer", "event:campus-alarm"


class Clock:
    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return utcnow() + self.offset


def build(tmp_path: Path, clock: Clock) -> MissionService:
    ledger = BusinessLedger(tmp_path / "ledger.sqlite3")
    operations = build_operations(ROOT, ledger, CATALOG, MEMBERS, backups=tmp_path / "backups", clock=clock)
    registry = Registry(ROOT, scene=ROOT / M2_SCENE)
    service = MissionService(root=ROOT, scene=ROOT / M2_SCENE, ledger=ledger, hub=FleetHub(ledger, tmp_path / "media"),
                             signing_key=SigningKey.generate(),
                             approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                             planner=scripted_planner(), operations=operations, clock=clock,
                             provenance_context=source_context(ROOT, ROOT / M2_SCENE, registry.sha256,
                                                               backend="logical_sim"))
    workflows = build_workflows(ROOT, ledger, WORKFLOWS, operations, backups=tmp_path / "backups", clock=clock)
    service.workflows = WorkflowEngine(service, workflows, root=ROOT, worker_id="worker-1")
    return service


def restart(service: MissionService, clock: Clock, name: str) -> WorkflowEngine:
    """A replacement process: a new engine and worker id; the old lease must expire first. / 替换进程：新引擎与 worker。"""
    clock.offset += timedelta(seconds=11)
    service.workflows = WorkflowEngine(service, service.workflows.workflows, root=ROOT, worker_id=name)
    return service.workflows


async def call(service, actor, method, trust="first_party", **params):
    return await dispatch(service, {"method": method, "actor": actor, "trust": trust, "params": params})


async def start(service, workflow_id="quick_check", inputs=None, request_id="r-1", actor=OPERATOR) -> str:
    response = await call(service, actor, "workflows.start", project_id="campus_ops", workflow_id=workflow_id,
                          request_id=request_id, inputs=inputs if inputs is not None else {"asset": "asset_red"})
    assert response["ok"], response
    return response["result"]["run"]["run_id"]


def missions(service, run_id) -> list[str]:
    return [row["mission_id"] for row in service.ledger._rows(
        "SELECT mission_id FROM requests WHERE requested_by=?", (run_principal(run_id),))]


async def test_a_manual_run_submits_one_deterministic_mission_that_still_needs_a_human_approval(tmp_path):
    clock = Clock()
    service = build(tmp_path, clock)
    run_id = await start(service)
    assert await start(service) == run_id, "the same request id returns the same run"
    service.tick_workflows()
    [mission_id] = missions(service, run_id)
    view = service._view(mission_id)
    assert view["mission"]["status"] == "awaiting_approval" and view["versions"][0]["origin"] == "workflow"
    assert view["versions"][0]["provenance"]["planning"]["source"] == "deterministic"
    assert view["versions"][0]["provenance"]["planning"]["model_id"] == "quick_check@v1"
    assert view["binding"]["robot_id"] == "uav_c" and view["request"]["requested_by"] == run_principal(run_id)
    got = (await call(service, VIEWER, "workflows.get", project_id="campus_ops", run_id=run_id))["result"]
    await_node = next(n for n in got["nodes"] if n["node_id"] == "await_inspection")
    assert got["run"]["state"] == "waiting" and await_node["reason"] == "approval" and got["waiting"] == ["approval"]
    for _ in range(3):
        service.tick_workflows()
    assert missions(service, run_id) == [mission_id]


@pytest.mark.parametrize("window", ["before_outbox_commit", "after_outbox_commit", "response_lost",
                                    "before_node_completion"])
async def test_each_crash_window_recovers_with_exactly_one_mission(tmp_path, window):
    clock = Clock()
    service = build(tmp_path, clock)
    engine, store = service.workflows, service.workflows.store
    run_id = await start(service)
    original_transition, original_submit, original_delivered = store.transition, service.submit_workflow, \
        store.delivered

    def crash_in_transition(*args, **kwargs):
        if kwargs.get("outbox") is not None:
            with store.transaction():
                original_transition(*args, **kwargs)
                raise RuntimeError("process died before the commit")
        return original_transition(*args, **kwargs)

    def crash_after_accept(**kwargs):
        original_submit(**kwargs)
        raise RuntimeError("response lost after the mission was accepted")

    def crash_before_record(*args, **kwargs):
        raise RuntimeError("process died before the node completion was recorded")

    if window == "before_outbox_commit":
        store.transition = crash_in_transition
    elif window == "after_outbox_commit":
        engine._deliver = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("process died before delivery"))
    elif window == "response_lost":
        service.submit_workflow = crash_after_accept
    else:
        store.delivered = crash_before_record
    service.tick_workflows()
    rows = store.outbox(run_id)
    if window == "before_outbox_commit":
        assert rows == [] and store.nodes(run_id)["inspect"]["state"] == "pending"
    else:
        assert [r["state"] for r in rows] in (["pending"], ["claimed"])
    assert len(missions(service, run_id)) == (1 if window in ("response_lost", "before_node_completion") else 0)
    store.transition, store.delivered = original_transition, original_delivered
    service.submit_workflow = original_submit
    restart(service, clock, "worker-2")
    for _ in range(3):
        service.tick_workflows()
    [mission_id] = missions(service, run_id)
    assert store.nodes(run_id)["inspect"]["result"] == {"mission_id": mission_id, "schema_version": "0.1.0"}
    assert [r["state"] for r in store.outbox(run_id)] == ["delivered"]
    assert store.run(run_id)["owner"] == "worker-2" and store.run(run_id)["owner_epoch"] == 2


async def test_one_event_raised_a_hundred_times_starts_one_run_and_forged_events_are_refused(tmp_path):
    service = build(tmp_path, Clock())
    params = {"project_id": "campus_ops", "workflow_id": "asset_check", "trigger_id": "asset_alarm",
              "event_type": "asset.alarm", "event_id": "alarm-0001", "payload": {"asset": "asset_blue"}}
    results = [await call(service, ALARM, "workflows.event", trust="backend", **params) for _ in range(100)]
    assert all(r["ok"] for r in results) and len({r["result"]["run_id"] for r in results}) == 1
    assert sum(r["result"]["created"] for r in results) == 1
    other = await call(service, ALARM, "workflows.event", trust="backend", **{**params, "event_id": "alarm-0002"})
    assert other["result"]["run_id"] != results[0]["result"]["run_id"]
    forged = [
        ("event:intruder", "backend", params),
        (ALARM, "backend", {**params, "project_id": "harbor_ops", "workflow_id": "harbor_check",
                            "trigger_id": "harbor_alarm"}),
        (ALARM, "backend", {**params, "payload": {"asset": "asset_blue", "robot_id": "uav_h"}}),
        (ALARM, "backend", {**params, "payload": {"asset": "asset_green"}}),
        (ALARM, "backend", {**params, "event_type": "asset.cleared"}),
        (ALARM, "backend", {**params, "workflow_id": "quick_check", "trigger_id": "every_30_min"}),
        (ALARM, "backend", {**params, "event_id": "../../etc"}),
    ]
    for actor, trust, values in forged:
        refused = await call(service, actor, "workflows.event", trust=trust, **values)
        assert not refused["ok"] and refused["issue"]["code"] == "workflow.event_refused", (actor, values)
    for actor, trust in ((OPERATOR, "first_party"), ("dock:p1-s0-sim", "backend"), ("a2a:client", "third_party")):
        refused = await call(service, actor, "workflows.event", trust=trust, **params)
        assert refused["issue"]["code"] in ("auth.method_not_allowed", "auth.scope_missing"), actor
    assert (await call(service, ALARM, "workflows.list", trust="backend", project_id="campus_ops"))["issue"][
        "code"] in ("auth.method_not_allowed", "auth.scope_missing")
    assert (await call(service, ALARM, "docks.report", trust="backend", report={}))["issue"][
        "code"] == "auth.method_not_allowed"
    assert len(service.workflows.store.runs("campus_ops")) == 2


async def test_schedules_fire_once_per_occurrence_and_never_replay_a_backlog(tmp_path):
    clock = Clock()
    service = build(tmp_path, clock)
    store = service.workflows.store
    params = {"project_id": "campus_ops", "workflow_id": "quick_check", "trigger_id": "every_30_min",
              "action": "enable", "reason": "test"}
    assert (await call(service, VIEWER, "workflows.schedule", **params))["issue"]["code"] == "auth.project_denied"
    enabled = (await call(service, OPERATOR, "workflows.schedule", **params))["result"]
    cursor = datetime.fromisoformat(enabled["cursor_at"])
    assert enabled["state"] == "enabled" and cursor.minute % 30 == 0 and cursor.second == 0
    clock.offset += cursor - utcnow() + timedelta(seconds=5)
    for _ in range(3):
        service.tick_workflows()
    runs = store.runs("campus_ops")
    assert len(runs) == 1 and runs[0]["trigger_event"] == cursor.isoformat()
    restart(service, clock, "worker-2")
    service.tick_workflows()
    assert len(store.runs("campus_ops")) == 1, "a restart inside the window does not fire the occurrence again"
    # Down for two hours and ten minutes: four occurrences passed, the newest is 10 min late, beyond the 5 min window.
    # 停机两小时十分钟：错过四个发生时刻，最近的一个迟到 10 分钟，超出 5 分钟启动窗。
    clock.offset += timedelta(hours=2, minutes=10)
    service.tick_workflows()
    triggers = store.triggers("campus_ops", "quick_check")
    missed = [t for t in triggers if t["disposition"] == "missed"]
    assert len(store.runs("campus_ops")) == 1 and len(missed) == 4, "an outage longer than the window starts nothing"
    params["action"] = "disable"
    assert (await call(service, OPERATOR, "workflows.schedule", **params))["result"]["state"] == "disabled"
    clock.offset += timedelta(hours=1)
    service.tick_workflows()
    assert len(store.runs("campus_ops")) == 1
    audit = store.events("schedule:campus_ops:quick_check:every_30_min")
    assert [e["kind"] for e in audit] == ["schedule.enabled", "schedule.disabled"]
    assert all(e["actor"] == OPERATOR for e in audit)


async def test_a_cancel_before_delivery_leaves_no_mission(tmp_path):
    service = build(tmp_path, Clock())
    run_id = await start(service)
    engine = service.workflows
    original = engine._deliver
    engine._deliver = lambda *a, **k: False  # the effect is written but not delivered yet / 效果已写入但尚未投递
    service.tick_workflows()
    assert [r["state"] for r in engine.store.outbox(run_id)] == ["pending"]
    cancelled = await call(service, OPERATOR, "workflows.cancel", project_id="campus_ops", run_id=run_id,
                           request_id="c-1", reason="operator")
    assert cancelled["result"]["run"]["state"] == "cancel_requested"
    engine._deliver = original
    for _ in range(3):
        service.tick_workflows()
    assert missions(service, run_id) == [] and engine.store.run(run_id)["state"] == "cancelled"
    assert [r["state"] for r in engine.store.outbox(run_id)] == ["void"]
    assert all(n["state"] in ("cancelled",) for n in engine.store.nodes(run_id).values())
    again = await call(service, OPERATOR, "workflows.cancel", project_id="campus_ops", run_id=run_id,
                       request_id="c-2", reason="again")
    assert again["ok"] and again["result"]["run"]["cancel"]["request_id"] == "c-1", "the first cancel stays"


async def test_a_cancel_committed_after_the_claim_refuses_the_submission(tmp_path):
    service = build(tmp_path, Clock())
    run_id = await start(service)
    store, original = service.workflows.store, service.submit_workflow

    def cancel_first(**kwargs):
        store.request_cancel(run_id, actor=OPERATOR, request_id="c-race", reason="race")
        return original(**kwargs)

    service.submit_workflow = cancel_first
    service.tick_workflows()
    service.submit_workflow = original
    for _ in range(3):
        service.tick_workflows()
    assert missions(service, run_id) == [] and store.run(run_id)["state"] == "cancelled"
    assert store.outbox(run_id)[0]["result"]["reason"] in ("refused_after_claim", "cancelled")


async def test_a_cancel_after_submission_blocks_the_approval_and_waits_for_the_mission(tmp_path):
    service = build(tmp_path, Clock())
    run_id = await start(service)
    service.tick_workflows()
    [mission_id] = missions(service, run_id)
    await call(service, OPERATOR, "workflows.cancel", project_id="campus_ops", run_id=run_id, request_id="c-1",
               reason="operator")
    assert service.ops.store.cancel_intent(mission_id)["requested_by"] == OPERATOR
    view = service._view(mission_id)
    refused = await call(service, OPERATOR, "approve", mission_id=mission_id, version=1,
                         package_hash=view["versions"][0]["package_hash"])
    assert refused["issue"]["code"] == "dispatch.cancelled"
    for _ in range(3):
        service.tick_workflows()
        for dirty in sorted(service.dirty):
            service.refresh(dirty)
    assert service.ledger.mission(mission_id)["status"] == "cancelled"
    assert service.workflows.store.run(run_id)["state"] == "cancelled"
    assert missions(service, run_id) == [mission_id], "no second mission after the cancel"


async def test_run_views_and_starts_are_scoped_to_the_project(tmp_path):
    service = build(tmp_path, Clock())
    run_id = await start(service)
    for actor, method, params, code in (
            (VIEWER, "workflows.start", {"project_id": "campus_ops", "workflow_id": "quick_check",
                                         "request_id": "v-1", "inputs": {"asset": "asset_red"}}, "auth.project_denied"),
            ("harness:p2-harbor", "workflows.get", {"project_id": "campus_ops", "run_id": run_id}, "service.not_found"),
            ("harness:p2-harbor", "workflows.get", {"project_id": "harbor_ops", "run_id": run_id}, "service.not_found"),
            ("harness:p2-harbor", "workflows.cancel", {"project_id": "campus_ops", "run_id": run_id,
                                                       "request_id": "h-1", "reason": "x"}, "service.not_found"),
            (REVIEWER, "workflows.cancel", {"project_id": "campus_ops", "run_id": run_id, "request_id": "r-9",
                                            "reason": "x"}, "auth.project_denied"),
            (OPERATOR, "workflows.start", {"project_id": "campus_ops", "workflow_id": "asset_reinspection",
                                           "request_id": "i-1", "inputs": {"asset": "asset_red"}},
             "workflow.not_startable"),
            (OPERATOR, "workflows.start", {"project_id": "campus_ops", "workflow_id": "quick_check",
                                           "request_id": "x-1", "inputs": {"asset": "asset_red", "robot": "uav_h"}},
             "workflow.invalid_inputs")):
        refused = await call(service, actor, method, **params)
        assert not refused["ok"] and refused["issue"]["code"] == code, (actor, method, refused)
    agent = await call(service, "a2a:client", "workflows.start", trust="third_party", project_id="campus_ops",
                       workflow_id="quick_check", request_id="a-1", inputs={"asset": "asset_red"})
    assert agent["issue"]["code"] in ("auth.scope_missing", "auth.method_not_allowed")
    listed = (await call(service, VIEWER, "workflows.list", project_id="campus_ops"))["result"]
    assert {t["workflow_id"] for t in listed["templates"]} == {"campus_round", "asset_check", "asset_reinspection",
                                                                "quick_check"}
    assert [r["run_id"] for r in listed["runs"]] == [run_id]
