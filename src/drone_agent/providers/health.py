# Ported from embodied-agent src/embodied/providers/health.py @ e20fe33 (from car-agent
# llm-gateway/health.py @ f0b08f8), changes: bilingual docstrings only; logic unchanged.
"""Passive provider health: calls record their own outcome, there is no background probing.

"Available" means a key is configured; "healthy" means recent calls actually answered. A rolling
in-process window is enough because health is recent history, not state worth persisting.

Provider 被动健康统计：调用路径顺手记账，刻意**无后台周期探活**（不给付费 API 烧闲置 token）。
「可用」= 配了 key；「健康」= 最近真的答得上来。进程内滚动窗口，重启清零。
"""

from __future__ import annotations

import time
from collections import deque

_WINDOW = 50


class ProviderHealth:
    """Rolling success/failure window per provider id. / 每个 provider 的滚动成功 / 失败窗口。"""

    def __init__(self, window: int = _WINDOW):
        self._window = window
        self._results: dict[str, deque] = {}
        self._last_error: dict[str, str] = {}
        self._last_ok_at: dict[str, float] = {}
        self._ewma_ms: dict[str, float] = {}

    def record(self, pid: str, ok: bool, *, kind: str = "", latency_ms: float = 0.0, error: str = "") -> None:
        """kind: "" | "timeout" | "rate_limited" | "refusal"; ok=True updates the latency EWMA.

        kind：失败分类；ok=True 时更新 EWMA 时延。
        """
        if not pid:
            return
        dq = self._results.setdefault(pid, deque(maxlen=self._window))
        dq.append((ok, kind))
        if ok:
            self._last_ok_at[pid] = time.time()
            prev = self._ewma_ms.get(pid)
            self._ewma_ms[pid] = latency_ms if prev is None else prev * 0.8 + latency_ms * 0.2
        elif error:
            self._last_error[pid] = error[:200]

    def snapshot(self) -> dict[str, dict]:
        """Counts, failure kinds, last error and latency per provider. / 每个 provider 的计数、失败分类与时延。"""
        out: dict[str, dict] = {}
        for pid, dq in self._results.items():
            n = len(dq)
            ok = sum(1 for o, _ in dq if o)
            out[pid] = {
                "window": n,
                "ok": ok,
                "err": n - ok,
                "timeout": sum(1 for o, k in dq if not o and k == "timeout"),
                "rate_limited": sum(1 for o, k in dq if not o and k == "rate_limited"),
                "refusal": sum(1 for o, k in dq if not o and k == "refusal"),
                "last_error": self._last_error.get(pid, ""),
                "last_ok_at": int(self._last_ok_at.get(pid, 0)),
                "ewma_latency_ms": round(self._ewma_ms.get(pid, 0.0), 1),
            }
        return out


health_tracker = ProviderHealth()
