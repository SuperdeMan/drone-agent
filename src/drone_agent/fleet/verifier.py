"""Evidence Verifier: deterministic re-check of onboard evidence, plus an optional VLM business judgment (WP-M2-13).

The deterministic part recomputes, from the uploaded media and the evidence contract alone, what the
aircraft concluded: media digest, target id, producing skill instance, pose covariance, time window,
image quality against the asset's registered signature and distance to the asset. When it disagrees
with the onboard verdict the result is `unknown` and an issue is raised; it can lower a verdict, never
raise one. The VLM part runs afterwards on already-judged evidence and can only produce a belief
WorldFact (source=model, with model version and confidence); below the scene threshold it stays a
candidate. Nothing it returns feeds back into an effect verdict, the report's completion column or
any flight decision.

证据验证器：对机载证据的确定性复核，加上可选的 VLM 业务判断（WP-M2-13）。

确定性部分只根据上传的媒体与证据契约重算飞行器的结论：媒体摘要、目标 ID、产生它的技能实例、位姿
协方差、时间窗、按资产登记特征的影像质量以及与资产的距离。与机载判定不一致时结果为 `unknown` 并
产生问题；它只能降低判定，永不提升。VLM 部分在已判定的证据上事后运行，只能产出 belief 类 WorldFact
（source=model，带模型版本与置信度）；低于场景阈值时只是候选。它的输出从不回流到效果判定、报告的
完成列或任何飞行决策。
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime

from pydantic import Field

from drone_agent.contracts import (
    EffectVerdict,
    Evidence,
    FactSource,
    MissionPackage,
    WorldFact,
    WorldKind,
    utcnow,
)
from drone_agent.contracts.common import ContractModel
from drone_agent.mission.registry import Registry, distance
from drone_agent.mission.verify import image_quality

VLM_CANDIDATE_THRESHOLD = 0.7


class ServiceVerification(ContractModel):
    """The service's independent verdict for one piece of evidence. / 服务对一份证据的独立判定。"""

    evidence_id: str
    mission_id: str
    mission_version: int
    step_id: str
    onboard_verdict: EffectVerdict | None = None
    service_verdict: EffectVerdict
    final_verdict: EffectVerdict
    agrees: bool
    checks: dict[str, str] = Field(default_factory=dict)
    verified_at: datetime


def recheck(evidence: Evidence, media: bytes | None, width: int | None, height: int | None,
            package: MissionPackage, registry: Registry) -> tuple[EffectVerdict, dict[str, str]]:
    """Recompute the effect verdict from uploaded media; missing inputs are unknown.

    根据上传的媒体重算效果判定；缺少输入即为 unknown。
    """
    checks: dict[str, str] = {}
    node = next((n for n in package.nodes if n.task_id == evidence.produced_by_skill_instance), None)
    if node is None or "asset_id" not in node.params:
        checks["skill_instance"] = "evidence names no inspection step of this package"
        return EffectVerdict.REFUTED, checks
    asset = registry.data["assets"].get(node.params["asset_id"])
    if media is None or width is None or height is None:
        checks["media"] = "media not uploaded"
        return EffectVerdict.UNKNOWN, checks
    if hashlib.sha256(media).hexdigest() != evidence.sha256:
        checks["media"] = "digest mismatch"
        return EffectVerdict.REFUTED, checks
    checks["media"] = "ok"
    if evidence.subject_ids != [node.params["asset_id"]] or asset is None:
        checks["target"] = "subject does not match the step's asset"
        return EffectVerdict.REFUTED, checks
    checks["target"] = "ok"
    pose = evidence.captured_pose
    covariance = pose.position.covariance if pose else None
    if pose is None or covariance is None or any(not math.isfinite(v) for v in covariance):
        checks["pose"] = "missing pose or covariance"
        return EffectVerdict.UNKNOWN, checks
    threshold = registry.data["thresholds"]["image"]
    window = evidence.time_window
    if window.valid_until is None or not 0 <= (window.valid_until - window.timestamp).total_seconds() <= threshold["max_age_s"]:
        checks["time_window"] = "missing or too long"
        return EffectVerdict.UNKNOWN, checks
    checks["time_window"] = "ok"
    if max(covariance[0], covariance[4], covariance[8]) > threshold["max_position_variance"]:
        checks["pose"] = "variance above threshold"
        return EffectVerdict.UNVERIFIED, checks
    checks["pose"] = "ok"
    quality = image_quality(media, width, height)
    signature = asset.get("visual_signature", "red")
    low = threshold.get("min_signature_fraction", threshold.get("min_red_fraction"))
    high = threshold.get("max_signature_fraction", threshold.get("max_red_fraction"))
    fraction = quality.get(f"{signature}_fraction", 0)
    if (quality.get("width", 0) < threshold["min_width"] or quality.get("height", 0) < threshold["min_height"]
            or not low <= fraction <= high or quality.get("edge_contrast", 0) < threshold["min_edge_contrast"]):
        checks["quality"] = f"{signature} fraction {fraction:.3f}, edge {quality.get('edge_contrast', 0):.3f}"
        return EffectVerdict.UNVERIFIED, checks
    checks["quality"] = "ok"
    limit = registry.data["thresholds"].get("above_asset_m", 2)
    point = pose.position
    if distance([point.x, point.y], asset["position"][:2]) > limit:
        checks["distance"] = "captured too far from the asset"
        return EffectVerdict.REFUTED, checks
    checks["distance"] = "ok"
    return EffectVerdict.VERIFIED, checks


