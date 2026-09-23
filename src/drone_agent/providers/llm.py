# Ported from embodied-agent src/embodied/providers/llm.py @ e20fe33 (itself ported from car-agent
# llm-gateway/providers.py @ f0b08f8), changes: AnthropicProvider, embeddings and the xiaomimimo default
# endpoint removed (D029: MiniMax-M3 is the default, selected in runtime.py); content-filter endings and
# MiniMax `base_resp` errors normalized (refusal -> finish "refusal", other errors -> ProviderHTTPError);
# responses without choices fail loudly instead of raising KeyError; comments made bilingual.
"""LLM provider abstraction and the OpenAI-compatible implementation.

Changing vendors is an environment change, not a code change: every supported vendor (MiniMax by
default, also MiMo, DeepSeek and Qwen) speaks OpenAI-compatible chat/completions, and the per-vendor
differences are three constructor parameters (token field name, thinking switch style, auth style).
MockProvider is the offline stand-in used when no key is configured.

LLM Provider 抽象与 OpenAI 兼容实现。**更换服务商优先改 env，无需改代码**。

所有支持的厂商（默认 MiniMax，另有 MiMo、DeepSeek、Qwen）都走 OpenAI 兼容 chat/completions，
厂商差异是三个构造参数（token 字段名、思考开关方式、鉴权方式）。无 key 时用 MockProvider 离线兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

logger = logging.getLogger("drone_agent.providers")

# Outbound connection pool and timeouts: reuse connections instead of a TLS handshake per call.
# 出站 HTTP 连接池 + 超时（复用连接，免去每调用新建 client 的 TLS 握手开销）。
_HTTP_CONNECT_S = float(os.getenv("LLM_HTTP_CONNECT_S", "5") or 5)
_HTTP_READ_CAP_S = float(os.getenv("LLM_HTTP_READ_CAP_S", "75") or 75)  # complete cap / complete 兜底上限
_STREAM_STALL_S = float(os.getenv("LLM_STREAM_STALL_S", "30") or 30)  # per-chunk silence cap / 流式单块静默上限

# MiniMax reports business errors in `base_resp`; these two mean the content was filtered.
# MiniMax 在 `base_resp` 里报告业务错误；这两个码表示内容被过滤。
_CONTENT_FILTER_CODES = frozenset({1026, 1027})


def _read_budget(budget_s, cap_s: float) -> float:
    """Upstream read timeout: 90% of the caller's remaining deadline when known, else the cap.

    Failing just before the caller does returns a clean error instead of being cancelled midway.

    上游 read 超时：有调用方 deadline 时取其 90% 收进窗口内——先于调用方失败、返回干净错误，
    而非被调用方中途取消；无 deadline 时用 cap 兜底。
    """
    try:
        b = float(budget_s) if budget_s is not None else 0.0
    except (TypeError, ValueError):
        b = 0.0
    if b > 0:
        return max(1.0, min(cap_s, b * 0.9))
    return cap_s


def _http_timeout(budget_s, read_cap: float):
    import httpx

    return httpx.Timeout(_read_budget(budget_s, read_cap), connect=min(_HTTP_CONNECT_S, read_cap), pool=5.0)


def _strict_mock_gate(domain: str, why: str) -> None:
    """With REQUIRE_REAL_PROVIDERS=on a fallback to MockProvider refuses to start.

    严格栈（REQUIRE_REAL_PROVIDERS=on）：mock 决议直接拒绝启动。
    """
    if os.getenv("REQUIRE_REAL_PROVIDERS", "off").strip().lower() not in ("on", "true", "1", "yes"):
        return
    raise RuntimeError(
        f"REQUIRE_REAL_PROVIDERS=on: provider[{domain}] would fall back to mock ({why}); configure a key / "
        f"严格栈禁止 provider[{domain}] 落 mock（{why}），请补齐凭证"
    )


class ProviderHTTPError(RuntimeError):
    """Structured upstream error: status code plus Retry-After, message keeps the body snippet.

    上游 HTTP 错误：状态码 + Retry-After 结构化；消息保持 `provider HTTP <code>: <body片段>` 格式，
    日志可诊断口径不变。
    """

    def __init__(self, status_code: int, snippet: str, retry_after: float | None = None):
        super().__init__(f"provider HTTP {status_code}: {snippet}")
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after_s(resp) -> float | None:
    """Seconds from a numeric Retry-After header; defensive for header-less test stubs.

    解析 Retry-After 秒数（仅数字形式）；对无 headers 的测试桩防御。
    """
    headers = getattr(resp, "headers", None) or {}
    v = (headers.get("retry-after") or "").strip()
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def normalize_tool_calls(raw_calls) -> list[dict]:
    """OpenAI-shaped tool_calls -> ``[{"id", "name", "arguments": dict}]``.

    Malformed arguments drop that call (counted in a warning) and are deliberately not string-salvaged:
    the value of tool calling is the server-side constraint, so a malformed call is a protocol failure
    and the caller falls back to its own JSON salvage/retry path. Providers that send an object are
    accepted as-is.

    OpenAI 形状 tool_calls → 统一形状。畸形 arguments **丢弃该条**（warning 计数），刻意不做字符串
    抢救：tool-calling 的价值就是服务端约束，畸形 = 协议失败，诚实回退让调用方走 JSON 抢救 / 重试
    路径。个别服务商直接给 object 的宽容接收。
    """
    out = []
    for tc in raw_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        args_raw = fn.get("arguments")
        if isinstance(args_raw, dict):
            args = args_raw
        else:
            try:
                args = json.loads(args_raw or "{}")
            except (TypeError, ValueError) as e:
                logger.warning("tool_call %s has malformed arguments, dropped / arguments 畸形，丢弃: %s", name, e)
                continue
        if not isinstance(args, dict):
            logger.warning("tool_call %s arguments are not an object, dropped / arguments 非 object，丢弃", name)
            continue
        out.append({"id": tc.get("id") or "", "name": name, "arguments": args})
    return out


class BaseProvider:
    """The provider protocol. / Provider 协议。"""

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        """Returns (content, model_used, finish_reason, (prompt_tokens, completion_tokens)).

        thinking: None uses the vendor default, True enables and False disables it for this call.

        返回 (content, model_used, finish_reason, (prompt_tokens, completion_tokens))。
        thinking：None = 用服务商默认；True = 本次开思考；False = 本次关思考。
        """
        raise NotImplementedError

    async def complete_tools(self, messages, model, temperature, max_tokens,
                             tools=None, tool_choice=None, thinking=None, timeout_s=None):
        """Completion with tool definitions; returns the complete() tuple plus normalized tool_calls.

        The default falls back to plain complete() with no tool calls, so a provider that cannot call
        tools fails open to the caller's JSON salvage path instead of breaking the 4-tuple contract.

        带工具定义的补全；返回 complete() 的四元组加上归一化的 tool_calls。默认实现回落纯文本
        complete() + 空 tool_calls：不会调工具的 provider 由调用方走 JSON 抢救路径，不破坏四元组契约。
        """
        content, used, finish, usage = await self.complete(
            messages, model, temperature, max_tokens, thinking=thinking, timeout_s=timeout_s)
        return content, used, finish, usage, []

    async def stream(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        raise NotImplementedError
        yield  # pragma: no cover


class MockProvider(BaseProvider):
    """Offline stand-in without a key; it never claims to have planned anything.

    无 API key 时的兜底，保证离线可跑；它从不声称完成了任何规划。
    """

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        # Test hook: LLM_MOCK_DELAY_MS injects latency at call time to exercise caller timeouts.
        # 测试钩子：LLM_MOCK_DELAY_MS 在调用时注入延迟，用于刻画调用方超时。
        delay_ms = int(os.getenv("LLM_MOCK_DELAY_MS", "0") or 0)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        text = f"[mock] no model configured; received: {str(user)[:80]}"
        return text, "mock", "stop", (0, 0)

    async def stream(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        content, *_ = await self.complete(messages, model, temperature, max_tokens)
        for ch in content:
            yield ch


# Reasoning models such as MiniMax-M3 inline the thinking block at the head of `content`
# (`<think>…</think>\n\n answer`) when thinking is on. Probes (2026-07-12, four vendors × complete/stream ×
# thinking on/off) showed only MiniMax leaked it, so every provider strips it at its exit.
# MiniMax-M3 等推理模型**开思考**时把思考段内联在 content 头部（`<think>…</think>\n\n正文`），
# 而非独立 reasoning_content 字段。真栈探针（2026-07-12，四家 × complete/stream × 开/关思考）：
# 仅 MiniMax 开思考泄漏。统一在 provider 出口剥——思考是内部推理，任何调用方都不该收到。
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def strip_think_block(text: str) -> str:
    """Strip only a leading <think>…</think> block; an unclosed one yields "" (no answer present).

    剥离**头部** <think>…</think> 块。只看头部，正文中间出现的字面 <think> 不动。未闭合（被 max_tokens
    截断在思考里）→ 无正文可用，诚实返回空串，绝不把半截思考当答案。
    """
    t = text or ""
    head = t.lstrip()
    if not head.startswith(_THINK_OPEN):
        return t
    end = head.find(_THINK_CLOSE)
    if end == -1:
        return ""
    return head[end + len(_THINK_CLOSE):].lstrip("\n").lstrip()


class ThinkStreamStripper:
    """Streaming version of strip_think_block that is safe across chunk boundaries.

    流式头部 <think> 剥离状态机（与 strip_think_block 同语义，跨 chunk 安全）。
    probe：缓冲首若干字符判定是否 `<think>` 前缀；drop：吞到 `</think>` 后放流；pass：透传。
    """

    def __init__(self):
        self._mode = "probe"  # probe | drop | pass
        self._buf = ""

    def feed(self, delta: str) -> str:
        if self._mode == "pass":
            return delta
        self._buf += delta
        if self._mode == "probe":
            probe = self._buf.lstrip()
            if not probe:
                return ""
            if probe.startswith(_THINK_OPEN):
                self._mode = "drop"
            elif _THINK_OPEN.startswith(probe[:len(_THINK_OPEN)]):
                return ""  # still a "<th"-like prefix, keep waiting / 仍是 "<th" 类前缀，继续观望
            else:
                self._mode = "pass"
                out, self._buf = self._buf, ""
                return out
        if self._mode == "drop":
            end = self._buf.find(_THINK_CLOSE)
            if end == -1:
                return ""
            rest = self._buf[end + len(_THINK_CLOSE):].lstrip("\n").lstrip()
            self._mode = "pass"
            self._buf = ""
            return rest
        return ""

    def flush(self) -> str:
        """End of stream: release a probe residue unchanged; drop an unclosed thinking block.

        流结束收尾：probe 残留原样放出不丢字；drop 未闭合 = 整段思考被截断，丢弃。
        """
        if self._mode == "probe":
            out, self._buf = self._buf, ""
            return out
        return ""


def _check_business_error(data) -> str | None:
    """Map a MiniMax `base_resp` error: content filtering returns "refusal", other codes raise.

    解析 MiniMax `base_resp`：内容过滤返回 "refusal"，其他非零码抛出错误。
    """
    if not isinstance(data, dict):
        raise ProviderHTTPError(200, "non-object response / 非对象响应")
    base = data.get("base_resp") or {}
    code = base.get("status_code") if isinstance(base, dict) else None
    if code in _CONTENT_FILTER_CODES:
        return "refusal"
    if code not in (None, 0):
        raise ProviderHTTPError(200, f"base_resp {code}: {str(base.get('status_msg', ''))[:200]}")
    if not data.get("choices"):
        raise ProviderHTTPError(200, "response without choices / 响应缺 choices")
    return None


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI-compatible Chat Completions (MiniMax by default; MiMo, DeepSeek, Qwen, self-hosted).

    Endpoint, auth and thinking switch are injected by configuration, so changing vendor never
    changes code:
    - base_url: full chat/completions URL
    - auth_style: ``bearer`` (MiniMax and most vendors) | ``api-key`` (MiMo)
    - token_param: ``max_completion_tokens`` (MiniMax/MiMo) | ``max_tokens`` (DeepSeek/Qwen)
    - thinking_style: ``mimo`` sends ``thinking: {type: disabled}`` to disable (MiniMax too); ``qwen``
      sends ``enable_thinking``; ``none`` sends nothing

    OpenAI 兼容 Chat Completions 提供商。端点、鉴权、思考开关全部经配置注入——更换 LLM 服务商只改
    env、不动代码。
    """

    def __init__(self, api_key: str, base_url: str, auth_style: str = "bearer", disable_thinking: bool = True,
                 token_param: str = "max_completion_tokens", thinking_style: str = "mimo"):
        if not base_url:
            raise ValueError("an explicit chat/completions endpoint is required")
        self.api_key = api_key
        self.base_url = base_url
        self.auth_style = (auth_style or "bearer").lower()
        self.disable_thinking = disable_thinking
        self.token_param = (token_param or "max_completion_tokens").strip()
        self.thinking_style = (thinking_style or "mimo").strip().lower()
        self._client = None  # reusable outbound pool, created lazily on the running loop / 复用的出站连接池（懒建）

    def _get_client(self):
        import httpx

        if self._client is None:
            limits = httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=30.0)
            self._client = httpx.AsyncClient(limits=limits)
        return self._client

    async def aclose(self) -> None:
        """Close the pooled client. / 关闭连接池。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth_style == "bearer":
            h["Authorization"] = f"Bearer {self.api_key}"
        else:  # MiMo style / MiMo 风格
            h["api-key"] = self.api_key
        return h

    def _resolve_thinking(self, thinking) -> bool:
        """Whether this call disables thinking: None uses the constructor default.

        本次调用是否关思考：thinking=None 用构造默认；True/False 覆盖本次。
        """
        return self.disable_thinking if thinking is None else (not thinking)

    def _build_body(self, messages, model, temperature, max_tokens, thinking, stream: bool) -> dict:
        """Build the chat/completions body with the vendor's token field and thinking switch.

        按厂商差异（token_param / thinking_style）构造 chat/completions 请求体。
        """
        disable = self._resolve_thinking(thinking)
        # With thinking on, reasoning eats the budget and starves content; raise the floor to 2048.
        # 开思考时给足 token：reasoning 占预算，content 容易被饿空 / 截断；下限抬到 2048。
        max_out = (max_tokens or 512) if disable else max((max_tokens or 512), 2048)
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            self.token_param: max_out,
            "stream": stream,
        }
        if self.thinking_style == "mimo":
            # MiMo/MiniMax spend nearly the whole budget on reasoning by default, starving structured
            # output; disabling returns clean, deterministic, low-latency content.
            # MiMo/MiniMax 等推理模型默认把 token 预算几乎全花在 reasoning 上，导致结构化任务的 content
            # 被饿空 / 截断——关思考拿干净、确定、低延迟 content。开思考时不发本键（回原生思考态）。
            if disable:
                body["thinking"] = {"type": "disabled"}
        elif self.thinking_style == "qwen":
            # DashScope compatible mode: thinking is switched by enable_thinking.
            # DashScope 兼容模式 qwen3：思考经 enable_thinking 显式控制（结构化任务须置 false）。
            body["enable_thinking"] = not disable
        return body

    async def _post_chat(self, body, timeout_s) -> dict:
        """POST chat/completions; 4xx/5xx carry the body snippet because the reason lives there.

        非流式 chat/completions POST + 错误结构化。4xx/5xx 的真实拒因在响应体里（如 MiniMax 422 只有
        body 说得清是参数还是内容问题），截断入异常，日志直接可诊断。
        """
        resp = await self._get_client().post(
            self.base_url, headers=self._headers(), json=body, timeout=_http_timeout(timeout_s, _HTTP_READ_CAP_S))
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise ProviderHTTPError(resp.status_code, snippet, _retry_after_s(resp))
        return resp.json()

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        body = self._build_body(messages, model, temperature, max_tokens, thinking, stream=False)
        data = await self._post_chat(body, timeout_s)
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        tokens = (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        if _check_business_error(data) == "refusal":
            return "", model, "refusal", tokens
        choice = data["choices"][0]
        content = strip_think_block((choice.get("message") or {}).get("content") or "")
        finish = choice.get("finish_reason") or "stop"
        return content, model, "refusal" if finish == "content_filter" else finish, tokens

    async def complete_tools(self, messages, model, temperature, max_tokens,
                             tools=None, tool_choice=None, thinking=None, timeout_s=None):
        """Completion with OpenAI wire-format tools injected as-is; tool_calls are normalized.

        finish_reason is passed through, never used to decide whether a tool was called: some vendors
        report "stop" while returning tool calls, so tool use is judged by the tool_calls field alone.

        带 tools 的补全：tools / tool_choice 为 OpenAI 线格式原样注入；响应 tool_calls 归一化。
        finish_reason 只透传不判断：有的厂商出 tool_calls 时 finish_reason 仍是 "stop"——是否工具调用
        一律按 tool_calls 置位判断。
        """
        if not tools:
            return await super().complete_tools(
                messages, model, temperature, max_tokens, thinking=thinking, timeout_s=timeout_s)
        body = self._build_body(messages, model, temperature, max_tokens, thinking, stream=False)
        body["tools"] = list(tools)
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        data = await self._post_chat(body, timeout_s)
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        tokens = (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        if _check_business_error(data) == "refusal":
            return "", model, "refusal", tokens, []
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = strip_think_block(msg.get("content") or "")
        tool_calls = normalize_tool_calls(msg.get("tool_calls"))
        finish = choice.get("finish_reason") or "stop"
        return content, model, "refusal" if finish == "content_filter" else finish, tokens, tool_calls

    async def stream(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        import httpx

        body = self._build_body(messages, model, temperature, max_tokens, thinking, stream=True)
        # The read timeout acts as a per-chunk stall detector. / 流式：read 超时作单块停顿检测。
        stall = _read_budget(timeout_s, _STREAM_STALL_S)
        stripper = ThinkStreamStripper()
        async with self._get_client().stream(
                "POST", self.base_url, headers=self._headers(), json=body,
                timeout=httpx.Timeout(stall, connect=_HTTP_CONNECT_S, pool=5.0)) as resp:
            if resp.status_code >= 400:
                raw = await resp.aread()
                snippet = raw[:300].decode("utf-8", "replace").replace("\n", " ")
                raise ProviderHTTPError(resp.status_code, snippet, _retry_after_s(resp))
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0].get("delta", {})
                    # Only content is forwarded; reasoning deltas are dropped. / 只取 content，思考增量丢弃。
                    text = delta.get("content", "")
                    if text:
                        out = stripper.feed(text)
                        if out:
                            yield out
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        tail = stripper.flush()
        if tail:
            yield tail
