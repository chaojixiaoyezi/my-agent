# LLM: Prompt-building module; keep assembled prompt sections and file-loading behavior stable.
# 模块用途: 构造系统提示、工具说明、任务上下文和会话片段。

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
from pathlib import Path
from typing import Any

from ..config import AgentConfig
from ..memory import MemoryRecord


# LLM: ToolSections is a Prompt 构造 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: Bundle for PromptBuilder.build tool-related parameters.
@dataclass
class ToolSections:
    """Bundle for PromptBuilder.build tool-related parameters."""

    tool_catalog_section: str = ""
    tool_recommendations_section: str = ""
    tool_context: list[str] | None = None


# LLM: PromptBuildRequest is a Prompt 构造 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 保存 PromptBuildRequest 的输入字段，调用方先构造这个对象再进入 Prompt 构造，避免继续散传参数。
@dataclass
class PromptBuildRequest:
    """bundle for PromptBuilder.build inputs."""

    user_prompt: str
    memories: list[MemoryRecord]
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    tools: ToolSections | None = None
    system_prompt_override: str | None = None


# LLM: PromptBuilder is a Prompt 构造 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 负责构造每一轮发给模型的完整 prompt。
class PromptBuilder:
    """负责构造每一轮发给模型的完整 prompt。"""

    # LLM: PromptBuilder.__init__ belongs to Prompt 构造; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 PromptBuilder 的方法，通常依赖实例字段。
    def __init__(self, config: AgentConfig, root: Path, home_paths: Any | None = None):
        self.config = config
        self.root = root
        self.home_paths = home_paths

    # LLM: PromptBuilder.read_prompt_files belongs to Prompt 构造; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 读取动态 prompt 文件并拼接内容。 大白话解释： 这些文件相当于'额外行为规则'，只要被读进来，这一轮模型就真的能看到。。
    def read_prompt_files(self, extra_files: list[str] | None = None) -> list[str]:
        """读取动态 prompt 文件并拼接内容。

        大白话解释：
        这些文件相当于'额外行为规则'，只要被读进来，这一轮模型就真的能看到。"""

        chunks: list[str] = []
        for name in [*self.config.prompt_files, *(extra_files or [])]:
            path = Path(name)
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                chunks.append(f"# Prompt File: {path}\n" + path.read_text(encoding="utf-8"))
        return chunks

    # LLM: PromptBuilder.build belongs to Prompt 构造; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 拼出完整 prompt。 这版和旧版最大的区别是把工具信息拆成了两层： - `tool_catalog_section`：常驻的工具目录，告诉模型'你手里有什么工具' - `tool_recommendations_section`：按当前任务筛出来的少量候选详情，告诉模型'这次。
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
    ) -> str:
        """拼出完整 prompt。

        这版和旧版最大的区别是把工具信息拆成了两层：
        - `tool_catalog_section`：常驻的工具目录，告诉模型'你手里有什么工具'
        - `tool_recommendations_section`：按当前任务筛出来的少量候选详情，告诉模型'这次大概率该用谁'"""

        request = request or PromptBuildRequest(
            user_prompt,
            memories or [],
            inject,
            prompt_files,
            tools,
            system_prompt_override,
        )
        _tools = request.tools or ToolSections()
        system_prompt = request.system_prompt_override or self.config.system_prompt
        memory_text = "\n".join(
            f"- [{m.kind}] {m.role}: {m.content}" for m in request.memories
        ) or "（无相关记忆）"
        dynamic = "\n".join([*self.read_prompt_files(request.prompt_files), *self.read_home_context(request.user_prompt)])
        injected = "\n".join(request.inject or [])
        task_and_transcript = _task_and_transcript_section(request.user_prompt, _tools.tool_context or [])
        default_tools = "# Tools\n（当前未启用工具）"
        default_recommendations = "# Recommended Tools\n（当前无候选工具详情）"
        return (
            f"# System\n{system_prompt}\n\n"
            f"# Related Memory\n{memory_text}\n\n"
            f"# Dynamic Prompt Files\n{dynamic or '（无）'}\n\n"
            f"# Runtime Injection\n{injected or '（无）'}\n\n"
            f"{_tools.tool_catalog_section or default_tools}\n\n"
            f"{_tools.tool_recommendations_section or default_recommendations}\n\n"
            f"{task_and_transcript}\n"
        )

    # LLM: read_home_context injects owner entry files every round, then only matching lesson files.
    # 函数用途: 每轮读取家目录关键入口文件，并按任务匹配少量 lesson 文件作为运行时动态上下文。
    def read_home_context(self, user_prompt: str) -> list[str]:
        if not self.home_paths or not bool(getattr(self.config, "home_context_enabled", True)):
            return []
        chunks = _home_entry_context_chunks(self.home_paths)
        chunks.extend(_matching_lesson_chunks(self.home_paths, user_prompt, _lesson_limit(self.config)))
        return chunks


# LLM: _task_and_transcript_section belongs to Prompt 构造; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 完成 Prompt 构造 里的 _task_and_transcript_section 步骤，保持现有返回值、异常和副作用语义。
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


# LLM: _home_entry_context_chunks loads stable owner entry files with AGENTS.md first as the boot contract.
# 函数用途: 读取 SOUL/USER/AGENTS/memory 四个家目录关键文件；文件为空或不存在时跳过。
def _home_entry_context_chunks(home_paths: Any) -> list[str]:
    entries = (
        ("AGENTS.md", Path(home_paths.agents_md)),
        ("SOUL.md", Path(home_paths.soul_md)),
        ("USER.md", Path(home_paths.user_md)),
        ("memory.md", Path(home_paths.memory_md)),
    )
    chunks: list[str] = []
    for label, path in entries:
        content = _read_text_if_nonempty(path)
        if content:
            chunks.append(f"# Home Entry: {label}\nPath: {path}\n{content}")
    return chunks


# LLM: _matching_lesson_chunks uses simple filename matching until semantic lesson routing is added.
# 函数用途: 按 lesson 文件名和当前任务文本匹配少量教训文件，避免每轮全量读取。
def _matching_lesson_chunks(home_paths: Any, user_prompt: str, limit: int) -> list[str]:
    if limit <= 0:
        return []
    lessons_dir = Path(home_paths.memory_lessons_dir)
    if not lessons_dir.exists():
        return []
    prompt_text = str(user_prompt or "").casefold()
    chunks: list[str] = []
    for path in sorted(lessons_dir.glob("*.md")):
        if len(chunks) >= limit:
            break
        if path.stem.casefold() not in prompt_text:
            continue
        content = _read_text_if_nonempty(path)
        if content:
            chunks.append(f"# Home Lesson: {path}\n{content}")
    return chunks


# LLM: _lesson_limit centralizes config coercion for home lesson reads.
# 函数用途: 获取每轮 prompt 最多自动读取多少个 lesson 文件。
def _lesson_limit(config: AgentConfig) -> int:
    return max(0, int(getattr(config, "home_lesson_auto_read_limit", 3) or 0))


# LLM: _read_text_if_nonempty is a tolerant prompt-context reader for user-owned markdown files.
# 函数用途: 安全读取 UTF-8 文本，文件缺失、权限或编码问题时返回空字符串。
def _read_text_if_nonempty(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text
