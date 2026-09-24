"""The guardian drives the external mode as its own egress and turns its failures into v2 recoveries (D039, D042).

guardian 把外部模式当作自己的出口来驱动，并把其失效转为 v2 恢复（D039、D042）。
"""

import asyncio
import time

import pytest

from drone_agent.autonomy.messages import AuthorizationRevoked, AuthorizedSetpoint, LocalTask, SegmentStatus
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
        (lambda m, t: m.autonomy.latest.pop("trajectory_segment"), RecoveryTrigger.TRAJECTORY_STALE,
         "no_valid_segment"),
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
    revoked_before_recovery = []
    recover = m3.adapter.recover

    async def recorded(behavior, permitted, site=None):
        revoked_before_recovery.append(isinstance(m3.egress.sent[-1], AuthorizationRevoked))
        await recover(behavior, permitted, site)

    m3.adapter.recover = recorded
    await m3.guardian.tick()
    await m3.settle()
    intervention = next(r["data"] for r in m3.journal.rows if r["kind"] == "safety_intervention")
    assert intervention["reason"] == trigger.value and intervention["detail"].startswith(detail)
    assert intervention["context"]["control_mode"] == "external_mode"
    kinds = m3.kinds()
    assert kinds.index("external_stop") < kinds.index("safety_intervention") < kinds.index("recovery_intent")
    # D047: the only egress message after the failure is the revocation, sent before the MAVLink recovery, in the
    # authorizations' sequence space. / D047：失效之后发给出口节点的唯一消息是撤销，在 MAVLink 恢复之前发出，与授权
    # 共用序号空间。
    assert not m3.guardian.external.active and len(m3.egress.sent) == before + 1
    revocation = m3.egress.sent[-1]
    assert isinstance(revocation, AuthorizationRevoked) and revocation.reason == "intervention:" + trigger.value
    assert revocation.command_seq > max(a.command_seq for a in m3.egress.sent[:-1])
    assert revoked_before_recovery == [True]
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
    revocations = [m for m in m3.egress.sent if isinstance(m, AuthorizationRevoked)]
    assert [r.reason for r in revocations] == ["step_handover"]


async def test_the_mode_is_requested_only_once_the_new_task_has_a_segment(m3):
    # SITL: after a step handover the next mode switch took 106 ms, before the 5 Hz planner's first segment for the
    # new task, and the first supervision tick found no valid segment (D047). / SITL：步骤交接后下一次模式切换只用了
    # 106 ms，早于 5 Hz 规划节点给新任务的首个片段，第一个监督周期就找不到有效片段（D047）。
    loop = asyncio.get_running_loop()

    def slow_planner(model):
        if isinstance(model, LocalTask) and model.active:
            loop.call_later(0.25, lambda: m3.autonomy.put("trajectory_segment", segment(model.task_id)))

    m3.autonomy.on_send = slow_planner
    had_segment = []
    request = m3.adapter.request_external_mode

    async def recorded(nav_state, permitted):
        had_segment.append(m3.guardian.external.segment() is not None)
        await request(nav_state, permitted)

    m3.adapter.request_external_mode = recorded
    await enter(m3)
    assert had_segment == [True]
    kinds = m3.kinds()
    assert kinds.index("external_first_segment") < kinds.index("external_active")
    # The hold authorization is renewed every 100 ms while waiting. / 等待期间每 100 ms 续发悬停授权。
    assert len([a for a in m3.egress.sent if isinstance(a, AuthorizedSetpoint)]) >= 3


async def test_a_recovery_while_entering_never_leaves_the_mode_counted_as_ours(m3):
    # Review for D047: the entering loops can wake while a recovery is still sending its revocation; they must neither
    # count a late mode switch as their own activation nor authorize again. / D047 评审：进入循环可能在恢复仍在发送撤销
    # 时醒来；它们既不能把迟到的模式切换算作自己的激活，也不能再次授权。
    m3.adapter.switch_mode = False
    send = m3.egress.send

    async def slow_revocation(model):
        if isinstance(model, AuthorizationRevoked):
            m3.adapter.mode = "EXTERNAL1"  # PX4's switch lands just now / PX4 的切换恰在此时到达
            await asyncio.sleep(0.25)  # the entering loop wakes meanwhile / 进入循环在此期间醒来
        return await send(model)

    m3.egress.send = slow_revocation
    submitted = asyncio.create_task(m3.submit("inspect_green", "approach"))
    while not any(write[0] == "external_mode" for write in m3.adapter.writes):
        await asyncio.sleep(0.01)
    await m3.guardian.intervene(RecoveryTrigger.USER_CANCEL, "operator_cancel")
    decision = await submitted
    await m3.settle()
    assert not decision.accepted and not m3.guardian.external.active
    assert "external_active" not in m3.kinds()
    revoked_at = next(i for i, m in enumerate(m3.egress.sent) if isinstance(m, AuthorizationRevoked))
    assert m3.egress.sent[revoked_at].reason == "intervention:user_cancel"
    assert not [m for m in m3.egress.sent[revoked_at:] if isinstance(m, AuthorizedSetpoint)]


