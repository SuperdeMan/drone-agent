"""P4 analysis: the quality layer, deterministic encoding, the strict answer mapping and model failures (D063 §2–4).

P4 分析：质量层、确定性编码、严格的回答映射与模型失败（D063 §2–4）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np

from drone_agent.eval.scripted_vision import ScriptedVisionProvider, frame
from drone_agent.fleet.analysis import (
    analyze_model,
    check_quality,
    decide,
    encode,
    model_messages,
    parse_answer,
    pixels,
)
from drone_agent.fleet.business_models import load_profile, load_quality
from drone_agent.providers.replay import RecordingProvider, ReplayProvider

ROOT = Path(__file__).resolve().parents[2]
PROFILE, _ = load_profile(ROOT, "configs/analysis/vlm_scripted_s0_v1.yaml")
QUALITY, _ = load_quality(ROOT, "configs/analysis/quality_sim_v1.yaml")


def logical(damaged: bool = False, nonce: int = 1) -> np.ndarray:
    return pixels(frame("red", damaged=damaged, nonce=nonce), 160, 120)


def test_quality_layer_refuses_small_dark_and_blurred_frames():
    assert check_quality(logical(), QUALITY)[0] is None
    assert check_quality(logical(damaged=True), QUALITY)[0] is None
    assert check_quality(logical()[:60, :80], QUALITY)[0] == "quality.resolution"
    assert check_quality(np.zeros((120, 160, 3), dtype=np.uint8), QUALITY)[0] == "quality.exposure"
    flat = np.full((120, 160, 3), 128, dtype=np.uint8)
    reason, measures = check_quality(flat, QUALITY)
    assert reason == "quality.blurry" and measures["sharpness"] == 0.0


def test_encoding_is_deterministic_and_images_are_never_tools():
    first, digest = encode(logical(), PROFILE)
    again, digest_again = encode(logical(), PROFILE)
    assert (first, digest) == (again, digest_again) and first.startswith("data:image/png;base64,")
    messages = model_messages(PROFILE, [first], first, "Red equipment marker")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "tools" not in json.dumps(messages) and "Red equipment marker" in messages[1]["content"][-1]["text"]


def test_answers_map_deterministically_and_extra_fields_are_malformed():
    answer = {"image_usable": True, "target_matches_reference": True, "anomaly_score": 0.7, "defect_type": "damage",
              "description": "band", "unusable_reason": "none"}
    parsed = parse_answer("```json\n" + json.dumps(answer) + "\n```", PROFILE)
    assert decide(parsed, PROFILE, 0.5)["verdict"] == "suspected"
    assert decide(parsed, PROFILE, 0.8)["verdict"] == "normal"
    none_type = parse_answer(json.dumps({**answer, "defect_type": "none"}), PROFILE)
    assert decide(none_type, PROFILE, 0.5)["defect_type"] == PROFILE.generic_type
    assert decide(parse_answer(json.dumps({**answer, "image_usable": False, "unusable_reason": "blurred"}), PROFILE),
                  PROFILE, 0.5)["reasons"][0] == "analysis.undeterminable"
    assert decide(parse_answer(json.dumps({**answer, "target_matches_reference": False}), PROFILE), PROFILE,
                  0.5)["reasons"] == ("target.mismatch",)
    assert parse_answer(json.dumps({**answer, "approve": True}), PROFILE) is None
    assert parse_answer(json.dumps({**answer, "defect_type": "rust"}), PROFILE) is None
    assert parse_answer("the marker is fine", PROFILE) is None


def run(provider, *, model="scripted-vision", damaged=True):
    return asyncio.run(analyze_model(profile=PROFILE, threshold=0.5, provider=provider, provider_id="scripted",
                                     model=model, references=[logical()], current=logical(damaged=damaged, nonce=9),
                                     asset="Red marker"))


def test_model_analysis_and_every_failure_is_an_explicit_refusal():
    provider = ScriptedVisionProvider()
    suspected = run(provider)
    assert (suspected.verdict, suspected.source, suspected.defect_type) == ("suspected", "scripted", "damage")
    assert run(ScriptedVisionProvider(), damaged=False).verdict == "normal"
    assert run(None).reasons == ("model.unavailable",) and run(None).source == "not_run"
    assert run(provider, model="another-model").reasons == ("model.profile_mismatch",)
    for mode, reason, calls in (("malformed", "model.malformed", 2), ("inject", "model.malformed", 2),
                                ("refusal", "model.refusal", 1), ("error", "model.error", 2)):
        double = ScriptedVisionProvider()
        double.mode = mode
        result = run(double)
        assert (result.verdict, result.reasons, result.model_calls) == ("refused", (reason,), calls), mode


def test_model_timeout_is_refused(monkeypatch):
    fast = PROFILE.model_copy(update={"timeout_s": 0.05, "attempts": 1})
    double = ScriptedVisionProvider()
    double.mode = "timeout"
    result = asyncio.run(analyze_model(profile=fast, threshold=0.5, provider=double, provider_id="scripted",
                                       model="scripted-vision", references=[logical()], current=logical(),
                                       asset="Red marker"))
    assert result.reasons == ("model.timeout",) and result.verdict == "refused"


def test_recorded_answers_replay_to_the_same_verdict():
    recorder = RecordingProvider(ScriptedVisionProvider())
    live = run(recorder)
    recording = recorder.recording(source="recorded", provider_id="scripted", model="scripted-vision")
    replayed = run(ReplayProvider(recording))
    assert (replayed.verdict, replayed.score) == (live.verdict, live.score)
    assert replayed.source == "recorded_model"
