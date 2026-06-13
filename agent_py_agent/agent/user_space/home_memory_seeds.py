
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
related_terms: hot memory, memory hot
when_to_read: Always read this first when reasoning about durable project behavior.
authority_path: memory-hot.md
inject_mode: summary
scope: owner
priority: 100
stale_check: review when project rules change""",
    """## lessons.real-tests
topic: real my-agent task testing prompt style
trigger_keywords: 真实测试, 真实LLM, 普通中文, 提示词, 不要专业术语
related_terms: real task prompt, live task
when_to_read: Read before creating or debugging real my-agent task tests.
authority_path: memory/lessons/real-tests.md
inject_mode: summary
scope: owner
priority: 90
stale_check: review after major runtime changes""",
    """## lessons.compact
topic: long task compact and continuation
trigger_keywords: compact, 上下文压缩, 续接, 长任务, 多轮压缩
related_terms: compaction, resume after compact
when_to_read: Read when changing or debugging context compaction and continuation.
authority_path: memory/lessons/compact.md
inject_mode: summary
scope: owner
priority: 85
stale_check: review after memory runtime changes""",
    """## lessons.subagents
topic: subagent delegation and collaboration
trigger_keywords: 子代理, 孙代理, 协作, 派工, 多代理, agent tree
related_terms: subagent collaboration, delegation
when_to_read: Read when changing delegation, collaboration, or agent-tree behavior.
authority_path: memory/lessons/subagents.md
inject_mode: summary
scope: owner
priority: 85
stale_check: review after subagent runtime changes""",
    """## lessons.artifacts
topic: artifact delivery and closeout behavior
trigger_keywords: 产物, closeout, 验收, 交付, xlsx, pdf, word
related_terms: delivery quality, artifact validation
when_to_read: Read when changing delivery, artifact registry, or closeout behavior.
authority_path: memory/lessons/artifacts.md
inject_mode: summary
scope: owner
priority: 80
stale_check: review after tool or delivery changes""",
    """## lessons.open-world
topic: open-world concepts and auditable defaults
trigger_keywords: 开放世界, 文件格式, 产物类型, MIME, 协议, 后缀
related_terms: open world, extensible formats
when_to_read: Read before adding mappings for file types, artifact kinds, protocols, or MIME types.
authority_path: memory/lessons/open-world.md
inject_mode: summary
scope: owner
priority: 80
stale_check: review when new hard-coded maps are added""",
    """## lessons.workspace
topic: task workspace directory usage
trigger_keywords: 输出目录, 交付目录, 输入目录, output_dir, 旧报告, 整理, 落盘
related_terms: workspace layout, delivery directory, input root
when_to_read: Read when unsure where to write deliverables or how to treat input directories.
authority_path: memory/lessons/workspace.md
inject_mode: summary
scope: owner
priority: 75
stale_check: review after workspace runtime changes""",
    """## lessons.research
topic: research and retrieval completeness before absolute conclusions
trigger_keywords: 检索, 搜索, 调研, 论文, 找不到, 不存在, 最新, 榜单, 数据源
related_terms: search completeness, retrieval, not found, latest publications
when_to_read: Read before concluding that something does not exist or cannot be found.
authority_path: memory/lessons/research.md
inject_mode: summary
scope: owner
priority: 88
stale_check: review after retrieval tooling changes""",
)


def default_memory_lessons() -> dict[str, str]:
    return dict(_DEFAULT_LESSONS)


_DEFAULT_LESSONS: dict[str, str] = {
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
- Data integrity beats quantity: when a required data point cannot be obtained, mark it as missing with the reason. NEVER fill it with estimated/interpolated/extrapolated numbers to satisfy a file-count or row-count requirement — fabricated-looking numbers are worse than an honest gap, even when the method is disclosed.
- Every delivered data value should trace back to a real retrieval or computation in this run; if you cannot point to where a number came from, do not deliver it as fact.
""",
        "open-world.md": """# Open World Concepts

- File formats, artifact kinds, protocols, and MIME types grow over time.
- Built-in maps are useful hints but cannot be the only authority.
- Prefer explicit user/config/contract declarations first, known mappings second, and auditable defaults third.
- Unknown does not automatically mean forbidden; it means ask for or infer enough metadata to proceed safely.
""",
        # 稳而不管减负(2026-06-12):原 workspace 注入段的 13 行教学文案收编至此,
        # 按需召回而非每轮灌输(每条都有历史实锤:R4 输入目录当交付目录等)。
        "workspace.md": """# Task Workspace Usage

- The directory the user asks you to read/analyze/scan is INPUT, never the default delivery directory — even if it contains an `output/` folder.
- Old reports inside input directories are leads only; unless the user explicitly says to reuse them, re-read current sources and produce this round's artifacts into this round's output_dir.
- output_dir doubles as the shared artifact area during collaboration; before closing, keep only user-facing deliverables there and move drafts/logs/per-agent partials to work_dir (or index them in the final report).
- task_root/output_dir/work_dir locate this task's artifacts and process files; they are NOT the base for the user's relative input paths.
- Pure chat answers need no files; do not force-write artifacts for a conversational request.
""",
        # R5c/R6c/R7c 三轮实锤 + R7 跨产品对照归因:窄字段检索返回的"小而全"结果集
        # 是假全集(author 字段 6 篇 vs 全字段 2200+),对照产品靠多渠道并行+宽字段
        # 时间倒序命中目标。教训通用化为检索完备性纪律,零站点专项。
        "research.md": """# 检索完备性纪律（下"不存在/找不到"结论之前必读）

- 窄口径检索（作者字段/精确名/单一来源）结果少不等于不存在——那只是窄镜头。小结果集看起来权威，实际是假全集。
- 下任何"不存在/找不到"结论前，必须用最宽口径（全文/全字段）按日期倒序复核，并翻看前几页，不能只看第一屏。
- **署名形态是多样的，绝不能用单一字段一票否决**：机构的成果可能以机构名、团队名、或成员个人名义署名。作者字段搜机构名为 0，不能推出"该机构没发过"——还要看：页面隶属信息（affiliation）、官方代码仓库/官网是否引用该成果、内容与该机构产品的强关联。已经打开过的疑似页面要按这些证据综合判定，不要因为"作者列表里没有机构名"就直接排除。
- **按产品线枚举搜索**，不要只搜机构名：机构发布物通常以产品/系列命名（如 <机构>-OCR、<机构>-Coder、<机构>-Prover、<机构>-VL）。先从其代码托管组织页列出已知产品线，再逐一作为关键词检索；只搜机构主名会漏掉以产品命名的成果。
- **平台不提供历史数据 ≠ 历史数据不存在**：平台官方接口往往只给当前快照，但事件流归档、公共数据集镜像、第三方分析服务通常保有历史。试着搜"<平台名> historical data / events archive / public dataset"找到事件级归档或分析站点，再自行聚合出所需口径。
- 机构成果分布在多个独立渠道：官网、代码托管组织页、模型/论文聚合页、全文搜索引擎。下绝对结论前至少试过两个独立渠道。
- 某个渠道被网络环境拦住时，必须在报告里明说，并补一个独立渠道作为补偿——不能因此降低证据标准。
- 最终报告里记录试过什么、明知没试什么（tried_channels / untried_channels_known）。
""",
    }


__all__ = [
    "default_memory_hot_md",
    "default_memory_lessons",
    "default_memory_md",
    "default_memory_route_index_md",
]
