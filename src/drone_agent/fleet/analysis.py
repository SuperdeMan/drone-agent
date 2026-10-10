"""Analysis over service-verified evidence: the P2 labelled analyzers and the P4 quality layer and model analyzer.

P2 (D057): an analysis runs only on evidence the service re-checked as verified, and first checks that the stored
media still matches the evidence digest. The `scripted` analyzer answers from a hand-written fixture whose digest is
pinned by the run's catalog; the `deterministic` analyzer measures the asset's registered colour signature and
compares it with a nominal band.

P4 (D063): every P4 job first passes the deterministic quality layer (`quality-v1`: media digest, resolution,
exposure, sharpness, capture time); a failure refuses the job before any model is called. P6 (D078): a `quality-v2`
profile also checks the target region from the capture pose and the profile's camera; poses carry no heading, so
the geometry is the conservative one that holds at every heading. The model analyzer shows a
vision model the asset's registered reference images and the current capture with fixed text and no tools, accepts
only the profile's exact JSON, and maps it to a verdict with a pinned threshold. Everything here produces a
candidate with its source; nothing can change a flight's execution status, effect verdict or safety verdict, and a
result the analyzer cannot give is an explicit refusal with a reason, never a default verdict.

作用于服务复核通过证据的分析：P2 的带标注分析器，以及 P4 的质量层与模型分析器。

P2（D057）：分析只在服务复核为已证实的证据上运行，并先核对存储的媒体仍与证据摘要一致。`scripted` 分析器按手写夹具
作答，夹具摘要由运行的目录固定；`deterministic` 分析器测量资产登记的颜色特征并与正常区间比较。

P4（D063）：每个 P4 作业先经确定性质量层（`quality-v1`：媒体摘要、分辨率、曝光、清晰度、采集时刻）；不合格即在调用任何
模型前拒判。P6（D078）：`quality-v2` 画像另按采集位姿与画像固定的相机检查目标区域；位姿没有航向，几何按任何航向都成立的
保守方式计算。模型分析器以固定文本、无工具的方式向视觉模型展示资产的登记参考图与当前采集，只接受画像规定的确切 JSON，
并按固定阈值映射为结论。这里的一切都只产生带来源的候选；都不能改变飞行的执行状态、效果判定或安全判定，给不出的结果
是带原因的明确拒判，从不是缺省判定。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import math
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
    TargetQualityProfile,
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


# ── P6: target-region quality (`quality-v2`, D078) / P6：目标区域质量（`quality-v2`，D078） ──

# Headings sampled for the worst visible fraction, in 1 degree steps. / 求最坏可见比例时采样的航向，步长 1 度。
HEADINGS = tuple(math.radians(step) for step in range(360))
MIN_DEPTH_M = 0.3


def _clip(polygon: list[tuple[float, float]], width: float, height: float) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clipping of a convex polygon to the frame [0, width] x [0, height].

    把凸多边形按 Sutherland-Hodgman 方法裁剪到画面 [0, width] × [0, height]。
    """
    for axis, bound, lower in ((0, 0.0, True), (0, float(width), False), (1, 0.0, True), (1, float(height), False)):
        def inside(point, a=axis, b=bound, low=lower):
            return point[a] >= b if low else point[a] <= b

        def cross(p, q, a=axis, b=bound):
            share = (b - p[a]) / (q[a] - p[a])
            return p[0] + share * (q[0] - p[0]), p[1] + share * (q[1] - p[1])

        clipped: list[tuple[float, float]] = []
        for index, current in enumerate(polygon):
            previous = polygon[index - 1]
            if inside(current):
                if not inside(previous):
                    clipped.append(cross(previous, current))
                clipped.append(current)
            elif inside(previous):
                clipped.append(cross(previous, current))
        polygon = clipped
        if not polygon:
            break
    return polygon


def _area(polygon: list[tuple[float, float]]) -> float:
    if len(polygon) < 3:
        return 0.0
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1],
                                                                   strict=True))) / 2


