"""Planner tool allowlist: the only tools the L1 planner may reach through MCP.

Every tool is read-only by contract. Names that look like they write control, missions, leases
or platform state are rejected at load time, so a misconfigured allowlist fails closed.

规划器工具白名单：L1 规划器经 MCP 能触达的全部工具。

按契约所有工具都是只读。名字看起来会写控制、任务、租约或平台状态的工具在加载时即被拒绝，
配置错了也是 fail closed。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, field_validator, model_validator

from drone_agent.contracts.common import ContractModel

# Tokens that indicate a writing tool; matched against dot- and underscore-split name parts.
# 表示写操作的词；与工具名按点和下划线拆分后的片段比对。
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
    """One read-only tool exposed to the planner. / 暴露给规划器的一个只读工具。"""

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
    """The whole allowlist as loaded from configs/planner_tools.yaml. / 从 configs/planner_tools.yaml 加载的完整白名单。"""

    tools: list[PlannerTool] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> PlannerToolAllowlist:
        """Load and validate the allowlist file. / 加载并校验白名单文件。"""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def names(self) -> list[str]:
        """Tool names in file order. / 按文件顺序返回工具名。"""
        return [t.name for t in self.tools]
