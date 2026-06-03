
from __future__ import annotations

"""compatibility facade for skill cards moved to `agent.capability.skills`.

Skill 扫描本质上属于能力路由的一部分，所以真实实现已经搬到 capability 目录。
旧入口继续保留。
"""

from .capability.skills import SkillCard, SkillRegistry, parse_skill_file

__all__ = ["SkillCard", "SkillRegistry", "parse_skill_file"]
