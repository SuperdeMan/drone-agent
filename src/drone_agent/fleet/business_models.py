"""P4 business contracts: the business catalog, analysis profiles, jobs, findings, reviews, orders, rounds (D063).

A business catalog (`configs/analysis/*.yaml`) is versioned configuration reviewed like the operations, workflow and
scheduling catalogs. It names the catalogs it was written for, the defect families findings are clustered by, the
closure, reuse, runner and reference policies, and the default quality profile of analyses that bring none. Model
and quality profiles are separate files pinned by their SHA-256 together with the catalog that references them, so
a prompt, a threshold or a quality limit can only change as a new reviewed version.

Everything a model returns is a candidate: the only verdicts an analysis can give are `suspected`, `normal` or an
explicit refusal with a reason, and none of them is a flight verdict. Findings change state only through human
reviews and the reinspection settlement; `settle_round` is the one deterministic rule that may close an order.

P4 业务契约：业务目录、分析画像、作业、发现、复核、工单与轮次（D063）。

业务目录（`configs/analysis/*.yaml`）与运营、工作流、调度目录一样是经评审的版本化配置。它声明所对应的目录、发现聚合
所用的缺陷族、关单 / 复用 / 执行器 / 参考外观策略，以及未自带质量画像的分析所用的默认质量画像。模型画像与质量画像是
单独文件，按 SHA-256 与引用它们的目录一同固定，因此提示、阈值或质量限值只能作为新的评审版本改变。

模型返回的一切都是候选：分析只能给出 `suspected`、`normal` 或带原因的明确拒判，都不是飞行判定。发现只经人工复核与复检
结算改变状态；`settle_round` 是唯一可以关单的确定性规则。
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_serializer, model_validator

from drone_agent.contracts.common import ContractModel
from drone_agent.fleet.provenance import digest

CATALOG_FORMAT = "drone.business-catalog/v1"
PROFILE_FORMAT = "drone.analysis-profile/v1"
QUALITY_FORMAT = "drone.quality-profile/v1"
QUALITY_V2_FORMAT = "drone.quality-profile/v2"
ID = r"^[a-z][a-z0-9_]{0,39}$"
SHA256 = r"^[0-9a-f]{64}$"
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
CLOSURE_RULE = "reinspection-v1"
REUSE_RULE = "reuse-v1"
QUALITY_RULE = "quality-v1"
QUALITY_V2_RULE = "quality-v2"
RECAPTURE_RULE = "recapture-v1"


class BusinessModel(ContractModel):
    """Service-side business record: unknown fields rejected, instances frozen. / 服务侧业务记录：拒绝未知字段、实例冻结。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["0.1.0"] = "0.1.0"


class JobPurpose(StrEnum):
    """Why an analysis ran; only `reinspection` jobs may ever settle a round. / 分析的用途；只有复检作业能结算轮次。"""

    INSPECTION = "inspection"
    REINSPECTION = "reinspection"
    REUSE = "reuse"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    REFUSED = "refused"
    CANCELLED = "cancelled"


TERMINAL_JOB = frozenset({JobState.COMPLETED, JobState.REFUSED, JobState.CANCELLED})


class FindingState(StrEnum):
    """Only reviews and the settlement move a finding. / 只有复核与结算改变发现。"""

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


OPEN_FINDING = frozenset({FindingState.CANDIDATE, FindingState.CONFIRMED})


class OrderState(StrEnum):
    """Repair feedback and the reinspection run start in one transaction, so there is no separate reported state.

    维修反馈与复检运行在一个事务中开始，因此没有单独的「已反馈」状态。
    """

    OPEN = "open"
    REINSPECTION_REQUESTED = "reinspection_requested"
    REINSPECTION_FAILED = "reinspection_failed"
    REINSPECTION_UNKNOWN = "reinspection_unknown"
    CLOSED = "closed"


# Orders that accept a new round of repair feedback. / 接受新一轮维修反馈的工单状态。
REPAIRABLE = frozenset({OrderState.OPEN, OrderState.REINSPECTION_FAILED, OrderState.REINSPECTION_UNKNOWN})


class RoundState(StrEnum):
    REINSPECTING = "reinspecting"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReviewSubject(StrEnum):
    FINDING = "finding"
    ROUND = "round"


