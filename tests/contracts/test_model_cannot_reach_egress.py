"""Red line D004/D012: nothing the planner produces can be a control command, and its tools are read-only."""

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from drone_agent.contracts import (
    ControlCommandEnvelope,
    MissionPackage,
    MissionSpec,
    PackageNode,
    PlannerTool,
    PlannerToolAllowlist,
    TaskNode,
)
from tests.contracts.factories import mission_spec

REPO = Path(__file__).resolve().parents[2]
CONTROL_FIELD_TERMS = ("setpoint", "attitude", "thrust", "pwm", "actuator", "mavlink", "offboard", "lease_epoch", "command_seq")


def _field_names(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    seen = seen or set()
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for name, info in model.model_fields.items():
        names.add(name)
        ann = info.annotation
        for candidate in getattr(ann, "__args__", (ann,)):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                names |= _field_names(candidate, seen)
    return names


@pytest.mark.parametrize("model", [MissionSpec, TaskNode, MissionPackage, PackageNode])
def test_planner_facing_models_have_no_control_fields(model):
    names = _field_names(model)
    offending = sorted(n for n in names if any(term in n for term in CONTROL_FIELD_TERMS))
    assert not offending, f"{model.__name__} exposes control-level fields: {offending}"


def test_mission_models_cannot_nest_a_control_envelope():
    assert ControlCommandEnvelope not in {
        c for m in (MissionSpec, MissionPackage) for c in _nested_models(m)
    }


def _nested_models(model: type[BaseModel], seen: set[type] | None = None) -> set[type]:
    seen = seen or set()
    if model in seen:
        return set()
    seen.add(model)
    for info in model.model_fields.values():
        ann = info.annotation
        for candidate in getattr(ann, "__args__", (ann,)):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                _nested_models(candidate, seen)
    return seen


@pytest.mark.parametrize("key", ["setpoint", "Attitude", "thrust", "pwm", "actuator", "mavlink", "raw_command", "offboard"])
def test_task_params_reject_control_level_keys(key):
    with pytest.raises(ValidationError):
        TaskNode(task_id="t", skill_id="skill.flight.fly_route", params={key: 1})


def test_task_params_accept_skill_level_keys():
    spec = mission_spec()
    assert spec.tasks[0].params == {"altitude_m_agl": 20}


def test_planner_tool_allowlist_is_read_only():
    allow = PlannerToolAllowlist.from_yaml(REPO / "configs" / "planner_tools.yaml")
    assert allow.tools, "allowlist must not be empty"
    assert all(t.read_only for t in allow.tools)


@pytest.mark.parametrize("name", ["drone.takeoff", "cmd_vel.publish", "mission.package.upload", "lease.grant", "flight.control"])
def test_write_looking_tool_names_are_rejected(name):
    with pytest.raises(ValidationError):
        PlannerTool(name=name, read_only=True)


def test_non_read_only_tool_is_rejected():
    with pytest.raises(ValidationError):
        PlannerTool(name="assets.lookup", read_only=False)


def test_recovery_policy_is_a_reference_not_planner_content():
    # The planner names a policy; it never authors one (D009).
    spec = mission_spec()
    assert isinstance(spec.recovery_policy_ref, str)
    assert "edges" not in _field_names(MissionSpec)
