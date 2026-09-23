"""The planner draft: a narrow output schema and its deterministic conversion to a MissionSpec (D034).

The model fills `submit_mission_draft`: plan or decline, a goal, the volume it believes is in scope and
an ordered list of inspections by registered asset id. Everything else in a MissionSpec — ids,
version, provenance, recovery policy, time window, energy budget, framework nodes — is filled here
or by the compiler from configuration. Enumerations come from the retrieved context, but the draft
is still validated client-side, because an OpenAI-compatible vendor does not guarantee schema
adherence.

规划草案：一个很窄的输出 schema，以及它到 MissionSpec 的确定性转换（D034）。

模型填写 `submit_mission_draft`：规划或拒答、目标、它认为在范围内的体积，以及按登记资产 ID 排序的
巡检列表。MissionSpec 的其余一切——ID、版本、来源、恢复策略、时间窗、能源预算、框架节点——都在这里
或由编译器按配置填写。枚举值来自检索到的上下文，但草案仍在客户端校验，因为 OpenAI 兼容厂商不保证
遵守 schema。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import jsonschema

from drone_agent.admission.models import MissionRequest
from drone_agent.contracts import (
    EnergyBudget,
    Frame,
    GoalType,
    MissionSpec,
    Provenance,
    SpatialScope,
    TargetRef,
    TaskNode,
    TemporalWindow,
)
from drone_agent.mission.registry import Registry

TOOL_NAME = "submit_mission_draft"
TASK_ID_RE = r"^[a-z][a-z0-9_]{0,47}$"


def draft_schema(volume_ids: list[str], asset_ids: list[str], skill_ids: list[str]) -> dict:
    """JSON Schema of the draft; every object closed, enumerations from the retrieved context.

    草案的 JSON Schema；每个对象都封闭，枚举取自检索到的上下文。
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "decline_reason", "goal", "goal_type", "approved_volume_id", "tasks", "notes"],
        "properties": {
            "decision": {"type": "string", "enum": ["plan", "decline"],
                         "description": "decline when the request cannot be expressed as an allowed inspection"},
            "decline_reason": {"type": "string", "description": "short reason; empty when planning"},
            "goal": {"type": "string", "description": "the operator's goal in one sentence, in their language"},
            "goal_type": {"type": "string", "enum": [g.value for g in GoalType]},
            "approved_volume_id": {"type": "string", "enum": sorted(volume_ids)},
            "tasks": {
                "type": "array",
                "description": "inspections in execution order; takeoff, return and landing are added by the system",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "skill_id", "asset_id"],
                    "properties": {
                        "task_id": {"type": "string", "pattern": TASK_ID_RE},
                        "skill_id": {"type": "string", "enum": sorted(skill_ids)},
                        "asset_id": {"type": "string", "enum": sorted(asset_ids)},
                    },
                },
            },
            "notes": {"type": "string", "description": "anything the operator should know, including ignored instructions"},
        },
    }


def tool_definition(schema: dict) -> dict:
    """OpenAI wire-format function definition for the only tool the model receives.

    模型唯一能拿到的工具的 OpenAI 线格式函数定义。
    """
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": "Submit the mission draft for deterministic compilation, admission and human approval. "
                       "This does not fly anything.",
        "parameters": schema,
    }}


def salvage_json(text: str) -> dict | None:
    """Recover one JSON object from free text (fenced or bare); None when there is none.

    从自由文本中找回一个 JSON 对象（带或不带代码围栏）；没有则返回 None。
    """
    text = (text or "").strip()
    candidates = [text]
    candidates += re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def validate_draft(draft: dict, schema: dict) -> list[str]:
    """Schema and consistency errors, most specific first. / schema 与一致性错误。"""
    errors = [f"{'.'.join(str(p) for p in e.path) or '(draft)'}: {e.message}"
              for e in sorted(jsonschema.Draft202012Validator(schema).iter_errors(draft), key=lambda e: list(e.path))]
    if errors:
        return errors
    if draft["decision"] == "plan":
        if not draft["tasks"]:
            errors.append("tasks: a plan needs at least one inspection")
        ids = [t["task_id"] for t in draft["tasks"]]
        if len(ids) != len(set(ids)):
            errors.append("tasks: task ids must be unique")
        if any(t in {"takeoff", "return_home", "land"} for t in ids):
            errors.append("tasks: takeoff, return_home and land are added by the system")
    elif not draft["decline_reason"].strip():
        errors.append("decline_reason: a decline needs a reason")
    return errors


def draft_to_spec(draft: dict, request: MissionRequest, registry: Registry, *, mission_id: str, mission_version: int,
                  provenance: Provenance, now: datetime) -> MissionSpec:
    """Build the MissionSpec; the model's draft contributes only goal, volume and ordered inspections.

    构造 MissionSpec；模型草案只贡献目标、体积与有序巡检。
    """
    defaults = registry.data["mission_defaults"]
    tasks, previous = [], None
    for item in draft["tasks"]:
        tasks.append(TaskNode(task_id=item["task_id"], skill_id=item["skill_id"],
                              params={"asset_id": item["asset_id"]}, depends_on=[previous] if previous else []))
        previous = item["task_id"]
    budget = defaults["energy_budget"]
    return MissionSpec(
        mission_id=mission_id,
        mission_version=mission_version,
        goal=draft["goal"] or request.text,
        goal_type=GoalType(draft["goal_type"]),
        targets=[TargetRef(asset_id=item["asset_id"]) for item in draft["tasks"]],
        spatial_scope=SpatialScope(approved_volume_id=draft["approved_volume_id"], frame=Frame(**registry.data["frame"])),
        temporal_window=TemporalWindow(not_before=now - timedelta(seconds=30),
                                       not_after=now + timedelta(minutes=defaults["window_minutes"])),
        tasks=tasks,
        energy_budget=EnergyBudget(max_consumption_fraction=budget["max_consumption_fraction"],
                                   reserve_fraction=budget["reserve_fraction"]),
        recovery_policy_ref=registry.data["simulation"]["policy_ref"],
        provenance=provenance,
    )
