# Ported from embodied-agent tests/providers/{test_think_strip,test_toolcall,test_thinking,test_cache_ratelimit,
# test_guarded}.py @ e20fe33 / cb933ab (from car-agent llm-gateway tests @ f0b08f8), changes: imports adapted to
# drone_agent.providers; Anthropic and embedding cases dropped with the code (D029); MiniMax content-filter,
# base_resp, missing-choices and health cases added; comments made bilingual.
"""OpenAI-compatible provider behaviour, rate limiting, caching and health accounting (no network).

OpenAI 兼容 provider 行为、限流、缓存与健康记账（不打网络）。
"""

from __future__ import annotations

import time

import httpx
import pytest

from drone_agent.providers import (
    GuardedProvider,
    LLMCache,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderHTTPError,
    RateLimited,
    RateLimiter,
    ThinkStreamStripper,
    TokenBucket,
    health_tracker,
    normalize_tool_calls,
    strip_think_block,
)

MINIMAX = "https://api.minimaxi.com/v1/chat/completions"


def minimax(**overrides):
    values = dict(base_url=MINIMAX, auth_style="bearer", token_param="max_completion_tokens", thinking_style="mimo")
    values.update(overrides)
    return OpenAICompatibleProvider("sk-test", **values)


# ── <think> stripping (only MiniMax leaks it with thinking on) / <think> 剥离（仅 MiniMax 开思考泄漏） ──


def test_strip_think_block_removes_leading_block_only():
    assert strip_think_block("<think>plan it</think>\n\n{\"a\": 1}") == '{"a": 1}'
    assert strip_think_block("  \n<think>x</think>answer") == "answer"
    mid = "a literal <think> tag in the middle stays"
    assert strip_think_block(mid) == mid
    assert strip_think_block("") == ""


def test_unclosed_think_block_yields_no_answer():
    # Truncated inside the thinking block: there is no answer, never half a thought.
    # 截断在思考里：没有答案，绝不把半截思考当答案。
    assert strip_think_block("<think>not finished") == ""


def _feed_all(chunks):
    stripper = ThinkStreamStripper()
    out = [stripper.feed(c) for c in chunks]
    out.append(stripper.flush())
    return "".join(x for x in out if x)


def test_stream_stripper_across_chunk_boundaries():
    assert _feed_all(["<th", "ink>why", " blue</thi", "nk>\n\nbecause", " scattering"]) == "because scattering"
    assert _feed_all(["<3 ", "nice"]) == "<3 nice"
    assert _feed_all(["<th"]) == "<th"
    assert _feed_all(["<think>never closed"]) == ""


# ── tool-call normalisation / 工具调用归一化 ──


def test_normalize_tool_calls_shapes():
    calls = normalize_tool_calls([
        {"id": "c1", "type": "function", "function": {"name": "submit", "arguments": '{"ok": true}'}},
        {"id": "c2", "function": {"name": "submit", "arguments": '{"steps": [tru'}},  # malformed / 畸形
        {"id": "c3", "function": {"name": "f", "arguments": {"a": 1}}},  # object tolerated / 宽容接收 object
        {"id": "c4", "function": {"name": "f", "arguments": "[1,2]"}},  # not an object / 非 object
        {"id": "c5", "function": {"arguments": "{}"}},  # no name / 无 name
        "not-a-dict",
    ])
    assert calls == [
        {"id": "c1", "name": "submit", "arguments": {"ok": True}},
        {"id": "c3", "name": "f", "arguments": {"a": 1}},
    ]
    assert normalize_tool_calls(None) == []
    assert normalize_tool_calls([{"id": "c", "function": {"name": "f"}}]) == [{"id": "c", "name": "f", "arguments": {}}]


async def test_base_complete_tools_falls_back_to_plain_complete():
    content, used, finish, usage, calls = await MockProvider().complete_tools(
        [{"role": "user", "content": "hello"}], "m", 0.1, 64, tools=[{"type": "function"}], tool_choice="auto")
    assert calls == [] and "hello" in content and used == "mock"


# ── request body and response handling / 请求体与响应处理 ──


def test_minimax_body_disables_thinking_and_uses_completion_token_field():
    body = minimax()._build_body([{"role": "user", "content": "x"}], "MiniMax-M3", 0.1, 4096, None, stream=False)
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_completion_tokens"] == 4096 and "max_tokens" not in body


