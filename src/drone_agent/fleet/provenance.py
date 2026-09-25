"""Trusted, persistent source records for service-side runs (D054); never an execution authority.

服务侧运行的受信持久来源记录（D054）；来源记录从不构成执行权限。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field

from drone_agent.contracts.common import ContractModel, utcnow

NAMESPACE = "run_provenance"
Backend = Literal["none", "logical_sim", "px4_sitl", "vendor_protocol_sim", "real_device", "legacy_unknown"]
ImageSource = Literal["sim_render", "recorded_real", "live_sensor", "test_fixture", "unknown", "legacy_unknown"]
ModelSource = Literal["live_model", "recorded_model", "scripted", "deterministic", "not_run", "unknown",
                      "legacy_unknown"]


def digest(value) -> str:
    """Hash canonical JSON, independently of presentation. / 按规范 JSON 计算摘要，与展示形式无关。"""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


class SourceModel(ContractModel):
    """Reject extra fields and freeze every individual record. / 拒绝额外字段并冻结每条来源记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["0.1.0"] = "0.1.0"


class ModelUse(SourceModel):
    """Actual invocation origin, separate from a model's reported name. / 实际调用来源与模型自报名称分开。"""

    source: ModelSource = "not_run"
    provider_id: str = ""
    model_id: str = ""
    reported_model_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    input_sha256: str = ""
    recording_sha256: str = ""
    outcome: str = "not_run"


class RunHeader(SourceModel):
    """Immutable deployment and planning binding for one mission version. / 一个任务版本的不可变部署与规划绑定。"""

    mission_id: str
    mission_version: int = Field(ge=1)
    run_id: str = Field(min_length=1)
    execution_backend: Backend
    default_image_source: ImageSource
    software_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    dirty_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scenario_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    platform_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planning: ModelUse
    timestamp: AwareDatetime = Field(default_factory=utcnow)


class EvidenceOrigin(SourceModel):
    """The origin of one acquisition, not a label for an entire media collection. / 单次采集的来源，不能代替整个媒体集合。"""

    evidence_id: str
    media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ImageSource
    run_id: str


class AnalysisOrigin(SourceModel):
    """One analysis attempt, including failed or refused attempts. / 一次分析尝试，包括失败或拒判的尝试。"""

    attempt_id: str
    evidence_id: str
    use: ModelUse
    timestamp: AwareDatetime = Field(default_factory=utcnow)


class RunProvenance(SourceModel):
    """A frozen projection of immutable records; late evidence produces a new projection.

    不可变记录的冻结投影；迟到证据产生新的投影。
    """

    format: Literal["drone.run-provenance/v1"] = "drone.run-provenance/v1"
    mission_id: str
    mission_version: int
    status: Literal["recorded", "legacy_unknown"]
    run: RunHeader | None = None
    execution_backend: Backend = "legacy_unknown"
    planning: ModelUse = Field(default_factory=lambda: ModelUse(source="legacy_unknown", outcome="unknown"))
    imagery: tuple[EvidenceOrigin, ...] = ()
    analysis: tuple[AnalysisOrigin, ...] = ()
    analysis_sources: tuple[ModelSource, ...] = ("legacy_unknown",)

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class SourceContext(SourceModel):
    """Built only by trusted process configuration, never from API parameters. / 只由受信进程配置构造，不读取 API 参数。"""

    execution_backend: Literal["none", "logical_sim", "px4_sitl"] = "none"
    run_id: str
    software_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    dirty_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scenario_sha256: str
    platform_sha256: str
    registry_sha256: str

    def header(self, mission_id: str, version: int, planning: ModelUse) -> RunHeader:
        """Bind the trusted context to a newly created version. / 把受信上下文绑定到新建任务版本。"""
        image = {"none": "unknown", "logical_sim": "test_fixture", "px4_sitl": "sim_render"}[self.execution_backend]
        return RunHeader(**self.model_dump(), mission_id=mission_id, mission_version=version,
                         default_image_source=image, planning=planning)


def source_context(root: Path, scene: Path, registry_sha256: str, *, backend: str = "none",
                   run_id: str | None = None) -> SourceContext:
    """Bind immutable deployment inputs; local dirty code is hashed, never called a clean release.

    绑定不可变部署输入；本机脏代码记录摘要，绝不冒充干净发行版本。
    """
    revision = os.environ.get("DRONE_SOURCE_SHA", "")
    dirty = None
    if (root / ".git").exists():
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        scope = ["src", "scripts", "configs", "proto", "sim", "ros2_ws", "pyproject.toml", "uv.lock"]
        changes = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", *scope], cwd=root)
        untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *scope],
                                            cwd=root).decode().split("\0")
        for name in sorted(filter(None, untracked)):
            changes += name.encode() + b"\0" + (root / name).read_bytes()
        dirty = hashlib.sha256(changes).hexdigest() if changes else None
    return SourceContext(
        execution_backend=backend, run_id=run_id or f"service-{uuid.uuid4().hex}",
        software_sha=revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None, dirty_sha256=dirty,
        scenario_sha256=hashlib.sha256(scene.read_bytes()).hexdigest(), registry_sha256=registry_sha256,
        platform_sha256=hashlib.sha256((root / "configs/platforms/px4_sitl_multirotor.yaml").read_bytes()).hexdigest())