async def test_without_a_first_segment_the_external_mode_is_never_requested(m3):
    m3.autonomy.on_send = None
    m3.guardian.external.settings["first_segment_timeout_s"] = 0.3
    decision = await m3.submit("inspect_green", "approach")
    await m3.settle()
    assert not decision.accepted and decision.reason == "adapter_error:PermissionError"
    assert not any(write[0] == "external_mode" for write in m3.adapter.writes)
    assert not m3.guardian.external.active and not m3.guardian.external.entering
    stop = next(r["data"] for r in m3.journal.rows if r["kind"] == "external_stop")
    assert stop["reason"] == "start_failed:no_first_segment"
    assert isinstance(next(m for m in m3.egress.sent if isinstance(m, AuthorizationRevoked)), AuthorizationRevoked)
    withdrawn = [t for t in m3.autonomy.sent if isinstance(t, LocalTask) and not t.active]
    assert len(withdrawn) == 1


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


async def test_a_report_without_fresh_estimator_flags_is_no_report_not_a_gnss_loss(m3):
    # SITL (D048): the XRCE link came up only at takeoff; the first report had fresh GNSS and local position but no
    # estimator flags yet, so "not fused" was unknown, and the guardian handed the takeoff to the flight controller.
    # SITL（D048）：XRCE 链路直到起飞才建立；第一份报告的 GNSS 与本地位置已新鲜，估计器标志却还没到，「未融合」其实
    # 未知，guardian 却把起飞交还给了飞控。
    unknown = localization(gnss_ok=False, gnss_position_fused=False, visual_ok=False, ev_position_fused=False,
                           px4_status_age_s=0.02, estimator_flags_age_s=1e6)
    m3.autonomy.put("localization_report", unknown)
    assert m3.guardian.external.localization() is None
    m3.beat()
    m3.guardian.active_step = m3.node("takeoff")
    await m3.guardian.tick()
    await m3.settle()
    assert not [r for r in m3.journal.rows if r["kind"] == "safety_intervention"]
    # Fresh flags that really say GNSS is not fused are a GNSS loss. / 新鲜标志确实表明 GNSS 未融合时才是 GNSS 失效。
    m3.autonomy.put("localization_report", unknown.model_copy(update={"estimator_flags_age_s": 0.3,
                                                                      "visual_ok": True, "ev_position_fused": True}))
    assert m3.guardian.external.localization() is not None


async def test_an_external_mission_is_served_only_once_its_dependencies_deliver(m3):
    # D048: the guardian had accepted the lease and taken off before the localization node had any PX4 data.
    # D048：定位节点还没有任何 PX4 数据时，guardian 就已接受租约并起飞。
    from drone_agent.runtime.launch import wait_for_external

    m3.refresh()
    m3.autonomy.put("localization_report", localization(estimator_flags_age_s=1e6))
    waiting = asyncio.create_task(wait_for_external(m3.guardian, 3.0))
    await asyncio.sleep(0.3)
    assert not waiting.done()
    m3.refresh()
    result = await waiting
    assert result["ready"] and result["egress"] and result["autonomy"] and result["waited_s"] >= 0.3
    # A dependency that never delivers is journaled as not ready after the bounded wait. / 始终未交付的依赖在有界
    # 等待后记为未就绪。
    m3.refresh()
    m3.autonomy.put("localization_report", localization(px4_status_age_s=1e6))
    result = await wait_for_external(m3.guardian, 0.2)
    assert (result["ready"], result["egress"], result["autonomy"]) == (False, True, False)


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
