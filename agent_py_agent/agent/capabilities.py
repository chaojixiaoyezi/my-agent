from __future__ import annotations

"""LLM: compatibility facade for capability routing moved to `agent.capability`.

给人看的解释：
真实能力路由代码已经放进 `agent_py_agent.agent.capability`。
这个文件只保留旧入口，让测试和历史调用不用立刻改 import。
"""

from .capability import (  # noqa: F401
    CapabilityCard,
    CapabilityRouter,
    CapabilitySearchHit,
    classify_tool_risk,
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
    "from_skill_card",
    "from_tool_spec",
    "score_card",
    "tokenize",
]
