# LLM: 本模块从宿主字段构造 prompt；原生动态来源和诊断正文共用布局，目录取名仅为共享软纪律，不能按标题裁决状态。
# 模块用途: 为主/子代理组织模型输入；文本协议保留原格式，原生协议让未变化状态留在已缓存历史里。
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
from ..task_progress_guidance import (
    task_progress_closeout_guidance_enabled,
    task_progress_model_discipline,
)
from .cache_layout import CacheStructuredPrompt
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

    # LLM: Every root and delegated model turn must receive the same evidence boundary. Native
    # prompts carry source-keyed volatile sections so changes do not duplicate unrelated facts.
    # 函数用途: 拼出完整模型输入；原生工具协议同时标出各动态字段来源，未变化分段由 IR 留在原位置。
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
        if home_guide := _home_directory_guide(self):
            owner_scope += "\n\n" + home_guide
        dynamic = _dynamic_prompt_text(self, request, task_local)
        injected = "\n".join(request.inject or [])
        workspace_context = (
            _workspace_context_text(self)
            if request.workspace_context_override is None
            else request.workspace_context_override
        )
        default_tools = "# Tools\n（当前未启用工具）"
        default_recommendations = "# Recommended Tools\n（当前无候选工具详情）"
        if _tools.native_tool_use:
            return CacheStructuredPrompt(
                _native_cache_stable_prefix(
                    system_prompt=system_prompt,
                    owner_scope=owner_scope,
                    dynamic=dynamic,
                    tool_catalog=_tools.tool_catalog_section or default_tools,
                ),
                volatile_sections=_native_cache_volatile_sections(
                    memory_text=memory_text,
                    tool_recommendations=(
                        _tools.tool_recommendations_section or default_recommendations
                    ),
                    workspace_context=workspace_context,
                    injected=injected,
                    execution_facts=_tools.execution_facts_section,
                ),
                canonical_user_turn=f"# User Task\n{request.user_prompt}",
            )
        task_and_transcript = _task_and_transcript_section(
            self.config, request.user_prompt, _transcript_tool_context(_tools)
        )
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


# LLM: Stable native content is restricted to run-invariant instructions, owner scope, prompt files,
# persona/skill metadata, and the textual tool catalog. Request memory, clocks and history stay out.
# 函数用途: 组装原生模型请求可跨轮复用的固定前缀，供供应商缓存断点使用。
def _native_cache_stable_prefix(
    *,
    system_prompt: str,
    owner_scope: str,
    dynamic: str,
    tool_catalog: str,
) -> str:
    return (
        f"# System\n{system_prompt}\n\n"
        f"# Owner Scope\n{owner_scope}\n\n"
        f"# Dynamic Prompt Files\n{dynamic or '（无）'}\n\n"
        f"{tool_catalog}"
    )


# LLM: 来源来自宿主已知字段，不解析 Markdown；改变一个分段只追加该段，旧 IR 的字节和顺序保持。
# 函数用途: 分开记忆、推荐、工作区、运行注入和执行事实，同时保留原完整正文的拼接顺序与格式。
def _native_cache_volatile_sections(
    *,
    memory_text: str,
    tool_recommendations: str,
    workspace_context: str,
    injected: str,
    execution_facts: str,
) -> tuple[tuple[str, str], ...]:
    return (
        ("prompt.related_memory", f"# Related Memory\n{memory_text}"),
        ("prompt.tool_recommendations", tool_recommendations),
        ("prompt.workspace", f"# Workspace Context\n{workspace_context}"),
        ("prompt.runtime_injection", f"# Runtime Injection\n{injected or '（无）'}"),
        ("prompt.execution", f"{execution_facts}\n"),
    )


