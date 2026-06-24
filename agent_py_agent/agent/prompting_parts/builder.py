
from __future__ import annotations

"""主智能体使用的 prompt 拼装器。

这个模块的职责很纯粹：
把系统人格、相关记忆、动态规则、工具目录、候选工具详情、用户任务和工具执行记录
按固定顺序拼成模型真正看到的上下文。

顺序设计不是随便排的：
- 前面先放人格和长期规则，保证回答风格稳定
- 中间放记忆和工具信息，帮助模型理解'这次该怎么做'
- 没有工具记录时，最后放用户任务，让模型聚焦当前问题
- 已经有工具记录时，最后放工具记录和继续指令，让模型从最新工具结果往下走
"""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..common import agent_time
from ..memory_store import MemoryRecord
from ..settings import AgentConfig


@dataclass
class ToolSections:
    """Bundle for PromptBuilder.build tool-related parameters."""

    tool_catalog_section: str = ""
    tool_recommendations_section: str = ""
    tool_context: list[str] | None = None
    # native tool_use 下工具往返由原生 messages 携带，prompt 不再折入 tool_context 文本
    # （避免文本+原生双份重复）。text 协议默认 False，行为不变。
    native_tool_use: bool = False


@dataclass
class PromptBuildRequest:
    """bundle for PromptBuilder.build inputs."""

    user_prompt: str
    memories: list[MemoryRecord]
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    tools: ToolSections | None = None
    system_prompt_override: str | None = None
    context_scope: str = "default"


@dataclass(frozen=True)
class _PromptBuildFields:
    user_prompt: str
    memories: list[MemoryRecord] | None
    inject: list[str] | None
    prompt_files: list[str] | None
    tools: ToolSections | None
    system_prompt_override: str | None
    context_scope: str


class PromptBuilder:
    """负责构造每一轮发给模型的完整 prompt。"""

    def __init__(self, config: AgentConfig, root: Path, home_paths: Any | None = None):
        self.config = config
        self.root = root
        self.home_paths = home_paths

    def read_prompt_files(self, extra_files: list[str] | None = None, *, include_config: bool = True, scope: str = "default") -> list[str]:
        """读取动态 prompt 文件并拼接内容。

        scope="isolated" 时跳过项目级 prompt 文件，只读取 caller 显式传入的 extra_files。"""

        chunks: list[str] = []
        skip_project_files = _is_isolated_scope(scope)
        configured = [] if (not include_config or skip_project_files) else self.config.prompt_files
        for name in [*configured, *(extra_files or [])]:
            path = Path(name)
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                chunks.append(f"# Prompt File: {path}\n" + path.read_text(encoding="utf-8"))
        return chunks

    def build(
        self,
        user_prompt: str = "",
        memories: list[MemoryRecord] | None = None,
        *,
        request: PromptBuildRequest | None = None,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        tools: ToolSections | None = None,
        system_prompt_override: str | None = None,
        context_scope: str = "default",
    ) -> str:
        """拼出完整 prompt。

        当前 prompt 把工具信息拆成两层：
        - `tool_catalog_section`：常驻的工具目录，告诉模型'你手里有什么工具'
        - `tool_recommendations_section`：按当前任务筛出来的少量候选详情，告诉模型'这次大概率该用谁'"""

        request = _prompt_build_request(
            request,
            _PromptBuildFields(
                user_prompt,
                memories,
                inject,
                prompt_files,
                tools,
                system_prompt_override,
                context_scope,
            ),
        )
        _tools = request.tools or ToolSections()
        system_prompt = request.system_prompt_override or self.config.system_prompt
        task_local = _is_task_local_context(request.context_scope)
        memory_text = _memory_text([] if task_local else request.memories)
        dynamic = _dynamic_prompt_text(self, request, task_local)
        injected = "\n".join(request.inject or [])
        workspace_context = _workspace_context_text(self)
        task_and_transcript = _task_and_transcript_section(
            self.config, request.user_prompt, _transcript_tool_context(_tools)
        )
        default_tools = "# Tools\n（当前未启用工具）"
        default_recommendations = "# Recommended Tools\n（当前无候选工具详情）"
        return (
            f"# System\n{system_prompt}\n\n"
            f"# Related Memory\n{memory_text}\n\n"
            f"# Dynamic Prompt Files\n{dynamic or '（无）'}\n\n"
            f"# Workspace Context\n{workspace_context}\n\n"
            f"# Runtime Injection\n{injected or '（无）'}\n\n"
            f"{_tools.tool_catalog_section or default_tools}\n\n"
            f"{_tools.tool_recommendations_section or default_recommendations}\n\n"
            f"{task_and_transcript}\n"
        )

    def read_home_context(self, user_prompt: str) -> list[str]:
        if not self.home_paths or not bool(getattr(self.config, "home_context_enabled", True)):
            return []
        chunks = _home_entry_context_chunks(self.home_paths)
        chunks.extend(
            _matching_lesson_chunks(
                self.home_paths,
                user_prompt,
                _lesson_limit(self.config),
                stale_days=float(getattr(self.config, "home_lesson_stale_caveat_days", _LESSON_STALE_DAYS) or 0),
            )
        )
        return chunks


