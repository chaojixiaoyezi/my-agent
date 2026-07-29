
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

from ..capability.persona_repository import PersonaRepository, PersonaRepositoryError
from ..common import agent_time
from ..memory_store import MemoryRecord
from ..settings import AgentConfig
from .memory_context import memory_context_text

_BUILTIN_PROMPT_PREFIX = "builtin:"
_LEGACY_DEFAULT_PROMPT = "prompts/default.md"


@dataclass
class ToolSections:
    """Bundle for PromptBuilder.build tool-related parameters."""

    tool_catalog_section: str = ""
    tool_recommendations_section: str = ""
    tool_context: list[str] | None = None
    execution_facts_section: str = ""
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
    workspace_context_override: str | None = None


@dataclass(frozen=True)
class _PromptBuildFields:
    user_prompt: str
    memories: list[MemoryRecord] | None
    inject: list[str] | None
    prompt_files: list[str] | None
    tools: ToolSections | None
    system_prompt_override: str | None
    context_scope: str
    workspace_context_override: str | None


class PromptBuilder:
    """负责构造每一轮发给模型的完整 prompt。"""

    def __init__(
        self,
        config: AgentConfig,
        root: Path,
        home_paths: Any | None = None,
        workspace_root: Path | None = None,
        persona_repository: PersonaRepository | None = None,
    ):
        self.config = config
        self.root = root
        self.home_paths = home_paths
        self.persona_repository = persona_repository
        if (
            self.persona_repository is None
            and home_paths is not None
            and any(
                getattr(home_paths, attr, None)
                for attr in ("owner_agents_md", "owner_soul_md", "owner_user_md")
            )
        ):
            self.persona_repository = PersonaRepository.from_home_paths(home_paths)
        # root 仍只负责解析 prompt 文件；workspace_root 是模型相对路径和写入落点的唯一事实。
        self.workspace_root = workspace_root or root

    def read_prompt_files(self, extra_files: list[str] | None = None, *, include_config: bool = True, scope: str = "default") -> list[str]:
        """读取动态 prompt 文件并拼接内容。

        scope="isolated" 时跳过项目级 prompt 文件，只读取 caller 显式传入的 extra_files。"""

        chunks: list[str] = []
        skip_project_files = _is_isolated_scope(scope)
        configured = [] if (not include_config or skip_project_files) else self.config.prompt_files
        for name in [*configured, *(extra_files or [])]:
            chunk = _read_prompt_file(self.root, str(name))
            if chunk is not None:
                chunks.append(chunk)
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
        workspace_context_override: str | None = None,
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
                workspace_context_override,
            ),
        )
        _tools = request.tools or ToolSections()
        system_prompt = request.system_prompt_override or self.config.system_prompt
        task_local = _is_task_local_context(request.context_scope)
        memory_text = _memory_text([] if task_local else request.memories)
        owner_scope = _owner_scope_text(self)
        dynamic = _dynamic_prompt_text(self, request, task_local)
        injected = "\n".join(request.inject or [])
        workspace_context = (
            _workspace_context_text(self)
            if request.workspace_context_override is None
            else request.workspace_context_override
        )
        task_and_transcript = _task_and_transcript_section(
            self.config, request.user_prompt, _transcript_tool_context(_tools)
        )
        default_tools = "# Tools\n（当前未启用工具）"
        default_recommendations = "# Recommended Tools\n（当前无候选工具详情）"
        return (
            f"# System\n{system_prompt}\n\n"
            f"# Related Memory\n{memory_text}\n\n"
            f"# Owner Scope\n{owner_scope}\n\n"
            f"# Dynamic Prompt Files\n{dynamic or '（无）'}\n\n"
            f"# Workspace Context\n{workspace_context}\n\n"
            f"# Runtime Injection\n{injected or '（无）'}\n\n"
            f"{_tools.tool_catalog_section or default_tools}\n\n"
            f"{_tools.tool_recommendations_section or default_recommendations}\n\n"
            f"{task_and_transcript}\n\n"
            f"{_tools.execution_facts_section}\n"
        )

    def read_home_context(self, user_prompt: str) -> list[str]:
        if not self.home_paths or not bool(getattr(self.config, "home_context_enabled", True)):
            return []
        chunks = _home_entry_context_chunks(self.home_paths, self.persona_repository)
        chunks.extend(
            _matching_lesson_chunks(
                self.home_paths,
                user_prompt,
                _lesson_limit(self.config),
                stale_days=float(getattr(self.config, "home_lesson_stale_caveat_days", _LESSON_STALE_DAYS) or 0),
            )
        )
        return chunks

    def snapshot_workspace_context(self) -> str:
        """Freeze date/time and workspace facts for one model turn."""

        return _workspace_context_text(self)