# LLM: This projection must describe the exact ToolRegistry cwd. Permission roots are
# not placement hints: 会话运行时 exposes one turn cwd separately from its sandbox profile,
# so a broader owner wall must never compete with the cwd as the default destination.
# 函数用途: 把冻结的工作区提示更新为本轮真实 cwd，并明确“权限范围不等于默认落点”。
def project_runtime_workspace_context(
    snapshot: str,
    *,
    effective_cwd: str = "",
    allowed_write_roots: list[str] | tuple[str, ...] = (),
    task_output_dir: str = "",
    task_work_dir: str = "",
) -> str:
    """投影本轮真实 cwd/权限根；时间保持冻结，运行归档不改变文件路径。"""

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
                    projected.append(
                        f"- 权限允许写入目录（不代表默认落点）: {', '.join(roots)}"
                    )
                projected.append(relative_line)
                projected.append(
                    "- 文件工具和 shell 使用同一个 cwd；用户指定的普通相对路径直接按 cwd 解析。"
                )
                projected.append(
                    "- 文件整理遵循家目录约定；新工作先自选合适目录，旧工作沿用原目录。"
                    "这只是整理纪律，不是权限限制；目录名不会改变工具参数或任务身份。"
                )
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


# LLM: This prose guides behavior only. Permission is decided by the structured owner/full-access
# profile; never parse a user sentence or this text to grant a path.
# 函数用途: 用大白话告诉模型默认在哪工作、何时才应离开管理员自己的目录，以及跨用户时要少改动。
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
            "可在当前群的 owner home 内处理项目；外部网络不受 WorkspaceOnly 文件边界影响。"
        )
    if owner_kind == "user":
        return (
            "当前资料边界是这个用户的私人空间。这里的长期偏好、历史记忆、任务和成果只属于当前用户；"
            "绝不能读取、引用或推断其他用户或群聊的私有信息。"
            "可在当前用户的 owner home 内自由处理自己的项目；外部网络不受 WorkspaceOnly 文件边界影响。"
        )
    return (
        "当前资料边界是本地管理员自己的 owner home，默认始终在这里工作；不要把启动 TUI 时的进程目录"
        "当成任务目录。只有 Full Access 已由宿主开启，并且用户明确指定外部路径或明确要求排查系统问题时，"
        "才离开自己的 owner home。涉及其他用户目录时也必须有用户明确要求；无需额外反问授权，但默认优先"
        "只读，只修改用户明确要求的范围，并尽量少改。WorkspaceOnly 只限制本机文件范围，不限制外部网络。"
    )