DECISIONS = ("confirmed", "dismissed")


# ── reason codes / 原因码 ──

QUALITY_REASONS = ("quality.media_mismatch", "quality.media_missing", "quality.resolution", "quality.exposure",
                   "quality.blurry", "quality.capture_time_unknown")
# P6 (D078): the target-region checks of `quality-v2`. / P6（D078）：`quality-v2` 的目标区域检查。
TARGET_REASONS = ("quality.target_unknown", "quality.target_out_of_frame", "quality.target_too_small",
                  "quality.target_off_center", "quality.target_exposure", "quality.target_blurry")
# Refusals a flight of the same registered pose can change; nothing else may ever start a recapture (D078).
# 同一登记拍摄位的再次飞行可以改变的拒判；其他原因永远不能启动补拍（D078）。
RECAPTURABLE = ("quality.blurry", "quality.exposure", "quality.target_out_of_frame", "quality.target_off_center",
                "quality.target_exposure", "quality.target_blurry", "analysis.undeterminable")
MODEL_REASONS = ("model.unavailable", "model.timeout", "model.refusal", "model.malformed", "model.error",
                 "model.profile_mismatch", "model.uncalibrated", "model.budget_exhausted")
ANALYSIS_REASONS = ("analysis.no_reference", "analysis.undeterminable", "analysis.not_verified",
                    "analysis.attempts_exhausted", "analysis.fixture_changed", "analysis.no_scripted_answer",
                    "analysis.no_baseline", "analysis.run_cancelled", "analysis.error", "target.mismatch")
REUSE_REASONS = ("reuse.modality_mismatch", "reuse.source_unknown", "reuse.resolution", "reuse.stale",
                 "reuse.analyzer_not_allowed")
SETTLE_REASONS = ("reinspection.evidence_missing", "reinspection.capture_time_unknown", "reinspection.before_feedback",
                  "reinspection.not_new_acquisition", "reinspection.replayed_media", "reinspection.analysis_refused",
                  "reinspection.source_not_allowed", "reinspection.still_anomalous", "reinspection.review_dismissed",
                  "reinspection.review_missing", "reinspection.run_ended")


# ── profiles / 画像 ──


class QualityProfile(BusinessModel):
    """Deterministic limits of the generic quality layer (`quality-v1`). / 通用质量层的确定性限值（`quality-v1`）。"""

    format: Literal["drone.quality-profile/v1"]
    profile_id: str = Field(pattern=ID)
    version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=400)
    min_width: int = Field(ge=16, le=8192)
    min_height: int = Field(ge=16, le=8192)
    luminance: tuple[float, float] = Field(description="mean luminance range, 0-255 / 平均亮度范围，0-255")
    max_clipped_fraction: float = Field(ge=0, le=1, description="pixels at <=5 or >=250 / 亮度 <=5 或 >=250 的像素比例")
    min_sharpness: float = Field(ge=0, le=255, description="99.5th percentile gradient magnitude / 梯度幅值 99.5 分位")

    @model_validator(mode="after")
    def _ranges(self):
        low, high = self.luminance
        if not 0 <= low < high <= 255:
            raise ValueError("luminance is 0 <= low < high <= 255")
        return self


class CameraModel(BusinessModel):
    """The downward pinhole camera a `quality-v2` profile is written for (D078); pinned with the profile because the
    profile is the quality limit of one capture setup.

    `quality-v2` 画像所针对的下视针孔相机（D078）；随画像固定，因为画像就是某种采集配置下的质量限值。
    """

    camera_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    width: int = Field(ge=16, le=8192)
    height: int = Field(ge=16, le=8192)
    hfov_rad: float = Field(gt=0.05, lt=3.0, description="horizontal field of view / 水平视场角")
    mount_below_m: float = Field(ge=0, le=5, description="optical centre below the reported position / 光心低于上报位置的距离")

    @property
    def focal_px(self) -> float:
        return (self.width / 2) / math.tan(self.hfov_rad / 2)


