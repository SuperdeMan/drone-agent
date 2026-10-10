"""P2 workflow definitions: versioned templates of whitelisted activities, typed conditions and budgets (D057).

A workflow catalog (`configs/workflows/*.yaml`) is versioned configuration reviewed like the operations catalog. Each
template is an immutable (project, workflow, version) with its triggers and a directed acyclic graph of nodes. Every
node is one whitelisted activity with typed parameters; its condition is a typed predicate over the typed output of
an upstream node. There is no expression language, no code, no URL and no loop: `request_reinspection` starts a
separate run of a template that cannot itself request reinspection. The catalog is checked against the operations
catalog and the site registries, so a node can only name its own project's robots and the registered volumes and
assets of that robot's site. A run pins the template's digest; schedules resolve IANA zones from the pinned tzdata
package with an explicit daylight-saving policy.

P2 工作流定义：白名单活动的版本化模板、类型化条件与预算（D057）。

工作流目录（`configs/workflows/*.yaml`）是与运营目录同样经评审的版本化配置。每个模板是不可变的（项目、流程、版本），
带触发器与一个有向无环节点图。每个节点是一个带类型化参数的白名单活动；其条件是对上游节点类型化输出的类型化谓词。
没有表达式语言、代码、URL 或循环：`request_reinspection` 启动另一个模板的独立运行，而该模板自身不能再请求复检。
目录按运营目录与站点登记表核对，节点只能引用本项目的机器人及其站点登记的体积与资产。运行固定模板摘要；排班用固定
版本的 tzdata 包解析 IANA 时区，并有明确的夏令时策略。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import AwareDatetime, ConfigDict, Field, StrictBool, model_serializer, model_validator

from drone_agent.contracts.common import ContractModel
from drone_agent.fleet.provenance import digest

CATALOG_FORMAT = "drone.workflow-catalog/v1"
FIXTURE_FORMAT = "drone.analysis-fixture/v1"
ID = r"^[a-z][a-z0-9_]{0,39}$"
NODE_ID = r"^[a-z][a-z0-9_]{0,47}$"
EVENT_PRINCIPAL = r"^event:[A-Za-z0-9_.-]{1,80}$"
EVENT_ID = r"^[A-Za-z0-9_.:-]{1,120}$"
ZONE_KEY = re.compile(r"^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]{1,30}){1,2})$")
# Hard limits of one template; a bigger process is several templates. / 单个模板的硬上限；更大的流程拆成多个模板。
MAX_NODES = 40
MAX_MISSIONS = 10
MAX_WAIT_S = 30 * 86400
INSPECTION_SKILL = "skill.inspect.asset"


class WorkflowModel(ContractModel):
    """Service-side workflow record: unknown fields rejected, instances frozen. / 服务侧工作流记录：拒绝未知字段、实例冻结。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["0.1.0"] = "0.1.0"


class Activity(StrEnum):
    """The only activities a node may run (09-operations §4.1). / 节点唯一可运行的活动（09-operations §4.1）。"""

    SUBMIT_MISSION = "submit_mission"
    AWAIT_MISSION = "await_mission"
    ANALYZE_EVIDENCE = "analyze_evidence"
    HUMAN_REVIEW = "human_review"
    CREATE_WORK_ORDER = "create_work_order"
    AWAIT_REPAIR = "await_repair"
    REQUEST_REINSPECTION = "request_reinspection"
    BUILD_REPORT = "build_report"
    # P4 (D063): settle one reinspection round of a business work order. / P4（D063）：结算业务工单的一轮复检。
    SETTLE_REINSPECTION = "settle_reinspection"
    # P6 (D078): the first completed of an analysis and its recapture; no effect. / P6（D078）：取分析及其补拍中首个完成者；无副作用。
    SELECT_ANALYSIS = "select_analysis"


class RunState(StrEnum):
    """Workflow run lifecycle; cancellation has its own three states. / 工作流运行生命周期；取消有自己的三个状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_RUN = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.OUTCOME_UNKNOWN, RunState.CANCELLED})
CANCELLING_RUN = frozenset({RunState.CANCEL_REQUESTED, RunState.CANCELLING})


class NodeState(StrEnum):
    """One node's state; `running` means an effect is being delivered. / 单个节点的状态；`running` 表示效果投递中。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"


TERMINAL_NODE = frozenset({NodeState.COMPLETED, NodeState.SKIPPED, NodeState.FAILED, NodeState.OUTCOME_UNKNOWN,
                           NodeState.CANCELLED})


class SkipReason(StrEnum):
    """Why a node did not run; only the first two are a normal business path. / 节点未运行的原因；只有前两个是正常业务路径。"""

    CONDITION_FALSE = "condition_false"
    UPSTREAM_SKIPPED = "upstream_skipped"
    UPSTREAM_FAILED = "upstream_failed"
    UPSTREAM_UNKNOWN = "upstream_unknown"


LEGITIMATE_SKIPS = frozenset({SkipReason.CONDITION_FALSE, SkipReason.UPSTREAM_SKIPPED})


class WaitReason(StrEnum):
    """What a waiting node waits for (09-operations §4.1). / 等待中的节点在等什么（09-operations §4.1）。"""

    APPROVAL = "approval"
    DEVICE = "device"
    ENVIRONMENT = "environment"
    FLIGHT = "flight"
    EVIDENCE = "evidence"
    REVIEW = "review"
    REPAIR = "repair"
    DELIVERY = "delivery"
    ASSIGNMENT = "assignment"  # a P3 task waits for the scheduler (D059) / P3 任务等待调度器（D059）
    ANALYSIS = "analysis"  # a P4 analysis job is queued or running (D063) / P4 分析作业排队或运行中（D063）


def activity_key(run_id: str, node_id: str, occurrence: int = 1) -> str:
    """Logical activity identity; transport retries never change it (09-operations §4.2).

    逻辑活动身份；传输重试从不改变它（09-operations §4.2）。
    """
    return f"wf:{run_id}:{node_id}:{occurrence}"


def run_principal(run_id: str) -> str:
    """The requester name a run's missions carry. / 运行所创建任务携带的请求者名。"""
    return f"workflow:{run_id}"


# ── node parameters / 节点参数 ──


class InputRef(WorkflowModel):
    """A reference to one declared run input; never an expression. / 对一个已声明运行输入的引用；从不是表达式。"""

    input: str = Field(pattern=ID)


