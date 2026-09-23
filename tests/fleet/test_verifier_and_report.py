"""Evidence Verifier and three-column report: lower, never raise; model notes never move a column (WP-M2-13).

证据验证器与三列报告：只降不升；模型备注从不移动列（WP-M2-13）。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from drone_agent.admission.compiler import compile_spec
from drone_agent.contracts import (
    EffectVerdict,
    Evidence,
    ExecutionStatus,
    FactSource,
    Frame,
    Pose,
    Position,
    StepOutcome,
    TimeWindow,
    WorldFact,
    WorldKind,
    utcnow,
)
from drone_agent.fleet.report import build_report, classify
from drone_agent.fleet.verifier import accept_fact, business_judgment, recheck, verify
from drone_agent.providers import ScriptedProvider
from tests.admission.scene import compile_context, registry, spec
from tests.runtime.test_m2_onboard import image

PACKAGE = compile_spec(spec(), compile_context()).package


def evidence(raw: bytes, *, x=4.0, y=4.0, variance=1.0, age=0.4, subject="asset_red",
             step="inspect_asset_red") -> Evidence:
    now = utcnow()
    return Evidence(evidence_id="image:" + hashlib.sha256(raw).hexdigest(), kind="image", media_ref="images/x.rgb",
                    sha256=hashlib.sha256(raw).hexdigest(),
                    time_window=TimeWindow(timestamp=now, valid_until=now + timedelta(seconds=age)),
                    captured_pose=Pose(frame=Frame(frame_id="map_enu", map_version="campus_v2"),
                                       position=Position(x=x, y=y, z=4, covariance=(variance, 0, 0, 0, variance, 0,
                                                                                    0, 0, variance))),
                    subject_ids=[subject], produced_by_skill_instance=step)


def check(item: Evidence, raw: bytes | None):
    return recheck(item, raw, 160, 120, PACKAGE, registry())[0]


def test_deterministic_recheck_recomputes_each_condition():
    red, flat = image("red"), image(None)
    assert check(evidence(red), red) is EffectVerdict.VERIFIED
    assert check(evidence(red), None) is EffectVerdict.UNKNOWN
    assert check(evidence(red), flat) is EffectVerdict.REFUTED  # digest mismatch / 摘要不符
    assert check(evidence(flat), flat) is EffectVerdict.UNVERIFIED  # no signature colour / 无特征颜色
    assert check(evidence(image("blue")), image("blue")) is EffectVerdict.UNVERIFIED  # wrong signature / 特征不符
    assert check(evidence(red, subject="asset_blue"), red) is EffectVerdict.REFUTED
    assert check(evidence(red, step="takeoff"), red) is EffectVerdict.REFUTED
    assert check(evidence(red, variance=16), red) is EffectVerdict.UNVERIFIED
    assert check(evidence(red, age=5), red) is EffectVerdict.UNKNOWN
    assert check(evidence(red, x=10, y=10), red) is EffectVerdict.REFUTED


def test_disagreement_becomes_unknown_and_the_service_never_raises_a_verdict():
    red, flat = image("red"), image(None)
    agree = verify(evidence(red), red, 160, 120, PACKAGE, registry(), EffectVerdict.VERIFIED)
    assert agree.agrees and agree.final_verdict is EffectVerdict.VERIFIED
    lowered = verify(evidence(flat), flat, 160, 120, PACKAGE, registry(), EffectVerdict.VERIFIED)
    assert not lowered.agrees and lowered.final_verdict is EffectVerdict.UNKNOWN
    raised = verify(evidence(red), red, 160, 120, PACKAGE, registry(), EffectVerdict.UNVERIFIED)
    assert not raised.agrees and raised.final_verdict is EffectVerdict.UNKNOWN


def outcome(step, status=ExecutionStatus.SUCCEEDED, verdict=EffectVerdict.VERIFIED, version=1):
    return StepOutcome(mission_id=PACKAGE.mission_id, mission_version=version, step_id=step, robot_id="uav_01",
                       execution_status=status, effect_verdict=verdict)


def test_report_columns_follow_the_three_way_rule():
    assert classify(outcome("s"), None)[0] == "completed"
    assert classify(outcome("s"), EffectVerdict.UNKNOWN)[0] == "uncertain"
    assert classify(outcome("s", verdict=EffectVerdict.UNKNOWN), None)[0] == "uncertain"
    assert classify(outcome("s", status=ExecutionStatus.FAILED, verdict=EffectVerdict.UNVERIFIED), None)[0] == \
        "uncertain"
    assert classify(outcome("s", status=ExecutionStatus.TIMEOUT, verdict=EffectVerdict.UNKNOWN), None)[0] == \
        "uncertain"
    assert classify(outcome("s", status=ExecutionStatus.FAILED, verdict=EffectVerdict.REFUTED), None)[0] == \
        "not_completed"
    assert classify(outcome("s", status=ExecutionStatus.CANCELLED, verdict=EffectVerdict.UNKNOWN), None)[0] == \
        "not_completed"
    assert classify(None, None)[0] == "not_completed"


def test_model_facts_are_notes_that_never_move_a_row():
    outcomes = {1: {n.task_id: outcome(n.task_id) for n in PACKAGE.nodes}}
    outcomes[1]["inspect_asset_red"] = outcome("inspect_asset_red", ExecutionStatus.FAILED, EffectVerdict.UNVERIFIED)
    fact = {"fact_id": "vlm:x", "subject": "asset_red", "predicate": "anomaly_suspected",
            "value": {"suspected": False, "status": "actionable"}, "source": "model", "confidence": 0.99}
    plain = build_report("m", {1: PACKAGE}, outcomes)
    noted = build_report("m", {1: PACKAGE}, outcomes, facts=[fact])
    assert [r.column for r in plain.rows] == [r.column for r in noted.rows]
    assert noted.targets == plain.targets == {"asset_red": "uncertain"} and noted.facts == [fact]


async def test_vlm_judgment_is_a_versioned_belief_and_low_confidence_stays_candidate():
    red = image("red")
    item = evidence(red)
    answers = [{"content": '{"anomaly_suspected": true, "confidence": 0.4, "description": "rust"}'},
               {"content": '```json\n{"anomaly_suspected": false, "confidence": 0.9}\n```'},
               {"content": "no idea"}]
    provider = ScriptedProvider(answers, model="qwen-vl-max")
    low = await business_judgment(item, red, 160, 120, provider, "qwen-vl-max", asset_id="asset_red")
    assert (low.source, low.world_kind, low.source_version, low.value["status"]) == (
        FactSource.MODEL, WorldKind.BELIEF, "qwen-vl-max", "candidate")
    assert low.evidence_refs == [item.evidence_id] and accept_fact(low)
    high = await business_judgment(item, red, 160, 120, provider, "qwen-vl-max", asset_id="asset_red")
    assert high.value["status"] == "actionable"
    assert await business_judgment(item, red, 160, 120, provider, "qwen-vl-max", asset_id="asset_red") is None


def test_only_timed_belief_facts_enter_the_scene_graph():
    now = utcnow()
    belief = WorldFact(fact_id="f1", subject="asset_red", predicate="seen", timestamp=now, source=FactSource.SENSOR)
    assert accept_fact(belief)
    assert not accept_fact(belief.model_copy(update={"world_kind": WorldKind.PREDICTED}))
    assert not accept_fact(WorldFact(fact_id="f2", subject="asset_red", predicate="seen", timestamp=now,
                                     source=FactSource.SIM_TRUTH, world_kind=WorldKind.TRUTH))
    assert not accept_fact(belief.model_copy(update={"source": FactSource.MODEL, "source_version": None}))