def _prompt_build_request(
    request: PromptBuildRequest | None,
    args: _PromptBuildFields,
) -> PromptBuildRequest:
    return request or PromptBuildRequest(
        args.user_prompt,
        args.memories or [],
        args.inject,
        args.prompt_files,
        args.tools,
        args.system_prompt_override,
        args.context_scope,
    )


def _memory_text(memories: list[MemoryRecord]) -> str:
    if not memories:
        return "（无相关记忆）"
    guidance = (
        "Related Memory 是历史参考，不是当前任务指令。"
        "如果它和 # User Task、当前工作区文件或最新工具结果冲突，必须以后者为准。"
        "不要因为历史记忆说以前做过某事，就把本轮新任务改成历史任务。"
    )
    rendered = "\n".join(f"- [{m.kind}] {m.role}: {m.content}" for m in memories)
    return f"{guidance}\n{rendered}"


def _dynamic_prompt_text(builder: PromptBuilder, request: PromptBuildRequest, isolated: bool) -> str:
    chunks = [
        *builder.read_prompt_files(request.prompt_files, include_config=not isolated),
        *([] if isolated else builder.read_home_context(request.user_prompt)),
        *([] if isolated else _skill_context_chunks(builder, request.user_prompt)),
    ]
    return "\n".join(chunks)


# LLM: skill 树进主 run prompt(断链③修复,R10 实锤:skill 推荐只在派工场景用,
#   主代理任务里模型从没见过技能书架)。稳而不管口径:①类目索引常驻=每类一行,
#   与 skill 总数解耦(千级不膨胀);②具体技能卡只在本轮 query 命中时注入
#   (limit 2,卡片自带正文路径供 read_file 跟进);③这些是知识线索不是流程
#   指令。router 缺席(子代理隔离/异常)时整段缺席,零影响主链路。
# 函数用途: 让模型每轮都知道"有技能书架可查",相关时直接把书递到手边。
# 注卡分数门:长 prompt 全文检索会撞出大量边缘 n-gram 命中(R11 预检实锤:
# 周榜任务对两张无关卡打 8.5-13 分,真命中 49-56 分)。低于此线的卡不注——
# "命中才注"指真命中;边缘相关交给类目索引+skill_search 冷路,不占 prompt。
# 20→16(移植 23 个 builtin 方法论 skill 后重标定):方法论触发是自然口语
# ("测试一直报错""目标还模糊""拆给子代理"),真命中天然低于术语类(实测 16-69,
# 安全/代码类 40-69 更高)。tags 补特异短语(避通用子串"测试/功能/问题"以免边缘
# 膨胀)+收敛后,23/23 方法论真命中 >=16,长边缘真噪声 <=15.5(TDD 真命中 16.0 vs
# 边缘 15.5 精确卡位),普通噪声 <=1.5。16 既让方法论 skill 在真实任务注入又挡边缘。
_SKILL_INJECT_MIN_SCORE = 16.0