def target_geometry(pose: dict | None, asset: dict, profile: TargetQualityProfile, width: int,
                    height: int) -> dict[str, float] | None:
    """Where the registered asset can be in the capture, at any heading; None when it cannot be established.

    The asset is the map-aligned square of its registered position and `size_m`; the camera looks straight down from
    `mount_below_m` under the reported position. Without a heading the projected square can be anywhere on the circle
    of its offset, rotated with it, so the visible fraction is the minimum over headings; the core (pixels that are
    the target at every heading) is the disc around the image centre inside the square, which exists only while the
    nadir lies inside the square; the boundary ring holds every edge pixel at every heading.

    登记资产在采集中可能的位置（任何航向）；无法确立时为 None。资产是由登记位置与 `size_m` 给出的地图轴对齐正方形；相机
    从上报位置下方 `mount_below_m` 处垂直向下。没有航向时，投影正方形可在其偏移所在的圆上随之转动，因此可见比例取所有
    航向的最小值；核心（任何航向下都属于目标的像素）是画面中心周围、位于正方形内的圆盘，只有相机正下方点在正方形内时才
    存在；边界环在任何航向下都包含全部边缘像素。
    """
    camera = profile.camera
    position = (pose or {}).get("position") or {}
    place, size = asset.get("position"), asset.get("size_m")
    if asset.get("camera_id") != camera.camera_id or (width, height) != (camera.width, camera.height) \
            or not isinstance(place, (list, tuple)) or len(place) != 3 or not isinstance(size, (int, float)) \
            or isinstance(size, bool) or size <= 0 \
            or any(not isinstance(position.get(axis), (int, float)) for axis in "xyz"):
        return None
    values = [position["x"], position["y"], position["z"], *place, size]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        return None
    depth = position["z"] - camera.mount_below_m - place[2]
    if depth < MIN_DEPTH_M:
        return None
    focal = camera.focal_px
    dx, dy = place[0] - position["x"], place[1] - position["y"]
    half = focal * size / 2 / depth
    ox, oy = focal * dx / depth, focal * dy / depth
    corners = [(ox - half, oy - half), (ox + half, oy - half), (ox + half, oy + half), (ox - half, oy + half)]
    cx, cy = width / 2, height / 2
    whole = (2 * half) ** 2
    visible = 1.0
    for angle in HEADINGS:
        c, s = math.cos(angle), math.sin(angle)
        turned = [(cx + x * c - y * s, cy + x * s + y * c) for x, y in corners]
        visible = min(visible, _area(_clip(turned, width, height)) / whole)
    offset = math.hypot(ox, oy)
    return {"depth_m": round(depth, 4), "side_px": round(2 * half, 3), "offset_px": round(offset, 3),
            "visible_min": round(visible, 6),
            "core_radius_px": round(focal * (size / 2 - max(abs(dx), abs(dy))) / depth, 3),
            "ring_outer_px": round(offset + half * math.sqrt(2) + 2, 3)}


def check_target_quality(image: np.ndarray, profile: TargetQualityProfile, *, pose: dict | None,
                         asset: dict) -> tuple[str | None, dict[str, float]]:
    """`quality-v2`: geometry first, then the whole-frame checks of `quality-v1`, then the target region.

    Refusals report in this order: `quality.target_unknown`, the `quality-v1` reasons, then the target out of frame,
    too small, off centre, badly exposed and blurred. Every measure taken is returned.

    `quality-v2`：先几何，再 `quality-v1` 的整图检查，最后是目标区域。拒判按此顺序报告：`quality.target_unknown`、
    `quality-v1` 的原因，然后是目标出画、过小、偏离中心、曝光异常与模糊。返回已取得的全部测量值。
    """
    height, width = image.shape[:2]
    geometry = target_geometry(pose, asset, profile, width, height)
    if geometry is None:
        return "quality.target_unknown", {"width": float(width), "height": float(height)}
    reason, measures = check_quality(image, profile)
    measures.update({f"target_{key}": float(value) for key, value in geometry.items()})
    if reason is not None:
        return reason, measures
    limits = profile.target
    if geometry["visible_min"] < limits.min_visible_fraction:
        return "quality.target_out_of_frame", measures
    if geometry["side_px"] < limits.min_side_px:
        return "quality.target_too_small", measures
    rows, columns = np.mgrid[0:height, 0:width]
    distance = np.hypot(columns + 0.5 - width / 2, rows + 0.5 - height / 2)
    core = distance <= geometry["core_radius_px"]
    measures["target_core_px"] = float(core.sum())
    if core.sum() < limits.min_core_px:
        return "quality.target_off_center", measures
    inside = image[core].astype(np.int32)
    dark = float((inside.max(axis=1) < limits.dark_level).mean())
    bright = float((inside.min(axis=1) > limits.bright_level).mean())
    measures.update({"target_dark_fraction": round(dark, 6), "target_bright_fraction": round(bright, 6)})
    if dark > limits.max_dark_fraction or bright > limits.max_bright_fraction:
        return "quality.target_exposure", measures
    rgb = image.astype(np.float32)
    luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    gradient = np.hypot(np.abs(np.diff(luminance, axis=1))[:-1, :], np.abs(np.diff(luminance, axis=0))[:, :-1])
    ring = ((distance >= geometry["core_radius_px"] - 2) & (distance <= geometry["ring_outer_px"]))[:-1, :-1]
    sharpness = float(np.percentile(gradient[ring], 99.5)) if ring.any() else 0.0
    measures["target_sharpness"] = round(sharpness, 3)
    if sharpness < limits.min_sharpness:
        return "quality.target_blurry", measures
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


