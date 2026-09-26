"""P3 scheduler inside the formal service: API scopes, assignment with airspace holds, waits, withdrawal, cancels and
the workflow assignment mode (D059). Flights are left to the S0 matrix; these cases stop before any claim.

正式服务中的 P3 调度器：API 权限、带空域持有的分配、等待、撤回、取消与工作流分配模式（D059）。飞行交给 S0 矩阵；这些用例
在任何领取之前结束。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from drone_agent.eval.p3_world import ADMIN, OPERATOR, VIEWER, P3World
from drone_agent.fleet.reservation import is_cell

ROOT = Path(__file__).resolve().parents[2]


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def world(tmp_path):
    made = []

    def build(**options) -> P3World:
        value = P3World(tmp_path / f"case-{len(made)}", ROOT, seed=7, **options)
        made.append(value)
        return value

    yield build
    for value in made:
        value.ledger.close()


async def settled(w: P3World) -> None:
    await w.settle()


def test_task_api_scopes_and_idempotency(world):
    w = world()

    async def scenario():
        await settled(w)
        first = await w.task("asset_mid", key="same-key")
        again = await w.task("asset_mid", key="same-key")
        assert first == again
        conflict = await w.api(OPERATOR, "tasks.submit", project_id="campus_ops", asset_id="asset_east",
                               volume_id="campus_training", candidates=[], priority=0, idempotency_key="same-key")
        assert conflict["issue"]["code"] == "service.idempotency_conflict"
        viewer = await w.api(VIEWER, "tasks.submit", project_id="campus_ops", asset_id="asset_mid",
                             volume_id="campus_training", candidates=[], priority=0, idempotency_key="v-1")
        assert viewer["issue"]["code"] == "auth.project_denied"
        outside = await w.api(OPERATOR, "tasks.submit", project_id="campus_ops", asset_id="asset_mid",
                              volume_id="campus_training", candidates=["uav_h"], priority=0, idempotency_key="o-1")
        assert outside["issue"]["code"] == "task.candidate_outside_project"
        agent = await w.api("a2a:client", "tasks.submit", trust="third_party", project_id="campus_ops",
                            asset_id="asset_mid", volume_id="campus_training", candidates=[], priority=0,
                            idempotency_key="a-1")
        assert agent["issue"]["code"] in ("auth.method_not_allowed", "auth.scope_missing")
        stranger = await w.api("harness:stranger", "tasks.get", project_id="campus_ops", task_id=first)
        assert stranger["issue"]["code"] == "service.not_found"
        bad = await w.api(OPERATOR, "tasks.submit", project_id="campus_ops", asset_id="asset_mid",
                          volume_id="campus_training", candidates=[], priority=10, idempotency_key="p-1")
        assert bad["issue"]["code"] == "task.invalid_request"
        listed = await w.api(VIEWER, "tasks.list", project_id="campus_ops")
        assert [t["task_id"] for t in listed["result"]["tasks"]] == [first]

    run(scenario())


def test_an_assignment_holds_the_robot_and_its_cells_and_a_neighbour_on_the_same_cells_waits(world):
    w = world()

    async def scenario():
        await settled(w)
        first = await w.task("asset_mid", key="first")
        await w.drive_tasks(lambda: w.task_state(first) == "assigned", "assigned", approve=False, timeout_s=10)
        assert w.task_robot(first) == "uav_a"
        mission = w.task_mission(first)
        reservation = w.service.ops.store.reservations(mission_id=mission)[0]
        cells = [r for r in reservation.resources if is_cell(r)]
        assert cells and {"uav_a.motion", "dock_a.pad"} <= set(reservation.resources)
        assert w.tasks.footprint(f"mission:{mission}:v1")["cells"] == cells
        assert w.service._view(mission)["task"]["task_id"] == first
        second = await w.task("asset_mid", key="second")
        await w.hold(1.0)
        assert w.task_state(second) == "queued" and w.task_mission(second) is None
        reasons = {c["robot_id"]: c["reasons"] for c in w.tasks.last_decision(second)["body"]["candidates"]}
        assert "reservation.conflict" in reasons["uav_a"] and "airspace.cell_held" in reasons["uav_b"]
        east = await w.task("asset_east", key="east")
        await w.drive_tasks(lambda: w.task_state(east) == "assigned", "east assigned", approve=False, timeout_s=10)
        assert w.task_robot(east) == "uav_b"

    run(scenario())


def test_rejections_are_final_and_record_the_permanent_reason(world):
    w = world()

    async def scenario():
        await settled(w)
        task = await w.task("asset_west", candidates=["uav_b"], key="west-on-b")
        rooftop = await w.task("asset_mid", volume="campus_rooftop", key="rooftop")
        await w.drive_tasks(lambda: w.task_final(task) and w.task_final(rooftop), "rejected", approve=False,
                            timeout_s=10)
        assert w.task_state(task) == "rejected" and "asset.unregistered" in w.task_row(task)["reason"]
        assert w.task_state(rooftop) == "rejected" and "volume.unapproved" in w.task_row(rooftop)["reason"]
        assert not w.ledger.missions(10)

    run(scenario())


def test_a_robot_that_can_never_take_the_task_is_excluded_once_instead_of_retried_every_pass(world):
    from drone_agent.fleet.service import ServiceError

    w = world()
    submit = w.service.submit_assigned

    def refuse_uav_a(**params):
        if params["robot_id"] == "uav_a":
            raise ServiceError("dispatch.backend_mismatch", "uav_a runs another backend")
        return submit(**params)

    w.service.submit_assigned = refuse_uav_a

    async def scenario():
        await settled(w)
        task = await w.task("asset_mid", key="moves-on")
        await w.drive_tasks(lambda: w.task_state(task) == "assigned", "assigned", approve=False, timeout_s=15)
        assert w.task_robot(task) == "uav_b" and w.task_row(task)["epoch"] == 1
        assert [e["robot_id"] for e in w.task_row(task)["excluded"]] == ["uav_a"]
        refused = [e for e in w.tasks.events(f"task:{task}", 50) if e["kind"] == "assignment.refused"]
        assert len(refused) == 1 and refused[0]["body"]["reason"] == "dispatch.backend_mismatch"
        # Free the cells the first task holds, so the next one is decided on uav_a alone. / 释放第一个任务单持有的单元。
        await w.cancel_task(task)
        await w.drive_tasks(lambda: w.task_final(task), "cancelled", approve=False, timeout_s=15)
        only = await w.task("asset_mid", candidates=["uav_a"], key="nowhere")
        await w.drive_tasks(lambda: w.task_final(only), "rejected", approve=False, timeout_s=15)
        assert w.task_state(only) == "rejected" and "task.robot_excluded" in w.task_row(only)["reason"]

    run(scenario())


def test_a_blocked_unclaimed_assignment_is_withdrawn_and_reassigned_with_a_new_epoch(world):
    w = world()

    async def scenario():
        await settled(w)
        task = await w.task("asset_mid", key="withdraw")
        await w.drive_tasks(lambda: w.task_state(task) == "assigned", "assigned", approve=False, timeout_s=10)
        old = w.task_mission(task)
        w.docks["dock_a"].faults.upkeep = "maintenance"
        await w.drive_tasks(lambda: w.task_row(task)["epoch"] == 2 and w.task_state(task) == "assigned",
                            "reassigned", approve=False, timeout_s=30)
        assert w.task_robot(task) == "uav_b" and w.task_mission(task) != old
        assert w.service.ops.store.cancel_intent(old)["reason"] == "assignment_withdrawn"
        assert w.status(old) == "cancelled"
        assert w.scheduler.assignment_problem(old) == "assignment_superseded"
        assert w.scheduler.assignment_problem(w.task_mission(task)) is None
        states = [(a["epoch"], a["robot_id"], a["state"]) for a in w.assignments(task)]
        assert states == [(1, "uav_a", "withdrawn"), (2, "uav_b", "active")]
        refused = await w.approve(old, expect=False)
        assert refused["issue"]["code"] in ("dispatch.cancelled", "approval.stale_version")
        await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id="dock_a", action="release",
                    reason="test")

    run(scenario())


def test_cancelling_queued_and_assigned_tasks_leaves_nothing_to_dispatch(world):
    w = world()

    async def scenario():
        await settled(w)
        for dock in ("dock_a", "dock_b"):
            w.docks[dock].faults.upkeep = "maintenance"
        await w.hold(1.0)
        queued = await w.task("asset_mid", key="queued")
        await w.hold(0.5)
        response = await w.cancel_task(queued)
        assert response["result"]["task"]["state"] == "cancelled"
        again = await w.cancel_task(queued)
        assert again["issue"]["code"] == "task.not_cancellable"
        for dock in ("dock_a", "dock_b"):
            w.docks[dock].faults.upkeep = "normal"
            await w.api(ADMIN, "resources.maintenance", project_id="campus_ops", dock_id=dock, action="release",
                        reason="test")
        assigned = await w.task("asset_east", key="assigned")
        await w.drive_tasks(lambda: w.task_state(assigned) == "assigned", "assigned", approve=False, timeout_s=10)
        mission = w.task_mission(assigned)
        await w.cancel_task(assigned)
        await w.drive_tasks(lambda: w.task_final(assigned), "cancelled", approve=False, timeout_s=15)
        assert w.task_state(assigned) == "cancelled" and w.status(mission) == "cancelled"
        assert not w.service.ops.store.holders(w.service.ops.catalog.resources("uav_b"))
        assert w.task_state(queued) == "cancelled" and w.task_row(queued)["epoch"] == 0

    run(scenario())


def test_a_workflow_submits_tasks_the_scheduler_assigns_and_its_cancel_reaches_them(world):
    w = world()

    async def scenario():
        await settled(w)
        run_id = await w.start("pair_check")
        await w.drive_tasks(lambda: len([t for t in w.tasks.tasks(requested_by=f"workflow:{run_id}")
                                         if t["state"] == "assigned"]) == 2, "both assigned", approve=False,
                            timeout_s=15)
        tasks = w.tasks.tasks(requested_by=f"workflow:{run_id}")
        assert {t["asset_id"]: t["robot_id"] for t in tasks} == {"asset_mid": "uav_a", "asset_east": "uav_b"}
        await w.drive_tasks(lambda: w.store.nodes(run_id)["await_mid"]["reason"] == "approval", "waiting approval",
                            approve=False, timeout_s=5)
        nodes = w.store.nodes(run_id)
        assert nodes["inspect_mid"]["result"].keys() == {"schema_version", "task_id"}
        assert nodes["await_mid"]["detail"]["task_id"] == nodes["inspect_mid"]["result"]["task_id"]
        await w.cancel_run(run_id)
        await w.drive_tasks(lambda: w.final(run_id) and all(w.task_final(t["task_id"]) for t in tasks),
                            "cancelled", approve=False, timeout_s=20)
        assert w.run_state(run_id) == "cancelled"
        assert {w.task_state(t["task_id"]) for t in tasks} == {"cancelled"}

    run(scenario())


def test_the_service_entry_requires_the_catalog_and_scheduling_for_assigned_templates(tmp_path):
    result = subprocess.run([sys.executable, "-m", "drone_agent.fleet.main", "--scene",
                             str(ROOT / "configs/scenarios/m2_campus_v2.yaml"), "--scheduling",
                             str(ROOT / "configs/scheduling/p3_campus_v1.yaml")], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 2 and "--scheduling needs --catalog" in result.stderr
    from drone_agent.fleet.workflow_models import load_workflows

    assert load_workflows(ROOT / "configs/workflows/p3_campus_v1.yaml").assigned_nodes()
    assert not load_workflows(ROOT / "configs/workflows/p2_campus_v1.yaml").assigned_nodes()
