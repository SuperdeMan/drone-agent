# Ported from embodied-agent src/embodied/providers/runtime.py @ e20fe33 (from car-agent
# llm-gateway/llm_runtime.py @ f0b08f8), changes: default provider is minimax (D029) instead of the
# LLM_PROVIDER=xiaomimimo legacy default; Anthropic special case, embeddings, the global hot-switch control
# plane and tier aliases removed; a missing key raises ProviderUnavailable instead of silently becoming
# MockProvider (planning must never pretend); keys may come from `<NAME>_FILE` secret files; bilingual.
"""Provider registry: which vendor and model a role uses, resolved from the environment.

Two roles exist in M2: `planner` (default MiniMax-M3, D029) and `vision` (default the separate
Qwen-VL tier, as in car-agent, used only for the evidence verifier's business judgment). Variable
names follow car-agent's `.env.example`; values come from the process environment or from a
mounted secret file named by `<NAME>_FILE`. Nothing here reads another project's files.

Provider 注册表：某个角色用哪家厂商和哪个模型，由环境解析。M2 有两个角色：`planner`（默认
MiniMax-M3，D029）与 `vision`（默认沿用 car-agent 的独立 Qwen-VL 视觉档，只用于证据验证的业务
判断）。变量名参照 car-agent 的 `.env.example`；值来自进程环境或 `<NAME>_FILE` 指向的挂载密钥
文件。这里不读取任何其他项目的文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from drone_agent.providers.guarded import GuardedProvider
from drone_agent.providers.llm import MockProvider, OpenAICompatibleProvider

DEFAULT_PLANNER_PROVIDER = "minimax"
DEFAULT_VISION_PROVIDER = "qwen-vl"

# provider id -> static configuration; endpoint and model can be overridden via the *_env keys.
# provider id → 静态配置。endpoint / model 均 env 可覆盖（*_env 键）。
_PROVIDER_SPECS: dict[str, dict] = {
    "minimax": {
        "label": "MiniMax", "key_env": "MINIMAX_API_KEY", "base_url_env": "MINIMAX_BASE_URL",
        "base_url": "https://api.minimaxi.com/v1/chat/completions",
        "auth_style": "bearer", "token_param": "max_completion_tokens", "thinking_style": "mimo",
        "model_env": "MINIMAX_LLM_MODEL", "model": "MiniMax-M3",
    },
    "mimo": {
        "label": "MiMo", "key_env": "LLM_API_KEY", "base_url_env": "LLM_BASE_URL",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
        "auth_style": "api-key", "token_param": "max_completion_tokens", "thinking_style": "mimo",
        "model_env": "LLM_MODEL_PRIMARY", "model": "mimo-v2.5-pro",
    },
    "deepseek": {
        # DeepSeek v4 models also accept thinking: {type: disabled}, so they use the mimo style.
        # DeepSeek v4 同样认 thinking:{type:disabled}，故 thinking_style=mimo。
        "label": "DeepSeek", "key_env": "DEEPSEEK_API_KEY", "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "auth_style": "bearer", "token_param": "max_tokens", "thinking_style": "mimo",
        "model_env": "DEEPSEEK_MODEL_PRIMARY", "model": "deepseek-v4-pro",
    },
    "qwen": {
        "label": "Qwen", "key_env": "DASHSCOPE_LLM_KEY", "base_url_env": "QWEN_BASE_URL",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "auth_style": "bearer", "token_param": "max_tokens", "thinking_style": "qwen",
        "model_env": "QWEN_MODEL_PRIMARY", "model": "qwen3.7-max",
    },
    # Vision is its own tier, not a VL model squeezed into the qwen chat tier: every model in the chain
    # must be able to read images (car-agent P4b: the chat model returns 400 on multimodal content).
    # 视觉独立成一档，而不是往 qwen 聊天档里塞 VL 型号：降级链上的每个模型都必须能看图
    # （car-agent P4b：聊天模型对多模态 content 直接 400）。
    "qwen-vl": {
        "label": "Qwen-VL", "key_env": "DASHSCOPE_LLM_KEY", "base_url_env": "QWEN_BASE_URL",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "auth_style": "bearer", "token_param": "max_tokens", "thinking_style": "qwen",
        "model_env": "VISION_MODEL", "model": "qwen3-vl-plus", "vision": True,
    },
}


class ProviderUnavailable(RuntimeError):
    """No usable credentials for the requested provider; the caller must report, not pretend.

    请求的 provider 没有可用凭证；调用方必须如实报告，不能假装成功。
    """


def secret(name: str) -> str:
    """A secret from the environment, or from the file named by `<name>_FILE`; never logged.

    从环境或 `<name>_FILE` 指向的文件读取密钥；从不记录日志。
    """
    value = os.getenv(name, "")
    if value:
        return value.strip()
    path = os.getenv(name + "_FILE", "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    if name == "DASHSCOPE_LLM_KEY":
        # Same DashScope account as car-agent's ASR/embedding keys. / 与 car-agent ASR / embedding 同一百炼账号。
        return (os.getenv("DASHSCOPE_ASR_KEY") or os.getenv("LLM_EMBED_API_KEY") or "").strip()
    return ""


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved configuration of one provider; carries no secret. / 解析后的 provider 配置；不含密钥。"""

    provider_id: str
    label: str
    base_url: str
    model: str
    auth_style: str
    token_param: str
    thinking_style: str
    key_env: str
    available: bool
    vision: bool = False

    @property
    def endpoint_host(self) -> str:
        """Host of the endpoint, recorded in provenance instead of the full URL. / 端点主机名，写入来源记录。"""
        return urlsplit(self.base_url).netloc


