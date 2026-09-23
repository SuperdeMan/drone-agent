# Ported from embodied-agent src/embodied/providers/ratelimit.py @ e20fe33 (from car-agent
# llm-gateway/ratelimit.py @ f0b08f8), changes: bilingual docstrings only; logic unchanged.
"""Token-bucket rate limiting against per-key and global overload.

令牌桶限流。防止单调用方或全局过载。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("drone_agent.providers.ratelimit")


class TokenBucket:
    """A token bucket: `rate` tokens per second, bursts up to `capacity`.

    令牌桶限流器：每秒补充 `rate` 个令牌，允许突发到 `capacity`。
    """

    def __init__(self, rate: float = 10, capacity: float = 20):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def allow(self, cost: int = 1) -> bool:
        """Consume `cost` tokens if available. / 若令牌足够则消耗 `cost` 个并返回真。"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        logger.warning("rate limited / 已限流: tokens=%.1f cost=%d", self.tokens, cost)
        return False


class RateLimiter:
    """A global bucket plus one bucket per key. / 全局 + 每 key 限流。"""

    def __init__(self, global_rate: float = 20, global_capacity: float = 50,
                 per_key_rate: float = 5, per_key_capacity: float = 10):
        self.global_bucket = TokenBucket(global_rate, global_capacity)
        self.per_key_rate = per_key_rate
        self.per_key_capacity = per_key_capacity
        self._buckets: dict[str, TokenBucket] = {}

    def _get_bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(self.per_key_rate, self.per_key_capacity)
        return self._buckets[key]

    def allow(self, key: str = "default", cost: int = 1) -> bool:
        """Both the global and the key bucket must allow. / 全局桶与该 key 的桶都放行才放行。"""
        if not self.global_bucket.allow(cost):
            return False
        return self._get_bucket(key).allow(cost)