def verify(evidence: Evidence, media: bytes | None, width: int | None, height: int | None, package: MissionPackage,
           registry: Registry, onboard_verdict: EffectVerdict | None) -> ServiceVerification:
    """Recheck and compare; disagreement becomes unknown. / 复核并比较；不一致即为 unknown。"""
    service, checks = recheck(evidence, media, width, height, package, registry)
    agrees = onboard_verdict is None or onboard_verdict == service
    final = service if agrees else EffectVerdict.UNKNOWN
    # The service may confirm or lower, never raise: an onboard non-verified result stays non-verified.
    # 服务只能确认或降低，永不提升：机载未证实的结果保持未证实。
    if onboard_verdict is not None and onboard_verdict is not EffectVerdict.VERIFIED and final is EffectVerdict.VERIFIED:
        final = EffectVerdict.UNKNOWN
    return ServiceVerification(evidence_id=evidence.evidence_id, mission_id=package.mission_id,
                               mission_version=package.mission_version, step_id=evidence.produced_by_skill_instance,
                               onboard_verdict=onboard_verdict, service_verdict=service, final_verdict=final,
                               agrees=agrees, checks=checks, verified_at=utcnow())


def accept_fact(fact: WorldFact) -> bool:
    """Admission of a fact into the shared scene graph (05-world-model §5). / 事实进入共享场景图的门槛。"""
    if fact.source is FactSource.SIM_TRUTH or fact.world_kind is not WorldKind.BELIEF:
        return False
    if fact.timestamp is None or (fact.position is not None and (fact.frame is None or fact.position.covariance is None)):
        return False
    return not (fact.source is FactSource.MODEL and not fact.source_version)


VLM_QUESTION = (
    "This is a downward photo of a registered equipment marker taken during an inspection. Judge only what is "
    "visible. Reply with JSON only: {\"anomaly_suspected\": true|false, \"confidence\": number between 0 and 1, "
    "\"description\": short text}."
)


async def business_judgment(evidence: Evidence, media: bytes, width: int, height: int, provider, model: str, *,
                            asset_id: str, threshold: float = VLM_CANDIDATE_THRESHOLD) -> WorldFact | None:
    """Ask a vision model for a suspected-anomaly judgement; returns a belief fact or None.

    请视觉模型给出疑似异常判断；返回 belief 事实或 None。
    """
    from drone_agent.eval.viewer import png_data_uri
    from drone_agent.planner.draft import salvage_json

    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": png_data_uri(media, width, height)}},
        {"type": "text", "text": VLM_QUESTION},
    ]}]
    content, used, finish, _ = await provider.complete(messages, model, 0.1, 512, thinking=False)
    answer = salvage_json(content) if finish != "refusal" else None
    if not answer or not isinstance(answer.get("anomaly_suspected"), bool):
        return None
    try:
        confidence = min(1.0, max(0.0, float(answer.get("confidence", 0))))
    except (TypeError, ValueError):
        return None
    return WorldFact(
        fact_id=f"vlm:{evidence.evidence_id}", subject=asset_id, predicate="anomaly_suspected",
        value={"suspected": answer["anomaly_suspected"], "description": str(answer.get("description", ""))[:300],
               "status": "actionable" if confidence >= threshold else "candidate"},
        timestamp=utcnow(), source=FactSource.MODEL, source_version=used or model, confidence=confidence,
        world_kind=WorldKind.BELIEF, evidence_refs=[evidence.evidence_id],
    )
