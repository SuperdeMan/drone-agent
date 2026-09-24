"""Plugin interfaces of the local-autonomy layer (WP-M3-06, D013, D039).

Every implementation — deterministic planner, learned policy, perception model, world predictor — returns the same
contract objects: candidate `TrajectorySegment`s with a `valid_until` and a declared implementation stage, and
`WorldFact`s with frame, covariance, confidence and validity. Nothing returned here is a command: segments go through
the guardian's CBF filter, facts go to the BeliefWorld journal, and predicted facts never satisfy a precondition.
A learned implementation starts in the shadow stage, where its output is only recorded (see `shadow`).

局部自主层的插件接口（WP-M3-06，D013，D039）。

每种实现——确定性规划器、学习型策略、感知模型、世界预测器——都返回相同的契约对象：带 `valid_until` 与实现阶段声明的
候选 `TrajectorySegment`，以及带坐标系、协方差、置信度与有效期的 `WorldFact`。这里返回的都不是命令：片段要经过
guardian 的 CBF 过滤，事实进入 BeliefWorld 账本，预测事实永远不能满足前置条件。学习型实现从影子阶段开始，其输出只被
记录（见 `shadow`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from drone_agent.autonomy.messages import LocalTask, ObstacleSet, TrajectorySegment
from drone_agent.contracts import WorldFact


@dataclass(frozen=True)
class PlanningInput:
    """What a local planner or policy may see at one cycle: never truth. / 局部规划器或策略在一个周期内可见的输入：从不含真值。"""

    task: LocalTask
    position_enu: tuple[float, float, float]
    velocity_enu: tuple[float, float, float]
    obstacles: ObstacleSet | None
    stamp_monotonic: float
    extras: dict = field(default_factory=dict)


class LocalPlanner(Protocol):
    """Deterministic short-horizon planner. / 确定性短时域规划器。"""

    source_id: str
    source_version: str

    def plan(self, observation: PlanningInput) -> TrajectorySegment:  # pragma: no cover - protocol
        ...


class LocalPolicy(Protocol):
    """Learned local policy; its segments must declare `implementation=learned` and a learned stage.

    学习型局部策略；其片段必须声明 `implementation=learned` 与学习阶段。
    """

    source_id: str
    source_version: str

    def propose(self, observation: PlanningInput) -> TrajectorySegment:  # pragma: no cover - protocol
        ...


class PerceptionProvider(Protocol):
    """Sensor data to belief facts with covariance and confidence. / 把传感器数据变成带协方差与置信度的信念事实。"""

    def facts(self, frame: bytes, *, stamp) -> list[WorldFact]:  # pragma: no cover - protocol
        ...


class WorldPredictor(Protocol):
    """Predicted facts for candidate evaluation only (world_kind=predicted). / 只用于候选评估的预测事实（world_kind=predicted）。"""

    def predict(self, facts: list[WorldFact], *, horizon_s: float) -> list[WorldFact]:  # pragma: no cover - protocol
        ...