class SubmitMissionParams(WorkflowModel):
    """One single-asset inspection: for a fixed robot (P2), or a P3 task the scheduler assigns among candidates.

    With `robot_id` the node submits the mission itself (D057). Without it the node submits a task: `candidates` lists
    the project robots it may go to (empty for every robot of the project) and `priority` orders the queue (D059).
    The P3 fields are left out of the canonical form while unused, so every P2 template keeps its digest.

    单资产巡检：固定机器人（P2），或由调度器在候选中分配的 P3 任务。带 `robot_id` 时节点自己提交任务（D057）。不带时节点
    提交任务单：`candidates` 列出可去的本项目机器人（为空即本项目全部机器人），`priority` 决定排队顺序（D059）。P3 字段
    未使用时不进入规范形式，因此每个 P2 模板的摘要保持不变。
    """

    robot_id: str | None = Field(default=None, pattern=ID)
    volume_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    asset: str | InputRef = Field(description="registered asset id or run input / 登记资产 ID 或运行输入")
    candidates: tuple[str, ...] = Field(default=(), max_length=50)
    priority: int = Field(default=0, ge=0, le=9)
    # P6 (D078): this node flies the same pose again after the named analysis was refused for a recapturable reason;
    # left out of the canonical form while unused. / P6（D078）：所指分析因可补拍原因拒判后，本节点以同一拍摄位再飞一次；
    # 未使用时不进入规范形式。
    recapture_of: str | None = Field(default=None, pattern=NODE_ID)

    @model_validator(mode="after")
    def _mode(self):
        if self.robot_id is not None and (self.candidates or self.priority):
            raise ValueError("a fixed robot takes neither candidates nor a priority")
        if any(not re.fullmatch(ID, robot) for robot in self.candidates) or \
                len(set(self.candidates)) != len(self.candidates):
            raise ValueError("candidates are distinct robot ids")
        if self.recapture_of is not None and self.robot_id is None:
            raise ValueError("a recapture flies a fixed robot (D078)")
        return self

    @model_serializer(mode="wrap")
    def _canonical(self, handler):
        data = handler(self)
        if self.robot_id is None:
            data.pop("robot_id", None)
        if not self.candidates:
            data.pop("candidates", None)
        if not self.priority:
            data.pop("priority", None)
        if self.recapture_of is None:
            data.pop("recapture_of", None)
        return data

    @property
    def assigned(self) -> bool:
        """Whether the scheduler chooses the robot (P3). / 是否由调度器选择机器人（P3）。"""
        return self.robot_id is None

    def asset_pattern_ok(self) -> bool:
        return isinstance(self.asset, InputRef) or re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", self.asset) is not None


class AwaitMissionParams(WorkflowModel):
    mission_from: str = Field(pattern=NODE_ID, description="a submit_mission node / submit_mission 节点")


class AnalyzeParams(WorkflowModel):
    """P2: an inline labelled analysis. P4 (`findings: true`): a durable analysis job whose suspected result joins a
    finding; `purpose: reinspection` analyzes a work order's reinspection capture instead (D063). The P4 fields are
    left out of the canonical form while unused, so every P2 and P3 template keeps its digest.

    P2：内联的带标注分析。P4（`findings: true`）：持久分析作业，疑似结果归入发现；`purpose: reinspection` 则分析工单的
    复检采集（D063）。P4 字段未使用时不进入规范形式，因此每个 P2 与 P3 模板的摘要保持不变。
    """

    inspection_from: str = Field(pattern=NODE_ID, description="an await_mission node / await_mission 节点")
    analyzer: str = Field(pattern=ID, description="an analyzer of the catalog / 目录中的分析器")
    findings: bool = False
    purpose: Literal["inspection", "reinspection"] = "inspection"

    @model_validator(mode="after")
    def _purpose(self):
        if self.purpose == "reinspection" and not self.findings:
            raise ValueError("a reinspection analysis is a P4 job (findings: true)")
        return self

    @model_serializer(mode="wrap")
    def _canonical(self, handler):
        data = handler(self)
        if not self.findings:
            data.pop("findings", None)
        if self.purpose == "inspection":
            data.pop("purpose", None)
        return data


class ReviewParams(WorkflowModel):
    analysis_from: str = Field(pattern=NODE_ID, description="an analyze_evidence node / analyze_evidence 节点")
    timeout_s: int | None = Field(default=None, ge=60, le=MAX_WAIT_S)


class ReinspectionTarget(WorkflowModel):
    """The pinned template a P4 order's repair feedback starts (D063 §8). / P4 工单维修反馈所启动的固定模板（D063 §8）。"""

    workflow_id: str = Field(pattern=ID)
    version: int = Field(ge=1)
    asset_input: str = Field(pattern=ID)


class WorkOrderParams(WorkflowModel):
    """P2: a simulated order of one review node. P4: the one order of a confirmed finding, with its reinspection
    template (D063). / P2：一个复核节点的模拟工单。P4：已确认发现的唯一工单，带其复检模板（D063）。"""

    review_from: str = Field(pattern=NODE_ID, description="a human_review node / human_review 节点")
    reinspection: ReinspectionTarget | None = None

    @model_serializer(mode="wrap")
    def _canonical(self, handler):
        data = handler(self)
        if self.reinspection is None:
            data.pop("reinspection", None)
        return data


class AwaitRepairParams(WorkflowModel):
    order_from: str = Field(pattern=NODE_ID, description="a create_work_order node / create_work_order 节点")
    timeout_s: int | None = Field(default=None, ge=60, le=MAX_WAIT_S)


class ReinspectionParams(WorkflowModel):
    """Start one run of a pinned reinspection template for the order's asset. / 为工单资产启动一次固定版本复检模板的运行。"""

    order_from: str = Field(pattern=NODE_ID)
    repair_from: str = Field(pattern=NODE_ID)
    workflow_id: str = Field(pattern=ID)
    version: int = Field(ge=1)
    asset_input: str = Field(pattern=ID, description="the target's asset input / 目标模板的资产输入名")


class ReportParams(WorkflowModel):
    """The report covers the whole run. / 报告覆盖整个运行。"""


class SettleParams(WorkflowModel):
    """Settle the run's reinspection round from its analysis and the round's review (`reinspection-v1`, D063 §9).

    按分析与本轮复核结算运行的复检轮次（`reinspection-v1`，D063 §9）。
    """

    analysis_from: str = Field(pattern=NODE_ID, description="the reinspection analyze_evidence node / 复检分析节点")
    review_from: str = Field(pattern=NODE_ID, description="the round's human_review node / 本轮复核节点")


class SelectParams(WorkflowModel):
    """P6 (D078): the analyses to choose from, in order; the first that completed is the output.

    P6（D078）：按顺序列出的候选分析；首个完成者即为输出。
    """

    analyses: tuple[str, ...] = Field(min_length=2, max_length=3)