def _norm_id(pid: str) -> str:
    pid = (pid or "").strip().lower()
    return {"xiaomimimo": "mimo"}.get(pid, pid)


def provider_config(pid: str, *, model: str = "") -> ProviderConfig:
    """Resolve one provider's configuration from the environment. / 从环境解析某个 provider 的配置。"""
    pid = _norm_id(pid)
    if pid not in _PROVIDER_SPECS:
        raise ProviderUnavailable(f"unknown provider: {pid}")
    spec = _PROVIDER_SPECS[pid]
    return ProviderConfig(
        provider_id=pid,
        label=spec["label"],
        base_url=os.getenv(spec["base_url_env"], "") or spec["base_url"],
        model=model or os.getenv(spec["model_env"], "") or spec["model"],
        auth_style=spec["auth_style"],
        token_param=spec["token_param"],
        thinking_style=spec["thinking_style"],
        key_env=spec["key_env"],
        available=bool(secret(spec["key_env"])),
        vision=bool(spec.get("vision")),
    )


def role_provider_id(role: str) -> str:
    """Provider id for a role: planner (default minimax) or vision (default qwen-vl).

    角色对应的 provider id：planner（默认 minimax）或 vision（默认 qwen-vl）。
    """
    if role == "planner":
        return _norm_id(os.getenv("LLM_PROVIDER", "") or DEFAULT_PLANNER_PROVIDER)
    if role == "vision":
        return _norm_id(os.getenv("VISION_PROVIDER", "") or DEFAULT_VISION_PROVIDER)
    raise ValueError(f"unknown provider role: {role}")


def build_provider(role: str, *, model: str = "", guarded: bool = True):
    """(provider, config) for a role; raises ProviderUnavailable instead of falling back to a mock.

    `LLM_PROVIDER=mock` selects the offline MockProvider explicitly (demos only; it never plans).

    返回某角色的 (provider, config)；缺凭证时抛 ProviderUnavailable，不静默回落 mock。
    `LLM_PROVIDER=mock` 才显式选择离线 MockProvider（仅演示；它从不产出规划）。
    """
    pid = role_provider_id(role)
    if pid == "mock":
        config = ProviderConfig("mock", "Mock", "", "mock", "none", "max_tokens", "none", "", True)
        return MockProvider(), config
    config = provider_config(pid, model=model)
    if role == "vision" and not config.vision:
        raise ProviderUnavailable(f"provider {pid} is not a vision tier")
    key = secret(config.key_env)
    if not key:
        raise ProviderUnavailable(f"{config.key_env} is not configured for provider {pid}")
    provider = OpenAICompatibleProvider(
        key, base_url=config.base_url, auth_style=config.auth_style, disable_thinking=True,
        token_param=config.token_param, thinking_style=config.thinking_style)
    return (GuardedProvider(provider, provider_id=pid) if guarded else provider), config
