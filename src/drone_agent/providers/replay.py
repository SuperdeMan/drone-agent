"""Record and strictly replay provider exchanges; a replay never improvises.

A recording binds every exchange to the SHA-256 of the exact request (model, messages including tool
data, tool schemas, tool choice, thinking switch, sampling and token limits) plus the prompt version
and model that produced it. Replay recomputes the digest of each request and fails with
ReplayMismatch when it differs or when the recording is exhausted, so a changed prompt, registry or
tool return can never silently reuse an old model answer. Recordings never contain credentials:
headers are not part of the request that is stored.

`source` distinguishes real model output (`recorded`) from hand-written test doubles (`scripted`);
only `recorded` fixtures may be reported as model behaviour.

录制并严格回放 provider 交互；回放从不即兴发挥。

录制把每次交互绑定到确切请求（模型、含工具数据的消息、工具 schema、工具选择、思考开关、采样与
token 上限）的 SHA-256，以及产生它的提示版本与模型。回放逐次重算请求摘要，不一致或录制用尽即抛
ReplayMismatch，提示、登记表或工具返回一变就不可能静默复用旧的模型回答。录制中不含凭证：
存储的请求不包含请求头。

`source` 区分真实模型输出（`recorded`）与手写测试替身（`scripted`）；只有 `recorded` 可以当作
模型行为来报告。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from drone_agent.contracts.common import ContractModel, utcnow
from drone_agent.providers.llm import BaseProvider

FIXTURE_FORMAT = "drone.provider.recording/v1"


class ReplayMismatch(RuntimeError):
    """The request differs from the recording, or the recording is exhausted. / 请求与录制不符或录制已用尽。"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def request_body(kind, messages, model, temperature, max_tokens, tools=None, tool_choice=None, thinking=None) -> dict:
    """The part of a call that determines the answer; this is what the digest covers.

    决定回答的那部分调用参数；摘要覆盖的正是这些。
    """
    return {
        "kind": kind,
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "tools": tools or [],
        "tool_choice": tool_choice,
        "thinking": thinking,
    }


def digest(body: dict) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


class Exchange(ContractModel):
    """One request/response pair. / 一次请求与响应。"""

    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: dict[str, Any]
    content: str = ""
    model_used: str = ""
    finish: str = "stop"
    usage: tuple[int, int] = (0, 0)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: float = 0.0


class Recording(ContractModel):
    """A versioned fixture: provenance plus the ordered exchanges. / 带来源信息与有序交互的版本化夹具。"""

    format: Literal["drone.provider.recording/v1"] = FIXTURE_FORMAT
    source: Literal["recorded", "scripted"]
    provider_id: str
    model: str
    endpoint_host: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    software_revision: str = ""
    recorded_at: str = ""
    label: str = ""
    exchanges: list[Exchange] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> Recording:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")


class RecordingProvider(BaseProvider):
    """Wraps a provider and records every exchange in call order. / 包装 provider 并按调用顺序录制每次交互。"""

    def __init__(self, inner: BaseProvider):
        self.inner = inner
        self.exchanges: list[Exchange] = []

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        body = request_body("complete", messages, model, temperature, max_tokens, thinking=thinking)
        started = time.monotonic()
        content, used, finish, usage = await self.inner.complete(
            messages, model, temperature, max_tokens, thinking=thinking, timeout_s=timeout_s)
        self.exchanges.append(Exchange(request_digest=digest(body), request=body, content=content, model_used=used,
                                       finish=finish, usage=tuple(usage),
                                       latency_ms=round((time.monotonic() - started) * 1000, 1)))
        return content, used, finish, usage

    async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                             thinking=None, timeout_s=None):
        body = request_body("complete_tools", messages, model, temperature, max_tokens, tools, tool_choice, thinking)
        started = time.monotonic()
        content, used, finish, usage, calls = await self.inner.complete_tools(
            messages, model, temperature, max_tokens, tools=tools, tool_choice=tool_choice, thinking=thinking,
            timeout_s=timeout_s)
        self.exchanges.append(Exchange(request_digest=digest(body), request=body, content=content, model_used=used,
                                       finish=finish, usage=tuple(usage), tool_calls=calls,
                                       latency_ms=round((time.monotonic() - started) * 1000, 1)))
        return content, used, finish, usage, calls

    def recording(self, *, source: str, provider_id: str, model: str, endpoint_host: str = "",
                  prompt_version: str = "", prompt_sha256: str = "", software_revision: str = "",
                  label: str = "") -> Recording:
        """Freeze what was recorded into a fixture. / 把已录制内容冻结为夹具。"""
        return Recording(source=source, provider_id=provider_id, model=model, endpoint_host=endpoint_host,
                         prompt_version=prompt_version, prompt_sha256=prompt_sha256,
                         software_revision=software_revision, recorded_at=utcnow().isoformat(), label=label,
                         exchanges=list(self.exchanges))


