# Ported from embodied-agent src/embodied/providers/guarded.py @ cb933ab, changes: bilingual comments;
# health is recorded per call (ok / timeout / rate_limited / refusal / error) against a provider id.
"""GuardedProvider: rate limiting, health accounting and a plain-completion cache around a provider.

Wrap real providers only. Replay and scripted providers are deterministic and stateful; caching or
throttling them would change what a test replays.

GuardedProvider：为 provider 加上限流、健康记账与纯文本补全缓存。只包装真实 provider；回放与
脚本 provider 是确定的、有状态的，缓存或限流会改变测试回放的内容。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any

from drone_agent.providers.cache import LLMCache
from drone_agent.providers.health import health_tracker
from drone_agent.providers.ratelimit import RateLimiter


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


class RateLimited(RuntimeError):
    """Admission to the provider was refused by the local rate limiter. / 本地限流拒绝了调用。"""


class GuardedProvider:
    """Token-bucket admission on every call and a TTL cache on complete() only.

    The cache stores (content, model_used) and cannot represent a complete_tools result, so planning
    calls are rate-limited and health-recorded but never cached.

    每次调用都经令牌桶准入；只对 complete() 做 TTL 缓存。缓存只存 (content, model_used)，无法表示
    complete_tools 的结果，因此规划调用只限流与记健康，从不缓存。
    """

    def __init__(self, inner: Any, *, provider_id: str, cache: LLMCache | None = None,
                 limiter: RateLimiter | None = None):
        self.inner = inner
        self.provider_id = provider_id
        self.last_source, self.last_cache_digest = "not_called", ""
        enabled = os.getenv("LLM_CACHE", "on").strip().lower() != "off"
        ttl = int(_env_float("LLM_CACHE_TTL_S", 120))
        self._cache = cache if cache is not None else (LLMCache(ttl_seconds=ttl) if enabled else None)
        self._limiter = limiter if limiter is not None else RateLimiter(
            global_rate=_env_float("LLM_RATE", 5), global_capacity=_env_float("LLM_BURST", 10))
        self._wait_cap_s = _env_float("LLM_RATE_WAIT_CAP_S", 5.0)

    async def _admit(self, key: str) -> None:
        waited = 0.0
        while not self._limiter.allow(key):
            if waited >= self._wait_cap_s:
                health_tracker.record(self.provider_id, False, kind="rate_limited", error="local rate limit")
                raise RateLimited(f"rate limit exceeded for {key}")
            await asyncio.sleep(0.2)
            waited += 0.2

    def _record(self, started: float, finish: str | None, error: Exception | None = None) -> None:
        latency = round((time.monotonic() - started) * 1000, 1)
        if error is not None:
            kind = "timeout" if "Timeout" in type(error).__name__ else (
                "rate_limited" if getattr(error, "status_code", 0) == 429 else "")
            health_tracker.record(self.provider_id, False, kind=kind, latency_ms=latency, error=str(error))
        elif finish == "refusal":
            health_tracker.record(self.provider_id, False, kind="refusal", latency_ms=latency, error="refusal")
        else:
            health_tracker.record(self.provider_id, True, latency_ms=latency)

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        self.last_source, self.last_cache_digest = "not_called", ""
        if self._cache is not None:
            hit = self._cache.get(messages, model, temperature, thinking)
            if hit is not None:
                # A cached response is reuse, not a fresh model invocation. / 缓存响应是复用，不是新的模型实调。
                self.last_source = "cache"
                self.last_cache_digest = hashlib.sha256(json.dumps(
                    {"messages": messages, "model": model, "response": hit}, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                return hit
        await self._admit("complete")
        started = time.monotonic()
        self.last_source = "provider"
        try:
            content, model_used, finish, usage = await self.inner.complete(
                messages, model, temperature, max_tokens, thinking=thinking, timeout_s=timeout_s)
        except Exception as error:
            self._record(started, None, error)
            raise
        self._record(started, finish)
        self.last_source = "provider"
        if self._cache is not None and finish == "stop":
            self._cache.put(messages, model, temperature, content, model_used, thinking)
        return content, model_used, finish, usage

    async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                             thinking=None, timeout_s=None):
        self.last_source, self.last_cache_digest = "not_called", ""
        await self._admit("complete_tools")
        started = time.monotonic()
        self.last_source = "provider"
        try:
            result = await self.inner.complete_tools(
                messages, model, temperature, max_tokens, tools=tools, tool_choice=tool_choice,
                thinking=thinking, timeout_s=timeout_s)
        except Exception as error:
            self._record(started, None, error)
            raise
        self._record(started, result[2])
        self.last_source = "provider"
        return result

    def __getattr__(self, name: str) -> Any:  # stream and anything else pass through / 其余直通
        return getattr(self.inner, name)