def test_thinking_on_omits_disabled_key_and_raises_token_floor():
    body = minimax()._build_body([{"role": "user", "content": "x"}], "m", 0.1, 400, True, stream=False)
    assert "thinking" not in body and body["max_completion_tokens"] >= 2048
    qwen = minimax(thinking_style="qwen", token_param="max_tokens")._build_body([], "m", 0.1, 400, None, False)
    assert qwen["enable_thinking"] is False and qwen["max_tokens"] == 400


def test_bearer_and_api_key_headers():
    assert minimax()._headers()["Authorization"] == "Bearer sk-test"
    assert minimax(auth_style="api-key")._headers()["api-key"] == "sk-test"


def test_an_endpoint_is_mandatory():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("k", base_url="")


TOOLS = [{"type": "function", "function": {"name": "submit_mission_draft", "parameters": {"type": "object"}}}]
NAMED = {"type": "function", "function": {"name": "submit_mission_draft"}}


def fake_post(response: dict, seen: dict | None = None):
    async def post(body, timeout_s):
        if seen is not None:
            seen.update(body)
        return response

    return post


async def test_complete_tools_injects_tools_and_parses_calls(monkeypatch):
    provider, seen = minimax(), {}
    monkeypatch.setattr(provider, "_post_chat", fake_post({
        "choices": [{"finish_reason": "stop", "message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "submit_mission_draft", "arguments": '{"a": 1}'}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, seen))
    content, used, finish, usage, calls = await provider.complete_tools(
        [{"role": "user", "content": "hi"}], "MiniMax-M3", 0.1, 256, tools=TOOLS, tool_choice=NAMED)
    assert seen["tools"] == TOOLS and seen["tool_choice"] == NAMED
    # finish_reason "stop" with tool calls is passed through; tool use is judged by tool_calls alone.
    # 带工具调用的 "stop" 原样透传；是否工具调用只看 tool_calls。
    assert finish == "stop" and calls[0]["arguments"] == {"a": 1}
    assert usage == (10, 5) and content == ""


@pytest.mark.parametrize(
    "response",
    [
        {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}], "usage": {}},
        {"base_resp": {"status_code": 1026, "status_msg": "input new_sensitive"}, "choices": []},
        {"base_resp": {"status_code": 1027, "status_msg": "output new_sensitive"}},
    ],
)
async def test_content_filtering_is_a_refusal_not_an_answer(monkeypatch, response):
    provider = minimax()
    monkeypatch.setattr(provider, "_post_chat", fake_post(response))
    content, _, finish, _, calls = await provider.complete_tools([], "m", 0.1, 64, tools=TOOLS, tool_choice=NAMED)
    assert finish == "refusal" and calls == [] and content == ""
    _, _, plain_finish, _ = await provider.complete([], "m", 0.1, 64)
    assert plain_finish == "refusal"


@pytest.mark.parametrize(
    "response,fragment",
    [
        ({"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}}, "base_resp 1008"),
        ({"usage": {}}, "without choices"),
        ([], "non-object"),
    ],
)
async def test_business_errors_and_missing_choices_fail_loudly(monkeypatch, response, fragment):
    provider = minimax()
    monkeypatch.setattr(provider, "_post_chat", fake_post(response))
    with pytest.raises(ProviderHTTPError, match=fragment):
        await provider.complete_tools([], "m", 0.1, 64, tools=TOOLS, tool_choice=NAMED)


async def test_http_error_carries_status_body_and_retry_after():
    def handler(request):
        return httpx.Response(429, text="rate limited by upstream", headers={"retry-after": "7"})

    provider = minimax()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderHTTPError) as caught:
        await provider.complete([{"role": "user", "content": "x"}], "m", 0.1, 64)
    assert caught.value.status_code == 429 and caught.value.retry_after == 7.0
    assert "rate limited" in str(caught.value) and "sk-test" not in str(caught.value)
    await provider.aclose()


async def test_request_is_sent_to_the_configured_endpoint_with_bearer_auth():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>ok"}}], "usage": {}})

    provider = minimax()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    content, *_ = await provider.complete([{"role": "user", "content": "x"}], "MiniMax-M3", 0.1, 64)
    assert content == "ok" and seen == {"url": MINIMAX, "auth": "Bearer sk-test"}
    await provider.aclose()


# ── cache, rate limit, guard and health / 缓存、限流、保护与健康 ──


def test_cache_hit_miss_ttl_and_eviction():
    cache = LLMCache(max_size=2)
    msgs = [{"role": "user", "content": "hello"}]
    cache.put(msgs, "m", 0.1, "world", "m")
    assert cache.get(msgs, "m", 0.1)[0] == "world"
    assert cache.get([{"role": "user", "content": "other"}], "m", 0.1) is None
    for i in range(3):
        cache.put([{"role": "user", "content": str(i)}], "m", 0.1, f"r{i}", "m")
    assert cache.stats["size"] == 2
    expiring = LLMCache(ttl_seconds=0)
    expiring.put(msgs, "m", 0.1, "y", "m")
    time.sleep(0.01)
    assert expiring.get(msgs, "m", 0.1) is None


def test_token_bucket_and_rate_limiter():
    bucket = TokenBucket(rate=100, capacity=2)
    assert bucket.allow() and bucket.allow() and not bucket.allow()
    limiter = RateLimiter(global_rate=1, global_capacity=1, per_key_rate=1, per_key_capacity=1)
    assert limiter.allow("a") and not limiter.allow("a")


class CountingProvider:
    def __init__(self, finish="stop", error: Exception | None = None):
        self.completes = self.tool_calls = 0
        self.finish, self.error = finish, error

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        self.completes += 1
        if self.error:
            raise self.error
        return f"r{self.completes}", "m", self.finish, (1, 1)

    async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                             thinking=None, timeout_s=None):
        self.tool_calls += 1
        if self.error:
            raise self.error
        return "", "m", self.finish, (1, 1), [{"id": "1", "name": "t", "arguments": {}}]


MSGS = [{"role": "user", "content": "hi"}]


def guarded(inner, pid="test", **kwargs):
    return GuardedProvider(inner, provider_id=pid, limiter=RateLimiter(global_rate=100, global_capacity=100), **kwargs)


async def test_guard_caches_plain_completions_but_never_planning_calls():
    inner = CountingProvider()
    guard = guarded(inner, cache=LLMCache(ttl_seconds=60))
    assert (await guard.complete(MSGS, "m", 0.1, 64))[0] == (await guard.complete(MSGS, "m", 0.1, 64))[0]
    assert inner.completes == 1
    first = await guard.complete_tools(MSGS, "m", 0.1, 64, tools=[{"a": 1}])
    second = await guard.complete_tools(MSGS, "m", 0.1, 64, tools=[{"a": 1}])
    assert inner.tool_calls == 2 and first[4] and second[4]


async def test_guard_rate_limit_raises_after_wait_cap(monkeypatch):
    monkeypatch.setenv("LLM_RATE_WAIT_CAP_S", "0.3")
    guard = GuardedProvider(CountingProvider(), provider_id="rl", cache=None,
                            limiter=RateLimiter(global_rate=0.001, global_capacity=1))
    await guard.complete(MSGS, "m", 0.1, 64)
    with pytest.raises(RateLimited):
        await guard.complete([{"role": "user", "content": "again"}], "m", 0.1, 64)


async def test_guard_records_health_for_success_refusal_and_timeout():
    await guarded(CountingProvider(), pid="health-ok").complete_tools(MSGS, "m", 0.1, 64, tools=[{}])
    await guarded(CountingProvider(finish="refusal"), pid="health-refusal").complete_tools(MSGS, "m", 0.1, 64, tools=[{}])
    with pytest.raises(httpx.ReadTimeout):
        await guarded(CountingProvider(error=httpx.ReadTimeout("slow")), pid="health-timeout").complete_tools(
            MSGS, "m", 0.1, 64, tools=[{}])
    snapshot = health_tracker.snapshot()
    assert snapshot["health-ok"]["ok"] == 1
    assert snapshot["health-refusal"]["refusal"] == 1
    assert snapshot["health-timeout"]["timeout"] == 1


def test_guard_passes_other_attributes_through():
    inner = CountingProvider()
    inner.custom_attr = 42
    assert guarded(inner).custom_attr == 42
