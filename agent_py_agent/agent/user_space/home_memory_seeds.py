
from __future__ import annotations


def default_memory_md() -> str:
    return """# Memory

This file is a short navigation page, not the place for every lesson.

- Always-loaded high-frequency reminders: `memory-hot.md`
- Detailed reusable lessons: `memory/lessons/`
- Route index for task-specific memory: `memory/routing/INDEX.md`

Keep this file small. Add detailed rules as lessons and route to them from the index.
"""


def default_memory_hot_md() -> str:
    return """# Memory HOT

HOT memory is always safe to read. Keep it short and route details elsewhere.

1. Use ordinary user language for real my-agent tests; avoid framework terms in test prompts.
2. Fix reusable bottom-layer behavior, not one-off task templates.
3. Open-world concepts such as file formats and artifact kinds need explicit metadata or auditable defaults.
4. Failed quality checks should guide rework unless the issue is a real safety boundary.
5. Update project docs when runtime, memory, contract, or tool behavior changes.

For details, use `memory/routing/INDEX.md` to find the matching lesson.
"""


def default_memory_route_index_md() -> str:
    return "# Memory Routing Index\n\n" + "\n\n".join(_MEMORY_ROUTE_SECTIONS) + "\n"


_MEMORY_ROUTE_SECTIONS = (
    """## memory.hot
topic: always-loaded high-frequency memory
trigger_keywords: memory-hot, HOT, 高频教训, 铁律, 启动记忆
aliases: hot memory, memory hot
when_to_read: Always read this first when reasoning about durable project behavior.
authority_path: memory-hot.md
inject_mode: summary
scope: owner
priority: 100
stale_check: review when project rules change""",
    """## lessons.real-tests
topic: real my-agent task testing prompt style
trigger_keywords: 真实测试, 真实LLM, 普通中文, 提示词, 不要专业术语
aliases: real task prompt, live task
when_to_read: Read before creating or debugging real my-agent task tests.
authority_path: memory/lessons/real-tests.md
inject_mode: summary
scope: owner
priority: 90
stale_check: review after major runtime changes""",
    """## lessons.compact
topic: long task compact and continuation
trigger_keywords: compact, 上下文压缩, 续接, 长任务, 多轮压缩
aliases: compaction, resume after compact
when_to_read: Read when changing or debugging context compaction and continuation.
authority_path: memory/lessons/compact.md
inject_mode: summary
scope: owner
priority: 85
stale_check: review after memory runtime changes""",
    """## lessons.subagents
topic: subagent delegation and collaboration
trigger_keywords: 子代理, 孙代理, 协作, 派工, 多代理, agent tree
aliases: subagent collaboration, delegation
when_to_read: Read when changing delegation, collaboration, or agent-tree behavior.
authority_path: memory/lessons/subagents.md
inject_mode: summary
scope: owner
priority: 85
stale_check: review after subagent runtime changes""",
    """## lessons.artifacts
topic: artifact delivery and closeout behavior
trigger_keywords: 产物, closeout, 验收, 交付, xlsx, pdf, word
aliases: delivery quality, artifact validation
when_to_read: Read when changing delivery, artifact registry, or closeout behavior.
authority_path: memory/lessons/artifacts.md
inject_mode: summary
scope: owner
priority: 80
stale_check: review after tool or delivery changes""",
    """## lessons.open-world
topic: open-world concepts and auditable defaults
trigger_keywords: 开放世界, 文件格式, 产物类型, MIME, 协议, 后缀
aliases: open world, extensible formats
when_to_read: Read before adding mappings for file types, artifact kinds, protocols, or MIME types.
authority_path: memory/lessons/open-world.md
inject_mode: summary
scope: owner
priority: 80
stale_check: review when new hard-coded maps are added""",
)


def default_memory_lessons() -> dict[str, str]:
    return {
        "real-tests.md": """# Real Task Testing

- Test prompts should sound like a normal user's Chinese request.
- Do not put internal terms such as contracts, schemas, checkpoints, or framework stages into the user's prompt.
- When a real run fails, first check whether the runtime or tool path blocked normal work before adding new constraints.
- Keep fixes reusable; do not create a task-specific template just to pass one run.
""",
        "compact.md": """# Compact And Continuation

- Runtime compact is normal long-task flow, not an error.
- After compact, restore from compact handoff first, then task/run state, then raw archive if needed.
- Allow repeated compact as long as the agent made real progress after the previous compact.
- Stop only the self-loop where compact immediately triggers another compact without any useful work.
""",
        "subagents.md": """# Subagents And Collaboration

- Subagents inherit the owner/main-agent memory entry points; they do not create a separate user identity.
- A subagent should keep its own run state, compact package, artifacts, and final summary under its task/agent workspace.
- Parent agents should inspect structured status and artifact refs, not guess from prose only.
- Collaboration should collect responses until the deadline, record missing responders, and continue instead of waiting forever.
""",
        "artifacts.md": """# Artifact Delivery

- The artifact registry is the delivery truth; model text paths are hints until registered.
- Delivery failures should produce clear rework hints and avoid blocking normal research loops.
- Do not add a new hard gate for every quality miss. Prefer evidence, guidance, and reusable closeout checks.
- Generated files should be validated through generic readers or declared artifact metadata where possible.
""",
        "open-world.md": """# Open World Concepts

- File formats, artifact kinds, protocols, and MIME types grow over time.
- Built-in maps are useful hints but cannot be the only authority.
- Prefer explicit user/config/contract declarations first, known mappings second, and auditable defaults third.
- Unknown does not automatically mean forbidden; it means ask for or infer enough metadata to proceed safely.
""",
    }


__all__ = [
    "default_memory_hot_md",
    "default_memory_lessons",
    "default_memory_md",
    "default_memory_route_index_md",
]