def _resolve_prompt_file(root: Path, name: str) -> tuple[Path, str, bool]:
    """解析 prompt 来源；builtin: 始终指向随安装包发布的资源，不依赖服务 cwd。"""
    text = str(name or "").strip()
    if text.startswith(_BUILTIN_PROMPT_PREFIX):
        relative = _safe_builtin_prompt_path(text.removeprefix(_BUILTIN_PROMPT_PREFIX))
        return _builtin_prompt_root() / relative, text, True
    path = Path(text).expanduser()
    if path.is_absolute():
        return path, str(path), False
    workspace_path = root / path
    # 兼容老配置：历史默认值 prompts/default.md 在部署 cwd 下找不到时，迁移到同一内置事实源。
    if text == _LEGACY_DEFAULT_PROMPT and not workspace_path.is_file():
        return _builtin_prompt_root() / _LEGACY_DEFAULT_PROMPT, f"builtin:{_LEGACY_DEFAULT_PROMPT}", True
    return workspace_path, str(workspace_path), False


def _read_prompt_file(root: Path, name: str) -> str | None:
    path, source, required = _resolve_prompt_file(root, name)
    if path.is_file():
        return f"# Prompt File: {source}\n" + path.read_text(encoding="utf-8")
    if required:
        raise FileNotFoundError(f"内置 prompt 资源不存在: {source} ({path})")
    return None


def _safe_builtin_prompt_path(value: str) -> Path:
    path = Path(str(value or "").strip())
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"无效的内置 prompt 路径: {value}")
    return path


def _builtin_prompt_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
        args.workspace_context_override,
    )


def _memory_text(memories: list[MemoryRecord]) -> str:
    return memory_context_text(memories)


def _owner_scope_text(builder: PromptBuilder) -> str:
    """Describe the already-resolved owner boundary without exposing its identifier."""

    owner_kind = str(getattr(getattr(builder, "home_paths", None), "owner_kind", "") or "main")
    if owner_kind == "group":
        return (
            "当前资料边界是这个群的共享空间。这里的长期偏好、历史记忆、任务和成果属于整个群，"
            "群成员可在同一群内共同使用；它们不等于当前发言成员的私人资料。"
            "除非有结构化的写入者证据，否则不要把群组事实说成是当前成员本人曾经写入或说过。"
            "这个群会话绝不能直接访问任何成员的私人空间或其他群的空间；"
            "成员要共享私人内容时，必须把内容复制、上传或通过受控分享进入当前群的共享空间。"
        )
    if owner_kind == "user":
        return (
            "当前资料边界是这个用户的私人空间。这里的长期偏好、历史记忆、任务和成果只属于当前用户；"
            "绝不能读取、引用或推断其他用户或群聊的私有信息。"
        )
    return "当前资料边界是本地主空间；只使用这里的长期偏好、历史记忆、任务和成果。"


