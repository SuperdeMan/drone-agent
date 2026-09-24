"""The guardian drives the external mode as its own egress and turns its failures into v2 recoveries (D039, D042).

guardian 把外部模式当作自己的出口来驱动，并把其失效转为 v2 恢复（D039、D042）。
"""

import time

import pytest

from drone_agent.autonomy.messages import AuthorizedSetpoint, LocalTask, SegmentStatus
from drone_agent.contracts import RecoveryBehavior, RecoveryTrigger
from tests.guardian.m3 import Runtime, egress_status, localization, obstacles, segment


@pytest.fixture
def m3(tmp_path):
    runtime = Runtime(tmp_path)
    yield runtime
    runtime.journal.close()


async def enter(runtime):
    decision = await runtime.submit("inspect_green", "approach")
    assert decision.accepted, decision.reason
    assert runtime.guardian.external.active
    return runtime.guardian.external.task


def test_an_external_package_is_refused_while_the_egress_node_is_down(tmp_path):
    with pytest.raises(ValueError, match="unsupported capability"):
        Runtime(tmp_path, egress_up=False)


async def test_the_adapter_captures_for_the_local_inspection_but_never_flies_its_approach(m3, tmp_path):
    # The first M3 SITL case reached the asset and then had its capture refused as an unsupported skill.
    # 首个 M3 SITL 用例飞到了资产上方，拍摄却因「不支持的技能」被拒。
    from drone_agent.adapters.px4_mavsdk import Px4Adapter

    adapter = Px4Adapter(m3.registry, tmp_path, tmp_path / "sensor.json")
    calls = []

    async def capture(node, permitted):
        calls.append(("capture", node.task_id))
        return {"media_ref": "images/x.rgb"}

    async def route(*args):
        calls.append(("route",))

    adapter.capture, adapter.route = capture, route
    node = m3.node("inspect_green")
    assert node.skill_id == "skill.inspect.asset_local"
    await adapter.execute(node, lambda: True, phase="capture")
    with pytest.raises(ValueError, match="phase"):
        await adapter.execute(node, lambda: True, phase="approach")
    assert calls == [("capture", "inspect_green")] and adapter.evidence["inspect_green"]["media_ref"]


def test_the_live_capability_offers_external_mode_only_with_a_healthy_egress(m3):
    assert "external_mode" in m3.guardian.live_capabilities().control_modes
    m3.egress.put("egress_status", egress_status(compatibility_ok=False))
    assert "external_mode" not in m3.guardian.live_capabilities().control_modes


async def test_entering_publishes_the_task_holds_first_and_confirms_the_mode(m3):
    task = await enter(m3)
    assert isinstance(m3.autonomy.sent[0], LocalTask) and m3.autonomy.sent[0].goal.as_list() == [0, 28, 4]
    first = m3.egress.sent[0]
    assert isinstance(first, AuthorizedSetpoint) and first.ttl_ms == 300
    assert first.position_ned.as_list() == pytest.approx([0, 0, -4])
    assert ("external_mode", 23) in m3.adapter.writes and m3.adapter.mode == "EXTERNAL1"
    assert task.lease_epoch == 1 and "external_active" in m3.kinds()


async def test_a_fresh_candidate_is_filtered_and_authorized_with_monotonic_sequence(m3):
    task = await enter(m3)
    sent = len(m3.egress.sent)
    for index in range(3):
        m3.refresh()
        m3.autonomy.put("trajectory_segment", segment(task.task_id, seq=index, target=(0, 1.5, 4)))
        m3.beat()
        await m3.guardian.tick()
    authorizations = m3.egress.sent[sent:]
    assert len(authorizations) == 3 and m3.guardian.recovery is None
    seqs = [a.command_seq for a in authorizations]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert all(a.step_id == "inspect_green" and a.lease_epoch == 1 for a in authorizations)
    assert "autonomy/filter" in m3.log and "autonomy/authorization" in m3.log