def provider_source(provider) -> tuple[ModelSource, str]:
    """Classify trusted provider implementations, not their labels or returned model names.

    根据受信 Provider 实现分类，不采信标签或模型返回的名称。
    """
    from drone_agent.providers.guarded import GuardedProvider
    from drone_agent.providers.llm import OpenAICompatibleProvider
    from drone_agent.providers.replay import KeyedScriptedProvider, RecordingProvider, ReplayProvider, ScriptedProvider

    if isinstance(provider, (ScriptedProvider, KeyedScriptedProvider)):
        return "scripted", ""
    if isinstance(provider, ReplayProvider):
        source = "recorded_model" if provider.recording.source == "recorded" else "scripted"
        return source, digest(provider.recording.model_dump(mode="json"))
    if isinstance(provider, RecordingProvider):
        return provider_source(provider.inner)
    if isinstance(provider, GuardedProvider):
        if provider.last_source == "not_called":
            return "not_run", ""
        if provider.last_source == "cache":
            inner, _ = provider_source(provider.inner)
            return ("recorded_model" if inner == "live_model" else inner), provider.last_cache_digest
        return provider_source(provider.inner)
    if isinstance(provider, OpenAICompatibleProvider):
        return "live_model", ""
    return ("not_run" if provider is None else "unknown"), ""


def planning_use(planner, outcome) -> ModelUse:
    """A tool failure before any call is not model execution. / 调用模型前的工具失败不算模型执行。"""
    if outcome is None or not outcome.input_hash:
        return ModelUse()
    source, recording = provider_source(getattr(planner, "provider", None))
    return ModelUse(source=source, provider_id=outcome.provider_id, model_id=outcome.model_id,
                    reported_model_id=outcome.spec.provenance.model_id if outcome.spec else "",
                    prompt_version=outcome.prompt_version, prompt_sha256=outcome.prompt_sha256,
                    input_sha256=outcome.input_hash, recording_sha256=recording, outcome=outcome.status)


def seal(record: SourceModel) -> dict:
    """Self-checking record; the database's write-once rule enforces immutability. / 自校验记录；账本只写一次规则保证不可覆盖。"""
    body = record.model_dump(mode="json")
    return {"body": body, "sha256": digest(body)}


def unseal(record: dict, model):
    """Reject corruption instead of inferring a replacement source. / 拒绝损坏记录，不推断替代来源。"""
    if set(record) != {"body", "sha256"} or digest(record["body"]) != record["sha256"]:
        raise ValueError("provenance record digest mismatch")
    return model.model_validate(record["body"])


def project(record: dict) -> RunProvenance:
    """Reconstruct a version from saved records; legacy rows stay unknown after an upgrade.

    从持久记录重建版本；升级后旧行仍保持来源未知。
    """
    saved = (record.get("decision") or {}).get(NAMESPACE, {})
    header = unseal(saved["run"], RunHeader) if "run" in saved else None
    mission_id, version = record["mission_id"], record["version"]
    if header is not None and (header.mission_id, header.mission_version) != (mission_id, version):
        raise ValueError("provenance mission binding mismatch")
    images = tuple(unseal(value, EvidenceOrigin) for key, value in sorted(saved.items()) if key.startswith("evidence:"))
    analyses = tuple(unseal(value, AnalysisOrigin) for key, value in sorted(saved.items()) if key.startswith("analysis:"))
    return RunProvenance(
        mission_id=mission_id, mission_version=version, status="recorded" if header else "legacy_unknown", run=header,
        execution_backend=header.execution_backend if header else "legacy_unknown",
        planning=header.planning if header else ModelUse(source="legacy_unknown", outcome="unknown"),
        imagery=images, analysis=analyses,
        analysis_sources=tuple(sorted({a.use.source for a in analyses})) if analyses else (
            "not_run" if header else "legacy_unknown",))


def export(record: dict) -> dict:
    """JSON projection with a reproducible digest. / 带可复算摘要的 JSON 投影。"""
    value = project(record)
    return {**value.model_dump(mode="json"), "sha256": value.sha256}
