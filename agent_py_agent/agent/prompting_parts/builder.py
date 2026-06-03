
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import AgentConfig
from ..memory import MemoryRecord


@dataclass
class ToolSections:
    """Bundle for PromptBuilder.build tool-related parameters."""

    tool_catalog_section: str = ""
    tool_recommendations_section: str = ""
    tool_context: list[str] | None = None


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
class _PromptBuildCompatArgs:
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

        这版和旧版最大的区别是把工具信息拆成了两层：
        - `tool_catalog_section`：常驻的工具目录，告诉模型'你手里有什么工具'
        - `tool_recommendations_section`：按当前任务筛出来的少量候选详情，告诉模型'这次大概率该用谁'"""

        request = _prompt_build_request(
            request,
            _PromptBuildCompatArgs(
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
        task_and_transcript = _task_and_transcript_section(request.user_prompt, _tools.tool_context or [])
        default_tools = "# Tools\n（当前未启用工具）"
        default_recommendations = "# Recommended Tools\n（当前无候选工具详情）"
        return (
            f"# System\n{system_prompt}\n\n"
            f"# Related Memory\n{memory_text}\n\n"
            f"# Dynamic Prompt Files\n{dynamic or '（无）'}\n\n"
            f"# Runtime Injection\n{injected or '（无）'}\n\n"
            f"# Workspace Context\n{workspace_context}\n\n"
            f"{_tools.tool_catalog_section or default_tools}\n\n"
            f"{_tools.tool_recommendations_section or default_recommendations}\n\n"
            f"{task_and_transcript}\n"
        )

    def read_home_context(self, user_prompt: str) -> list[str]:
        if not self.home_paths or not bool(getattr(self.config, "home_context_enabled", True)):
            return []
        chunks = _home_entry_context_chunks(self.home_paths)
        chunks.extend(_matching_lesson_chunks(self.home_paths, user_prompt, _lesson_limit(self.config)))
        return chunks


def _prompt_build_request(
    request: PromptBuildRequest | None,
    args: _PromptBuildCompatArgs,
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
        "不要因为旧记忆说以前做过某事，就把本轮新任务改成旧任务。"
    )
    rendered = "\n".join(f"- [{m.kind}] {m.role}: {m.content}" for m in memories)
    return f"{guidance}\n{rendered}"


def _dynamic_prompt_text(builder: PromptBuilder, request: PromptBuildRequest, isolated: bool) -> str:
    chunks = [
        *builder.read_prompt_files(request.prompt_files, include_config=not isolated),
        *([] if isolated else builder.read_home_context(request.user_prompt)),
    ]
    return "\n".join(chunks)


def _workspace_context_text(builder: PromptBuilder) -> str:
    root = Path(builder.root).resolve()
    now = datetime.now().astimezone()
    today = now.date()
    current_week_start = today - timedelta(days=today.weekday())
    current_week_end = current_week_start + timedelta(days=6)
    last_7_days_start = today - timedelta(days=6)
    return "\n".join([
        f"- primary_workspace_root: {root}",
        f"- current_local_date: {today.isoformat()}",
        f"- current_local_year: {today.year}",
        f"- current_local_time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- current_week_range: {current_week_start.isoformat()}..{current_week_end.isoformat()}",
        f"- last_7_days_range: {last_7_days_start.isoformat()}..{today.isoformat()}",
        "- 写报告日期时优先使用 current_local_date，不要从旧文件、旧记忆或训练知识里猜日期。",
        "- 任务里出现“今天、最近、近一周、本周、今年”等相对时间时，先按 current_local_date 换成明确日期范围；"
        "搜索和报告都使用这个明确范围，不能把旧网页年份或训练知识年份当成本轮日期。",
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
        "把正文路径放进子代理任务的 input_refs/context_manifest，交给对应小傻妞读取分析。",
        "- 除非用户明确要求主代理亲自验收正文，否则不要在派工前把所有长文档、数据表或产物正文都读进 root 上下文。",
    ])


def _task_and_transcript_section(user_prompt: str, tool_context: list[str]) -> str:
    tools_history = "\n\n".join(tool_context)
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
        ("AGENTS.md", _owner_and_legacy_paths(home_paths, "owner_agents_md", "agents_md")),
        ("SOUL.md", _owner_and_legacy_paths(home_paths, "owner_soul_md", "soul_md")),
        ("USER.md", _owner_and_legacy_paths(home_paths, "owner_user_md", "user_md")),
        ("memory.md", _owner_and_legacy_paths(home_paths, "owner_memory_md", "memory_md")),
        ("memory-hot.md", _owner_and_legacy_paths(home_paths, "owner_memory_hot_md", "memory_hot_md")),
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


def _owner_and_legacy_paths(home_paths: Any, owner_attr: str, legacy_attr: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    owner_path = Path(getattr(home_paths, owner_attr, "") or "")
    legacy_path = Path(getattr(home_paths, legacy_attr, "") or "")
    for path in (owner_path, legacy_path):
        if path and path not in paths:
            paths.append(path)
    return tuple(paths)


def _matching_lesson_chunks(home_paths: Any, user_prompt: str, limit: int) -> list[str]:
    if limit <= 0:
        return []
    prompt_text = str(user_prompt or "").casefold()
    chunks: list[str] = []
    for path in _matching_lesson_paths(home_paths, prompt_text):
        if len(chunks) >= limit:
            return chunks
        content = _read_text_if_nonempty(path)
        if content:
            chunks.append(f"# Home Lesson: {path}\n{content}")
    return chunks


def _matching_lesson_paths(home_paths: Any, prompt_text: str) -> list[Path]:
    paths: list[Path] = []
    for lessons_dir in _owner_and_legacy_paths(home_paths, "owner_memory_lessons_dir", "memory_lessons_dir"):
        if lessons_dir.exists():
            paths.extend(path for path in sorted(lessons_dir.glob("*.md")) if path.stem.casefold() in prompt_text)
    return paths


def _lesson_limit(config: AgentConfig) -> int:
    return max(0, int(getattr(config, "home_lesson_auto_read_limit", 3) or 0))


def _read_text_if_nonempty(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text
