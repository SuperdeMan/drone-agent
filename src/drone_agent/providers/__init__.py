"""Model providers for the mission-service planner and evidence verifier (M2, D029).

Ported from embodied-agent / car-agent: OpenAI-compatible vendors (MiniMax-M3 by default), a passive
health tracker, rate limiting, a plain-completion cache and strict record/replay. Onboard processes
never import this package; a contract test enforces that.

任务服务中规划器与证据验证器使用的模型 provider（M2，D029）。从 embodied-agent / car-agent 移植：
OpenAI 兼容厂商（默认 MiniMax-M3）、被动健康统计、限流、纯文本补全缓存与严格录制回放。机载进程
从不导入本包，契约测试负责钉住这一点。
"""

from drone_agent.providers.cache import LLMCache
from drone_agent.providers.guarded import GuardedProvider, RateLimited
from drone_agent.providers.health import ProviderHealth, health_tracker
from drone_agent.providers.llm import (
    BaseProvider,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderHTTPError,
    ThinkStreamStripper,
    normalize_tool_calls,
    strip_think_block,
)
from drone_agent.providers.ratelimit import RateLimiter, TokenBucket
from drone_agent.providers.replay import (
    Exchange,
    Recording,
    RecordingProvider,
    ReplayMismatch,
    ReplayProvider,
    ScriptedProvider,
)
from drone_agent.providers.runtime import (
    DEFAULT_PLANNER_PROVIDER,
    DEFAULT_VISION_PROVIDER,
    ProviderConfig,
    ProviderUnavailable,
    build_provider,
    provider_config,
    role_provider_id,
    secret,
)

__all__ = [
    "DEFAULT_PLANNER_PROVIDER",
    "DEFAULT_VISION_PROVIDER",
    "BaseProvider",
    "Exchange",
    "GuardedProvider",
    "LLMCache",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ProviderConfig",
    "ProviderHTTPError",
    "ProviderHealth",
    "ProviderUnavailable",
    "RateLimited",
    "RateLimiter",
    "Recording",
    "RecordingProvider",
    "ReplayMismatch",
    "ReplayProvider",
    "ScriptedProvider",
    "ThinkStreamStripper",
    "TokenBucket",
    "build_provider",
    "health_tracker",
    "normalize_tool_calls",
    "provider_config",
    "role_provider_id",
    "secret",
    "strip_think_block",
]