class Predicate(WorkflowModel):
    """`node.output == equals` over a typed output; the only condition form. `recapture` (P6, D078) is the one output
    a failed node has: whether the business catalog let its refusal be recaptured.

    对类型化输出的相等判断；唯一的条件形式。`recapture`（P6，D078）是失败节点唯一的输出：业务目录是否允许其拒判补拍。
    """

    node: str = Field(pattern=NODE_ID)
    output: Literal["suspected", "decision", "recapture"]
    # Strict: "yes" or 1 is never coerced into true. / 严格类型："yes" 或 1 从不被转换为 true。
    equals: StrictBool | Literal["confirmed", "dismissed"]


# Outputs a predicate may read, by the activities that produce them and their values. / 谓词可读的输出：产生它的活动与取值。
PREDICATE_OUTPUTS: dict[str, tuple[tuple[Activity, ...], tuple]] = {
    "suspected": ((Activity.ANALYZE_EVIDENCE, Activity.SELECT_ANALYSIS), (True, False)),
    "decision": ((Activity.HUMAN_REVIEW,), ("confirmed", "dismissed")),
    "recapture": ((Activity.ANALYZE_EVIDENCE,), (True, False)),
}
# A reference to an analysis may name the selection over an analysis and its recapture (P6, D078).
# 对分析的引用可以指向分析及其补拍之上的选择节点（P6，D078）。
STANDS_FOR = {Activity.ANALYZE_EVIDENCE: (Activity.ANALYZE_EVIDENCE, Activity.SELECT_ANALYSIS)}


class _Node(WorkflowModel):
    node_id: str = Field(pattern=NODE_ID)
    after: tuple[str, ...] = Field(default=(), max_length=MAX_NODES)
    requires: Literal["success", "done"] = Field(
        default="success", description="`after` nodes must succeed, or only finish / `after` 节点须成功，或只须结束")
    when: tuple[Predicate, ...] = Field(default=(), max_length=4)

    def references(self) -> list[tuple[str, Activity]]:
        """Upstream nodes whose output this node reads, with their required activity. / 本节点读取其输出的上游节点。"""
        return []


class SubmitMissionNode(_Node):
    activity: Literal["submit_mission"]
    params: SubmitMissionParams


class AwaitMissionNode(_Node):
    activity: Literal["await_mission"]
    params: AwaitMissionParams

    def references(self):
        return [(self.params.mission_from, Activity.SUBMIT_MISSION)]


class AnalyzeNode(_Node):
    activity: Literal["analyze_evidence"]
    params: AnalyzeParams

    def references(self):
        return [(self.params.inspection_from, Activity.AWAIT_MISSION)]


class ReviewNode(_Node):
    activity: Literal["human_review"]
    params: ReviewParams

    def references(self):
        return [(self.params.analysis_from, Activity.ANALYZE_EVIDENCE)]


class WorkOrderNode(_Node):
    activity: Literal["create_work_order"]
    params: WorkOrderParams

    def references(self):
        return [(self.params.review_from, Activity.HUMAN_REVIEW)]


class AwaitRepairNode(_Node):
    activity: Literal["await_repair"]
    params: AwaitRepairParams

    def references(self):
        return [(self.params.order_from, Activity.CREATE_WORK_ORDER)]


class ReinspectionNode(_Node):
    activity: Literal["request_reinspection"]
    params: ReinspectionParams

    def references(self):
        return [(self.params.order_from, Activity.CREATE_WORK_ORDER), (self.params.repair_from, Activity.AWAIT_REPAIR)]


class ReportNode(_Node):
    activity: Literal["build_report"]
    params: ReportParams = ReportParams()


class SettleNode(_Node):
    """It must settle whatever its inputs did, so it reads them through `after` with `requires: done` instead of as
    references (a failed reference would skip it). / 无论输入结果如何都必须结算，因此经 `after` 与 `requires: done` 读取
    输入，而不作为引用（引用失败会使其被跳过）。"""

    activity: Literal["settle_reinspection"]
    params: SettleParams


class SelectNode(_Node):
    """P6 (D078): like the settle node it reads its analyses through `after` with `requires: done`, because the
    analysis a recapture replaced has failed. / P6（D078）：与结算节点一样经 `after` 与 `requires: done` 读取分析，
    因为被补拍接替的分析已经失败。"""

    activity: Literal["select_analysis"]
    params: SelectParams


Node = Annotated[SubmitMissionNode | AwaitMissionNode | AnalyzeNode | ReviewNode | WorkOrderNode | AwaitRepairNode
                 | ReinspectionNode | ReportNode | SettleNode | SelectNode, Field(discriminator="activity")]


# ── triggers and schedules / 触发器与排班 ──


class InputSpec(WorkflowModel):
    """A run input: one of the listed registered assets. / 运行输入：所列登记资产之一。"""

    kind: Literal["asset_id"]
    choices: tuple[str, ...] = Field(min_length=1, max_length=20)
    required: bool = True


class DailySchedule(WorkflowModel):
    """Local wall-clock time in an IANA zone; gaps shift forward (PEP 495 fold=0), overlaps run once, first.

    IANA 时区中的本地时刻；夏令时空档按 PEP 495 fold=0 向后顺延，重叠时刻只在第一次运行一次。
    """

    every: Literal["day"]
    at: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=3, max_length=64)
    weekdays: tuple[int, ...] = Field(default=(), max_length=7, description="ISO 1-7; empty = every day / 空为每天")
    nonexistent: Literal["shift_forward"] = "shift_forward"
    ambiguous: Literal["first"] = "first"

    @model_validator(mode="after")
    def _values(self):
        zone(self.timezone)
        if any(day not in range(1, 8) for day in self.weekdays) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays are distinct ISO days 1-7")
        return self


class IntervalSchedule(WorkflowModel):
    """Every N minutes on UTC multiples of N (anchored at 1970-01-01T00:00Z). / 每 N 分钟，锚定 UTC 纪元的 N 分钟整倍数。"""

    every: Literal["interval"]
    minutes: int = Field(ge=1, le=1440)


class ManualTrigger(WorkflowModel):
    trigger_id: str = Field(pattern=ID)
    kind: Literal["manual"]


class ScheduleTrigger(WorkflowModel):
    """At most the latest due occurrence starts, and only within `start_window_s`; older ones are recorded missed
    unless `catch_up` explicitly allows a bounded number within `catch_up_window_s` (09-operations §4.2).

    最多只启动最近一个到期发生时刻，且只在 `start_window_s` 内；更早的记为错过，除非 `catch_up` 明确允许在
    `catch_up_window_s` 内补跑有限个（09-operations §4.2）。
    """

    trigger_id: str = Field(pattern=ID)
    kind: Literal["schedule"]
    schedule: DailySchedule | IntervalSchedule = Field(discriminator="every")
    start_window_s: int = Field(default=600, ge=1, le=86400)
    catch_up: int = Field(default=0, ge=0, le=3)
    catch_up_window_s: int = Field(default=0, ge=0, le=7 * 86400)
    inputs: dict[str, str] = Field(default_factory=dict, description="fixed inputs of scheduled runs / 排班运行的固定输入")

    @model_validator(mode="after")
    def _catch_up(self):
        if self.catch_up and self.catch_up_window_s <= self.start_window_s:
            raise ValueError("catch_up needs a catch_up_window_s longer than start_window_s")
        return self


