# LLM: Session runtime module; keep conversation state and persistence contracts stable.
# 模块用途: 维护会话运行时状态、上下文和持久化边界。

from __future__ import annotations

from .admin_query import AdminCrossChannelQuery
from .context_sync import SessionContextSync, format_context_for_channel
from .cross_channel import CrossChannelSession
from .manager import SessionManager
from .models import Session, generate_session_id

__all__ = [
    "Session",
    "generate_session_id",
    "SessionManager",
    "CrossChannelSession",
    "SessionContextSync",
    "format_context_for_channel",
    "AdminCrossChannelQuery",
]
