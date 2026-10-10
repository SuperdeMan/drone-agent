"""A patient live provider for recordings (WP-P6-03, D079): paced calls and bounded waits on HTTP 429.

The model plan behind the provider throttles bursts (HTTP 429, Token Plan rate limit). A 429 carries no model answer,
so asking again with the identical request is not a second answer to the same case: the wrapper waits (Retry-After
when given, otherwise exponential backoff) and retries the same call until a total wait budget is spent, then raises
the last error, which the caller reports as a case without an answer. Every call also starts at least `pace_s` after
the previous one. After two consecutive calls spent their whole budget, the plan's longer window is presumed exhausted
and every later call fails at once, so the run ends and the cases left can be resumed later. Only for live recording
runs: replays and doubles never sleep, and nothing here changes a request.

供录制使用的耐心实调 provider（WP-P6-03，D079）：调用限速，HTTP 429 时有上限地等待。

provider 背后的模型套餐会节流突发调用（HTTP 429，Token Plan 速率限制）。429 不含任何模型回答，因此以完全相同的请求再次
询问并不是同一用例的第二个回答：包装器等待（有 Retry-After 时按其等待，否则指数退避）后重试同一调用，直到总等待预算用尽，
再抛出最后的错误，由调用方报告为没有回答的用例。每次调用的开始时间也至少比上一次晚 `pace_s`。连续两次调用用尽全部预算后，视为套餐的长窗口额度已耗尽，此后的调用立即失败，
运行就此结束，剩下的用例之后可以续跑。只用于实调录制运行：回放与替身从不等待，这里也不改变任何请求。
"""

from __future__ import annotations

import asyncio
import time


def throttled(error: BaseException) -> bool:
    return getattr(error, "status_code", None) == 429


class Patient:
    """Wraps a live provider; keeps counts of throttled calls and waits for the receipt.

    包装实调 provider；为回执记录被节流的调用次数与等待时长。
    """

    def __init__(self, inner, *, pace_s: float = 6.0, budget_s: float = 900.0, base_s: float = 10.0,
                 cap_s: float = 120.0, sleep=asyncio.sleep, clock=time.monotonic):
        self.inner, self.pace_s, self.budget_s, self.base_s, self.cap_s = inner, pace_s, budget_s, base_s, cap_s
        self.sleep, self.clock = sleep, clock
        self.lock = asyncio.Lock()
        self.last_start: float | None = None
        self.throttled_calls, self.waited_s, self.gave_up = 0, 0.0, 0
        self.consecutive_give_ups, self.refused_fast, self.last_error = 0, 0, None

    async def _paced(self) -> None:
        async with self.lock:
            if self.last_start is not None:
                gap = self.pace_s - (self.clock() - self.last_start)
                if gap > 0:
                    await self.sleep(gap)
            self.last_start = self.clock()

    async def _call(self, method: str, *args, **kwargs):
        if self.consecutive_give_ups >= 2:
            self.refused_fast += 1
            raise self.last_error
        waited, attempt = 0.0, 0
        while True:
            await self._paced()
            try:
                result = await getattr(self.inner, method)(*args, **kwargs)
                self.consecutive_give_ups = 0
                return result
            except Exception as error:
                if not throttled(error):
                    raise
                self.throttled_calls += 1
                delay = getattr(error, "retry_after", None) or min(self.cap_s, self.base_s * 2 ** attempt)
                if waited + delay > self.budget_s:
                    self.gave_up += 1
                    self.consecutive_give_ups += 1
                    self.last_error = error
                    raise
                await self.sleep(delay)
                waited += delay
                self.waited_s += delay
                attempt += 1

    async def complete(self, *args, **kwargs):
        return await self._call("complete", *args, **kwargs)

    async def complete_tools(self, *args, **kwargs):
        return await self._call("complete_tools", *args, **kwargs)

    def __getattr__(self, name: str):
        # Everything else is the live provider's own (identity, source bookkeeping). / 其余属性都属于实调 provider 自身。
        return getattr(self.inner, name)

    def counts(self) -> dict:
        return {"throttled_calls": self.throttled_calls, "waited_s": round(self.waited_s, 1), "gave_up": self.gave_up,
                "refused_fast": self.refused_fast, "pace_s": self.pace_s, "budget_s": self.budget_s}
