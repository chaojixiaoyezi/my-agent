
from __future__ import annotations

"""compatibility facade for capability routing moved to `agent.capability`.

真实能力路由代码已经放进 `agent_py_agent.agent.capability`。
这个文件只保留旧入口，让测试和历史调用不用立刻改 import。
"""

from .capability import (  # noqa: F401
    CapabilityCard,
    CapabilityRouter,
    CapabilitySearchHit,
    classify_tool_risk,
    default_capability_cards,
    from_skill_card,
    from_tool_spec,
    score_card,
    tokenize,
)

__all__ = [
    "CapabilityCard",
    "CapabilityRouter",
    "CapabilitySearchHit",
    "classify_tool_risk",
    "default_capability_cards",
    "from_skill_card",
    "from_tool_spec",
    "score_card",
    "tokenize",
]