class ReplayProvider(BaseProvider):
    """Serves a recording in order and refuses any request whose digest differs.

    按顺序提供录制内容，任何摘要不同的请求都被拒绝。
    """

    def __init__(self, recording: Recording):
        self.recording = recording
        self.position = 0

    def _next(self, body: dict) -> Exchange:
        if self.position >= len(self.recording.exchanges):
            raise ReplayMismatch("recording exhausted / 录制已用尽")
        exchange = self.recording.exchanges[self.position]
        actual = digest(body)
        if exchange.request_digest != actual:
            raise ReplayMismatch(
                f"request {self.position} differs from the recording "
                f"(recorded {exchange.request_digest[:12]}, actual {actual[:12]}) / 第 {self.position} 次请求与录制不符"
            )
        self.position += 1
        return exchange

    @property
    def exhausted(self) -> bool:
        return self.position == len(self.recording.exchanges)

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        exchange = self._next(request_body("complete", messages, model, temperature, max_tokens, thinking=thinking))
        return exchange.content, exchange.model_used, exchange.finish, exchange.usage

    async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                             thinking=None, timeout_s=None):
        exchange = self._next(request_body("complete_tools", messages, model, temperature, max_tokens, tools,
                                           tool_choice, thinking))
        return exchange.content, exchange.model_used, exchange.finish, exchange.usage, list(exchange.tool_calls)


SCRIPTED_FORMAT = "drone.planner.scripted/v1"


class KeyedScriptedProvider(BaseProvider):
    """A hand-written double that answers by the exact operator request text; never reported as a model.

    Harness runs without a model key use it so the service, signing, uplink and flight path can be
    exercised; every outcome it produces carries the model id `scripted` and must not be counted as model
    behaviour (admission-rate baselines need `recorded` or live answers).

    按操作者请求原文作答的手写替身；永远不当作模型报告。没有模型密钥的编排运行用它来走通服务、签名、
    uplink 与飞行路径；它产生的每个结果都带模型 ID `scripted`，不能计入模型行为（准入率基线需要
    `recorded` 或实调回答）。
    """

    def __init__(self, answers: dict[str, dict], model: str = "scripted"):
        self.answers, self.model = dict(answers), model

    @classmethod
    def load(cls, path: str | Path) -> KeyedScriptedProvider:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("format") != SCRIPTED_FORMAT or data.get("source") != "scripted":
            raise ValueError("not a scripted planner fixture")
        return cls(data["answers"])

    def _answer(self, messages) -> dict:
        # Key on the request block, not on the latest user turn: retries append validation feedback, and a
        # fooled double keeps giving the same answer. / 以请求块为键而非最新用户轮次：重试会追加校验反馈，
        # 被骗的替身会一直给出同一个回答。
        user = next((m["content"] for m in messages if m["role"] == "user" and "OPERATOR_REQUEST:" in str(m["content"])),
                    "")
        text = str(user).split("OPERATOR_REQUEST:\n", 1)[-1].split("\n\nOPERATOR_SCOPE:", 1)[0].strip()
        if text not in self.answers:
            raise ReplayMismatch("no scripted answer for this request / 该请求没有脚本回答")
        return self.answers[text]

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        answer = self._answer(messages)
        return answer.get("content", ""), self.model, answer.get("finish", "stop"), tuple(answer.get("usage", (0, 0)))

    async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                             thinking=None, timeout_s=None):
        answer = self._answer(messages)
        return (answer.get("content", ""), self.model, answer.get("finish", "stop"),
                tuple(answer.get("usage", (0, 0))), list(answer.get("tool_calls", [])))


class ScriptedProvider(BaseProvider):
    """A hand-written test double that answers from a queue; wrap it to produce `scripted` fixtures.

    按队列回答的手写测试替身；用 RecordingProvider 包装即可生成 `scripted` 夹具。
    """

    def __init__(self, answers: list[dict], model: str = "scripted"):
        self.answers = list(answers)
        self.model = model
        self.calls: list[dict] = []

    def _pop(self) -> dict:
        if not self.answers:
            raise ReplayMismatch("scripted answers exhausted / 脚本回答已用尽")
        return self.answers.pop(0)

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        answer = self._pop()
        self.calls.append({"messages": messages})
        return answer.get("content", ""), self.model, answer.get("finish", "stop"), tuple(answer.get("usage", (0, 0)))

    async def complete_tools(self, messages, model, temperature, max_tokens, tools=None, tool_choice=None,
                             thinking=None, timeout_s=None):
        answer = self._pop()
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        if "raise" in answer:
            raise answer["raise"]
        return (answer.get("content", ""), self.model, answer.get("finish", "stop"),
                tuple(answer.get("usage", (0, 0))), list(answer.get("tool_calls", [])))