def _skill_context_chunks(builder: PromptBuilder, user_prompt: str) -> list[str]:
    router = getattr(builder, "capability_router", None)
    if router is None:
        return []
    try:
        index = router.render_category_index()
        if not index:
            return []
        hits = [
            hit
            for hit in router.search(str(user_prompt or ""), limit=4, kinds={"skill"})
            if hit.score >= _SKILL_INJECT_MIN_SCORE
        ]
        chunks = [index]
        if hits:
            chunks.append(
                "# Matched Skills\n" + "\n".join(hit.card.render_compact() for hit in hits[:3])
            )
        return chunks
    except Exception:
        return []


def _workspace_context_text(builder: PromptBuilder) -> str:
    root = Path(builder.root).resolve()
    # 按用户配置时区渲染(审计 #21):env AGENT_TIMEZONE > config.timezone > 服务器本地;周起始随 locale。
    now = agent_time.now(getattr(builder.config, "timezone", "") or "")
    today = now.date()
    current_week_start = agent_time.week_start_date(today, getattr(builder.config, "week_start", "monday") or "monday")
    current_week_end = current_week_start + timedelta(days=6)
    last_7_days_start = today - timedelta(days=6)
    return "\n".join([
        f"- primary_workspace_root: {root}",
        f"- current_local_date: {today.isoformat()}",
        f"- current_local_year: {today.year}",
        f"- current_local_time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- current_week_range: {current_week_start.isoformat()}..{current_week_end.isoformat()}",
        f"- last_7_days_range: {last_7_days_start.isoformat()}..{today.isoformat()}",
        "- 写报告日期时优先使用 current_local_date，不要从历史文件、历史记忆或训练知识里猜日期。",
        "- 任务里出现“今天、最近、近一周、本周、今年”等相对时间时，先按 current_local_date 换成明确日期范围；"
        "搜索和报告都使用这个明确范围，不能把历史网页年份或训练知识年份当成本轮日期。",
        "- 最终产物里写 URL、项目地址、论文地址、下载地址或接口地址时，优先使用工具结果里真实出现的链接；"
        "如果链接是你从名称推断出来的，先用网页/HTTP 工具验证可访问，不能靠项目名猜仓库地址。",
        "- 做研究、汇总、对比、翻译、审计这类需要引用来源的工作时，给关键结论和表格行保留 source_ref；"
        "搜索片段只能当线索，最终依据优先来自官方页面、原始论文、仓库页面、接口返回或抓取归档。",
        "- 做长任务、长报告、多文件整理或代码生成时，优先把已确认的阶段成果持续写进草稿、目标文件或阶段笔记；"
        "不要连续大量读取后才第一次落盘。用户没有要求文件产物时，不要为了落盘强行写文件。",
        "- 相对路径默认相对 primary_workspace_root。",
        "- 写文件、读文件、创建 artifacts/deliverables 时优先使用这个真实路径。",
        "- 不要把 /workspace 当作真实路径，除非用户明确给了这个绝对目录。",
        "- 如果用户要求派工或任务材料很多，先读 README/目录/评分标准等最小必要线索；"
        "把正文路径放进子代理任务的 input_refs/context_manifest，交给对应子代理读取分析。",
        "- 除非用户明确要求主代理亲自验收正文，否则不要在派工前把所有长文档、数据表或产物正文都读进 root 上下文。",
    ])


def _transcript_tool_context(tools: ToolSections) -> list[str]:
    # native 下工具往返由原生 messages 携带，prompt 旁路 tool_context 文本（不双份重复）。
    return [] if tools.native_tool_use else (tools.tool_context or [])


def _task_and_transcript_section(config: AgentConfig, user_prompt: str, tool_context: list[str]) -> str:
    from ..agent_core.tool_context.microcompact import (
        DEFAULT_MICROCOMPACT_KEEP_RECENT,
        DEFAULT_MICROCOMPACT_MIN_CHARS,
        microcompact_tool_context,
    )

    # 渲染 prompt 时回收窗口外的旧工具结果正文（保留 read_artifact 锚点），省 context；
    # 不改累积的 tool_context 历史本身。keep_recent 配置为 0 表示关闭回收。
    tools_history = "\n\n".join(
        microcompact_tool_context(
            tool_context,
            keep_recent=int(getattr(config, "tool_context_microcompact_keep_recent", DEFAULT_MICROCOMPACT_KEEP_RECENT) or 0),
            min_chars=int(getattr(config, "tool_context_microcompact_min_chars", DEFAULT_MICROCOMPACT_MIN_CHARS) or 0),
        )
    )
    if not tools_history:
        return "# Tool Transcript\n（无）\n\n" f"# User Task\n{user_prompt}"
    return (
        f"# User Task\n{user_prompt}\n\n"
        f"# Tool Transcript\n{tools_history}\n\n"
        "# Continue From Tool Transcript\n"
        "从最新的工具结果继续推进，不要重新开始任务。"
        "如果某个工具调用已经成功，不要重复调用同一个工具和同一组参数；"
        "直接使用已有结果进入下一步，或在证据足够时给出最终答案。"
    )


