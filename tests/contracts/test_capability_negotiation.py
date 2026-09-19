"""P5: a robot that only declares mission_upload cannot take work that needs offboard control; gaps are explicit.

P5：只声明 mission_upload 的机器人不能接需要 offboard 控制的任务；能力缺口必须显式列出。
"""

from drone_agent.contracts import ControlMode
from tests.contracts.factories import capability


def test_offboard_task_not_assignable_to_mission_upload_only_robot():
    caps = capability(control_modes={ControlMode.MISSION_UPLOAD})
    gaps = caps.missing_for(control_modes=[ControlMode.OFFBOARD_VELOCITY])
    assert gaps == ["control_mode:offboard_velocity"]


def test_missing_skill_is_reported_not_faked():
    caps = capability(skills=["skill.flight.takeoff"])
    assert caps.missing_for(skills=["skill.inspect.asset"]) == ["skill:skill.inspect.asset"]


def test_full_match_has_no_gaps():
    caps = capability(control_modes={ControlMode.MISSION_UPLOAD, ControlMode.OFFBOARD_VELOCITY}, skills=["skill.flight.takeoff"])
    assert caps.missing_for(skills=["skill.flight.takeoff"], control_modes=[ControlMode.MISSION_UPLOAD]) == []


def test_absent_control_mode_is_absent_not_none():
    # The descriptor has no 'supported=False' flag: unsupported modes are simply not declared.
    # 描述里没有 supported=False 这种标志：不支持的模式就是不声明。
    caps = capability(control_modes=set())
    assert ControlMode.OFFBOARD_POSITION not in caps.control_modes
    assert caps.missing_for(control_modes=[ControlMode.OFFBOARD_POSITION])
