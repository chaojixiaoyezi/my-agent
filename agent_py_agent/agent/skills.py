# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

from __future__ import annotations

"""compatibility facade for skill cards moved to `agent.capability.skills`.

给人看的解释：
Skill 扫描本质上属于能力路由的一部分，所以真实实现已经搬到 capability 目录。
旧入口继续保留。
"""

from .capability.skills import (
    SkillCard,
    SkillDraftRequest,
    SkillLifecycleEvent,
    SkillLifecycleResult,
    SkillLifecycleStore,
    SkillRegistry,
    parse_skill_file,
)

__all__ = [
    "SkillCard",
    "SkillDraftRequest",
    "SkillLifecycleEvent",
    "SkillLifecycleResult",
    "SkillLifecycleStore",
    "SkillRegistry",
    "parse_skill_file",
]