def _is_task_local_context(value: object) -> bool:
    return str(value or "").strip().lower() in {"task_local", "control_plane"}


def _is_isolated_scope(value: object) -> bool:
    return str(value or "").strip().lower() in {"isolated", "task_local", "control_plane"}


def _home_entry_context_chunks(home_paths: Any) -> list[str]:
    entries = (
        ("AGENTS.md", _owner_paths(home_paths, "owner_agents_md")),
        ("SOUL.md", _owner_paths(home_paths, "owner_soul_md")),
        ("USER.md", _owner_paths(home_paths, "owner_user_md")),
        ("memory.md", _owner_paths(home_paths, "owner_memory_md")),
        ("memory-hot.md", _owner_paths(home_paths, "owner_memory_hot_md")),
    )
    chunks: list[str] = []
    for label, paths in entries:
        for path in paths:
            chunks.extend(_home_entry_chunk(label, path))
    return chunks


def _home_entry_chunk(label: str, path: Path) -> list[str]:
    content = _read_text_if_nonempty(path)
    if not content:
        return []
    return [f"# Home Entry: {label}\nPath: {path}\n{content}"]


def _owner_paths(home_paths: Any, owner_attr: str) -> tuple[Path, ...]:
    owner_path = Path(getattr(home_paths, owner_attr, "") or "")
    return (owner_path,) if owner_path else ()


_LESSON_STALE_DAYS = 7.0


def _lesson_age_caveat(path: Path, now: float, stale_days: float = _LESSON_STALE_DAYS) -> str:
    """召回的 lesson 超过 stale_days 天未更新就加陈旧提示。

    防止把陈旧记忆当现状——记忆反映写入时的事实，与当前代码/状态冲突时应以现状为准。
    stale_days<=0 表示关闭提示（对应配置 home_lesson_stale_caveat_days=0）。
    """
    if stale_days <= 0:
        return ""
    try:
        mtime = float(path.stat().st_mtime)
    except OSError:
        return ""
    age_days = (now - mtime) / 86400.0
    if age_days < stale_days:
        return ""
    return (
        f"\n[memory-age-caveat] 这条记忆约 {int(age_days)} 天未更新，是当时的事实，"
        "可能已过期；与当前代码/状态冲突时以现状为准。"
    )


def _matching_lesson_chunks(
    home_paths: Any,
    user_prompt: str,
    limit: int,
    *,
    stale_days: float = _LESSON_STALE_DAYS,
) -> list[str]:
    if limit <= 0:
        return []
    import time

    now = time.time()
    prompt_text = str(user_prompt or "").casefold()
    chunks: list[str] = []
    for path in _matching_lesson_paths(home_paths, prompt_text):
        if len(chunks) >= limit:
            return chunks
        content = _read_text_if_nonempty(path)
        if content:
            chunks.append(f"# Home Lesson: {path}\n{content}{_lesson_age_caveat(path, now, stale_days)}")
    return chunks