def _dynamic_prompt_text(builder: PromptBuilder, request: PromptBuildRequest, isolated: bool) -> str:
    chunks = [
        *builder.read_prompt_files(request.prompt_files, include_config=not isolated),
        *([] if isolated else builder.read_home_context(request.user_prompt)),
        *([] if isolated else _skill_context_chunks(builder, request.user_prompt)),
    ]
    return "\n".join(chunks)


# LLM: 对齐 会话运行时 core-skills：主 run 在 2% context 预算内暴露当前逐轮
#   Skill snapshot 的 name+description+stable id，正文仍须模型显式 skill_search
#   get 后才进入上下文。这里不按用户自然语言自动选择/执行 Skill，也不赋权。
# 函数用途: 让模型看见每本可用 Skill 的短卡，匹配后再按 stable id 读取正文；
#   没有合适技能时继续走普通任务。
def _skill_context_chunks(builder: PromptBuilder, user_prompt: str) -> list[str]:
    del user_prompt
    router = getattr(builder, "capability_router", None)
    if router is None:
        return []
    try:
        config = getattr(builder, "config", None)
        index = router.render_skill_metadata_index(
            context_window_tokens=getattr(config, "model_context_window_tokens", 0),
        )
        return [index] if index else []
    except Exception:
        return []


