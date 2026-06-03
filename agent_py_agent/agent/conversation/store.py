
from __future__ import annotations

from .store_context import ConversationContextStore


class ConversationStore(ConversationContextStore):
    """File-backed conversation control plane."""


__all__ = ["ConversationStore"]
