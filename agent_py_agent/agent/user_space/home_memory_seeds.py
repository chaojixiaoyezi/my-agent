from __future__ import annotations

"""Owner Memory 导航空种子；正式事实、lesson 和 HOT 只能由对应 Service 产生。"""

# LLM: 默认 home 不预装项目经验，避免未经证据的规则看起来像用户长期记忆。
# 模块用途: 创建短导航、空 HOT 和可重建 routing 的初始文件。


# LLM: memory.md 只列权威入口，不保存事实或教训正文。
# 函数用途: 返回 owner 的短导航种子。
def default_memory_md() -> str:
    return """# Memory

这是短导航，不是长期记忆正文库。

- 正式长期事实：`memory/long_term/memory.jsonl`
- 每日经历摘要：`memory/daily/`
- 待审核候选：`memory/candidates.jsonl`
- 正式教训：`memory/lessons/`
- 教训路由：`memory/routing/INDEX.md`
- 少量高频规则：`memory-hot.md`
"""


# LLM: HOT 初始必须为空；只有 HotRuleRepository 能增加正式短规则。
# 函数用途: 返回 owner 的 HOT 空种子。
def default_memory_hot_md() -> str:
    return """# Memory HOT

这里只放经过正式 lesson、重复独立证据和审核后晋升的短规则；初始为空。
"""


# LLM: routing 初始只有说明，LessonRepository 会根据正式元数据确定性重建。
# 函数用途: 返回 owner 的 lesson 路由空种子。
def default_memory_route_index_md() -> str:
    return "# Memory Routing Index\n\n此文件由正式 lesson 元数据确定性生成。\n"


# LLM: 旧调用方若仍读取默认 lessons 只得到空集合，不能恢复硬编码经验。
# 函数用途: 明确默认不创建任何正式 lesson。
def default_memory_lessons() -> dict[str, str]:
    return {}


__all__ = [
    "default_memory_hot_md",
    "default_memory_lessons",
    "default_memory_md",
    "default_memory_route_index_md",
]
