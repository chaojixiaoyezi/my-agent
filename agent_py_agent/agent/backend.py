from __future__ import annotations

"""LLM: compatibility facade for model backend adapters moved to `agent.backends`.

给人看的解释：
真实后端实现已经放到 `agent_py_agent.agent.backends`。
这个文件只保留旧导入路径，避免历史代码里的 `from agent.backend import ...` 失效。
"""

from .backends import (  # noqa: F401
    AnthropicCompatibleBackend,
    BackendOptions,
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
    "BackendOptions",
    "EchoBackend",
    "HttpBackend",
    "ModelResponse",
    "OpenAICompatibleBackend",
    "get_backend",
]
