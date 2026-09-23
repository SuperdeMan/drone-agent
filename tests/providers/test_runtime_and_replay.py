"""Provider selection from the environment and strict record/replay (D029).

从环境选择 provider 与严格录制回放（D029）。
"""

from __future__ import annotations

import json

import pytest

from drone_agent.providers import (
    GuardedProvider,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderUnavailable,
    Recording,
    RecordingProvider,
    ReplayMismatch,
    ReplayProvider,
    ScriptedProvider,
    build_provider,
    provider_config,
    role_provider_id,
)

ENV_KEYS = (
    "LLM_PROVIDER", "VISION_PROVIDER", "MINIMAX_API_KEY", "MINIMAX_API_KEY_FILE", "MINIMAX_BASE_URL",
    "MINIMAX_LLM_MODEL", "DEEPSEEK_API_KEY", "DASHSCOPE_LLM_KEY", "DASHSCOPE_ASR_KEY", "LLM_EMBED_API_KEY",
    "LLM_API_KEY", "VISION_MODEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_planner_is_minimax_m3_following_car_agent():
    assert role_provider_id("planner") == "minimax"
    config = provider_config("minimax")
    assert config.model == "MiniMax-M3"
    assert config.base_url == "https://api.minimaxi.com/v1/chat/completions"
    assert (config.auth_style, config.token_param, config.thinking_style) == ("bearer", "max_completion_tokens", "mimo")
    assert config.endpoint_host == "api.minimaxi.com" and not config.available


def test_missing_key_raises_instead_of_pretending_with_a_mock():
    with pytest.raises(ProviderUnavailable, match="MINIMAX_API_KEY"):
        build_provider("planner")


def test_key_from_environment_or_secret_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-env")
    provider, config = build_provider("planner")
    assert isinstance(provider, GuardedProvider) and isinstance(provider.inner, OpenAICompatibleProvider)
    assert provider.inner.api_key == "sk-env" and config.available
    monkeypatch.delenv("MINIMAX_API_KEY")
    secret_file = tmp_path / "minimax_api_key"
    secret_file.write_text("sk-file\n", encoding="utf-8")
    monkeypatch.setenv("MINIMAX_API_KEY_FILE", str(secret_file))
    provider, _ = build_provider("planner", guarded=False)
    assert provider.api_key == "sk-file"
    assert "sk-file" not in repr(provider_config("minimax"))


def test_vendor_and_model_follow_the_environment(monkeypatch):
    monkeypatch.setenv("MINIMAX_LLM_MODEL", "MiniMax-M3-custom")
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://example.test/v1/chat/completions")
    assert provider_config("minimax").model == "MiniMax-M3-custom"
    assert provider_config("minimax").endpoint_host == "example.test"
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-d")
    _, config = build_provider("planner")
    assert config.provider_id == "deepseek" and config.token_param == "max_tokens"
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    provider, config = build_provider("planner")
    assert isinstance(provider, MockProvider) and config.model == "mock"


def test_vision_role_uses_the_separate_vl_tier(monkeypatch):
    assert role_provider_id("vision") == "qwen-vl"
    with pytest.raises(ProviderUnavailable):
        build_provider("vision")
    monkeypatch.setenv("DASHSCOPE_ASR_KEY", "sk-dash")
    _, config = build_provider("vision")
    assert config.vision and config.model == "qwen3-vl-plus"
    monkeypatch.setenv("VISION_PROVIDER", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-m")
    with pytest.raises(ProviderUnavailable, match="not a vision tier"):
        build_provider("vision")


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderUnavailable):
        provider_config("nonexistent")


CALL = dict(tools=[{"type": "function", "function": {"name": "f"}}], tool_choice={"type": "function"})


async def record_one(tmp_path, answers, source="scripted"):
    recorder = RecordingProvider(ScriptedProvider(answers, model="MiniMax-M3"))
    result = await recorder.complete_tools([{"role": "user", "content": "inspect asset_red"}], "MiniMax-M3", 0.1, 512,
                                           **CALL)
    path = tmp_path / "fixture.json"
    recorder.recording(source=source, provider_id="minimax", model="MiniMax-M3", prompt_version="planner-v1",
                       prompt_sha256="0" * 64).save(path)
    return result, Recording.load(path)


async def test_replay_returns_exactly_what_was_recorded(tmp_path):
    answer = {"content": "", "finish": "stop", "usage": (12, 3),
              "tool_calls": [{"id": "c1", "name": "f", "arguments": {"x": 1}}]}
    recorded, fixture = await record_one(tmp_path, [answer])
    assert fixture.source == "scripted" and fixture.exchanges[0].request["kind"] == "complete_tools"
    replay = ReplayProvider(fixture)
    replayed = await replay.complete_tools([{"role": "user", "content": "inspect asset_red"}], "MiniMax-M3", 0.1, 512,
                                           **CALL)
    assert replayed == recorded and replay.exhausted


@pytest.mark.parametrize("change", ["message", "model", "tools", "temperature"])
async def test_any_change_to_the_request_is_a_replay_mismatch(tmp_path, change):
    _, fixture = await record_one(tmp_path, [{"content": "x"}])
    messages, model, temperature, call = [{"role": "user", "content": "inspect asset_red"}], "MiniMax-M3", 0.1, dict(CALL)
    if change == "message":
        messages = [{"role": "user", "content": "inspect asset_blue"}]
    elif change == "model":
        model = "other-model"
    elif change == "tools":
        call["tools"] = []
    else:
        temperature = 0.2
    with pytest.raises(ReplayMismatch):
        await ReplayProvider(fixture).complete_tools(messages, model, temperature, 512, **call)


async def test_exhausted_recording_is_a_mismatch(tmp_path):
    _, fixture = await record_one(tmp_path, [{"content": "x"}])
    replay = ReplayProvider(fixture)
    await replay.complete_tools([{"role": "user", "content": "inspect asset_red"}], "MiniMax-M3", 0.1, 512, **CALL)
    with pytest.raises(ReplayMismatch, match="exhausted"):
        await replay.complete_tools([{"role": "user", "content": "inspect asset_red"}], "MiniMax-M3", 0.1, 512, **CALL)


async def test_recordings_never_contain_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-must-not-leak")
    _, fixture = await record_one(tmp_path, [{"content": "x"}], source="recorded")
    raw = (tmp_path / "fixture.json").read_text(encoding="utf-8")
    assert "sk-must-not-leak" not in raw
    assert json.loads(raw)["format"] == "drone.provider.recording/v1"
    assert fixture.source == "recorded"
