from __future__ import annotations

"""LLM: public API for model backend adapters and normalized model responses.

给人看的解释：
这里放所有'怎么和模型服务说话'的代码。以后新增 OpenAI、Anthropic、本地模型、
公司内网模型，都应该进这个目录，而不是塞回 `core.py`。
"""

from .base import (
    AnthropicCompatibleBackend,
    BaseBackend,
    EchoBackend,
    HttpBackend,
    ModelResponse,
    OpenAICompatibleBackend,
    get_backend,
)

__all__ = [
    "AnthropicCompatibleBackend",
    "BaseBackend",
    "EchoBackend",
    "HttpBackend",
    "ModelResponse",
    "OpenAICompatibleBackend",
    "get_backend",
]