@pytest.mark.parametrize(
    ("setup", "trigger", "detail"),
    [
        (lambda m, t: None, RecoveryTrigger.TRAJECTORY_STALE, "no_valid_segment"),
        (lambda m, t: m.autonomy.put("trajectory_segment", segment("another.task", target=(0, 1, 4))),
         RecoveryTrigger.TRAJECTORY_STALE, "no_valid_segment"),
        (lambda m, t: m.autonomy.put("obstacle_set", obstacles(), age=5.0),
         RecoveryTrigger.OBSERVATION_STALE, "local_map_stale"),
        (lambda m, t: m.egress.put("egress_status", egress_status(), age=5.0),
         RecoveryTrigger.AUTONOMY_UNAVAILABLE, "egress_unavailable"),
        (lambda m, t: m.egress.put("egress_status", egress_status(fmu_link_ok=False)),
         RecoveryTrigger.AUTONOMY_UNAVAILABLE, "egress_unavailable"),
        (lambda m, t: m.egress.put("egress_status", egress_status(watchdog_exits=1)),
         RecoveryTrigger.TRAJECTORY_STALE, "egress_watchdog_exit"),
        (lambda m, t: m.autonomy.put("trajectory_segment", segment(t, target=(0, 14.5, 4))),
         RecoveryTrigger.TRAJECTORY_STALE, "cbf_rejected"),
    ],
)
async def test_external_failures_end_authorization_before_the_mavlink_recovery(m3, setup, trigger, detail):
    task = await enter(m3)
    if detail == "cbf_rejected":
        m3.adapter.position = [0.0, 14.2, 4.0]  # Inside the wall margin. / 位于墙体余量之内。
        m3.refresh()
    else:
        m3.refresh()
    setup(m3, task.task_id)
    m3.beat()
    before = len(m3.egress.sent)
    await m3.guardian.tick()
    await m3.settle()
    intervention = next(r["data"] for r in m3.journal.rows if r["kind"] == "safety_intervention")
    assert intervention["reason"] == trigger.value and intervention["detail"].startswith(detail)
    assert intervention["context"]["control_mode"] == "external_mode"
    kinds = m3.kinds()
    assert kinds.index("external_stop") < kinds.index("safety_intervention") < kinds.index("recovery_intent")
    assert not m3.guardian.external.active and len(m3.egress.sent) == before
    assert m3.adapter.writes[-1][:2] == ("recover", "hold")
    withdrawn = [t for t in m3.autonomy.sent if isinstance(t, LocalTask) and not t.active]
    assert withdrawn and withdrawn[-1].task_id == task.task_id


async def test_fresh_segments_that_never_progress_are_a_stall_not_a_success(m3, monkeypatch):
    task = await enter(m3)
    clock = [time.monotonic()]
    monkeypatch.setattr(m3.guardian.external, "clock", lambda: clock[0])
    m3.guardian.external.started = clock[0]
    stalled = None
    for index in range(30):
        clock[0] += 0.5
        m3.refresh()
        m3.autonomy.put("trajectory_segment", segment(task.task_id, seq=index, target=(0, 0, 4)))
        m3.beat()
        result = await m3.guardian.external.tick(m3.adapter.snapshot())
        if result:
            stalled = result
            break
    assert stalled == [(RecoveryTrigger.PROGRESS_STALLED, "no_progress_toward_goal")]


async def test_no_path_for_three_seconds_is_a_stall(m3, monkeypatch):
    task = await enter(m3)
    clock = [time.monotonic()]
    monkeypatch.setattr(m3.guardian.external, "clock", lambda: clock[0])
    results = []
    for index in range(8):
        clock[0] += 0.5
        m3.refresh()
        m3.autonomy.put("trajectory_segment", segment(task.task_id, seq=index, status=SegmentStatus.NO_PATH,
                                                      target=(0, 0, 4)))
        results.append(await m3.guardian.external.tick(m3.adapter.snapshot()))
    assert (RecoveryTrigger.PROGRESS_STALLED, "planner_no_path") in [r[0] for r in results if r]


async def test_two_consecutive_slow_planner_cycles_are_a_compute_overload(m3):
    task = await enter(m3)
    results = []
    for index in range(2):
        m3.refresh()
        m3.autonomy.put("trajectory_segment", segment(task.task_id, seq=index, cycle_ms=320, target=(0, 1, 4)))
        results.append(await m3.guardian.external.tick(m3.adapter.snapshot()))
    assert results[0] == [] and results[1][0][0] is RecoveryTrigger.COMPUTE_OVERLOADED


async def test_an_unrequested_hold_during_external_control_is_a_takeover(m3, monkeypatch):
    task = await enter(m3)
    m3.adapter.mode = "HOLD"
    clock = [time.monotonic()]
    monkeypatch.setattr(m3.guardian.external, "clock", lambda: clock[0])
    m3.refresh()
    m3.autonomy.put("trajectory_segment", segment(task.task_id, target=(0, 1, 4)))
    assert await m3.guardian.external.tick(m3.adapter.snapshot()) == []
    clock[0] += 1.0
    m3.refresh()
    m3.beat()
    await m3.guardian.tick()
    assert m3.guardian.taken_over and m3.guardian.reason == "external_mode_takeover"
    assert not m3.guardian.external.active