class TargetLimits(BusinessModel):
    """Limits of the target region (D078). / 目标区域的限值（D078）。"""

    min_visible_fraction: float = Field(ge=0, le=1, description="worst heading / 最坏航向下")
    min_side_px: float = Field(ge=1, le=8192, description="projected side / 投影边长")
    min_core_px: int = Field(ge=1, description="pixels that are the target at every heading / 任何航向下都属于目标的像素")
    dark_level: int = Field(ge=0, le=255, description="max(R,G,B) below it is dark / max(R,G,B) 低于它为暗")
    max_dark_fraction: float = Field(ge=0, le=1)
    bright_level: int = Field(ge=0, le=255, description="min(R,G,B) above it is blown / min(R,G,B) 高于它为过曝")
    max_bright_fraction: float = Field(ge=0, le=1)
    min_sharpness: float = Field(ge=0, le=255, description="99.5th percentile gradient on the target boundary ring / "
                                                       "目标边界环上梯度幅值的 99.5 分位")


class TargetQualityProfile(QualityProfile):
    """`quality-v2` (D078): the whole-frame limits of `quality-v1` plus the camera and the target-region limits.

    `quality-v2`（D078）：`quality-v1` 的整图限值，加上相机与目标区域限值。
    """

    format: Literal["drone.quality-profile/v2"]
    camera: CameraModel
    target: TargetLimits


class ModelProfile(BusinessModel):
    """A pinned vision-model analyzer: prompt, output schema, references, image budget and operating threshold.

    固定的视觉模型分析器：提示、输出 schema、参考图、图像预算与运行阈值。
    """

    format: Literal["drone.analysis-profile/v1"]
    profile_id: str = Field(pattern=ID)
    version: int = Field(ge=1)
    role: Literal["vision"]
    model: str = Field(min_length=1, max_length=80, description="the model the deployment must call / 部署须调用的模型")
    prompt_version: str = Field(pattern=r"^[a-z0-9_.-]{1,40}$")
    system_prompt: str = Field(min_length=1, max_length=4000)
    instruction: str = Field(min_length=1, max_length=4000, description="`{asset}` is the asset description")
    output_schema: Literal["change-v1"] = "change-v1"
    references: int = Field(ge=1, le=3)
    max_long_side: int = Field(ge=128, le=2048)
    zoom_grid: int = Field(default=1, ge=1, le=3, description="n > 1 also shows the current capture's n x n overlapping "
                                                             "parts, enlarged / n > 1 时另附当前采集 n×n 个重叠局部的放大图")
    zoom_overlap: float = Field(default=0.25, ge=0, le=0.5, description="overlap of adjacent parts / 相邻局部的重叠比例")
    detail: Literal["low", "default", "high"] = "default"
    image_format: Literal["jpeg", "png"] = "jpeg"
    jpeg_quality: int = Field(default=92, ge=50, le=100)
    temperature: float = Field(default=0.0, ge=0, le=1)
    thinking: bool = Field(default=False, description="let the model reason before its answer; only the final JSON is "
                                                     "parsed / 允许模型先推理再作答；只解析最终 JSON")
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None, description="thinking depth of an adaptive-thinking model; part of the pinned profile (D076) / "
                                  "自适应思考模型的思考深度；属于固定画像的一部分（D076）")
    max_tokens: int = Field(ge=64, le=65536, description="output budget, reasoning included / 输出预算，含推理")
    timeout_s: float = Field(gt=0, le=600)
    attempts: int = Field(ge=1, le=3)
    threshold: float | None = Field(default=None, ge=0, le=1, description="tau; None only in calibration / τ")
    family: str = Field(pattern=ID)
    defect_types: tuple[str, ...] = Field(min_length=1, max_length=16)
    generic_type: str = Field(description="the type of a suspected answer that names none / 疑似但未给类型时所用类型")
    modality: Literal["visible", "thermal"] = "visible"
    calibration: dict[str, str] = Field(default_factory=dict, description="where tau came from / τ 的来源")

    @model_validator(mode="after")
    def _types(self):
        from drone_agent.providers.llm import check_generation

        if any(not re.fullmatch(r"^[a-z][a-z_]{0,31}$", t) for t in self.defect_types) or "none" in self.defect_types:
            raise ValueError("defect types are lower-case slugs and never `none`")
        if "{asset}" not in self.instruction:
            raise ValueError("the instruction must place the asset description with {asset}")
        if self.generic_type not in self.defect_types:
            raise ValueError("the generic type is one of the defect types")
        # A profile the model cannot honour fails when the catalog loads, not at the first job (D076).
        # 模型无法遵从的画像在目录加载时失败，而不是在第一个作业时（D076）。
        check_generation(self.model, disable_thinking=not self.thinking, reasoning_effort=self.reasoning_effort)
        return self

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256((self.system_prompt + "\n\n" + self.instruction).encode()).hexdigest()


