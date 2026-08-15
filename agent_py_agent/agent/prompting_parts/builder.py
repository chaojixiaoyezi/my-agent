
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
from typing import TYPE_CHECKING, Any

from ..capability.persona_repository import PersonaRepository, PersonaRepositoryError
from ..common import agent_time
from ..settings import AgentConfig
from .memory_context import memory_context_text

if TYPE_CHECKING:
    # 循环导入根修: builder 被 prompting_parts/__init__ 顶层加载,而 memory_store/__init__
    # → retention_apply → conversation 链又回 prompting_parts。MemoryRecord 只在类型注解用
    # (__future__ annotations 延迟求值),运行时无需解析 → TYPE_CHECKING 下 import 打破回环。
    from ..memory_store import MemoryRecord

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
        del user_prompt
        return _persona_home_context_chunks(self.home_paths, self.persona_repository)

    def snapshot_workspace_context(self, *, facts_only: bool = False) -> str:
        """Freeze date/time and workspace facts for one model turn."""

        return _workspace_context_text(self, facts_only=facts_only)


def project_runtime_workspace_context(
    snapshot: str,
    *,
    effective_cwd: str = "",
    allowed_write_roots: list[str] | tuple[str, ...] = (),
    task_output_dir: str = "",
    task_work_dir: str = "",
    task_workspace_pending: bool = False,
) -> str:
    """Project the live Tool Gateway cwd/write roots into a frozen turn snapshot.

    Wall-clock facts remain frozen for prompt-cache stability, while the execution
    workspace may legitimately change once an ordinary conversation is promoted to
    a task.  The values here are host-authored runtime facts; no model text or user
    wording participates in choosing a path.
    """

    lines = str(snapshot or "").splitlines()
    root_prefix = "- 当前工具工作目录（仅供执行定位）:"
    relative_line = "- 相对路径默认相对当前工具工作目录。"
    generic_write_line = "- 写文件、读文件、创建 artifacts/deliverables 时优先使用这个真实路径。"

    if effective_cwd:
        projected: list[str] = []
        inserted = False
        roots = list(dict.fromkeys(str(item).strip() for item in allowed_write_roots if str(item).strip()))
        for line in lines:
            if line.startswith(root_prefix):
                projected.append(f"{root_prefix} {effective_cwd}")
                if task_output_dir:
                    projected.append(f"- task_output_dir: {task_output_dir}（最终交付物）")
                if task_work_dir:
                    projected.append(f"- task_work_dir: {task_work_dir}（过程文件）")
                if roots:
                    projected.append(f"- 当前允许写入目录: {', '.join(roots)}")
                projected.append(relative_line)
                projected.append("- 文件工具和 shell 使用同一个任务目录；交付写 output/，过程文件写 work/。")
                inserted = True
                continue
            if line in {relative_line, generic_write_line}:
                continue
            projected.append(line)
        if not inserted:
            projected = [
                f"{root_prefix} {effective_cwd}",
                *projected,
            ]
        return "\n".join(projected)

    if task_workspace_pending:
        pending = (
            "- 当前会话尚未建立任务写入目录；首次工作工具调用会由程序建立。"
            "写入参数使用相对的 output/...（交付）或 work/...（过程），"
            "不要把上面的宿主工作目录拼成绝对写路径。"
        )
        if pending not in lines:
            lines.append(pending)
    return "\n".join(lines)


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


def _workspace_context_text(
    builder: PromptBuilder,
    *,
    facts_only: bool = False,
) -> str:
    # 兼容只构造了 config/root 的轻量测试替身和第三方调用方；正式 PromptBuilder 始终
    # 显式带 workspace_root，回退只等价于旧行为，不会覆盖远程 owner 的结构化值。
    root = Path(getattr(builder, "workspace_root", builder.root)).resolve()
    # 按用户配置时区渲染(审计 #21):env AGENT_TIMEZONE > config.timezone > 服务器本地;周起始随 locale。
    now = agent_time.now(getattr(builder.config, "timezone", "") or "")
    today = now.date()
    current_week_start = agent_time.week_start_date(today, getattr(builder.config, "week_start", "monday") or "monday")
    current_week_end = current_week_start + timedelta(days=6)
    last_7_days_start = today - timedelta(days=6)
    facts = [
        f"- 当前工具工作目录（仅供执行定位）: {root}",
        f"- current_local_date: {today.isoformat()}",
        f"- current_local_year: {today.year}",
        f"- current_local_time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- current_week_range: {current_week_start.isoformat()}..{current_week_end.isoformat()}",
        f"- last_7_days_range: {last_7_days_start.isoformat()}..{today.isoformat()}",
        "- 相对路径默认相对当前工具工作目录。",
        "- 写文件、读文件、创建 artifacts/deliverables 时优先使用这个真实路径。",
        "- 不要把 /workspace 当作真实路径，除非用户明确给了这个绝对目录。",
    ]
    if facts_only:
        return "\n".join(facts)
    return "\n".join([
        *facts,
        "- 写报告日期时优先使用 current_local_date，不要从历史文件、历史记忆或训练知识里猜日期。",
        "- 任务里出现“今天、最近、近一周、本周、今年”等相对时间时，先按 current_local_date 换成明确日期范围；"
        "搜索和报告都使用这个明确范围，不能把历史网页年份或训练知识年份当成本轮日期。",
        "- 最终产物里写 URL、项目地址、论文地址、下载地址或接口地址时，优先使用工具结果里真实出现的链接；"
        "如果链接是你从名称推断出来的，先用网页/HTTP 工具验证可访问，不能靠项目名猜仓库地址。",
        "- 做研究、汇总、对比、翻译、审计这类需要引用来源的工作时，给关键结论和表格行保留 source_ref；"
        "搜索片段只能当线索，最终依据优先来自官方页面、原始论文、仓库页面、接口返回或抓取归档。",
        "- 有明确交付文件的长任务，直接按实际进展逐步更新目标文件；只有跨 compact 后确实需要恢复下一步时，"
        "才按需使用 task_progress 或草稿。不要为了形式单独建立检查点，也不要把内部记录动作反复当作用户进度回复。",
        "- task_progress 是模型可选的当前运行清单；它不选择会话、不切换工作区，"
        "也不会让普通任务自动续跑或阻止下一条用户消息。",
        "- 分析、调查、排查、取证、研究、对比这类有实质发现的任务，得出结论后要把发现、依据和结论写成报告文件交付再收尾，"
        "不能只在对话里口头汇报就算完成；只有纯问答、闲聊、一次性查值这类本就没有交付物的任务，才不必写文件。",
        "- 当前工具工作目录、owner/session/thread/request/task 等标识和字段名只用于内部执行；"
        "对用户说明资料归属时，用‘你的私人空间’或‘当前群的共享空间’等普通说法，不复述宿主路径或内部标识。",
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


# LLM: PromptBuilder 的 Home 层只读取 Persona；Memory/HOT/lesson 必须由 runtime recall 进入唯一信封。
# 函数用途: 返回当前 owner 的 SOUL、USER、AGENTS 分层上下文。
def _persona_home_context_chunks(
    home_paths: Any,
    persona_repository: PersonaRepository | None = None,
) -> list[str]:
    repository = persona_repository
    if repository is None and home_paths is not None:
        try:
            repository = PersonaRepository.from_home_paths(home_paths)
        except PersonaRepositoryError:
            repository = None
    return _persona_context_chunks(repository)


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