class EventTrigger(WorkflowModel):
    """Only `source` may raise it, only in this template's project, and only with these payload keys.

    只有 `source` 可以触发，只在本模板的项目内，且载荷只能有这些键。
    """

    trigger_id: str = Field(pattern=ID)
    kind: Literal["event"]
    source: str = Field(pattern=EVENT_PRINCIPAL)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    inputs_from_event: tuple[str, ...] = Field(default=(), max_length=4)


class InternalTrigger(WorkflowModel):
    """Started only by a `request_reinspection` node of the same project. / 只由同项目的 `request_reinspection` 节点启动。"""

    trigger_id: str = Field(pattern=ID)
    kind: Literal["internal"]


Trigger = Annotated[ManualTrigger | ScheduleTrigger | EventTrigger | InternalTrigger, Field(discriminator="kind")]


def zone(key: str) -> ZoneInfo:
    """An IANA zone from the pinned tzdata package, never the host database. / 从固定版本 tzdata 包加载 IANA 时区，不用宿主库。"""
    if not ZONE_KEY.fullmatch(key):
        raise ValueError(f"not an IANA zone key: {key!r}")
    try:
        with resources.files("tzdata.zoneinfo").joinpath(*key.split("/")).open("rb") as stream:
            return ZoneInfo.from_file(stream, key=key)
    except (FileNotFoundError, IsADirectoryError, ValueError) as error:
        raise ValueError(f"unknown time zone {key!r}") from error


def tz_database_version() -> str:
    import tzdata

    return tzdata.IANA_VERSION