class ModelAnswer(BaseModel):
    """The exact JSON a vision model must return (`change-v1`); anything else is malformed.

    视觉模型必须返回的确切 JSON（`change-v1`）；其他任何形式都是不合格输出。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    image_usable: StrictBool
    target_matches_reference: StrictBool
    anomaly_score: float = Field(ge=0, le=1)
    defect_type: str = Field(pattern=r"^[a-z][a-z_]{0,31}$")
    description: str = Field(max_length=400)
    unusable_reason: Literal["blurred", "dark", "overexposed", "occluded", "out_of_frame", "other", "none"]


# ── the catalog / 目录 ──


class ClosurePolicy(BusinessModel):
    version: Literal["reinspection-v1"] = "reinspection-v1"
    allowed_sources: tuple[Literal["live_model", "recorded_model", "deterministic", "scripted"], ...] = Field(
        min_length=1, description="analysis sources that may settle a round / 可以结算轮次的分析来源")


class ReusePolicy(BusinessModel):
    version: Literal["reuse-v1"] = "reuse-v1"
    max_age_s: int = Field(ge=60, le=366 * 86400)
    analyzers: tuple[str, ...] = Field(default=(), description="analyzers reuse may name; empty = all / 空为全部")


class RunnerPolicy(BusinessModel):
    concurrency: int = Field(ge=1, le=16)
    lease_s: float = Field(ge=5, le=3600)
    max_attempts: int = Field(ge=1, le=5)
    daily_tokens: int = Field(ge=0, description="per project and UTC day; 0 = no model analyses / 0 表示不做模型分析")


class Prices(BusinessModel):
    """Fixed prices for cost estimates, with where they came from. / 费用估算用的固定价格及其来源。"""

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    source: str = Field(min_length=1, max_length=300)


class RecapturePolicy(BusinessModel):
    """`recapture-v1` (D078): the refusals after which a template's recapture node flies the same pose once.

    `recapture-v1`（D078）：模板的补拍节点在这些拒判之后以同一拍摄位再飞一次。
    """

    version: Literal["recapture-v1"] = "recapture-v1"
    reasons: tuple[str, ...] = Field(min_length=1, max_length=len(RECAPTURABLE))

    @model_validator(mode="after")
    def _reasons(self):
        refused = [reason for reason in self.reasons if reason not in RECAPTURABLE]
        if refused:
            raise ValueError(f"a flight of the same pose cannot change {refused}")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("recapture reasons are distinct")
        return self


class BusinessCatalog(BusinessModel):
    """Policies of the business loop for one deployment. / 一个部署的业务闭环策略。"""

    format: Literal["drone.business-catalog/v1"]
    catalog_id: str = Field(pattern=ID)
    operations_catalog: str = Field(pattern=ID)
    workflow_catalog: str = Field(pattern=ID)
    default_quality: str = Field(min_length=1, description="quality profile path for analyses without one")
    families: dict[str, tuple[str, ...]] = Field(min_length=1)
    generic_defect: str = Field(pattern=r"^[a-z][a-z_]{0,31}$",
                                description="defect type of analyzers that name none / 不给类型的分析器所用缺陷类型")
    closure: ClosurePolicy
    reuse: ReusePolicy
    runner: RunnerPolicy
    max_references: int = Field(default=3, ge=1, le=10)
    prices: Prices | None = None
    # P6 (D078); left out of the canonical form while unset, so every P4 and P5 catalog keeps its digest.
    # P6（D078）；未设置时不进入规范形式，因此每个 P4 与 P5 目录的摘要保持不变。
    recapture: RecapturePolicy | None = None

    @model_serializer(mode="wrap")
    def _canonical(self, handler):
        data = handler(self)
        if self.recapture is None:
            data.pop("recapture", None)
        return data

    @model_validator(mode="after")
    def _families(self):
        if any(not re.fullmatch(ID, name) for name in self.families):
            raise ValueError("family names are lower-case slugs")
        if self.family_of(self.generic_defect) is None:
            raise ValueError("the generic defect belongs to a family")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    def family_of(self, defect_type: str) -> str | None:
        return next((name for name, types in sorted(self.families.items()) if defect_type in types), None)


def load_yaml_file(root: Path, relative: str) -> tuple[dict, str]:
    """A repository file as data plus its SHA-256; absolute or escaping paths are refused.

    仓库内文件的内容与 SHA-256；绝对路径或越出仓库的路径被拒绝。
    """
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not (root / path).is_file():
        raise ValueError(f"{relative} is missing or outside the repository")
    raw = (root / path).read_bytes()
    return yaml.safe_load(raw), hashlib.sha256(raw).hexdigest()


def load_quality(root: Path, relative: str) -> tuple[QualityProfile, str]:
    """A `quality-v1` or, by its format, a `quality-v2` profile. / `quality-v1` 画像，或按格式为 `quality-v2` 画像。"""
    data, sha = load_yaml_file(root, relative)
    v2 = isinstance(data, dict) and data.get("format") == QUALITY_V2_FORMAT
    return (TargetQualityProfile if v2 else QualityProfile).model_validate(data), sha


def load_profile(root: Path, relative: str) -> tuple[ModelProfile, str]:
    data, sha = load_yaml_file(root, relative)
    return ModelProfile.model_validate(data), sha


def load_catalog(path: Path) -> BusinessCatalog:
    return BusinessCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# ── jobs / 作业 ──


class EvidenceRef(BusinessModel):
    """The acquisition an analysis reads; identity and digest are never rewritten. / 分析读取的采集；身份与摘要从不改写。"""

    evidence_id: str
    mission_id: str
    mission_version: int = Field(ge=1)
    asset_id: str
    media_sha256: str = Field(pattern=SHA256)
    captured_at: AwareDatetime | None
    image_source: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class ReferenceRef(BusinessModel):
    reference_id: str
    evidence_id: str
    media_sha256: str = Field(pattern=SHA256)


class JobInputs(BusinessModel):
    """Everything a job is pinned to when it is created. / 作业创建时固定的全部输入。"""

    purpose: JobPurpose
    analyzer: str = Field(pattern=ID)
    analyzer_kind: Literal["scripted", "deterministic", "model"]
    analyzer_sha256: str = Field(pattern=SHA256)
    workflow_catalog_sha256: str = Field(pattern=SHA256)
    quality_sha256: str = Field(pattern=SHA256)
    evidence: EvidenceRef
    references: tuple[ReferenceRef, ...] = ()
    asset_description: str = Field(default="", max_length=300)
    run_id: str | None = None
    order_id: str | None = None
    round: int | None = None


class Usage(BusinessModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class Cost(BusinessModel):
    amount: float = Field(ge=0)
    currency: str
    source: str


class JobResult(BusinessModel):
    """One analysis outcome: a candidate verdict or a refusal, always with its source. / 一次分析结果：候选结论或拒判，始终带来源。"""

    verdict: Literal["suspected", "normal", "refused"]
    reasons: tuple[str, ...] = ()
    score: float | None = Field(default=None, ge=0, le=1)
    threshold: float | None = Field(default=None, ge=0, le=1)
    defect_type: str | None = None
    family: str | None = None
    description: str = Field(default="", max_length=400)
    source: Literal["live_model", "recorded_model", "scripted", "deterministic", "not_run", "unknown"]
    provider_id: str = ""
    model_id: str = ""
    reported_model_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    recording_sha256: str = ""
    quality: dict[str, float] = Field(default_factory=dict)
    measures: dict[str, float] = Field(default_factory=dict)
    usage: Usage = Usage(input_tokens=0, output_tokens=0)
    latency_ms: float = Field(default=0.0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    cost: Cost | None = None

    @model_validator(mode="after")
    def _shape(self):
        if self.verdict == "refused" and not self.reasons:
            raise ValueError("a refusal names its reason")
        if self.verdict == "suspected" and (not self.defect_type or not self.family):
            raise ValueError("a suspected result names its defect type and family")
        return self


def refusal(reason: str, *, source: str = "not_run", **values) -> JobResult:
    return JobResult(verdict="refused", reasons=(reason,), source=source, **values)


# ── keys / 键 ──


def asset_key(project_id: str, scope: str, asset_id: str) -> str:
    """`<project>/<scope>/<asset>`: scope is `*` in a shared frame (P3), otherwise the site.

    `<项目>/<范围>/<资产>`：共享坐标（P3）下范围为 `*`，否则为站点。
    """
    return f"{project_id}/{scope}/{asset_id}"


def cluster_key(asset: str, family: str) -> str:
    return f"{asset}#{family}"


def round_subject(order_id: str, round_number: int) -> str:
    return f"{order_id}:r{round_number}"


# ── the settlement rule / 结算规则 ──


class Feedback(BusinessModel):
    feedback_id: str
    request_id: str
    reported_by: str
    reported_at: AwareDatetime
    note: str = Field(default="", max_length=300)


class Conclusion(BusinessModel):
    """The settled result of one reinspection round. / 一轮复检的结算结论。"""

    rule: Literal["reinspection-v1"] = "reinspection-v1"
    status: Literal["passed", "failed", "unknown"]
    reasons: tuple[str, ...] = ()
    feedback_id: str
    evidence_id: str | None = None
    mission_id: str | None = None
    media_sha256: str | None = None
    captured_at: AwareDatetime | None = None
    job_id: str | None = None
    job_source: str | None = None
    review_id: str | None = None
    decided_at: AwareDatetime


def settle_round(*, feedback: Feedback, inspection: dict | None, run_missions: set[str], job: dict | None,
                 review: dict | None, seen_evidence: set[str], seen_media: set[str],
                 allowed_sources: tuple[str, ...], now: datetime) -> Conclusion:
    """`reinspection-v1`: passed only when every condition holds; a model or a reviewer alone never closes an order.

    `inspection` is the reinspection run's verified inspection output (None when the flight gave none); `job` is the
    reinspection analysis job of that evidence (None when it never ran); `seen_evidence` and `seen_media` are the
    asset's earlier evidence IDs and media digests. Unknown is the answer whenever a fact cannot be established.

    `reinspection-v1`：只有全部条件成立才通过；单凭模型或复核人永远不能关单。`inspection` 是复检运行经服务复核的巡检输出
    （飞行未给出时为 None）；`job` 是该证据的复检分析作业（从未运行时为 None）；`seen_evidence` 与 `seen_media` 是该资产
    此前的证据 ID 与媒体摘要。任何事实无法确立时结论为 unknown。
    """
    base = {"feedback_id": feedback.feedback_id, "decided_at": now}

    def conclude(status: str, *reasons: str, **extra) -> Conclusion:
        return Conclusion(status=status, reasons=tuple(reasons), **base, **extra)

    if inspection is None:
        return conclude("unknown", "reinspection.evidence_missing")
    evidence = {"evidence_id": inspection["evidence_id"], "mission_id": inspection["mission_id"],
                "media_sha256": inspection["evidence_sha256"]}
    captured = inspection.get("captured_at")
    captured_at = datetime.fromisoformat(captured) if isinstance(captured, str) else captured
    if captured_at is None:
        return conclude("unknown", "reinspection.capture_time_unknown", **evidence)
    evidence["captured_at"] = captured_at
    if captured_at <= feedback.reported_at:
        return conclude("unknown", "reinspection.before_feedback", **evidence)
    if inspection["mission_id"] not in run_missions or inspection["evidence_id"] in seen_evidence:
        return conclude("unknown", "reinspection.not_new_acquisition", **evidence)
    if inspection["evidence_sha256"] in seen_media:
        return conclude("unknown", "reinspection.replayed_media", **evidence)
    if job is None or job.get("verdict") not in ("suspected", "normal"):
        return conclude("unknown", "reinspection.analysis_refused", **evidence,
                        job_id=job["job_id"] if job else None)
    common = {**evidence, "job_id": job["job_id"], "job_source": job["source"]}
    if job["source"] not in allowed_sources:
        return conclude("unknown", "reinspection.source_not_allowed", **common)
    if job["verdict"] == "suspected":
        return conclude("failed", "reinspection.still_anomalous", **common)
    if review is None:
        return conclude("unknown", "reinspection.review_missing", **common)
    if review["decision"] != "confirmed":
        return conclude("failed", "reinspection.review_dismissed", **common, review_id=review["review_id"])
    return conclude("passed", **common, review_id=review["review_id"])
