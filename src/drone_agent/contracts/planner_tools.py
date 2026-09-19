"""Planner tool allowlist: the only tools the L1 planner may reach through MCP.

Every tool is read-only by contract. Names that look like they write control, missions, leases
or platform state are rejected at load time, so a misconfigured allowlist fails closed.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, field_validator, model_validator

from drone_agent.contracts.common import ContractModel

WRITE_VERBS: frozenset[str] = frozenset(
    {
        "arm",
        "disarm",
        "takeoff",
        "land",
        "goto",
        "set",
        "write",
        "publish",
        "command",
        "control",
        "lease",
        "package",
        "upload",
        "execute",
        "cmd_vel",
        "offboard",
        "kill",
    }
)


class PlannerTool(ContractModel):
    name: str = Field(min_length=1)
    read_only: bool
    description: str = ""

    @field_validator("name")
    @classmethod
    def _no_write_verbs(cls, value: str) -> str:
        tokens = {t for part in value.lower().split(".") for t in part.split("_")}
        bad = sorted(tokens & WRITE_VERBS)
        if bad:
            raise ValueError(f"planner tool name '{value}' contains write verbs {bad}")
        return value

    @model_validator(mode="after")
    def _must_be_read_only(self) -> PlannerTool:
        if not self.read_only:
            raise ValueError(f"planner tool '{self.name}' must be read_only")
        return self


class PlannerToolAllowlist(ContractModel):
    tools: list[PlannerTool] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> PlannerToolAllowlist:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def names(self) -> list[str]:
        return [t.name for t in self.tools]