def _workspace_context_text(builder: PromptBuilder) -> str:
    # 兼容只构造了 config/root 的轻量测试替身和第三方调用方；正式 PromptBuilder 始终
    # 显式带 workspace_root，回退只等价于旧行为，不会覆盖远程 owner 的结构化值。
    root = Path(getattr(builder, "workspace_root", builder.root)).resolve()
    # 按用户配置时区渲染(审计 #21):env AGENT_TIMEZONE > config.timezone > 服务器本地;周起始随 locale。
    now = agent_time.now(getattr(builder.config, "timezone", "") or "")
    today = now.date()
    current_week_start = agent_time.week_start_date(today, getattr(builder.config, "week_start", "monday") or "monday")
    current_week_end = current_week_start + timedelta(days=6)
    last_7_days_start = today - timedelta(days=6)
    return "\n".join([
        f"- 当前工具工作目录（仅供执行定位）: {root}",
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
        "不要连续大量读取后才第一次落盘。",
        "- 做多步骤的大任务时，可用 task_progress 把当前运行要做的事记成清单，方便 compact 后恢复；"
        "它不选择会话、不切换工作区，也不会让普通任务自动续跑或阻止下一条用户消息。",
        "- 分析、调查、排查、取证、研究、对比这类有实质发现的任务，得出结论后要把发现、依据和结论写成报告文件交付再收尾，"
        "不能只在对话里口头汇报就算完成；只有纯问答、闲聊、一次性查值这类本就没有交付物的任务，才不必写文件。",
        "- 做研判、监控、排查、取证这类要下结论的分析时，关键判断（如某次攻击是否真得手、某操作是否异常、某数据是否被拖走）"
        "必须跨多个数据源交叉印证后再下结论——例如同时看访问日志、WAF/安全设备是放行(pass)还是拦截(block)、后端应用异常、"
        "数据库审计，不能只凭单一来源（比如只看访问日志的流量大小）就拍板；对每个可疑点要挖到底（看状态码、响应体积、"
        "源头后续动作、有没有对应的批量导出/数据外泄记录），把关键证据挖透、交叉对上了再判，别看一眼就收。",
        "- 做长时间监控/值守类任务时，维护一个状态记录本（记录每个数据源已读到的位置/行数、当前轮次、已确认与待观察的告警台账），"
        "便于持续盯下去、中断后能从记录的位置接着读新增内容，而不是每轮从头重读或分析一轮就散场；以“持续值守、待命续读”的姿态收尾，"
        "而不是一轮看完就判“任务完成”。",
        "- 相对路径默认相对当前工具工作目录。",
        "- 写文件、读文件、创建 artifacts/deliverables 时优先使用这个真实路径。",
        "- 当前工具工作目录、owner/session/thread/request/task 等标识和字段名只用于内部执行；"
        "对用户说明资料归属时，用‘你的私人空间’或‘当前群的共享空间’等普通说法，不复述宿主路径或内部标识。",
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


def _home_entry_context_chunks(
    home_paths: Any,
    persona_repository: PersonaRepository | None = None,
) -> list[str]:
    repository = persona_repository
    if repository is None and home_paths is not None:
        try:
            repository = PersonaRepository.from_home_paths(home_paths)
        except PersonaRepositoryError:
            repository = None
    chunks = _persona_context_chunks(repository)
    entries = (
        ("memory.md", _owner_paths(home_paths, "owner_memory_md")),
        ("memory-hot.md", _owner_paths(home_paths, "owner_memory_hot_md")),
    )
    for label, paths in entries:
        for path in paths:
            chunks.extend(_home_entry_chunk(label, path))
    return chunks


def _persona_context_chunks(repository: PersonaRepository | None) -> list[str]:
    if repository is None:
        return []
    # 长期助手 keeps agent identity and the user profile in explicitly different
    # system-prompt tiers.  Preserve our single Persona repository while making
    # the same semantic boundary unambiguous to the model: SOUL describes the
    # assistant; USER describes the current owner; AGENTS describes their work
    # agreement.  These labels carry no authority and never select an owner.
    labels = {
        "agents": "LONG-TERM WORKING AGREEMENT (how the agent and current user or group work together)",
        "soul": "ASSISTANT PERSONA (who the assistant is and how it speaks)",
        "user": "CURRENT USER OR GROUP PROFILE (stable facts and preferences)",
    }
    chunks: list[str] = []
    diagnostics: list[dict[str, object]] = []
    for target, snapshot in repository.snapshot().items():
        content = _strip_injection_comments(snapshot.content)
        if target == "user":
            content = _strip_empty_markdown_sections(content)
        if content.strip():
            chunks.append(f"# Home Entry: {labels[target]}\n{content}")
        diagnostic = snapshot.diagnostic
        if diagnostic.state not in {"ok", "missing"}:
            diagnostics.append(diagnostic.to_dict())
    if diagnostics:
        chunks.append(
            "# Persona Load Diagnostics\n"
            + "\n".join(
                f"- {row['target']}: state={row['state']}, truncated={row['truncated']}, "
                f"blocked_lines={row['blocked_lines']}, detail={row['detail'] or '-'}"
                for row in diagnostics
            )
        )
    return chunks


def _home_entry_chunk(label: str, path: Path) -> list[str]:
    content = _read_text_if_nonempty(path)
    if not content:
        return []
    content = _strip_injection_comments(content)
    if not content.strip():
        return []
    # owner 的真实宿主路径只用于结构化文件访问，不是模型需要告诉用户的知识。
    # prompt 仅保留稳定逻辑标签，避免普通回复复述服务器目录布局。
    return [f"# Home Entry: {label}\n{content}"]


def _strip_injection_comments(text: str) -> str:
    """注入系统提示词前剥离 HTML 注释(对标 终端应用:<!-- --> 给人看、注入时隐藏、Read 时可见)。
    让人格/记忆模板里的引导注释零 token——模板可带丰富填写提示,却不占每轮上下文。"""
    import re

    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)  # 注释删掉后收敛多余空行
    return stripped.strip("\n")


def _strip_empty_markdown_sections(text: str) -> str:
    """Hide empty H2 template sections from model context without mutating source files."""

    lines = text.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("## "):
            kept.append(line)
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        section_body = lines[index + 1 : end]
        if any(item.strip() for item in section_body):
            kept.extend(lines[index:end])
        index = end
    return "\n".join(kept).strip("\n")


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
            chunks.append(
                f"# Home Lesson: memory/lessons/{path.name}\n"
                f"{content}{_lesson_age_caveat(path, now, stale_days)}"
            )
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