# LLM: 目录用途与语义取名同属稳定 prefix，main/child 共用且受 home_context_enabled 控制；不扫描目录、不调用模型、不迁移旧文件。
# 函数用途: 引导助手用目标摘要给业务目录起名并续作旧成果；用户可覆盖整理偏好，但名称不产生权限或任务身份。
def _home_directory_guide(builder: PromptBuilder) -> str:
    if not bool(getattr(builder.config, "home_context_enabled", True)):
        return ""
    home = getattr(builder.home_paths, "owner_home_dir", None)
    if home is None:
        return ""
    template = str(getattr(builder.config, "workspace_task_path_template", "tasks/{date}/{task_slug}"))
    return (
        f"# 家目录与整理约定\n- 你的家（owner home）: {home}\n"
        "- 家是主代理和普通子代理共同的文件工作区；tasks 里的不同目录不是权限隔离区。\n"
        f"- 新的文件工作建议放在 {template}；date 是开始日期，task_slug 是简短可读名称。"
        "脚本、代码、PPT、报告等小事大事都就近整理，不把文件散落在家根目录。\n"
        "- 新建前先理解真正要解决的事情，用用户语言概括‘主题或对象＋成果或动作’作为 task_slug，"
        "例如‘家庭物品清单’‘季度合同条款核对’；名称要让用户过一个月仍能辨认。"
        "不要截取用户原话的开头，不带问候、请求语气、整段要求、代理职责或运行编号。"
        "用户明确给了目录名则沿用，不替用户擅自润色改名。\n"
        "- 用户说上次那个或继续改时，先查会话/记忆索引和相关文件找到原目录，在原处续做。"
        "不要因新回合、日期变化或任务完成另建副本，也不用判断长期项目/短期任务状态。"
        "目标不同时可新建目录；用户明确指定位置或组织方式时优先照办。\n"
        "- 创建前只检查相关候选目录：同一件事沿用原目录；不同工作重名时加简短范围说明，"
        "不覆盖已有成果、不全量扫描家目录，也不为美化名称搬动旧项目。"
        "需要长期找回时在现有记忆或简短目录索引保留主题与实际路径，不重复复制项目。\n"
        "- 一个工作目录按需用 inputs/ 放来源材料，src/ 或实际项目结构放代码，output/ 放成品，"
        "tmp/ 放可重建中间文件；不必为没有内容的分类创建空目录。\n"
        "- 借用已有项目验证新工具时，先在当前工作的 tmp/ 下准备样本副本；"
        "不要为了制造增删改案例修改原项目。若用户要求保留原件，交付前用实际文件核对，而不是口头假定未改。\n"
        "- 上传文件先看工具给出的真实引用，保留原件与来源；有明确归属时在对应工作里整理，"
        "暂时没有归属的资料可按日期/主题放 inputs/。不要搬动通道的内部存储或随意清理旧成果。\n"
        "- artifacts/ 可放用户明确希望收藏的成品，skills/ 放可复用技能；需要时建立简短索引，"
        "不要为了整理重复复制整份项目。workspace/ 是已有工作空间，继续尊重里面现有结构。\n"
        "- SOUL.md 是人格，修改须用户确认；USER.md 是用户画像，AGENTS.md 是长期约定，"
        "均通过人格工具维护。memory.md、memory-hot.md 和 memory/ 属于记忆系统，"
        "通过记忆工具检索/维护，不拿运行日志代替长期记忆，也不把普通任务进度写进人格。\n"
        "- runs/、agents/、compact/、data/、logs/ 及权限配置是宿主控制记录，不是项目输出目录；"
        "不要编辑它们绕过运行时。目录用途只是软纪律，实际读写仍服从当前用户权限。"
    )


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
    progress_guidance = (
        task_progress_model_discipline()
        if task_progress_closeout_guidance_enabled(builder)
        else (
            "task_progress 是模型可选的当前运行清单；它不选择会话、不切换工作区，"
            "也不会让普通任务自动续跑或阻止下一条用户消息。"
        )
    )
    guidance = [
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
        f"- {progress_guidance}",
        "- 分析、调查、排查、取证、研究、对比这类有实质发现的任务，得出结论后通常要把发现、依据和结论写成报告文件交付再收尾，"
        "不能只在对话里口头汇报就算完成；但用户明确要求只读、不要修改、不要落盘或只在对话中回答时，必须服从本轮要求，"
        "不能创建报告文件，也不能把写报告文件列入 task_progress。只有纯问答、闲聊、一次性查值这类本就没有交付物的任务，才不必写文件。",
        "- 当前工具工作目录、owner/session/thread/request/task 等标识和字段名只用于内部执行；"
        "对用户说明资料归属时，用‘你的私人空间’或‘当前群的共享空间’等普通说法，不复述宿主路径或内部标识。",
        "- 如果用户要求派工或任务材料很多，先读 README/目录/评分标准等最小必要线索；"
        "把正文路径放进子代理任务的 input_refs/context_manifest，交给对应子代理读取分析。",
        "- 除非用户明确要求主代理亲自验收正文，否则不要在派工前把所有长文档、数据表或产物正文都读进 root 上下文。",
    ]
    return "\n".join(guidance)


def _transcript_tool_context(tools: ToolSections) -> list[str]:
    # native 下工具往返由原生 messages 携带，prompt 旁路 tool_context 文本（不双份重复）。
    return [] if tools.native_tool_use else (tools.tool_context or [])


# LLM: 工具历史之后的尾部指令是慢/长上下文模型最后看到的执行边界；必须明确说明
# 无工具正文会结束 active turn，但不能解析模型正文、读取 Todo 来替模型裁决完成。
# 函数用途: 把用户任务、已执行工具记录和“继续还是最终回复”的模型纪律拼成最后一段。
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
        "直接使用已有结果进入下一步，或在工作确已完成、没有下一项动作时给出最终答案。"
        "注意：一条不含工具调用的助手正文会立即结束当前 active turn；"
        "只要你仍准备检查、生成、修改、验证或汇总，就必须在这一轮同时调用对应工具，"
        "不能只写‘接下来会继续’之类的进度句后停下。"
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