def _local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of a local wall-clock time; fold=0 gives the first of an overlap and shifts a gap forward.

    本地时刻对应的 UTC 瞬间；fold=0 取重叠时刻的第一次，并把空档时刻向后顺延。
    """
    return datetime.combine(day, at, tzinfo=tz).replace(fold=0).astimezone(timezone.utc)


def next_occurrence(schedule: DailySchedule | IntervalSchedule, after: datetime) -> datetime:
    """The first occurrence strictly after `after` (UTC). / 严格晚于 `after` 的第一个发生时刻（UTC）。"""
    if after.tzinfo is None:
        raise ValueError("an aware instant is required")
    if isinstance(schedule, IntervalSchedule):
        step = schedule.minutes * 60
        seconds = int(after.timestamp()) // step * step + step
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    tz = zone(schedule.timezone)
    hour, minute = (int(part) for part in schedule.at.split(":"))
    start = after.astimezone(tz).date() - timedelta(days=1)
    for offset in range(0, 16):
        day = start + timedelta(days=offset)
        if schedule.weekdays and day.isoweekday() not in schedule.weekdays:
            continue
        candidate = _local(day, time(hour, minute), tz)
        if candidate > after:
            return candidate
    raise ValueError("no occurrence within two weeks")


def due_occurrences(trigger: ScheduleTrigger, cursor: datetime, now: datetime,
                    limit: int = 1000) -> tuple[list[datetime], list[datetime], datetime]:
    """(fire, missed, next cursor) for the occurrences from `cursor` up to `now`.

    Only the latest past occurrence may start, and only inside the start window; every other past occurrence is
    missed, except the most recent `catch_up` ones inside the catch-up window. A restart therefore never replays a
    backlog.

    从 `cursor` 到 `now` 的发生时刻：（启动，错过，下一个游标）。只有最近一个过去的发生时刻可以启动，且必须在启动窗
    内；其余过去的发生时刻都记为错过，只有补跑窗口内最近的 `catch_up` 个例外。因此重启从不重放积压。
    """
    past, current = [], cursor
    while current <= now and len(past) < limit:
        past.append(current)
        current = next_occurrence(trigger.schedule, current)
    fire: list[datetime] = []
    missed = list(past)
    if past and (now - past[-1]).total_seconds() <= trigger.start_window_s:
        fire.append(missed.pop())
    if trigger.catch_up:
        eligible = [o for o in missed if (now - o).total_seconds() <= trigger.catch_up_window_s]
        chosen = eligible[-trigger.catch_up:]
        missed = [o for o in missed if o not in chosen]
        fire = sorted(chosen + fire)
    return fire, missed, current


# ── the template / 模板 ──


class Budget(WorkflowModel):
    """Static limits checked on load. / 加载时检查的静态上限。"""

    max_missions: int = Field(ge=1, le=MAX_MISSIONS)


class WorkflowSpec(WorkflowModel):
    """One immutable template version. / 一个不可变的模板版本。"""

    workflow_id: str = Field(pattern=ID)
    version: int = Field(ge=1)
    project_id: str = Field(pattern=ID)
    title: str = Field(min_length=1, max_length=160)
    inputs: dict[str, InputSpec] = Field(default_factory=dict, max_length=4)
    triggers: tuple[Trigger, ...] = Field(min_length=1, max_length=6)
    nodes: tuple[Node, ...] = Field(min_length=1, max_length=MAX_NODES)
    budget: Budget

    @model_validator(mode="after")
    def _graph(self):
        nodes = {node.node_id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("node ids must be unique")
        if len({t.trigger_id for t in self.triggers}) != len(self.triggers):
            raise ValueError("trigger ids must be unique")
        for kind in ("manual", "internal"):
            if sum(1 for t in self.triggers if t.kind == kind) > 1:
                raise ValueError(f"at most one {kind} trigger")
        if any(not re.fullmatch(ID, name) for name in self.inputs):
            raise ValueError("input names must be lower-case slugs")
        awaited: dict[str, str] = {}
        for node in self.nodes:
            for dependency in node.after:
                if dependency not in nodes or dependency == node.node_id:
                    raise ValueError(f"{node.node_id} runs after an unknown node {dependency}")
            for reference, activity in node.references():
                target = nodes.get(reference)
                if target is None or target.activity not in {a.value for a in STANDS_FOR.get(activity, (activity,))}:
                    raise ValueError(f"{node.node_id} must read a {activity.value} node, not {reference}")
            if isinstance(node, AwaitMissionNode):
                if node.params.mission_from in awaited:
                    raise ValueError(f"{node.params.mission_from} is awaited twice")
                awaited[node.params.mission_from] = node.node_id
            if isinstance(node, SubmitMissionNode):
                if isinstance(node.params.asset, InputRef) and node.params.asset.input not in self.inputs:
                    raise ValueError(f"{node.node_id} reads an undeclared input {node.params.asset.input}")
                if not node.params.asset_pattern_ok():
                    raise ValueError(f"{node.node_id} names an invalid asset id")
            for predicate in node.when:
                activities, values = PREDICATE_OUTPUTS[predicate.output]
                target = nodes.get(predicate.node)
                if target is None or target.activity not in {a.value for a in activities} \
                        or predicate.equals not in values or type(predicate.equals) is not type(values[0]):
                    raise ValueError(f"{node.node_id} has a condition on {predicate.node}.{predicate.output} "
                                     f"that the node cannot produce")
        submits = [n.node_id for n in self.nodes if isinstance(n, SubmitMissionNode)]
        if sorted(submits) != sorted(awaited):
            raise ValueError("every submit_mission node needs exactly one await_mission node")
        if len(submits) > self.budget.max_missions:
            raise ValueError(f"{len(submits)} missions exceed the budget of {self.budget.max_missions}")
        self.order()
        for node in self.nodes:
            ancestors = self.ancestors(node.node_id)
            for predicate in node.when:
                if predicate.node not in ancestors:
                    raise ValueError(f"{node.node_id} depends on a condition of {predicate.node}, not an ancestor")
            if isinstance(node, WorkOrderNode) and Predicate(node=node.params.review_from, output="decision",
                                                             equals="confirmed") not in node.when:
                # A model finding never becomes a work order without a human confirmation (D052).
                # 没有人工确认，模型发现永远不会成为工单（D052）。
                raise ValueError(f"{node.node_id} must require a confirmed decision of {node.params.review_from}")
            if isinstance(node, ReinspectionNode):
                repair = nodes[node.params.repair_from]
                if repair.params.order_from != node.params.order_from:
                    raise ValueError(f"{node.node_id} must reinspect the order its repair node awaits")
        self._business_rules(nodes)
        for trigger in self.triggers:
            self._check_trigger(trigger)
        return self

    def _business_rules(self, nodes: dict) -> None:
        """P4 (D063): a P4 order pins its reinspection template; only a reinspection template settles a round, from
        its reinspection analysis and the round's review, and that review only asks about an unsuspected capture.

        P4（D063）：P4 工单固定其复检模板；只有复检模板结算轮次，依据其复检分析与本轮复核，且该复核只针对未疑似的采集。
        """
        def analysis_of(review_id: str):
            return self.analysis(nodes[review_id].params.analysis_from)

        self._recapture_rules(nodes)
        rechecks = [n for n in self.nodes if isinstance(n, AnalyzeNode) and n.params.purpose == "reinspection"]
        settles = [n for n in self.nodes if isinstance(n, SettleNode)]
        for node in self.nodes:
            if isinstance(node, WorkOrderNode):
                analysis = analysis_of(node.params.review_from)
                if analysis.params.findings and (node.params.reinspection is None
                                                 or analysis.params.purpose != "inspection"):
                    raise ValueError(f"{node.node_id}: a P4 order comes from an inspection analysis and pins its "
                                     f"reinspection template")
                if not analysis.params.findings and node.params.reinspection is not None:
                    raise ValueError(f"{node.node_id}: only a P4 order (findings) has a reinspection template")
            if isinstance(node, (AwaitRepairNode, ReinspectionNode)) and \
                    nodes[node.params.order_from].params.reinspection is not None:
                raise ValueError(f"{node.node_id}: a P4 order starts its reinspection from repair feedback")
            if isinstance(node, ReviewNode) and analysis_of(node.node_id).params.purpose == "reinspection":
                condition = Predicate(node=node.params.analysis_from, output="suspected", equals=False)
                if condition not in node.when:
                    raise ValueError(f"{node.node_id}: a reinspection review only asks about an unsuspected capture")
        if not rechecks and not settles:
            return
        if len(rechecks) != 1 or len(settles) != 1:
            raise ValueError("a reinspection template has exactly one reinspection analysis and one settle node")
        if not any(t.kind == "internal" for t in self.triggers):
            raise ValueError("a reinspection template is started only by its order (internal trigger)")
        settle, recheck = settles[0], rechecks[0]
        review = nodes.get(settle.params.review_from)
        if settle.params.analysis_from != recheck.node_id or not isinstance(review, ReviewNode) \
                or review.params.analysis_from != recheck.node_id:
            raise ValueError(f"{settle.node_id} settles from the reinspection analysis and its review")
        if settle.requires != "done" or not {recheck.node_id, review.node_id} <= set(settle.after):
            raise ValueError(f"{settle.node_id} runs after its analysis and review whatever they did (requires: done)")
        for node in self.nodes:
            if isinstance(node, WorkOrderNode) and nodes[node.params.review_from].params.analysis_from \
                    == recheck.node_id:
                raise ValueError(f"{node.node_id}: a reinspection never opens an order")

    def _recapture_rules(self, nodes: dict) -> None:
        """P6 (D078): a recapture flies the same fixed robot, volume and asset once after one inspection analysis,
        only when that analysis may be recaptured, and its own analysis is the same; a selection chooses among
        inspection analyses of one analyzer whatever they did.

        P6（D078）：补拍在一次巡检分析之后、仅当该分析可补拍时，以相同的固定机器人、体积与资产飞一次，其分析与原分析相同；
        选择节点无论候选分析结果如何都在同一分析器的巡检分析中选择。
        """
        recaptured: set[str] = set()
        for node in self.nodes:
            for predicate in node.when:
                if predicate.output == "recapture" and not (isinstance(node, SubmitMissionNode)
                                                            and node.params.recapture_of == predicate.node):
                    raise ValueError(f"{node.node_id}: only the recapture of {predicate.node} reads its recapture")
            if isinstance(node, SelectNode):
                choices = [nodes.get(name) for name in node.params.analyses]
                if len(set(node.params.analyses)) != len(choices) or any(
                        not isinstance(c, AnalyzeNode) or not c.params.findings or c.params.purpose != "inspection"
                        for c in choices) or len({c.params.analyzer for c in choices}) != 1:
                    raise ValueError(f"{node.node_id} selects among distinct inspection analyses of one analyzer")
                if node.requires != "done" or not set(node.params.analyses) <= set(node.after) or node.when:
                    raise ValueError(f"{node.node_id} runs after its analyses whatever they did, unconditionally")
            if not isinstance(node, SubmitMissionNode) or node.params.recapture_of is None:
                continue
            target = nodes.get(node.params.recapture_of)
            if not isinstance(target, AnalyzeNode) or not target.params.findings \
                    or target.params.purpose != "inspection":
                raise ValueError(f"{node.node_id} recaptures {node.params.recapture_of}, not an inspection analysis")
            if target.node_id in recaptured:
                raise ValueError(f"{target.node_id} is recaptured twice")
            recaptured.add(target.node_id)
            gate = (Predicate(node=target.node_id, output="recapture", equals=True),)
            if node.requires != "done" or target.node_id not in node.after or node.when != gate:
                raise ValueError(f"{node.node_id} runs after {target.node_id} whatever it did, only when it may be "
                                 f"recaptured")
            original = nodes[nodes[target.params.inspection_from].params.mission_from]
            if original.params.recapture_of is not None:
                raise ValueError(f"{node.node_id}: a recapture is never recaptured")
            if original.params.model_dump(exclude={"recapture_of"}) != node.params.model_dump(exclude={"recapture_of"}):
                raise ValueError(f"{node.node_id} flies the robot, volume and asset of {original.node_id}")
            waits = {n.node_id for n in self.nodes if isinstance(n, AwaitMissionNode)
                     and n.params.mission_from == node.node_id}
            for analysis in self.nodes:
                if isinstance(analysis, AnalyzeNode) and analysis.params.inspection_from in waits and (
                        analysis.params.analyzer != target.params.analyzer or not analysis.params.findings
                        or analysis.params.purpose != "inspection"):
                    raise ValueError(f"{analysis.node_id} analyses the recapture like {target.node_id}")

    def analysis(self, node_id: str):
        """The analysis node a reference stands for: itself, or the first analysis of a selection (P6).

        引用所代表的分析节点：自身，或选择节点的第一个候选分析（P6）。
        """
        node = self.node(node_id)
        return self.node(node.params.analyses[0]) if isinstance(node, SelectNode) else node

    def _check_trigger(self, trigger) -> None:
        required = {name for name, spec in self.inputs.items() if spec.required}
        if isinstance(trigger, ScheduleTrigger):
            if set(trigger.inputs) - set(self.inputs) or required - set(trigger.inputs):
                raise ValueError(f"schedule {trigger.trigger_id} must fix every required input and nothing else")
            for name, value in trigger.inputs.items():
                if value not in self.inputs[name].choices:
                    raise ValueError(f"schedule {trigger.trigger_id} input {name} is not one of its choices")
        if isinstance(trigger, EventTrigger):
            names = set(trigger.inputs_from_event)
            if len(names) != len(trigger.inputs_from_event) or names - set(self.inputs) or required - names:
                raise ValueError(f"event {trigger.trigger_id} must carry every required input and nothing else")

    def dependencies(self, node_id: str) -> set[str]:
        """`after` nodes plus the nodes whose output it reads. / `after` 节点加上其读取输出的节点。"""
        node = self.node(node_id)
        return set(node.after) | {reference for reference, _ in node.references()}

    def node(self, node_id: str):
        return next(n for n in self.nodes if n.node_id == node_id)

    def order(self) -> list[str]:
        """Deterministic topological order; a cycle is refused. / 确定性的拓扑顺序；有环即拒绝。"""
        remaining = {node.node_id: set(self.dependencies(node.node_id)) for node in self.nodes}
        declared = [node.node_id for node in self.nodes]
        ordered: list[str] = []
        while remaining:
            ready = [node_id for node_id in declared if node_id in remaining and not remaining[node_id]]
            if not ready:
                raise ValueError(f"the node graph has a cycle through {sorted(remaining)}")
            for node_id in ready:
                ordered.append(node_id)
                del remaining[node_id]
                for dependencies in remaining.values():
                    dependencies.discard(node_id)
        return ordered

    def ancestors(self, node_id: str) -> set[str]:
        found, pending = set(), list(self.dependencies(node_id))
        while pending:
            current = pending.pop()
            if current not in found:
                found.add(current)
                pending.extend(self.dependencies(current))
        return found

    def trigger(self, trigger_id: str):
        return next((t for t in self.triggers if t.trigger_id == trigger_id), None)

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    def check_inputs(self, values: dict) -> dict[str, str]:
        """Validated run inputs: declared names, allowed choices, every required one present.

        校验后的运行输入：名称已声明、取值在选项内、必填项齐全。
        """
        if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
            raise ValueError("inputs are a mapping of names to registered ids")
        unknown = set(values) - set(self.inputs)
        missing = {name for name, spec in self.inputs.items() if spec.required} - set(values)
        if unknown or missing:
            raise ValueError(f"inputs differ from the template: unknown {sorted(unknown)}, missing {sorted(missing)}")
        for name, value in values.items():
            if value not in self.inputs[name].choices:
                raise ValueError(f"input {name} must be one of {list(self.inputs[name].choices)}")
        return dict(sorted(values.items()))


# ── analyzers / 分析器 ──


class ScriptedAnalyzer(WorkflowModel):
    """Answers from a labelled fixture; recorded as `scripted`, never as model output. / 按带标注夹具作答；记为 scripted。"""

    kind: Literal["scripted"]
    fixture: str = Field(min_length=1)


class SignatureAnalyzer(WorkflowModel):
    """Measures the registered colour signature and compares it with a per-asset nominal band.

    测量登记颜色特征并与逐资产的正常区间比较。
    """

    kind: Literal["deterministic"]
    method: Literal["color_signature"]
    bands: dict[str, tuple[float, float]] = Field(min_length=1)
    # P6 (D078): a quality profile of its own, pinned like a model analyzer's, for deployments whose sites have
    # different cameras; left out of the canonical form while unset (the business catalog's default applies).
    # P6（D078）：自带的质量画像，与模型分析器一样固定，用于各站点相机不同的部署；未设置时不进入规范形式（使用业务目录的默认画像）。
    quality: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _bands(self):
        if any(not 0 <= low < high <= 1 for low, high in self.bands.values()):
            raise ValueError("bands are 0 <= low < high <= 1")
        return self

    @model_serializer(mode="wrap")
    def _canonical(self, handler):
        data = handler(self)
        if self.quality is None:
            data.pop("quality", None)
        return data


class ModelAnalyzer(WorkflowModel):
    """A vision-model analyzer of P4 jobs: a pinned model profile and quality profile (D063 §1).

    P4 作业的视觉模型分析器：固定的模型画像与质量画像（D063 §1）。
    """

    kind: Literal["model"]
    profile: str = Field(min_length=1, description="configs/analysis model profile / 模型画像")
    quality: str = Field(min_length=1, description="configs/analysis quality profile / 质量画像")


Analyzer = Annotated[ScriptedAnalyzer | SignatureAnalyzer | ModelAnalyzer, Field(discriminator="kind")]


class FixtureAnswer(WorkflowModel):
    verdict: Literal["suspected", "normal"]
    confidence: float = Field(ge=0, le=1)
    description: str = Field(max_length=300)


class AnalysisFixture(WorkflowModel):
    """A hand-written test double for analysis; its digest goes into every result's provenance.

    手写的分析测试替身；其摘要进入每个结果的来源。
    """

    format: Literal["drone.analysis-fixture/v1"]
    source: Literal["scripted"]
    note: str = Field(min_length=1)
    answers: dict[str, FixtureAnswer]


# ── the catalog / 目录 ──


class WorkflowCatalog(WorkflowModel):
    """Every template version the deployment may run, and the analyzers they use. / 部署可运行的全部模板版本及其分析器。"""

    format: Literal["drone.workflow-catalog/v1"]
    catalog_id: str = Field(pattern=ID)
    operations_catalog: str = Field(pattern=ID)
    analyzers: dict[str, Analyzer] = Field(default_factory=dict)
    workflows: tuple[WorkflowSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _references(self):
        keys = [(w.project_id, w.workflow_id, w.version) for w in self.workflows]
        if len(keys) != len(set(keys)):
            raise ValueError("a (project, workflow, version) is listed twice")
        if any(not re.fullmatch(ID, name) for name in self.analyzers):
            raise ValueError("analyzer ids must be lower-case slugs")
        for workflow in self.workflows:
            for node in workflow.nodes:
                if isinstance(node, AnalyzeNode) and node.params.analyzer not in self.analyzers:
                    raise ValueError(f"{workflow.workflow_id}.{node.node_id} names an unknown analyzer")
                if isinstance(node, AnalyzeNode) and not node.params.findings and \
                        isinstance(self.analyzers[node.params.analyzer], ModelAnalyzer):
                    raise ValueError(f"{workflow.workflow_id}.{node.node_id}: a model analyzer runs only as a P4 job")
                if isinstance(node, WorkOrderNode) and node.params.reinspection is not None:
                    target_ref = node.params.reinspection
                    target = self.spec(workflow.project_id, target_ref.workflow_id, target_ref.version)
                    if target is None or not any(isinstance(n, SettleNode) for n in target.nodes):
                        raise ValueError(f"{workflow.workflow_id}.{node.node_id} names no reinspection template "
                                         f"with a settle node")
                    spec = target.inputs.get(target_ref.asset_input)
                    others = {name for name, s in target.inputs.items() if s.required} - {target_ref.asset_input}
                    if spec is None or others:
                        raise ValueError(f"{target.workflow_id} must take exactly the asset input "
                                         f"{target_ref.asset_input}")
                if isinstance(node, ReinspectionNode):
                    target = self.spec(workflow.project_id, node.params.workflow_id, node.params.version)
                    if target is None:
                        raise ValueError(f"{workflow.workflow_id}.{node.node_id} names an unknown template")
                    if not any(t.kind == "internal" for t in target.triggers):
                        raise ValueError(f"{target.workflow_id} has no internal trigger")
                    if any(isinstance(n, ReinspectionNode) for n in target.nodes):
                        # Reinspection never chains, so no run can start runs without end. / 复检从不链式触发，无无界运行。
                        raise ValueError(f"{target.workflow_id} may not itself request reinspection")
                    spec = target.inputs.get(node.params.asset_input)
                    others = {name for name, s in target.inputs.items() if s.required} - {node.params.asset_input}
                    if spec is None or others:
                        raise ValueError(f"{target.workflow_id} must take exactly the asset input "
                                         f"{node.params.asset_input}")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    def spec(self, project_id: str, workflow_id: str, version: int) -> WorkflowSpec | None:
        return next((w for w in self.workflows if (w.project_id, w.workflow_id, w.version)
                     == (project_id, workflow_id, version)), None)

    def latest(self, project_id: str, workflow_id: str) -> WorkflowSpec | None:
        """The newest listed version; runs already started keep their own. / 最新列出的版本；已开始的运行保留原版本。"""
        found = [w for w in self.workflows if (w.project_id, w.workflow_id) == (project_id, workflow_id)]
        return max(found, key=lambda w: w.version) if found else None

    def project_workflows(self, project_id: str) -> list[WorkflowSpec]:
        latest = {}
        for workflow in self.workflows:
            if workflow.project_id == project_id:
                current = latest.get(workflow.workflow_id)
                if current is None or workflow.version > current.version:
                    latest[workflow.workflow_id] = workflow
        return [latest[key] for key in sorted(latest)]

    def check(self, operations, registries: dict, root: Path) -> dict[str, str]:
        """Check every reference against the operations catalog, the site registries and the fixture files; returns
        the fixture digests.

        按运营目录、站点登记表与夹具文件核对全部引用；返回夹具摘要。
        """
        import hashlib

        if self.operations_catalog != operations.catalog_id:
            raise ValueError(f"written for {self.operations_catalog}, loaded with {operations.catalog_id}")
        fixtures: dict[str, str] = {}
        for name, analyzer in self.analyzers.items():
            if isinstance(analyzer, ScriptedAnalyzer):
                path = Path(analyzer.fixture)
                if path.is_absolute() or ".." in path.parts or not (root / path).is_file():
                    raise ValueError(f"analyzer {name} fixture is missing or outside the repository")
                raw = (root / path).read_bytes()
                AnalysisFixture.model_validate(yaml.safe_load(raw))
                fixtures[name] = hashlib.sha256(raw).hexdigest()
            if isinstance(analyzer, ModelAnalyzer):
                # P4 (D063): both profiles are pinned by digest like fixtures. / P4（D063）：两个画像与夹具一样按摘要固定。
                from drone_agent.fleet.business_models import load_profile, load_quality

                _, fixtures[f"{name}#profile"] = load_profile(root, analyzer.profile)
                _, fixtures[f"{name}#quality"] = load_quality(root, analyzer.quality)
            if isinstance(analyzer, SignatureAnalyzer) and analyzer.quality is not None:
                from drone_agent.fleet.business_models import load_quality

                _, fixtures[f"{name}#quality"] = load_quality(root, analyzer.quality)
        for workflow in self.workflows:
            if workflow.project_id not in operations.projects:
                raise ValueError(f"{workflow.workflow_id} names an unknown project {workflow.project_id}")
            for node in workflow.nodes:
                if not isinstance(node, SubmitMissionNode):
                    continue
                params = node.params
                assets = [params.asset] if isinstance(params.asset, str) else \
                    list(workflow.inputs[params.asset.input].choices)
                if params.assigned:
                    self._check_assigned(workflow, node, operations, registries, assets)
                    continue
                robot = operations.robots.get(params.robot_id)
                if robot is None or operations.project_of(params.robot_id) != workflow.project_id:
                    raise ValueError(f"{workflow.workflow_id}.{node.node_id} names a robot outside its project")
                registry = registries[robot.site_id].data
                if params.volume_id not in registry.get("volumes", {}):
                    raise ValueError(f"{workflow.workflow_id}.{node.node_id} names an unregistered volume")
                if INSPECTION_SKILL not in registry.get("mission_defaults", {}).get("planner_skills", []):
                    raise ValueError(f"{workflow.workflow_id}.{node.node_id}: the site does not offer inspections")
                for asset in assets:
                    entry = registry["assets"].get(asset)
                    if entry is None or entry.get("volume") != params.volume_id:
                        raise ValueError(f"{workflow.workflow_id}.{node.node_id} names {asset}, not a registered "
                                         f"asset of {params.volume_id} at {robot.site_id}")
            for node in workflow.nodes:
                if isinstance(node, AnalyzeNode):
                    analyzer = self.analyzers[node.params.analyzer]
                    if isinstance(analyzer, SignatureAnalyzer):
                        await_node = workflow.node(node.params.inspection_from)
                        submit = workflow.node(await_node.params.mission_from).params
                        assets = [submit.asset] if isinstance(submit.asset, str) else \
                            list(workflow.inputs[submit.asset.input].choices)
                        if any(asset not in analyzer.bands for asset in assets):
                            raise ValueError(f"{workflow.workflow_id}.{node.node_id}: analyzer "
                                             f"{node.params.analyzer} has no band for every asset")
        return fixtures

    @staticmethod
    def _check_assigned(workflow: WorkflowSpec, node, operations, registries: dict, assets: list[str]) -> None:
        """An assignment-mode node: candidates of its project, and every asset inspectable by at least one of them.

        分配模式节点：候选属于其项目，且每个资产至少可由其中一台巡检。
        """
        robots = sorted(r for r in operations.robots if operations.project_of(r) == workflow.project_id)
        candidates = list(node.params.candidates) or robots
        if any(robot not in robots for robot in candidates):
            raise ValueError(f"{workflow.workflow_id}.{node.node_id} names a candidate outside its project")
        for asset in assets:
            able = []
            for robot in candidates:
                registry = registries[operations.robots[robot].site_id].data
                entry = registry.get("assets", {}).get(asset)
                if entry is not None and entry.get("volume") == node.params.volume_id and \
                        node.params.volume_id in registry.get("volumes", {}) and \
                        INSPECTION_SKILL in registry.get("mission_defaults", {}).get("planner_skills", []):
                    able.append(robot)
            if not able:
                raise ValueError(f"{workflow.workflow_id}.{node.node_id}: no candidate can inspect {asset} in "
                                 f"{node.params.volume_id}")

    def assigned_nodes(self) -> list[tuple[str, str]]:
        """(workflow, node) of every assignment-mode submission; they need the P3 scheduler.

        每个分配模式提交节点的（流程，节点）；它们需要 P3 调度器。
        """
        return [(w.workflow_id, n.node_id) for w in self.workflows for n in w.nodes
                if isinstance(n, SubmitMissionNode) and n.params.assigned]


def load_workflows(path: Path) -> WorkflowCatalog:
    return WorkflowCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# ── typed node outputs / 类型化节点输出 ──


class MissionOutput(WorkflowModel):
    """submit_mission: the one mission of this activity, or the P3 task that carries it (D059).

    本活动唯一的任务，或承载它的 P3 任务单（D059）。
    """

    mission_id: str | None = None
    task_id: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.mission_id is None) == (self.task_id is None):
            raise ValueError("a submission output names exactly one mission or one task")
        return self

    @model_serializer(mode="wrap")
    def _canonical(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class InspectionOutput(WorkflowModel):
    """await_mission: only a service-verified inspection completes the node. / 只有服务复核通过的巡检才使节点完成。"""

    mission_id: str
    mission_status: Literal["completed"]
    mission_version: int = Field(ge=1)
    asset_id: str
    inspection: Literal["verified"]
    evidence_id: str
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: AwareDatetime


class AnalysisOutput(WorkflowModel):
    """analyze_evidence: a candidate finding with its source, never a flight verdict. A P4 job also names the finding
    its suspected result joined (D063). / 带来源的候选发现，从不是飞行判定。P4 作业还给出疑似结果所归入的发现（D063）。"""

    analysis_id: str
    verdict: Literal["suspected", "normal"]
    suspected: bool
    confidence: float = Field(ge=0, le=1)
    source: Literal["scripted", "deterministic", "live_model", "recorded_model"]
    finding_id: str | None = None
    new_finding: bool | None = None

    @model_serializer(mode="wrap")
    def _canonical(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class SelectOutput(AnalysisOutput):
    """select_analysis (P6, D078): the chosen analysis's output and which node it came from.

    select_analysis（P6，D078）：所选分析的输出及其来源节点。
    """

    selected: str = Field(pattern=NODE_ID)


class ReviewOutput(WorkflowModel):
    review_id: str
    decision: Literal["confirmed", "dismissed"]
    reviewer: str


class OrderOutput(WorkflowModel):
    order_id: str


class RepairOutput(WorkflowModel):
    """Repair feedback moves an order to reinspection; it never closes it. / 维修反馈让工单进入复检；从不关单。"""

    order_id: str
    feedback_id: str
    reported_at: AwareDatetime


class ReinspectionOutput(WorkflowModel):
    order_id: str
    run_id: str


class SettleOutput(WorkflowModel):
    """settle_reinspection: the round's conclusion; only `passed` closed the order. / 本轮结论；只有 `passed` 关单。"""

    order_id: str
    round: int = Field(ge=1)
    status: Literal["passed", "failed", "unknown"]
    reasons: tuple[str, ...] = ()


OUTPUTS: dict[Activity, type[WorkflowModel]] = {
    Activity.SUBMIT_MISSION: MissionOutput, Activity.AWAIT_MISSION: InspectionOutput,
    Activity.ANALYZE_EVIDENCE: AnalysisOutput, Activity.HUMAN_REVIEW: ReviewOutput,
    Activity.CREATE_WORK_ORDER: OrderOutput, Activity.AWAIT_REPAIR: RepairOutput,
    Activity.REQUEST_REINSPECTION: ReinspectionOutput, Activity.SETTLE_REINSPECTION: SettleOutput,
    Activity.SELECT_ANALYSIS: SelectOutput,
}


def predicate_holds(predicate: Predicate, outputs: dict[str, dict]) -> bool:
    """A condition over completed nodes' outputs; a missing output is false. / 对已完成节点输出的条件；缺输出即为假。"""
    output = outputs.get(predicate.node)
    return output is not None and output.get(predicate.output) == predicate.equals
