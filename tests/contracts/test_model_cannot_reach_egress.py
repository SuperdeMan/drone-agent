"""Red line D004/D012: nothing the planner produces can be a control command, and its tools are read-only.

红线 D004/D012：规划器产出的任何东西都不可能是控制命令，且它的工具全部只读。
"""

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
# Substrings that mark a control-level field. / 标记控制级字段的子串。
CONTROL_FIELD_TERMS = ("setpoint", "attitude", "thrust", "pwm", "actuator", "mavlink", "offboard", "lease_epoch", "command_seq")


def _field_names(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    """All field names of a model and its nested models. / 模型及其嵌套模型的全部字段名。"""
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


def _nested_models(model: type[BaseModel], seen: set[type] | None = None) -> set[type]:
    """Every model type reachable from `model`. / 从 `model` 可达的全部模型类型。"""
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


@pytest.mark.parametrize("model", [MissionSpec, TaskNode, MissionPackage, PackageNode])
def test_planner_facing_models_have_no_control_fields(model):
    names = _field_names(model)
    offending = sorted(n for n in names if any(term in n for term in CONTROL_FIELD_TERMS))
    assert not offending, f"{model.__name__} exposes control-level fields: {offending}"


def test_mission_models_cannot_nest_a_control_envelope():
    assert ControlCommandEnvelope not in {c for m in (MissionSpec, MissionPackage) for c in _nested_models(m)}


@pytest.mark.parametrize("key", ["setpoint", "Attitude", "thrust", "pwm", "actuator", "mavlink", "raw_command", "offboard"])
def test_task_params_reject_control_level_keys(key):
    with pytest.raises(ValidationError):
        TaskNode(task_id="t", skill_id="skill.flight.fly_route", params={key: 1})


def test_task_params_accept_skill_level_keys():
    spec = mission_spec()
    assert spec.tasks[0].params == {"altitude_m_agl": 20}


@pytest.mark.parametrize("params", [{"route": {"setpoint": 1}}, {"items": [{"Attitude": 1}]}, {"command_seq": 1}])
@pytest.mark.parametrize("compiled", [False, True])
def test_nested_control_keys_cannot_bypass_task_boundary(params, compiled):
    data = {"task_id": "t", "skill_id": "skill.flight.fly_route", "params": params}
    if compiled:
        data.update(skill_version="0.1.0", robot_id="uav_01", timeout_s=30)
    with pytest.raises(ValidationError):
        (PackageNode if compiled else TaskNode).model_validate(data)


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


SRC = REPO / "src" / "drone_agent"
# Onboard code (M1 runtime plus the M2 signing/state modules) never loads a model or planner (M2 design rule).
# 机载代码（M1 运行时与 M2 签名 / 状态模块）从不加载模型或规划器（M2 设计边界）。
ONBOARD = [SRC / "mission", SRC / "guardian", SRC / "adapters", *(SRC / "runtime" / name for name in (
    "launch.py", "ipc.py", "ledger.py", "wire.py", "recording.py", "signing.py", "robot_state.py", "uplink.py"))]
# Off-board and entry code never holds the guardian control channel (single control egress, D005/D031/D033).
# 机外与入口代码从不持有 guardian 控制通道（单一控制出口，D005/D031/D033）。
OFFBOARD = [SRC / "providers", SRC / "planner", SRC / "admission", SRC / "fleet", SRC / "console",
            SRC / "runtime" / "uplink.py"]


def _imports(paths) -> dict[str, set[str]]:
    import ast

    found = {}
    for root in paths:
        for path in sorted(root.rglob("*.py")) if root.is_dir() else ([root] if root.exists() else []):
            names = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names |= {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module)
            found[str(path.relative_to(REPO))] = names
    return found


def test_onboard_processes_never_import_models_or_planners():
    forbidden = ("drone_agent.providers", "drone_agent.planner", "drone_agent.admission", "httpx")
    offending = {path: sorted(n for n in names if n.startswith(forbidden)) for path, names in _imports(ONBOARD).items()}
    assert not {p: n for p, n in offending.items() if n}, offending


def test_offboard_and_entry_code_cannot_reach_the_guardian_channel():
    forbidden = ("drone_agent.runtime.ipc", "drone.control", "drone_agent.guardian", "drone_agent.adapters", "mavsdk")
    offending = {path: sorted(n for n in names if n.startswith(forbidden)) for path, names in _imports(OFFBOARD).items()}
    assert not {p: n for p, n in offending.items() if n}, offending
    assert any(path.endswith(".py") for path in _imports(OFFBOARD)), "the scan must see the off-board modules"


def test_uplink_import_graph_stays_onboard():
    # Transitively, not only direct imports: the networked onboard process loads no planner, model or
    # guardian channel code (D031). / 传递地而不只是直接导入：联网的机载进程不加载规划、模型或 guardian 通道代码。
    import os
    import subprocess
    import sys

    probe = ("import sys, drone_agent.runtime.uplink; print(sorted(m for m in sys.modules if m.startswith(("
             "'drone_agent.providers', 'drone_agent.planner', 'drone_agent.admission', 'drone_agent.fleet.service', "
             "'drone_agent.runtime.ipc', 'drone_agent.guardian', 'drone_agent.adapters', 'httpx', 'mavsdk'))))")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                            cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    assert result.stdout.strip() == "[]", result.stdout


def test_recovery_policy_is_a_reference_not_planner_content():
    # The planner names a policy; it never authors one (D009). / 规划器只引用策略，不编写策略（D009）。
    spec = mission_spec()
    assert isinstance(spec.recovery_policy_ref, str)
    assert "edges" not in _field_names(MissionSpec)