def zoom_parts(image: np.ndarray, grid: int, overlap: float) -> list[tuple[str, np.ndarray]]:
    """The capture's `grid` x `grid` overlapping parts, row by row, each named by its position.

    采集的 `grid` × `grid` 个重叠局部，逐行排列，各以位置命名。
    """
    if grid <= 1:
        return []
    height, width = image.shape[:2]
    part_h, part_w = height / (grid - (grid - 1) * overlap), width / (grid - (grid - 1) * overlap)
    rows = ("top", "bottom") if grid == 2 else ("top", "middle", "bottom")
    columns = ("left", "right") if grid == 2 else ("left", "centre", "right")
    parts = []
    for i, row in enumerate(rows):
        for j, column in enumerate(columns):
            top, left = round(i * part_h * (1 - overlap)), round(j * part_w * (1 - overlap))
            bottom, right = min(height, round(top + part_h)), min(width, round(left + part_w))
            parts.append((f"{row}-{column}", np.ascontiguousarray(image[top:bottom, left:right])))
    return parts


def model_messages(profile: ModelProfile, references: list[str], current: str, asset: str,
                   parts: list[tuple[str, str]] = ()) -> list[dict]:
    """References first, then any enlarged parts of the current capture, then the whole current capture (always the
    last image), then the fixed instruction; no tools are ever offered.

    先参考图，再是当前采集的放大局部（如有），然后是完整的当前采集（总是最后一张图），最后是固定指令；从不提供工具。
    """
    content: list[dict] = []
    for index, uri in enumerate(references, 1):
        content.append({"type": "text", "text": f"REFERENCE {index} of {len(references)}: the registered normal "
                                                "appearance of this asset."})
        content.append({"type": "image_url", "image_url": {"url": uri, "detail": profile.detail}})
    for index, (name, uri) in enumerate(parts, 1):
        content.append({"type": "text", "text": f"CURRENT PART {index} of {len(parts)}: the {name} part of the "
                                                "capture to judge, enlarged."})
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
    parts = [(name, encode(part, profile)[0]) for name, part in zoom_parts(current, profile.zoom_grid,
                                                                           profile.zoom_overlap)]
    messages = model_messages(profile, [encode(image, profile)[0] for image in references],
                              encode(current, profile)[0], asset, parts)
    tokens_in = tokens_out = calls = 0
    latency, reason, reported = 0.0, "model.error", ""
    # The effort is sent only when the profile pins one, so earlier profiles make exactly the same request (D076).
    # 只有画像固定了思考深度时才发送，因此早先的画像发出完全相同的请求（D076）。
    effort = {"reasoning_effort": profile.reasoning_effort} if profile.reasoning_effort is not None else {}
    for _ in range(profile.attempts):
        started = time.monotonic()
        calls += 1
        try:
            content, reported, finish, usage = await asyncio.wait_for(
                provider.complete(messages, model, profile.temperature, profile.max_tokens, thinking=profile.thinking,
                                  timeout_s=profile.timeout_s, **effort), profile.timeout_s)
        except TimeoutError:
            latency += (time.monotonic() - started) * 1000
            reason = "model.timeout"
            continue
        except ReplayMismatch:
            raise
        except Exception as error:  # an HTTP or transport failure spends the attempt / HTTP 或传输失败，本次尝试已用掉
            latency += (time.monotonic() - started) * 1000
            # The client's own read timeout (httpx) fires before the profile's deadline; it is still a timeout.
            # 客户端自身的读取超时（httpx）先于画像的截止时间触发；它仍是超时。
            reason = "model.timeout" if "Timeout" in type(error).__name__ else "model.error"
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
