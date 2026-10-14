"""X1 ground onboard behaviour (D088): the real guardian and executive drive the logical ground robot through the
ground skeleton, every recovery that ends the task stops and then disarms only once the robot is stationary, a pause
keeps it armed, the 2D fence look-ahead stops it outside a no-go zone, and the real PX4 adapter refuses a disarm that
is not stationary in Hold.

X1 地面机载行为（D088）：真实的 guardian 与 executive 驾驶逻辑地面车走完地面骨架；结束任务的恢复先停车，车静止后才上锁；
暂停保持解锁；二维围栏前瞻让车停在禁入区之外；真实 PX4 适配器拒绝不在 Hold 中静止时的上锁。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from drone_agent.admission.ground import compile_ground
from drone_agent.contracts import EffectVerdict, RecoveryBehavior, RecoveryPolicy
from drone_agent.eval.logical_flight import Client
from drone_agent.eval.logical_ground import Block, GroundFake, flat, quick_ground, view
from drone_agent.fleet.locator import COLOURS, locate
from drone_agent.guardian.core import Guardian
from drone_agent.mission.executive import Executive
from drone_agent.mission.verify import verify_ground_view
from drone_agent.runtime.ledger import Journal
from drone_agent.runtime.recording import Recorder
from drone_agent.runtime.robot_state import RobotAuthorityState
from drone_agent.runtime.signing import SigningKey, TrustStore
from tests.admission.ground_scene import ROOT, approve, ground, recheck_spec

KEY = SigningKey.generate()
TRUST = TrustStore.from_entries([KEY.trust_entry()])
POLICY = ROOT / "configs/recovery_policies/rover_campus_v1.yaml"
NEAR = [4.0, 0.0]  # one metre of path from the parking spot / 距停车位一米路径
ORANGE = Block(4.0, 0.0, 0.5, COLOURS["orange"])
# The skeleton's own disarm passes the same stationary guard as a terminal one. / 骨架自己的上锁与终态上锁走同一个
# 静止守卫。
SKELETON = [("arm", None), ("nav_to", None), ("recheck", "approach"), ("recheck", "capture"), ("return_home", None),
            ("stop", None), ("disarm", None), ("disarm_stationary", None)]


def signed(target=NEAR):
    package = approve(compile_ground(recheck_spec(target), ground(), robot_id="ugv_01").package)
    package.approval = KEY.sign(package.approval)
    return package


async def until(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return False


def operator(path, request_id: str, action: str) -> None:
    (path / "operator.json").write_text(json.dumps({"request_id": request_id, "action": action}))


async def drive(tmp_path, *, during=None, in_air="armed", blocks=(ORANGE,)):
    """Run one ground package with the guardian supervising, then let a terminal disarm finish.

    在 guardian 监督下执行一个地面任务包，然后等终态上锁完成。
    """
    reg = quick_ground(ground())
    fake = GroundFake(reg, tmp_path, in_air=in_air, blocks=blocks)
    package = signed()
    guardian_journal = Journal(tmp_path / "guardian.jsonl")
    journal, recorder = Journal(tmp_path / "executive.jsonl"), Recorder(tmp_path / "executive.mcap")
    try:
        guardian = Guardian(adapter=fake, package=package, registry=reg, journal=guardian_journal,
                            policy=RecoveryPolicy.from_yaml(POLICY), executive_id="executive-test", simulation=True,
                            trust=TRUST, robot_state=RobotAuthorityState(tmp_path / "robot/authority.json", "ugv_01"))
        executive = Executive(client=Client(guardian, fake), registry=reg, package=package, journal=journal,
                              recorder=recorder, artifacts=tmp_path, executive_id="executive-test", epoch=1,
                              trust=TRUST)
        tasks = [asyncio.create_task(guardian.supervise())]
        if during is not None:
            tasks.append(asyncio.create_task(during(executive, fake, tmp_path)))
        try:
            result = await executive.run()
            # A recovery ends with the guardian recording the safe terminal. / 恢复以 guardian 记录安全终态结束。
            await until(lambda: fake.armed is False and fake.velocity == (0.0, 0.0) and (
                guardian.recovery is None or guardian.reason == reg.safe_terminal), 5)
            if guardian.disarm_task is not None:
                await guardian.disarm_task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return result, guardian, fake, guardian_journal.rows, journal.rows
    finally:
        guardian_journal.close()
        journal.close()
        recorder.close()


def kinds(rows, kind):
    return [row["data"] for row in rows if row["kind"] == kind]


async def test_a_ground_recheck_runs_the_skeleton_and_ends_parked_and_disarmed(tmp_path):
    # The rover reports in_air true whenever armed (measured); nothing onboard may read it. / 实测地面车解锁即报告
    # in_air 为 true；机载代码不得读取它。
    result, guardian, fake, guardian_rows, rows = await drive(tmp_path)
    assert result["completed"], result
    assert fake.writes == SKELETON
    assert all((o["execution_status"], o["effect_verdict"]) == ("succeeded", "verified")
               for o in result["outcomes"].values())
    assert not kinds(guardian_rows, "safety_intervention")
    assert fake.armed is False and math.dist(fake.position, (0.0, 0.0)) <= 0.8
    assert guardian.registry.at_safe_terminal(fake.snapshot())
    # The face leg pointed the camera at the anomaly. / 正对段让相机对准了异常物。
    record = json.loads((tmp_path / "evidence-recheck_asset_mid.json").read_text())
    raw = (tmp_path / record["media_ref"]).read_bytes()
    found = locate(np.frombuffer(raw, dtype=np.uint8).reshape(120, 160, 3), "orange")
    assert found is not None and abs(found.u - 80) < 12
    assert all(guardian.registry.inside((x, y)) for _, x, y, _ in fake.trace)


async def test_a_cancel_during_the_recheck_stops_and_then_disarms_once_stationary(tmp_path):
    async def cancel(executive, fake, path):
        await until(lambda: executive.node is not None and executive.node.task_id == "recheck_asset_mid"
                    and fake.mode == "MISSION" and fake.velocity != (0.0, 0.0), 20)
        operator(path, "cancel-1", "cancel")

    result, guardian, fake, guardian_rows, rows = await drive(tmp_path, during=cancel)
    assert not result["completed"]
    outcome = result["outcomes"]["recheck_asset_mid"]
    assert (outcome["execution_status"], outcome["effect_verdict"]) == ("cancelled", "unknown")
    assert result["not_run"] == ["return_home", "stop", "disarm"]
    assert [d["behavior"] for d in kinds(guardian_rows, "safety_intervention")] == ["stop"]
    assert kinds(guardian_rows, "terminal_disarm_receipt") == [{"mission_id": "x1-test", "status": "accepted"}]
    assert kinds(guardian_rows, "recovered_to")[-1]["state"] == "stopped_disarmed"
    # The stop came first and the disarm only after the coast ended. / 先停车，滑行结束后才上锁。
    stopped = fake.writes.index(("recover", "stop"))
    assert fake.writes[stopped + 1:] == [("disarm_stationary", None)]
    assert fake.armed is False and math.dist(fake.position, (0.0, 0.0)) > 0.8


async def test_a_pause_keeps_the_robot_armed_and_resume_finishes_the_route(tmp_path):
    async def pause_then_resume(executive, fake, path):
        await until(lambda: executive.node is not None and executive.node.task_id == "nav_to"
                    and fake.mode == "MISSION" and fake.position[0] > 0.2, 20)
        operator(path, "pause-1", "pause")
        await until(lambda: fake.mode == "HOLD" and fake.velocity == (0.0, 0.0), 5)
        await asyncio.sleep(1.0)  # five stop hold times / 五个停车保持时长
        fake.paused_armed = fake.armed
        operator(path, "resume-1", "resume")

    result, guardian, fake, guardian_rows, rows = await drive(tmp_path, during=pause_then_resume)
    assert result["completed"], result
    assert fake.paused_armed is True
    assert not kinds(guardian_rows, "terminal_disarm_intent")
    assert ("resume", None) in fake.writes
    assert [d["reason"] for d in kinds(guardian_rows, "safety_intervention")] == ["user_pause"]


async def test_low_energy_while_driving_stops_and_disarms_in_place(tmp_path):
    async def drain(executive, fake, path):
        await until(lambda: executive.node is not None and executive.node.task_id == "return_home"
                    and fake.velocity != (0.0, 0.0), 30)
        fake.battery.fraction = 0.25

    result, guardian, fake, guardian_rows, rows = await drive(tmp_path, during=drain)
    assert not result["completed"]
    home = result["outcomes"]["return_home"]
    # Whether the executive observed the step once before the stop decides unknown or unverified; it is never
    # verified. / executive 在停车前是否观测过该步骤一次，决定是 unknown 还是 unverified；绝不是 verified。
    assert home["execution_status"] == "unknown" and home["effect_verdict"] in ("unknown", "unverified")
    assert [(d["reason"], d["behavior"]) for d in kinds(guardian_rows, "safety_intervention")] == [
        ("energy_low", "stop")]
    assert kinds(guardian_rows, "recovered_to")[-1]["state"] == "stopped_disarmed"
    assert fake.armed is False


async def test_the_fence_look_ahead_stops_a_robot_heading_into_a_no_go_zone(tmp_path):
    no_go = ground().map.ground.no_go[0]

    async def steer(executive, fake, path):
        await until(lambda: executive.node is not None and executive.node.task_id == "nav_to"
                    and fake.mode == "MISSION", 20)
        # Harness-only fault: the controller leaves the approved path towards the aircraft pad. / 只在编排中使用的
        # 故障：控制器偏离批准路径，驶向飞行器机位。
        fake.path = [(-1.0, 4.0)]

    result, guardian, fake, guardian_rows, rows = await drive(tmp_path, during=steer)
    assert not result["completed"]
    assert [(d["reason"], d["behavior"]) for d in kinds(guardian_rows, "safety_intervention")] == [
        ("geofence_predicted_breach", "stop")]
    assert kinds(guardian_rows, "recovered_to")[-1]["state"] == "stopped_disarmed"
    from drone_agent.mission.reachability import inside_polygon

    assert not any(inside_polygon((x, y), no_go) for _, x, y, _ in fake.trace)
    assert fake.position[1] > 1.0  # it was really heading in / 它确实在驶入


def test_the_ground_view_check_needs_a_sharp_frame_at_the_face_point(tmp_path):
    reg = ground()
    package = signed()
    node = package.nodes[2]
    fake = GroundFake(reg, tmp_path, blocks=[ORANGE])
    fake.position, fake.heading = list(node.params["face_point"]), 0.0
    verdicts = []
    for frame in (None, flat(reg.data["camera"])):
        fake.frames = [frame] if frame is not None else []
        fake.capture(node)
        record = json.loads((tmp_path / f"evidence-{node.task_id}.json").read_text())
        verdicts.append(verify_ground_view(record, tmp_path, node, reg))
    assert verdicts == [EffectVerdict.VERIFIED, EffectVerdict.UNVERIFIED]
    # The check proves the place, never the anomaly: an empty view still verifies. / 核验只证明到场，不判断异常物：
    # 空画面同样通过。
    fake.blocks, fake.frames = [], []
    fake.capture(node)
    record = json.loads((tmp_path / f"evidence-{node.task_id}.json").read_text())
    assert verify_ground_view(record, tmp_path, node, reg) == EffectVerdict.VERIFIED
    fake.position = [node.params["face_point"][0] - 1.5, node.params["face_point"][1]]
    fake.capture(node)
    record = json.loads((tmp_path / f"evidence-{node.task_id}.json").read_text())
    assert verify_ground_view(record, tmp_path, node, reg) == EffectVerdict.REFUTED


def test_the_forward_camera_places_blocks_where_the_locator_reads_them():
    camera = ground().data["camera"]
    left = view(camera, (0.0, 0.0), 0.0, [Block(2.0, 0.8, 0.5, COLOURS["magenta"])])
    image = np.frombuffer(left, dtype=np.uint8).reshape(120, 160, 3)
    found = locate(image, "magenta")
    # A block left of the axis appears left of the centre and below the horizon. / 光轴左侧的方块出现在画面中心左方、
    # 地平线下方。
    assert found is not None and found.u < 80 and found.v > 60
    assert locate(image, "orange") is None
    assert view(camera, (0.0, 0.0), math.pi, [Block(2.0, 0.0, 0.5, COLOURS["orange"])]) == view(camera, (0.0, 0.0),
                                                                                                  math.pi)


def ground_adapter(tmp_path, *, speed=0.0, mode="HOLD", age=0.0):
    from drone_agent.adapters.px4_mavsdk import Px4Adapter
    from drone_agent.contracts import utcnow

    adapter = Px4Adapter(ground(), tmp_path, tmp_path / "sensor.json")
    calls = []

    async def disarm():
        calls.append("disarm")

    adapter._system = NS(action=NS(disarm=disarm))
    adapter.values = {
        "position": NS(position=NS(east_m=1.0, north_m=0.0, down_m=0.0),
                       velocity=NS(east_m_s=speed, north_m_s=0.0, down_m_s=0.0)),
        "armed": True, "in_air": True, "mode": NS(name=mode),
        "health": NS(is_local_position_ok=True, is_global_position_ok=True, is_home_position_ok=True),
    }
    adapter.stamps["position"] = utcnow()
    adapter.received = {"position": time.monotonic() - age, "link": time.monotonic()}
    return adapter, calls


@pytest.mark.parametrize(("points", "uploaded"), [([[1.1, 0.0], [3.0, 2.0], [4.0, 5.0]], 2),
                                                   ([[3.0, 3.0], [4.0, 5.0]], 2), ([[1.2, 0.1]], 1)])
async def test_a_route_leaves_out_the_points_the_robot_already_stands_on(tmp_path, points, uploaded):
    # PX4 v1.17 marks such an item reached at the start and now and then never advances past it (X1 S1).
    # PX4 v1.17 会在启动时把这样的项记为已到达，偶尔就不再推进（X1 S1）。
    adapter, _ = ground_adapter(tmp_path)
    plans = []

    async def noop(*args):
        return None

    async def upload(plan):
        plans.append(plan)

    async def start():
        adapter.values["mode"] = NS(name="MISSION")

    adapter._system = NS(mission=NS(set_return_to_launch_after_mission=noop, upload_mission=upload,
                                    start_mission=start))
    adapter.values["home"] = NS(latitude_deg=47.0, longitude_deg=8.0)
    await adapter.route_2d(points, 1.0, lambda: True)
    assert len(plans[0].mission_items) == uploaded
    last = plans[0].mission_items[-1]
    assert last.is_fly_through is False and abs(last.latitude_deg - 47.0 - math.degrees(points[-1][1] / 6378137)) < 1e-9


@pytest.mark.parametrize(("speed", "mode", "age"), [(0.4, "HOLD", 0.0), (0.0, "MISSION", 0.0), (0.0, "HOLD", 1.0)])
async def test_the_real_adapter_refuses_a_disarm_unless_stationary_in_hold(tmp_path, speed, mode, age):
    adapter, calls = ground_adapter(tmp_path, speed=speed, mode=mode, age=age)
    with pytest.raises(PermissionError):
        await adapter.disarm_stationary(lambda: True)
    assert calls == [] and adapter.command_log == []


async def test_the_real_adapter_disarms_a_stationary_robot_in_hold_and_keeps_bodies_apart(tmp_path):
    from drone_agent.adapters.px4_mavsdk import Px4Adapter
    from drone_agent.mission.registry import Registry

    adapter, calls = ground_adapter(tmp_path)
    await adapter.disarm_stationary(lambda: True)
    assert calls == ["disarm"] and [c["operation"] for c in adapter.command_log] == ["ground_disarm"]
    assert adapter.capabilities.recovery_behaviors == {RecoveryBehavior.STOP, RecoveryBehavior.HANDOVER_TO_FC_FAILSAFE}
    aircraft = Px4Adapter(Registry(ROOT), tmp_path, tmp_path / "sensor.json")
    ground_node = signed().nodes[0]
    with pytest.raises(PermissionError):
        await aircraft.execute(ground_node, lambda: True)
    with pytest.raises(PermissionError):
        await aircraft.disarm_stationary(lambda: True)
    with pytest.raises(ValueError):
        await aircraft.recover(RecoveryBehavior.STOP, lambda: True)
    with pytest.raises(PermissionError):
        await adapter.execute(NS(skill_id="skill.flight.takeoff", params={}), lambda: True)


async def test_the_guardians_observe_only_mode_writes_the_idle_status_and_commands_nothing(tmp_path, monkeypatch):
    import drone_agent.adapters.px4_mavsdk as px4_mavsdk
    from drone_agent.runtime import launch

    fakes = []

    class Observed(GroundFake):
        def __init__(self, registry, artifacts, _sensor):
            super().__init__(registry, artifacts)
            fakes.append(self)

        async def connect(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(px4_mavsdk, "Px4Adapter", Observed)
    args = NS(role="guardian", root=ROOT, scene=ROOT / "configs/scenarios/x1_ground_a_v1.yaml", platform=None,
              observe_only=True, artifacts=tmp_path / "idle", sensor=tmp_path / "sensor.json")
    stop = asyncio.Event()
    task = asyncio.create_task(launch.observe_only(args, ground(), stop))
    await until(lambda: (tmp_path / "idle/status.json").is_file(), 5)
    stop.set()
    await task
    status = json.loads((tmp_path / "idle/status.json").read_text())
    assert status["active_step"] is None and status["observation"]["robot_id"] == "ugv_01"
    assert fakes[0].writes == [] and fakes[0].armed is False
