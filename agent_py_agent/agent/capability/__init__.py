from __future__ import annotations

"""LLM: public API for capability routing, capability config, and skill cards.

给人看的解释：
这里是'能力治理'目录。skill 和 tool 都会先变成能力卡，再由父代理判断该给谁、给多少、
什么时候上抛缺口。以后 resource、MCP、remote agent 也应该接到这里。
"""

from .config import CapabilityConfig, load_capability_config
from .router import (
    CapabilityCard,
    CapabilityRouter,
    CapabilitySearchHit,
    classify_tool_risk,
    from_skill_card,
    from_tool_spec,
    score_card,
    tokenize,
)
from .skills import SkillCard, SkillRegistry, parse_skill_file

__all__ = [
    "CapabilityCard",
    "CapabilityConfig",
    "CapabilityRouter",
    "CapabilitySearchHit",
    "SkillCard",
    "SkillRegistry",
    "classify_tool_risk",
    "from_skill_card",
    "from_tool_spec",
    "load_capability_config",
    "parse_skill_file",
    "score_card",
    "tokenize",
]
