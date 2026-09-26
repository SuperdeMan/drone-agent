"""P3 scheduling definitions: the scheduling catalog, task and assignment states, verdicts and reason codes (D059).

A scheduling catalog (`configs/scheduling/*.yaml`) is versioned configuration beside the operations catalog it names.
It places every site map in one shared frame (a translation per site), divides that frame into square airspace cells,
and fixes the ranking rules and the reassignment policy with their versions. It is a separate file so that loading it
never changes the digest of an operations catalog that P1 bindings already pinned. A task is one single-asset
inspection need in one project; an assignment gives it one robot under a cloud-side assignment epoch that is never the
onboard lease epoch. The P3 reason codes join the P1 table, so one verdict rule covers both.

P3 调度定义：调度目录、任务与分配状态、判定与原因码（D059）。

调度目录（`configs/scheduling/*.yaml`）是与其所指运营目录并列的版本化配置。它把每个站点地图放进同一共享坐标系（每个
站点一个平移），把该坐标系划分为方形空域单元，并固定排序规则与改派策略及其版本。它单独成文件，因此加载它从不改变 P1
绑定已固定的运营目录摘要。任务是一个项目内的单资产巡检需求；分配在云端分配代次下给它一台机器人，该代次从不是机载
租约代次。P3 原因码并入 P1 原因表，因此同一条判定规则覆盖两者。
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ConfigDict, Field, model_validator

from drone_agent.contracts.common import ContractModel
from drone_agent.fleet.provenance import digest

CATALOG_FORMAT = "drone.scheduling-catalog/v1"
ID = r"^[a-z][a-z0-9_]{0,39}$"
FRAME = r"^[a-z][a-z0-9_]{0,31}$"
TASK_KEY = re.compile(r"^[A-Za-z0-9_.:#-]{1,160}$")
# Codes that no later state change can clear for this task and candidate. / 之后任何状态变化都无法为该任务与候选清除的原因。
PERMANENT = frozenset({"robot.unbound", "capability.missing_skill", "asset.unregistered", "volume.unapproved",
                       "task.robot_excluded", "window.expired"})


class SchedulingModel(ContractModel):
    """Service-side scheduling record: unknown fields rejected, instances frozen. / 服务侧调度记录：拒绝未知字段、实例冻结。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["0.1.0"] = "0.1.0"


class TaskState(StrEnum):
    """Task lifecycle; cancellation has its own three states. / 任务生命周期；取消有自己的三个状态。"""

    QUEUED = "queued"
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REJECTED = "rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_TASK = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.OUTCOME_UNKNOWN, TaskState.REJECTED,
                           TaskState.CANCELLED})
CANCELLING_TASK = frozenset({TaskState.CANCEL_REQUESTED, TaskState.CANCELLING})


class AssignmentState(StrEnum):
    """One assignment: live, withdrawn before any physical effect, or ended with its mission.

    一次分配：有效、在任何物理动作前撤回，或随其任务结束。
    """

    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    ENDED = "ended"


class TaskVerdict(StrEnum):
    """What one decision concluded for a task. / 一次判定对任务的结论。"""

    ASSIGN = "assign"
    WAIT = "wait"
    REJECT = "reject"


# ── the scheduling catalog / 调度目录 ──


class SiteOrigin(SchedulingModel):
    """Where a site map's origin lies in the shared frame (translation only). / 站点地图原点在共享坐标中的位置（仅平移）。"""

    origin: tuple[float, float]


class AirspaceGrid(SchedulingModel):
    """The shared frame, its square cells and the lost-contact rule. / 共享坐标、其方形单元与失联规则。"""

    frame: str = Field(pattern=FRAME)
    cell_m: float = Field(gt=0.5, le=100)
    buffer_m: float = Field(ge=0, le=50)
    contact_timeout_s: float = Field(gt=0, le=120)
    envelope_margin_m: float = Field(ge=0, le=50)
    position_tolerance_m: float = Field(default=0.5, gt=0, le=5)
    sites: dict[str, SiteOrigin] = Field(min_length=1)


class Ranking(SchedulingModel):
    """Versioned candidate order: estimated arrival, recent use, robot id. / 版本化候选顺序：预计到场、近期使用、robot ID。"""

    version: str = Field(min_length=1)
    cruise_mps: float = Field(gt=0)
    setup_s: float = Field(ge=0)
    usage_window_s: float = Field(gt=0)


class SchedulingPolicy(SchedulingModel):
    """Versioned reassignment and queue limits. / 版本化的改派与队列限值。"""

    version: str = Field(min_length=1)
    reassign_after_s: float = Field(gt=0)
    max_assignments: int = Field(ge=1, le=10)
    task_window_s: float = Field(default=1800.0, gt=0)
    max_tasks_per_pass: int = Field(default=50, ge=1, le=1000)


class SchedulingCatalog(SchedulingModel):
    """The versioned P3 catalog; `check` binds it to an operations catalog and the site maps.

    版本化的 P3 目录；`check` 把它绑定到运营目录与站点地图。
    """

    format: Literal["drone.scheduling-catalog/v1"]
    catalog_id: str = Field(pattern=ID)
    operations_catalog: str = Field(pattern=ID)
    airspace: AirspaceGrid
    ranking: Ranking
    policy: SchedulingPolicy

    @model_validator(mode="after")
    def _ids(self):
        if any(not re.fullmatch(ID, key) for key in self.airspace.sites):
            raise ValueError("site identifiers must be lower-case slugs")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    def origin(self, site_id: str) -> tuple[float, float]:
        return self.airspace.sites[site_id].origin

    def check(self, operations, registries: dict) -> None:
        """Every site has an origin, and an asset several sites of one project register lies at one shared place.

        每个站点都有原点；同一项目多个站点登记的同一资产在共享坐标中位于同一处。
        """
        import math

        if self.operations_catalog != operations.catalog_id:
            raise ValueError(f"written for {self.operations_catalog}, loaded with {operations.catalog_id}")
        missing = sorted(set(operations.sites) - set(self.airspace.sites))
        unknown = sorted(set(self.airspace.sites) - set(operations.sites))
        if missing or unknown:
            raise ValueError(f"site origins differ from the operations catalog: missing {missing}, unknown {unknown}")
        places: dict[tuple[str, str], tuple[str, tuple[float, float]]] = {}
        for site_id, site in operations.sites.items():
            ox, oy = self.origin(site_id)
            data = registries[site_id].data
            for asset_id, asset in data.get("assets", {}).items():
                point = (ox + float(asset["position"][0]), oy + float(asset["position"][1]))
                known = places.get((site.project_id, asset_id))
                if known is not None and math.dist(known[1], point) > self.airspace.position_tolerance_m:
                    raise ValueError(f"{asset_id} of {site.project_id} lies at different shared places in "
                                     f"{known[0]} and {site_id}")
                places.setdefault((site.project_id, asset_id), (site_id, point))
            for key in ("home",):
                if key not in data:
                    raise ValueError(f"{site_id} has no {key}")


def load_scheduling(path: Path) -> SchedulingCatalog:
    return SchedulingCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def task_key_ok(value) -> bool:
    return isinstance(value, str) and TASK_KEY.fullmatch(value) is not None
