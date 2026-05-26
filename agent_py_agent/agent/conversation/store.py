# LLM: ConversationStore composes small ledgers; behavior lives in split modules.
# 模块用途: 对外保留原 ConversationStore 入口，内部按 thread/message/task/observation/wake/progress/claim 分层实现。

from __future__ import annotations

from .store_context import ConversationContextStore


class ConversationStore(ConversationContextStore):
    """File-backed conversation control plane."""


__all__ = ["ConversationStore"]
