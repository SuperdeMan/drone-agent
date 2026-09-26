"""P2 analysis activity: labelled scripted and deterministic analyzers over service-verified evidence (D057).

An analysis runs only on evidence the service re-checked as verified, and first checks that the stored media still
matches the evidence digest. The `scripted` analyzer answers from a hand-written fixture whose digest is pinned by
the run's catalog; the `deterministic` analyzer measures the asset's registered colour signature and compares it
with a nominal band. Both produce a candidate finding with its source; neither is a model, and nothing here can
change a flight's execution status, effect verdict or safety verdict. A result the analyzer cannot give is an
explicit refusal with a reason, never a default verdict.

P2 分析活动：作用于服务复核通过证据的带标注脚本与确定性分析器（D057）。

分析只在服务复核为已证实的证据上运行，并先核对存储的媒体仍与证据摘要一致。`scripted` 分析器按手写夹具作答，夹具
摘要由运行的目录固定；`deterministic` 分析器测量资产登记的颜色特征并与正常区间比较。两者都给出带来源的候选发现；
二者都不是模型，这里也不能改变飞行的执行状态、效果判定或安全判定。分析器给不出的结果是带原因的明确拒判，从不是
缺省判定。
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field

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