# LLM: lesson 召回的匹配权威(批 2 断链①修复,R10 实锤:旧算法要求英文文件名
#   作为子串出现在用户 prompt 里——中文任务 prompt 永远零命中,种了 lessons
#   从未被看到)。新算法:读 owner 路由索引(memory/routing/INDEX.md,播种时
#   每个 lesson 段都带中文 trigger_keywords)——任一关键词命中 prompt 即召回该
#   段 authority_path 指向的 lesson。"一个概念一个权威位置":索引就是召回路由,
#   匹配函数终于读它。stem 匹配保留为并集兜底而非回退分支——索引是加速器
#   不是封闭白名单,未登记进索引的手写 lesson 仍可按文件名命中(开放世界)。
# 函数用途: 按用户这句话的内容,从经验笔记里挑出真正相关的几篇。
def _matching_lesson_paths(home_paths: Any, prompt_text: str) -> list[Path]:
    candidates: list[Path] = []
    for lessons_dir in _owner_paths(home_paths, "owner_memory_lessons_dir"):
        if lessons_dir.exists():
            candidates += _routing_index_matches(lessons_dir, prompt_text)
            candidates += _stem_matches(lessons_dir, prompt_text)
    return [path for path in dict.fromkeys(candidates) if path.is_file()]


# 函数用途: 中文模糊召回——needle(lesson 文件名/触发词)去分隔符拆 3-gram,与 prompt 有 >= min_grams 个
#   公共 3-gram(≥4 字连续重叠)即算相关。补"完全子串匹配"对中文词序差异/部分提及召回不到的洞(R7 头号
#   短板:检索偏窄)。用绝对公共 gram 数而非占比——长文件名只要其中一段关键词出现在 prompt 就召回,
#   又因要 ≥4 字连续重叠而控噪不滥召。
def _ngram_hit(needle: str, prompt_text: str, *, min_grams: int = 2) -> bool:
    s = needle.casefold().replace("-", "").replace("_", "").replace(" ", "")
    if len(s) < 3:
        return s in prompt_text  # 短词回退完全子串
    grams = [s[idx:idx + 3] for idx in range(len(s) - 2)]
    hits = sum(1 for gram in grams if gram in prompt_text)
    if hits >= min(min_grams, len(grams)):
        return True
    # Phase 2 增量补召(只增不减):3-gram 对中文词序差异/部分提及会漏,用检索子系统的 CJK-bigram
    # 词元重叠兜底。加严控噪:needle 与 prompt 的 bigram 词元交集 >= 2 且 needle 本身 >= 2 个 bigram。
    from agent_py_agent.agent.retrieval.lexical import tokenize

    needle_grams = {t for t in tokenize(needle) if len(t) >= 2}
    if len(needle_grams) >= 2:
        prompt_grams = {t for t in tokenize(prompt_text) if len(t) >= 2}
        if len(needle_grams & prompt_grams) >= 2:
            return True
    return False


# 函数用途: stem 兜底——文件名直接出现 或 中文 3-gram 模糊命中 prompt 的 lesson。
def _stem_matches(lessons_dir: Path, prompt_text: str) -> list[Path]:
    return [
        path
        for path in sorted(lessons_dir.glob("*.md"))
        if path.stem.casefold() in prompt_text or _ngram_hit(path.stem, prompt_text)
    ]


# 函数用途: 解析路由索引(lessons 上级 memory/routing/INDEX.md)的各段,
#   trigger_keywords 任一命中 prompt 即返回该段 authority_path 对应的 lesson 文件。
def _routing_index_matches(lessons_dir: Path, prompt_text: str) -> list[Path]:
    index_path = lessons_dir.parent / "routing" / "INDEX.md"
    text = _read_text_if_nonempty(index_path)
    if not text:
        return []
    matches: list[Path] = []
    for section in text.split("\n## ")[1:]:
        keywords = _index_field(section, "trigger_keywords")
        authority = _index_field(section, "authority_path")
        if not keywords or not authority:
            continue
        terms = [term.strip().casefold() for term in keywords.split(",") if term.strip()]
        if not any(term and (term in prompt_text or _ngram_hit(term, prompt_text)) for term in terms):
            continue
        candidate = (lessons_dir.parent.parent / authority).resolve(strict=False)
        if candidate.suffix == ".md" and "lessons" in candidate.parts:
            matches.append(candidate)
    return matches


# 函数用途: 从索引段里取一个"key: value"字段的值(没有返回空串)。
def _index_field(section: str, key: str) -> str:
    for line in section.splitlines():
        if line.strip().startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def _lesson_limit(config: AgentConfig) -> int:
    return max(0, int(getattr(config, "home_lesson_auto_read_limit", 3) or 0))


def _read_text_if_nonempty(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text
