"""Analysis over service-verified evidence: the P2 labelled analyzers and the P4 quality layer and model analyzer.

P2 (D057): an analysis runs only on evidence the service re-checked as verified, and first checks that the stored
media still matches the evidence digest. The `scripted` analyzer answers from a hand-written fixture whose digest is
pinned by the run's catalog; the `deterministic` analyzer measures the asset's registered colour signature and
compares it with a nominal band.

P4 (D063): every P4 job first passes the deterministic quality layer (`quality-v1`: media digest, resolution,
exposure, sharpness, capture time); a failure refuses the job before any model is called. The model analyzer shows a
vision model the asset's registered reference images and the current capture with fixed text and no tools, accepts
only the profile's exact JSON, and maps it to a verdict with a pinned threshold. Everything here produces a
candidate with its source; nothing can change a flight's execution status, effect verdict or safety verdict, and a
result the analyzer cannot give is an explicit refusal with a reason, never a default verdict.

作用于服务复核通过证据的分析：P2 的带标注分析器，以及 P4 的质量层与模型分析器。

P2（D057）：分析只在服务复核为已证实的证据上运行，并先核对存储的媒体仍与证据摘要一致。`scripted` 分析器按手写夹具
作答，夹具摘要由运行的目录固定；`deterministic` 分析器测量资产登记的颜色特征并与正常区间比较。

P4（D063）：每个 P4 作业先经确定性质量层（`quality-v1`：媒体摘要、分辨率、曝光、清晰度、采集时刻）；不合格即在调用任何
模型前拒判。模型分析器以固定文本、无工具的方式向视觉模型展示资产的登记参考图与当前采集，只接受画像规定的确切 JSON，
并按固定阈值映射为结论。这里的一切都只产生带来源的候选；都不能改变飞行的执行状态、效果判定或安全判定，给不出的结果
是带原因的明确拒判，从不是缺省判定。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import time
from typing import Literal

import numpy as np
from PIL import Image
from pydantic import Field, ValidationError

from drone_agent.fleet.business_models import (
    Cost,
    JobResult,
    ModelAnswer,
    ModelProfile,
    Prices,
    QualityProfile,
    Usage,
    refusal,
)
from drone_agent.fleet.workflow_models import (
    AnalysisFixture,
    ScriptedAnalyzer,
    SignatureAnalyzer,
    WorkflowModel,
)
from drone_agent.mission.verify import image_quality


class AnalysisResult(WorkflowModel):
    """One analysis attempt: a candidate finding or a refusal, always with its source. / 一次分析尝试：候选发现或拒判，始终带来源。"""

    analyzer: str
    source: Literal["scripted", "deterministic"]
    verdict: Literal["suspected", "normal", "refused"]
    confidence: float = Field(ge=0, le=1)
    description: str = Field(default="", max_length=300)
    reasons: tuple[str, ...] = ()
    measures: dict[str, float] = Field(default_factory=dict)
    asset_id: str
    evidence_id: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: str = ""


def analyze(name: str, analyzer: ScriptedAnalyzer | SignatureAnalyzer, *, asset_id: str, evidence_id: str,
            evidence_sha256: str, media: bytes | None, width: int | None, height: int | None, asset: dict,
            fixture: AnalysisFixture | None = None, fixture_sha256: str = "") -> AnalysisResult:
    """Analyze one verified acquisition; refuse whenever an input is missing or does not match.

    分析一次已证实的采集；任何输入缺失或不一致时拒判。
    """
    source = "scripted" if isinstance(analyzer, ScriptedAnalyzer) else "deterministic"
    common = {"analyzer": name, "source": source, "asset_id": asset_id, "evidence_id": evidence_id,
              "input_sha256": evidence_sha256, "fixture_sha256": fixture_sha256}

    def refused(reason: str) -> AnalysisResult:
        return AnalysisResult(**common, verdict="refused", confidence=0.0, reasons=(reason,))

    if media is None or not width or not height:
        return refused("analysis.media_missing")
    if hashlib.sha256(media).hexdigest() != evidence_sha256:
        return refused("analysis.media_mismatch")
    if isinstance(analyzer, ScriptedAnalyzer):
        answer = fixture.answers.get(asset_id) if fixture is not None else None
        if answer is None:
            return refused("analysis.no_scripted_answer")
        return AnalysisResult(**common, verdict=answer.verdict, confidence=answer.confidence,
                              description=answer.description, reasons=("scripted_fixture",))
    band = analyzer.bands.get(asset_id)
    signature = asset.get("visual_signature")
    quality = image_quality(media, width, height)
    if band is None or not signature or f"{signature}_fraction" not in quality:
        return refused("analysis.no_baseline")
    fraction = quality[f"{signature}_fraction"]
    low, high = band
    measures = {"signature_fraction": round(fraction, 6), "edge_contrast": round(quality["edge_contrast"], 6),
                "band_low": low, "band_high": high}
    if fraction < low:
        return AnalysisResult(**common, verdict="suspected", confidence=1.0, measures=measures,
                              reasons=("signature_below_band",),
                              description=f"{signature} fraction {fraction:.3f} below {low:.3f}")
    if fraction > high:
        return AnalysisResult(**common, verdict="suspected", confidence=1.0, measures=measures,
                              reasons=("signature_above_band",),
                              description=f"{signature} fraction {fraction:.3f} above {high:.3f}")
    return AnalysisResult(**common, verdict="normal", confidence=1.0, measures=measures, reasons=("within_band",),
                          description=f"{signature} fraction {fraction:.3f} within [{low:.3f}, {high:.3f}]")


# ── P4: the quality layer and the model analyzer (D063) / P4：质量层与模型分析器（D063） ──


def pixels(media: bytes | None, width: int | None, height: int | None) -> np.ndarray | None:
    """Raw RGB bytes as an HxWx3 array; None when missing or the size does not match.

    原始 RGB 字节转为 HxWx3 数组；缺失或尺寸不符时为 None。
    """
    if media is None or not width or not height or len(media) != width * height * 3:
        return None
    return np.frombuffer(media, dtype=np.uint8).reshape(height, width, 3)


def decode_image(data: bytes) -> np.ndarray:
    """An encoded image (JPEG, PNG) as RGB pixels; metadata is dropped. / 已编码图像解码为 RGB 像素；丢弃元数据。"""
    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8).copy()


def check_quality(image: np.ndarray, profile: QualityProfile) -> tuple[str | None, dict[str, float]]:
    """`quality-v1`: the first failing check (resolution, exposure, sharpness) or None, with every measure.

    Luminance is Rec. 601; the clipped fraction counts pixels at or below 5 or at or above 250; sharpness is the 99.5th
    percentile of the luminance gradient magnitude, so a blurred edge scores low even when most of the frame is flat.

    `quality-v1`：第一个不合格项（分辨率、曝光、清晰度）或 None，以及全部测量值。亮度按 Rec. 601；截断比例统计亮度
    <=5 或 >=250 的像素；清晰度为亮度梯度幅值的 99.5 分位，因此即使画面大部分平坦，模糊的边缘也得分低。
    """
    height, width = image.shape[:2]
    rgb = image.astype(np.float32)
    luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    mean = float(luminance.mean())
    clipped = float(((luminance <= 5) | (luminance >= 250)).mean())
    gx = np.abs(np.diff(luminance, axis=1))[:-1, :]
    gy = np.abs(np.diff(luminance, axis=0))[:, :-1]
    sharpness = float(np.percentile(np.hypot(gx, gy), 99.5)) if gx.size else 0.0
    measures = {"width": float(width), "height": float(height), "luminance": round(mean, 3),
                "clipped_fraction": round(clipped, 6), "sharpness": round(sharpness, 3)}
    if width < profile.min_width or height < profile.min_height:
        return "quality.resolution", measures
    low, high = profile.luminance
    if not low <= mean <= high or clipped > profile.max_clipped_fraction:
        return "quality.exposure", measures
    if sharpness < profile.min_sharpness:
        return "quality.blurry", measures
    return None, measures


def encode(image: np.ndarray, profile: ModelProfile) -> tuple[str, str]:
    """The deterministic model input of one image: long side limited, JPEG or PNG; (data URI, SHA-256 of the bytes).

    一张图的确定性模型输入：限制长边，JPEG 或 PNG；返回（data URI，编码字节的 SHA-256）。
    """
    picture = Image.fromarray(np.ascontiguousarray(image), "RGB")
    long_side = max(picture.size)
    if long_side > profile.max_long_side:
        scale = profile.max_long_side / long_side
        size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
        picture = picture.resize(size, Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    if profile.image_format == "jpeg":
        picture.save(buffer, format="JPEG", quality=profile.jpeg_quality, optimize=False, progressive=False)
        mime = "image/jpeg"
    else:
        picture.save(buffer, format="PNG", optimize=False)
        mime = "image/png"
    data = buffer.getvalue()
    return f"data:{mime};base64," + base64.b64encode(data).decode(), hashlib.sha256(data).hexdigest()


def model_messages(profile: ModelProfile, references: list[str], current: str, asset: str) -> list[dict]:
    """References first, then the current capture, then the fixed instruction; no tools are ever offered.

    先参考图、再当前采集、最后是固定指令；从不提供工具。
    """
    content: list[dict] = []
    for index, uri in enumerate(references, 1):
        content.append({"type": "text", "text": f"REFERENCE {index} of {len(references)}: the registered normal "
                                                "appearance of this asset."})
        content.append({"type": "image_url", "image_url": {"url": uri, "detail": profile.detail}})
    content.append({"type": "text", "text": "CURRENT: the capture to judge."})
    content.append({"type": "image_url", "image_url": {"url": current, "detail": profile.detail}})
    content.append({"type": "text", "text": profile.instruction.replace("{asset}", asset[:300])})
    return [{"role": "system", "content": profile.system_prompt}, {"role": "user", "content": content}]


def parse_answer(content: str, profile: ModelProfile) -> ModelAnswer | None:
    """The profile's exact JSON or None; unknown fields, missing fields and unlisted types are all malformed.

    画像规定的确切 JSON 或 None；未知字段、缺失字段与未列出的类型都属不合格。
    """
    from drone_agent.planner.draft import salvage_json

    data = salvage_json(content)
    if data is None:
        return None
    try:
        answer = ModelAnswer.model_validate(data)
    except ValidationError:
        return None
    if answer.defect_type != "none" and answer.defect_type not in profile.defect_types:
        return None
    return answer


def decide(answer: ModelAnswer, profile: ModelProfile, threshold: float) -> dict:
    """The deterministic mapping of a valid answer to a candidate verdict. / 把合格回答确定性地映射为候选结论。"""
    common = {"score": answer.anomaly_score, "threshold": threshold, "description": answer.description[:400]}
    if not answer.image_usable:
        return {**common, "verdict": "refused", "reasons": ("analysis.undeterminable", f"model.{answer.unusable_reason}")}
    if not answer.target_matches_reference:
        return {**common, "verdict": "refused", "reasons": ("target.mismatch",)}
    if answer.anomaly_score >= threshold:
        defect = answer.defect_type if answer.defect_type != "none" else profile.generic_type
        return {**common, "verdict": "suspected", "reasons": ("model.score_at_or_above_threshold",),
                "defect_type": defect, "family": profile.family}
    return {**common, "verdict": "normal", "reasons": ("model.score_below_threshold",)}


def cost_of(usage: Usage, prices: Prices | None) -> Cost | None:
    if prices is None:
        return None
    amount = usage.input_tokens / 1e6 * prices.input_per_mtok + usage.output_tokens / 1e6 * prices.output_per_mtok
    return Cost(amount=round(amount, 8), currency=prices.currency, source=prices.source)


async def analyze_model(*, profile: ModelProfile, threshold: float, provider, provider_id: str, model: str,
                        references: list[np.ndarray], current: np.ndarray, asset: str, prices: Prices | None = None,
                        measures: dict[str, float] | None = None) -> JobResult:
    """Ask the vision model once per attempt; any failure is a refusal with its reason, never a verdict.

    A provider of None means no credentials were configured (`model.unavailable`). Replay mismatches are raised, not
    refused: a recording that does not match its request is a reproducibility failure of the caller.

    每次尝试询问一次视觉模型；任何失败都是带原因的拒判，从不是结论。provider 为 None 表示没有配置凭证
    （`model.unavailable`）。回放不匹配会被抛出而不是拒判：录制与请求不符是调用方的可复现性失败。
    """
    from drone_agent.fleet.provenance import provider_source
    from drone_agent.providers.replay import ReplayMismatch

    base = {"prompt_version": profile.prompt_version, "prompt_sha256": profile.prompt_sha256,
            "quality": dict(measures or {}), "threshold": threshold, "provider_id": provider_id, "model_id": model}
    if provider is None:
        return refusal("model.unavailable", **base)
    if model != profile.model:
        return refusal("model.profile_mismatch", **base)
    messages = model_messages(profile, [encode(image, profile)[0] for image in references],
                              encode(current, profile)[0], asset)
    tokens_in = tokens_out = calls = 0
    latency, reason, reported = 0.0, "model.error", ""
    for _ in range(profile.attempts):
        started = time.monotonic()
        calls += 1
        try:
            content, reported, finish, usage = await asyncio.wait_for(
                provider.complete(messages, model, profile.temperature, profile.max_tokens, thinking=False,
                                  timeout_s=profile.timeout_s), profile.timeout_s)
        except TimeoutError:
            latency += (time.monotonic() - started) * 1000
            reason = "model.timeout"
            continue
        except ReplayMismatch:
            raise
        except Exception:  # an HTTP or transport failure spends the attempt / HTTP 或传输失败，本次尝试已用掉
            latency += (time.monotonic() - started) * 1000
            reason = "model.error"
            continue
        latency += (time.monotonic() - started) * 1000
        tokens_in, tokens_out = tokens_in + int(usage[0] or 0), tokens_out + int(usage[1] or 0)
        if finish == "refusal":
            reason = "model.refusal"
            break
        answer = parse_answer(content, profile)
        if answer is None:
            reason = "model.malformed"
            continue
        spent = Usage(input_tokens=tokens_in, output_tokens=tokens_out)
        source, recording = provider_source(provider)
        return JobResult(**decide(answer, profile, threshold), **{k: v for k, v in base.items() if k != "threshold"},
                         source=source, recording_sha256=recording, reported_model_id=str(reported or "")[:80],
                         usage=spent, latency_ms=round(latency, 1), model_calls=calls, cost=cost_of(spent, prices))
    spent = Usage(input_tokens=tokens_in, output_tokens=tokens_out)
    source, recording = provider_source(provider)
    return refusal(reason, **base, source=source, recording_sha256=recording, reported_model_id=str(reported)[:80],
                   usage=spent, latency_ms=round(latency, 1), model_calls=calls, cost=cost_of(spent, prices))


def job_result(result: AnalysisResult, *, generic_defect: str, family: str) -> JobResult:
    """A P2 scripted or deterministic result in the P4 job shape. / 把 P2 的脚本或确定性结果转换为 P4 作业形式。"""
    if result.verdict == "refused":
        return refusal(result.reasons[0], source=result.source, measures=result.measures,
                       provider_id="workflow-analysis", model_id=result.analyzer,
                       recording_sha256=result.fixture_sha256)
    suspected = result.verdict == "suspected"
    return JobResult(verdict=result.verdict, reasons=result.reasons, score=result.confidence,
                     defect_type=generic_defect if suspected else None, family=family if suspected else None,
                     description=result.description, source=result.source, measures=result.measures,
                     provider_id="workflow-analysis", model_id=result.analyzer, recording_sha256=result.fixture_sha256)
