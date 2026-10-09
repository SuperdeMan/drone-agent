# Ported from embodied-agent src/embodied/providers/cache.py @ e20fe33 (from car-agent
# llm-gateway/cache.py @ f0b08f8), changes: bilingual docstrings; the key hash is the full SHA-256; the key covers
# every generation option that can change the answer (D075).
"""LRU response cache keyed by the request, for plain completions only.

The key is the messages, the model and every generation option that can change the answer (temperature, token
limit, thinking switch, reasoning effort): the same messages under another output budget are another request.
Planning calls are never cached: they go through complete_tools, whose tool calls this cache cannot represent,
and a replayed plan must come from an explicit recording, not from a cache hit.

按请求哈希的 LRU 响应缓存，只用于纯文本补全。键为消息、模型以及所有可能改变回答的生成选项（温度、token 上限、思考开关、
思考深度）：同样的消息换一个输出预算就是另一个请求。规划调用从不缓存：它走 complete_tools，本缓存无法表示其工具调用；
回放规划必须来自显式录制，而不是缓存命中。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict


class LLMCache:
    """LRU cache: key = request hash, value = (content, model_used, timestamp).

    LRU 缓存：key = 请求哈希，value = (content, model_used, 时间戳)。
    """

    def __init__(self, max_size: int = 256, ttl_seconds: int = 300):
        self._cache: OrderedDict[str, tuple] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _hash(messages: list[dict], model: str, options: dict) -> str:
        key_data = json.dumps({"m": messages, "model": model, "options": options}, sort_keys=True,
                              ensure_ascii=False)
        return hashlib.sha256(key_data.encode()).hexdigest()

    def get(self, messages: list[dict], model: str, options: dict) -> tuple | None:
        """A fresh hit as a complete() tuple, else None. / 未过期的命中以 complete() 四元组返回，否则 None。"""
        h = self._hash(messages, model, options)
        entry = self._cache.get(h)
        if entry is None:
            self._misses += 1
            return None
        content, model_used, ts = entry
        if time.time() - ts > self._ttl:
            del self._cache[h]
            self._misses += 1
            return None
        self._hits += 1
        self._cache.move_to_end(h)
        return content, model_used, "stop", (0, 0)

    def put(self, messages: list[dict], model: str, options: dict, content: str, model_used: str) -> None:
        """Store a completed response. / 存入一条已完成的响应。"""
        h = self._hash(messages, model, options)
        self._cache[h] = (content, model_used, time.time())
        self._cache.move_to_end(h)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    @property
    def stats(self) -> dict:
        """Size and hit statistics. / 容量与命中统计。"""
        total = self._hits + self._misses
        return {"size": len(self._cache), "hits": self._hits, "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0}