async def test_a_new_step_first_ends_the_external_stream_with_a_mavlink_hold(m3):
    task = await enter(m3)
    m3.refresh()
    m3.autonomy.put("trajectory_segment", segment(task.task_id, target=(0, 1, 4)))
    m3.beat()
    capture = await m3.submit("inspect_green", "capture", seq=2)
    # Not above the asset yet: refused, and the capture phase of the same step never ends the external stream.
    # 还未到资产上方：被拒；同一步骤的拍摄相位也不会结束外部模式。
    assert not capture.accepted and "above_asset" in capture.reason and m3.guardian.external.active
    m3.adapter.position = [0.0, 28.0, 4.0]
    m3.refresh()
    decision = await m3.submit("return_home", seq=3)
    assert decision.accepted, decision.reason
    writes = m3.adapter.writes
    hold = writes.index(("recover", "hold", None))
    assert writes.index(("execute", "return_home", None)) > hold
    assert "external_stop" in m3.kinds() and not m3.guardian.external.active


async def test_gnss_loss_with_visual_localization_holds_then_lands(m3):
    m3.autonomy.put("localization_report", localization(gnss_ok=False, gnss_position_fused=False))
    m3.beat()
    m3.guardian.active_step = m3.node("inspect_green")
    await m3.guardian.tick()
    await m3.settle()
    intervention = next(r["data"] for r in m3.journal.rows if r["kind"] == "safety_intervention")
    assert intervention["reason"] == "localization_degraded" and intervention["detail"] == "gnss_lost"
    assert m3.guardian.recovery.target is RecoveryBehavior.HOLD and m3.guardian.recovery.then is RecoveryBehavior.LAND_HERE


async def test_the_executive_ending_during_a_recovery_never_hands_the_recovery_away(m3):
    # SITL: GNSS lost, the guardian held; the executive then ended its aborted mission while the flight controller's
    # global position was already unhealthy, and the unmatched heartbeat loss handed control to the flight controller.
    # SITL：GNSS 丢失后 guardian 已 hold；executive 随后结束已中止的任务，此时飞控全局位置已不健康，未匹配的心跳丢失
    # 把控制交还了飞控。
    m3.autonomy.put("localization_report", localization(gnss_ok=False, gnss_position_fused=False))
    m3.beat()
    m3.guardian.active_step = m3.node("inspect_green")
    await m3.guardian.tick()
    await m3.settle()
    assert m3.guardian.recovery.target is RecoveryBehavior.HOLD
    m3.adapter.healthy = False
    m3.guardian.last_progress -= 5  # the executive is gone / executive 已退出
    await m3.guardian.tick()
    await m3.settle()
    assert not m3.guardian.taken_over and m3.guardian.recovery.then is RecoveryBehavior.LAND_HERE
    assert [r["data"]["reason"] for r in m3.journal.rows if r["kind"] == "safety_intervention"] == [
        "localization_degraded"]


async def test_a_report_built_from_stale_px4_inputs_is_no_report_not_a_gnss_loss(m3):
    # The DDS link is gone: the node still reports, but its "not fused" flags are unknowns, not a GNSS loss.
    # DDS 链路已断：节点仍在报告，但其「未融合」标志是未知量，而不是 GNSS 失效。
    m3.autonomy.put("localization_report", localization(gnss_ok=False, gnss_position_fused=False, visual_ok=False,
                                                        ev_position_fused=False, px4_status_age_s=3.0))
    assert m3.guardian.external.localization() is None
    m3.beat()
    m3.guardian.active_step = m3.node("inspect_green")
    await m3.guardian.tick()
    await m3.settle()
    assert not [r for r in m3.journal.rows if r["kind"] == "safety_intervention"]


async def test_low_energy_far_from_home_lands_at_the_reachable_site(m3):
    m3.guardian.active_step = m3.node("inspect_green")
    m3.adapter.position = [0.0, 28.0, 4.0]
    for _ in range(3):
        m3.refresh()
        m3.beat()
        await m3.guardian.tick()
    assert m3.guardian.recovery is None
    m3.adapter.battery = 0.29
    m3.refresh()
    m3.beat()
    await m3.guardian.tick()
    await m3.settle()
    intervention = next(r["data"] for r in m3.journal.rows if r["kind"] == "safety_intervention")
    assert intervention["behavior"] == "land_at" and intervention["site"] == [-3, 31, 0]
    assert ("recover", "land_at", [-3, 31, 0]) in m3.adapter.writes


async def test_the_guardians_own_slow_periods_are_a_compute_overload(m3):
    m3.guardian.active_step = m3.node("inspect_green")
    now = time.monotonic()
    m3.guardian.recent_periods.extend((now - 1.0 + i * 0.1, 0.18) for i in range(9))
    m3.beat()
    await m3.guardian.tick()
    await m3.settle()
    intervention = next(r["data"] for r in m3.journal.rows if r["kind"] == "safety_intervention")
    assert intervention["reason"] == "compute_overloaded" and intervention["detail"].startswith("guardian_period")
