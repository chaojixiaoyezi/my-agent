# LLM: 本模块是 TuiViewSnapshot 到 prompt_toolkit formatted lines 的唯一 block renderer；不得读取原始模型文本猜工具、权限或生命周期。
# 模块用途: 生成 终端交互 同构的欢迎卡、用户/助手/思考/工具块、权限面板、队列和底部提示。

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wcwidth import wcswidth

from .tui_markdown import (
    FormattedLine,
    Fragment,
    MarkdownRenderContext,
    display_width_fragments,
    display_width_text,
    fragments_text,
    render_markdown,
    sanitize_terminal_text,
    wrap_fragments,
)
from .tui_view_model import (
    TuiBlock,
    TuiContextUsage,
    TuiPendingSteer,
    TuiPermissionOverlay,
    TuiQueuedInput,
    TuiViewSnapshot,
)

WELCOME_CARD_MAX_WIDTH = 95
TOOL_PREVIEW_MAX_LINES = 6
TOOL_RICH_PREVIEW_MAX_LINES = 12
TOOL_RICH_EXPANDED_MAX_LINES = 40
WRITE_PREVIEW_MAX_LINES = 10
USER_MESSAGE_MAX_CHARS = 10_000
USER_MESSAGE_HEAD_CHARS = 2_500
# R2-1: 模型/工具输出洪水(如 seq 5000)若全量渲染会拖垮 TUI——渲染层截断,
# 只裁 UI 投影, canonical block.text 原文保留(与 _bounded_user_text 同思路)。
_ASSISTANT_RENDER_MAX_LINES = 200
_ASSISTANT_RENDER_DETAIL_MAX_LINES = 2_000
# 活动思考内容的默认可见行数上限（终端交互 行为：灰色实时可见；
# 超长折叠为提示行，Ctrl+O 看全部）。
_THINKING_LIVE_MAX_LINES = 50
_TOOL_RENDER_MAX_LINES = 200
_TOOL_RENDER_DETAIL_MAX_LINES = 2_000
USER_MESSAGE_TAIL_CHARS = 2_500
SPINNER_METRICS_AFTER_SECONDS = 30.0
SPINNER_STALL_AFTER_SECONDS = 3.0
PENDING_INPUT_PREVIEW_LINE_LIMIT = 3
TERMINAL_RENDER_PHASES = frozenset({"completed", "failed", "interrupted"})
SPINNER_GLYPHS = ("✻", "✢", "✶")
SPINNER_WORDS = (
    "Working",
    "Wrangling",
    "Vibing",
    "Sprouting",
    "Sautéing",
    "Brewing",
    "Thinking",
)
SPINNER_TIPS = (
    "Start with a small task, ask my-agent for a plan, then verify its edits",
    "Use /status to inspect the current session before a long-running task",
    "Press Esc to interrupt the active turn without closing this session",
)
HELP_SHORTCUT_GROUPS = (
    (
        "/ for commands",
        "@ for file paths",
        "/btw for side question",
    ),
    (
        "double tap esc to clear input",
        "ctrl + o for detailed transcript",
        "ctrl + r to search prompts",
        "meta + enter for newline",
    ),
    (
        "ctrl + s to stash prompt",
        "page up/down to scroll",
        "ctrl + l to redraw",
        "ctrl + c/d twice to exit",
    ),
)
TERMINAL_BUNNY_AVATAR = (
    (
        ("class:tui-avatar-hair", "╭╮          ╭╮"),
        ("", "       "),
        ("class:tui-avatar-umbrella", "╱╲"),
    ),
    (
        ("class:tui-avatar-hair", "│╰╮        ╭╯│"),
        ("", "      "),
        ("class:tui-avatar-umbrella", "╱╱ "),
    ),
    (
        ("class:tui-avatar-hair", "╰╮╰─"),
        ("class:tui-avatar-ribbon", "◆"),
        ("class:tui-avatar-hair", "────"),
        ("class:tui-avatar-ribbon", "◆"),
        ("class:tui-avatar-hair", "─╯╭╯"),
        ("", "     "),
        ("class:tui-avatar-umbrella", "╱╱  "),
    ),
    (
        ("class:tui-avatar-hair", " ╰╮  "),
        ("class:tui-avatar-face", "◕  ◕"),
        ("class:tui-avatar-hair", "  ╭╯"),
        ("", "    "),
        ("class:tui-avatar-umbrella", "╭─╯   "),
    ),
    (
        ("class:tui-avatar-hair", "  │"),
        ("class:tui-avatar-face", "   ᴗ    "),
        ("class:tui-avatar-hair", "│"),
        ("", "     "),
        ("class:tui-avatar-umbrella", "│     "),
    ),
    (
        ("class:tui-avatar-dress", "╭─┴╮  ♡  ╭─┴╮"),
        ("", "    "),
        ("class:tui-avatar-umbrella", "│     "),
    ),
    (
        ("class:tui-avatar-dress", "╰╮ ╰┬────┬╯ ╭╯"),
        ("", "    "),
        ("class:tui-avatar-umbrella", "│    "),
    ),
    (
        ("class:tui-avatar-bunny", "ᘏ⑅ᘏ"),
        ("", " "),
        ("class:tui-avatar-dress", "╰──╯    ╰──╯"),
        ("", "  "),
        ("class:tui-avatar-umbrella", "╯    "),
    ),
)
TERMINAL_BUNNY_AVATAR_COMPACT = (
    (("class:tui-avatar-hair", "╭╮    ╭╮"),),
    (
        ("class:tui-avatar-hair", "╰"),
        ("class:tui-avatar-ribbon", "◆────◆"),
        ("class:tui-avatar-hair", "╯"),
    ),
    (("class:tui-avatar-face", " (◕  ◕) "),),
    (("class:tui-avatar-face", "  ( ᴗ )  "),),
    (("class:tui-avatar-dress", "  ╱♡╲  "),),
    (
        ("class:tui-avatar-bunny", "ᘏ⑅ᘏ"),
        ("", "  "),
        ("class:tui-avatar-umbrella", "╱╲"),
    ),
)


# LLM: TuiRenderContext 固定所有会影响 golden 的显示输入，时钟/尺寸/品牌都不能从全局隐式读取。
# 类用途: 指定一次 TUI 渲染的宽度、产品映射、详细模式和动画帧。
@dataclass(frozen=True)
class TuiRenderContext:
    width: int
    agent_name: str = "my-agent"
    version: str = "0.3.0"
    model_name: str = ""
    workspace: str = ""
    detailed_transcript: bool = False
    show_all: bool = False
    spinner_index: int = 0
    now: float = 0.0
    status_started_at: float = 0.0
    status_last_event_at: float = 0.0
    context_tokens: int = 0
    context_usage: TuiContextUsage | None = None
    output_tokens: int = 0
    has_active_tools: bool = False
    notice: str = ""
    has_stash: bool = False
    is_pasting: bool = False
    help_open: bool = False

    # LLM: width 最小一列，名称字段只做展示字符串规范，不获得路径或配置控制权。
    # 函数用途: 规范渲染上下文，保证动画索引非负。
    def __post_init__(self) -> None:
        object.__setattr__(self, "width", max(1, int(self.width or 1)))
        object.__setattr__(self, "agent_name", str(self.agent_name or "my-agent"))
        object.__setattr__(self, "version", str(self.version or ""))
        object.__setattr__(self, "model_name", str(self.model_name or ""))
        object.__setattr__(self, "workspace", str(self.workspace or ""))
        object.__setattr__(self, "spinner_index", max(0, int(self.spinner_index or 0)))
        object.__setattr__(self, "now", max(0.0, float(self.now or 0.0)))
        object.__setattr__(
            self,
            "status_started_at",
            max(0.0, float(self.status_started_at or 0.0)),
        )
        object.__setattr__(
            self,
            "status_last_event_at",
            max(0.0, float(self.status_last_event_at or 0.0)),
        )
        object.__setattr__(self, "context_tokens", max(0, int(self.context_tokens or 0)))
        if not isinstance(self.context_usage, TuiContextUsage):
            object.__setattr__(self, "context_usage", None)
        object.__setattr__(self, "output_tokens", max(0, int(self.output_tokens or 0)))
        object.__setattr__(self, "has_active_tools", bool(self.has_active_tools))
        object.__setattr__(self, "notice", str(self.notice or ""))
        object.__setattr__(self, "has_stash", bool(self.has_stash))
        object.__setattr__(self, "is_pasting", bool(self.is_pasting))
        object.__setattr__(self, "help_open", bool(self.help_open))


# LLM: frame provider 必须复用 renderer 的可见上下文规则；wall clock 仅在活动 connection/thinking/background/compact 真正改变画面时进入 key。
# 函数用途: 生成一次完整 TUI 画面所需的上下文缓存键，避免空闲长会话被动画时钟反复重绘。
def tui_render_context_key(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
) -> tuple[Any, ...]:
    connection_animation = (
        context.spinner_index % len(SPINNER_GLYPHS)
        if any(block.role == "connection" for block in snapshot.active_blocks)
        else None
    )
    active_activity = tuple(
        block
        for block in snapshot.active_blocks
        if block.role in {"thinking", "background"}
        and block.phase not in TERMINAL_RENDER_PHASES
    )
    visible_activity = _visible_activity_blocks(snapshot, active_activity)
    thinking_animation = tuple(
        (
            block.block_id,
            _thinking_animation_key(block, context)
            if block.role == "thinking"
            else _background_animation_key(block, context),
        )
        for block in visible_activity
    )
    compact_animation = tuple(
        (
            block.block_id,
            context.spinner_index % len(SPINNER_GLYPHS),
            block.updated_seq,
        )
        for block in snapshot.active_blocks
        if block.role == "compact"
    )
    return (
        context.width,
        context.agent_name,
        context.version,
        context.model_name,
        context.workspace,
        context.detailed_transcript,
        context.show_all,
        context.context_tokens,
        context.context_usage,
        context.output_tokens,
        context.has_active_tools,
        context.notice,
        context.has_stash,
        context.is_pasting,
        context.help_open,
        connection_animation,
        thinking_animation,
        compact_animation,
    )


# LLM: TuiRenderFrame 分离滚动 transcript、固定 permission overlay 和 footer，布局层不得从行文案反推区域。
# 类用途: 返回一次可直接交给 prompt_toolkit controls 的不可变画面。
@dataclass(frozen=True)
class TuiRenderFrame:
    transcript_lines: tuple[FormattedLine, ...]
    overlay_lines: tuple[FormattedLine, ...]
    input_status_lines: tuple[FormattedLine, ...]
    footer: FormattedLine


# LLM: TuiRenderCacheStats 是只读诊断，不参与渲染决策或业务状态。
# 类用途: 暴露 block cache 命中、未命中和当前条目数，供压测验收。
@dataclass(frozen=True)
class TuiRenderCacheStats:
    hits: int
    misses: int
    entries: int


# LLM: 流式活动块（phase=delta/active thinking）每帧 seq 都变，渲染 miss 后若每次都全量
# 重渲染，万字级正文在 show_all 展开/滚动时每帧数十毫秒拖垮事件循环。短时间窗内复用
# 最近一次渲染，增量 0.15s 后自然追上，稳定块不受影响。
# 函数用途: 判断 block 是否处于逐帧更新的流式活动状态。
_LIVE_BLOCK_RENDER_THROTTLE_SECONDS = 0.15


def _is_live_stream_block(block: TuiBlock) -> bool:
    if block.phase == "delta":
        return True
    return block.role in {"thinking", "compact"} and block.phase not in TERMINAL_RENDER_PHASES


# LLM: TuiBlockRenderCache 只缓存 immutable block 输出；key 含 updated_seq/宽度/显示模式，绝不跨语义版本复用。
# 类用途: 避免流式活动块更新时重新渲染全部稳定历史，并把缓存限制在显式上限内。
class TuiBlockRenderCache:
    # LLM: max_entries 是 UI 内存边界，裁剪只影响重渲染性能，不改变 transcript 事实。
    # 函数用途: 创建一个有界 LRU block cache。
    def __init__(self, *, max_entries: int = 20_000) -> None:
        self.max_entries = max(1, int(max_entries or 1))
        self._entries: OrderedDict[tuple[Any, ...], tuple[FormattedLine, ...]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._live_last_key: dict[str, tuple[Any, ...]] = {}
        self._live_last_at: dict[str, float] = {}

    # LLM: render 使用 block identity/update 与必要 context 字段；稳定块不被 spinner frame 误伤。
    # 函数用途: 返回缓存或新渲染的单 block 行；流式活动块在节流窗内复用最近一次渲染。
    def render(self, block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
        key = _block_cache_key(block, context)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            self._hits += 1
            return cached
        if context.now is not None and _is_live_stream_block(block):
            last_key = self._live_last_key.get(block.block_id)
            if last_key is not None and context.now - self._live_last_at.get(block.block_id, 0.0) < _LIVE_BLOCK_RENDER_THROTTLE_SECONDS:
                stale = self._entries.get(last_key)
                if stale is not None:
                    # 复用最近一次渲染：流式增量不逐帧全量重渲染（万字块展开时避免卡死）。
                    return stale
        rendered = _render_block(block, context)
        self._entries[key] = rendered
        self._entries.move_to_end(key)
        self._misses += 1
        if _is_live_stream_block(block):
            self._live_last_key[block.block_id] = key
            self._live_last_at[block.block_id] = context.now
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return rendered

    # LLM: stats 只读取计数，调用方不能取得内部 OrderedDict 后篡改缓存。
    # 函数用途: 返回当前缓存诊断快照。
    def stats(self) -> TuiRenderCacheStats:
        return TuiRenderCacheStats(self._hits, self._misses, len(self._entries))


# LLM: render_tui_snapshot 只组合 snapshot 中 typed block/order；active/stable 通过 created_seq 合流但不改写 reducer。
# 函数用途: 渲染完整对话画面、权限覆盖层和状态提示。
def render_tui_snapshot(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
    *,
    cache: TuiBlockRenderCache | None = None,
) -> TuiRenderFrame:
    lines: list[FormattedLine] = []
    active_activity = tuple(
        block
        for block in snapshot.active_blocks
        if block.role in {"thinking", "background"}
        and block.phase not in TERMINAL_RENDER_PHASES
    )
    active_activity_ids = {block.block_id for block in active_activity}
    visible_activity = _visible_activity_blocks(snapshot, active_activity)
    blocks = sorted(
        (
            *snapshot.stable_blocks,
            *(
                block
                for block in snapshot.active_blocks
                if block.block_id not in active_activity_ids
            ),
        ),
        key=lambda block: (block.created_seq, block.updated_seq, block.block_id),
    )
    for block in blocks:
        rendered = cache.render(block, context) if cache is not None else _render_block(block, context)
        _append_block(lines, rendered)
    for block in visible_activity:
        rendered = cache.render(block, context) if cache is not None else _render_block(block, context)
        _append_block(lines, rendered)
    overlay = _render_permission(snapshot.permission, context) if snapshot.permission else ()
    return sanitize_tui_render_frame(
        TuiRenderFrame(
            tuple(lines),
            tuple(overlay),
            _render_input_status(snapshot, context),
            _render_footer(snapshot, context),
        )
    )


# LLM: This is the final TUI display-security chokepoint. It must preserve style and mouse
# handlers byte-for-byte while removing terminal controls from every visible text fragment.
# The operation is idempotent so transcript/search decorators can safely pass through it again.
# 函数用途: 在画面交给 prompt_toolkit 前统一净化 transcript、覆盖层、输入状态和 footer，确保
# provider、工具、路径和搜索内容都不能向宿主终端注入控制序列。
def sanitize_tui_render_frame(frame: TuiRenderFrame) -> TuiRenderFrame:
    return TuiRenderFrame(
        transcript_lines=tuple(_sanitize_formatted_line(line) for line in frame.transcript_lines),
        overlay_lines=tuple(_sanitize_formatted_line(line) for line in frame.overlay_lines),
        input_status_lines=tuple(
            _sanitize_formatted_line(line) for line in frame.input_status_lines
        ),
        footer=_sanitize_formatted_line(frame.footer),
    )


# LLM: prompt_toolkit fragments may carry a mouse handler after style/text. Keep every tail
# element unchanged; only the visible text field is untrusted terminal data.
# 函数用途: 净化一行所有可见文字，同时保留样式和点击处理器。
def _sanitize_formatted_line(line: FormattedLine) -> FormattedLine:
    sanitized = []
    for fragment in line:
        style, text, *tail = fragment
        sanitized.append((style, sanitize_terminal_text(text), *tail))
    return tuple(sanitized)


# LLM: 终端交互 在权限框和可见 assistant stream 期间隐藏全局 spinner，但不终止 turn activity；判断只看 typed overlay/block。
# 函数用途: 选择本帧应显示在内容底部的活动 spinner 块。
def _visible_activity_blocks(
    snapshot: TuiViewSnapshot,
    activity_blocks: tuple[TuiBlock, ...],
) -> tuple[TuiBlock, ...]:
    if snapshot.permission is not None:
        return ()
    if any(block.role == "compact" for block in snapshot.active_blocks):
        return ()
    has_visible_assistant_stream = any(
        block.role == "assistant"
        and block.phase not in TERMINAL_RENDER_PHASES
        and bool(block.text or block.detail)
        for block in snapshot.active_blocks
    )
    if has_visible_assistant_stream:
        return ()
    foreground = tuple(block for block in activity_blocks if block.role == "thinking")
    return foreground or tuple(block for block in activity_blocks if block.role == "background")


# LLM: block dispatch 只读取 reducer role/kind/phase，未知角色显式降为 system 样式而不透传 payload。
# 函数用途: 选择欢迎、用户、助手、思考、工具或系统块 renderer。
def _render_block(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    if block.kind == "session_started":
        return _render_welcome(block, context)
    if block.role == "connection":
        return _render_connection(block, context)
    if block.role == "background":
        return _render_background_activity(block, context)
    if block.role == "user":
        return _render_user(block, context)
    if block.role == "assistant":
        return _render_assistant(block, context)
    if block.role == "thinking":
        return _render_thinking(block, context)
    if block.role == "compact":
        return _render_compact_progress(block, context)
    if block.role == "tool":
        return _render_tool(block, context)
    if block.role == "todo":
        return _render_todo(block, context)
    if block.kind == "interrupt_notice":
        return _render_interrupt_notice(block, context)
    return _render_system(block, context)


# LLM: connection spinner 的正文来自 typed readiness block，动画帧只控制 glyph；它不能假定 provider 已可用。
# 函数用途: 显示启动期间真实 Gateway 探活状态。
def _render_connection(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    glyph = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
    label = block.text or "Connecting to Gateway"
    return ((
        ("class:tui-spinner", glyph + " "),
        ("class:tui-spinner", label + "…"),
    ),)


# LLM: The count comes only from ConversationThread.active_task_ids. This row
# is deliberately dim and removable, so it signals liveness without becoming
# an assistant claim or permanent transcript entry.
# 函数用途: 显示后台任务仍在运行的 终端交互 风格闪动图标、数量和耗时。
def _render_background_activity(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    glyph = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
    count = max(0, int(block.metadata.get("active_task_count") or 0))
    started_at = float(block.metadata.get("started_at") or 0.0)
    elapsed = max(0, int(context.now - started_at)) if started_at and context.now else 0
    detail = f" · 后台任务 {count} 个 · {elapsed // 60}:{elapsed % 60:02d}"
    return wrap_fragments(
        (
            ("class:tui-spinner-highlight", glyph + " "),
            *_animated_spinner_word("Working", context.spinner_index),
            ("class:tui-thinking", detail),
        ),
        width=context.width,
        continuation_prefix=(("class:tui-thinking", "  "),),
    )


# LLM: 中断提示是 终端交互 的专用 transcript 行，由 typed kind 选择；不能靠正文包含 Interrupted 来套样式。
# 函数用途: 显示已确认的用户中断及下一步提示，不添加普通系统消息菱形前缀。
def _render_interrupt_notice(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    return wrap_fragments(
        (("class:tui-muted", block.text),),
        width=context.width,
    )


# LLM: cache key 不包含不影响该 block 的状态；活动 thinking 才读取 spinner，session 才读取品牌元数据。
# 函数用途: 生成一个精确且可哈希的 block 渲染版本键。
def _block_cache_key(block: TuiBlock, context: TuiRenderContext) -> tuple[Any, ...]:
    key: tuple[Any, ...] = (
        block.block_id,
        block.updated_seq,
        block.phase,
        context.width,
        context.detailed_transcript,
        context.show_all,
    )
    if block.kind == "session_started":
        key += (context.agent_name, context.version, context.model_name, context.workspace)
    if block.role == "connection":
        key += (context.spinner_index % len(SPINNER_GLYPHS),)
    if block.role == "thinking" and block.phase not in {"completed", "failed", "interrupted"}:
        key += _thinking_animation_key(block, context)
    if block.role == "background" and block.phase not in {"completed", "failed", "interrupted"}:
        key += _background_animation_key(block, context)
    if block.role == "compact" and block.phase not in {"completed", "failed", "interrupted"}:
        key += (context.spinner_index % len(SPINNER_GLYPHS),)
    return key


# LLM: thinking 动画周期必须同时覆盖 glyph 与该 block 稳定选中的单词高亮长度；禁止用经验魔数造成跨周期错误复用。
# 函数用途: 返回活动 thinking block 的最小完整可见动画缓存键。
def _thinking_animation_key(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[Any, ...]:
    word = _stable_spinner_choice(block.block_id, SPINNER_WORDS)
    animation_cycle = math.lcm(len(SPINNER_GLYPHS), max(1, len(word)))
    return (
        context.spinner_index % animation_cycle,
        int(_spinner_elapsed_seconds(context)),
        context.output_tokens,
        _spinner_is_stalled(context),
    )


# LLM: Background animation uses its own typed start timestamp and count; it
# must not borrow the completed foreground turn's token/stall metrics.
# 函数用途: 生成后台 Working 块的动画与秒数缓存键。
def _background_animation_key(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[Any, ...]:
    started_at = float(block.metadata.get("started_at") or 0.0)
    elapsed = max(0, int(context.now - started_at)) if started_at and context.now else 0
    return (
        context.spinner_index % math.lcm(len(SPINNER_GLYPHS), len("Working")),
        elapsed,
        int(block.metadata.get("active_task_count") or 0),
    )


# LLM: 欢迎卡保持 终端交互 95 列上限和宽/窄两种结构，品牌/版本/模型/目录使用 my-agent 显式映射。
# 函数用途: 渲染启动欢迎卡和一行使用提示。
def _render_welcome(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    version = str(block.metadata.get("version") or context.version)
    model = str(block.metadata.get("model") or context.model_name or "Model unavailable")
    workspace = str(block.metadata.get("workspace") or context.workspace or ".")
    card_width = min(WELCOME_CARD_MAX_WIDTH, context.width)
    if card_width >= 80:
        lines = _wide_welcome_card(context.agent_name, version, model, workspace, card_width)
    else:
        lines = _narrow_welcome_card(context.agent_name, version, model, workspace, card_width)
    tip_text = "/help shows commands · /status shows current session"
    tip_padding = " " * max(0, context.width - display_width_text("  ↑ " + tip_text))
    lines.extend(
        [
            (),
            (
                ("", "  "),
                ("class:tui-tip-accent", "↑"),
                ("class:tui-muted", " " + tip_text + tip_padding),
            ),
        ]
    )
    return tuple(lines)


# LLM: 宽卡列宽固定由 card_width 推导，所有行都恰好同宽，避免终端 resize 时边框漂移。
# 函数用途: 生成双栏欢迎卡。
def _wide_welcome_card(
    agent_name: str,
    version: str,
    model: str,
    workspace: str,
    card_width: int,
) -> list[FormattedLine]:
    left_width = 49
    right_width = card_width - left_width - 3
    lines = [_welcome_header(agent_name, version, card_width)]
    left_rows: tuple[FormattedLine, ...] = (
        (("class:tui-strong", "Welcome back!"),),
        *TERMINAL_BUNNY_AVATAR,
        (("class:tui-muted", f"{model} · API" if model else "API"),),
        (("class:tui-muted", workspace),),
    )
    right_rows = (
        ("Recent activity", "class:tui-welcome-heading"),
        ("No recent activity", "class:tui-muted"),
        (" " + "─" * max(0, right_width - 2) + " ", "class:tui-accent"),
        ("What's new", "class:tui-welcome-heading"),
        ("Use /help to explore my-agent commands", "class:tui-muted"),
    )
    for row_index, left in enumerate(left_rows):
        right, right_style = right_rows[row_index] if row_index < len(right_rows) else ("", "")
        right_text = right if row_index == 2 else (" " + right if right else "")
        lines.append(
            _welcome_row(
                _fit_welcome_fragments(left, left_width),
                _fit_text(right_text, right_width, "left"),
                right_style,
            )
        )
    lines.append((("class:tui-accent", "╰" + "─" * (card_width - 2) + "╯"),))
    return lines


# LLM: 窄卡不硬挤双栏，保留同一标题/Logo/模型/目录信息并让每行严格落在当前宽度内。
# 函数用途: 生成单栏欢迎卡。
def _narrow_welcome_card(
    agent_name: str,
    version: str,
    model: str,
    workspace: str,
    card_width: int,
) -> list[FormattedLine]:
    inner_width = max(1, card_width - 2)
    lines = [_welcome_header(agent_name, version, card_width)]
    avatar = (
        TERMINAL_BUNNY_AVATAR
        if inner_width >= display_width_fragments(TERMINAL_BUNNY_AVATAR[0])
        else TERMINAL_BUNNY_AVATAR_COMPACT
    )
    rows: tuple[FormattedLine, ...] = (
        (("class:tui-strong", "Welcome back!"),),
        (),
        *avatar,
        (),
        (("class:tui-muted", model),),
        (("class:tui-muted", workspace),),
    )
    for fragments in rows:
        lines.append(
            (
                ("class:tui-accent", "│"),
                *_fit_welcome_fragments(fragments, inner_width),
                ("class:tui-accent", "│"),
            )
        )
    lines.append((("class:tui-accent", "╰" + "─" * inner_width + "╯"),))
    return lines


# LLM: header 只按显示列补边框，版本使用 muted role，过窄时通过 fit 后保持合法边界。
# 函数用途: 渲染欢迎卡顶边标题。
def _welcome_header(agent_name: str, version: str, width: int) -> FormattedLine:
    display_name = "my-agent" if agent_name.replace("-", "").lower() == "myagent" else agent_name
    display_version = version if version.lower().startswith("v") else f"v{version}"
    label = f"{display_name} {display_version}".strip()
    available = max(0, width - 7)
    fitted = _fit_text(label, available, "left").rstrip()
    used = display_width_text("╭─── " + fitted + " ")
    tail = "─" * max(0, width - used - 1) + "╮"
    version_start = fitted.rfind(display_version) if version else -1
    if version_start <= 0:
        return (("class:tui-accent", "╭─── " + fitted + " " + tail),)
    return (
        ("class:tui-accent", "╭─── " + fitted[:version_start]),
        ("class:tui-muted", fitted[version_start:]),
        ("class:tui-accent", " " + tail),
    )


# LLM: 欢迎行接收已经按左栏宽度居中的 styled fragments；边框和右栏 padding 必须维持精确总列宽。
# 函数用途: 生成保留头像多色样式的双栏卡片行。
def _welcome_row(left: FormattedLine, right: str, right_style: str) -> FormattedLine:
    return (
        ("class:tui-accent", "│"),
        *left,
        ("class:tui-welcome-divider", "│"),
        (right_style, right),
        ("class:tui-accent", "│"),
    )


# LLM: 欢迎头像的 styled fragments 只按显示列居中；极窄终端降级为有界纯文本，不能越过卡片边框。
# 函数用途: 把一行彩色终端头像补齐到指定栏宽。
def _fit_welcome_fragments(fragments: FormattedLine, width: int) -> FormattedLine:
    normalized_width = max(0, int(width))
    fragment_width = display_width_fragments(fragments)
    if fragment_width > normalized_width:
        text = _fit_text(fragments_text(fragments), normalized_width, "center")
        return (("class:tui-avatar-hair", text),)
    remaining = normalized_width - fragment_width
    left = remaining // 2
    return (("", " " * left), *fragments, ("", " " * (remaining - left)))


# LLM: 用户正文按 plain fragments 处理，不解析 Markdown；整行背景填到当前宽度并只在首行显示 ❯。
# 函数用途: 渲染一个可换行、多行的用户消息块。
def _render_user(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    first_prefix = (("class:tui-user-marker", "❯ "),)
    continuation = (("class:tui-user-marker", "  "),)
    wrapped = wrap_fragments(
        (("class:tui-user-text", _bounded_user_text(block.text)),),
        width=context.width,
        first_prefix=first_prefix,
        continuation_prefix=continuation,
    )
    return tuple(_fill_line(line, context.width, "class:tui-user-fill") for line in wrapped)


# LLM: assistant marker 与 Markdown 只做视觉组合，Markdown token 不获得 block phase 或业务控制权。
# 函数用途: 渲染带 ● marker 的助手 Markdown 块；工具边界前的过程段默认折叠为摘要。
def _render_assistant(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    content_width = max(1, context.width - 2)
    text = _bounded_render_text(
        block.text,
        max_lines=_ASSISTANT_RENDER_DETAIL_MAX_LINES if context.detailed_transcript else _ASSISTANT_RENDER_MAX_LINES,
    )
    markdown_lines = render_markdown(text, MarkdownRenderContext(width=content_width))
    lines: list[FormattedLine] = []
    first_content = True
    for line in markdown_lines:
        if not fragments_text(line):
            lines.append(())
            continue
        marker = "● " if first_content else "  "
        fold_hint = _is_fold_hint_line(line)
        if (first_content and block.metadata.get("process")) or fold_hint:
            style = "class:tui-muted"
        else:
            style = "class:tui-assistant-marker" if first_content else ""
        if fold_hint:
            # 终端交互 的快捷键提示是整行 dim；只给 marker 上色会让真正的提示文字
            # 继续继承普通 Markdown 正文颜色。
            line = _append_terminal_role(line, "class:tui-muted")
        lines.append(((style, marker), *line))
        first_content = False
    rendered = tuple(lines or [(("class:tui-assistant-marker", "●"),)])
    if (
        block.metadata.get("process")
        and not context.detailed_transcript
        and not context.show_all
    ):
        content = [line for line in rendered if fragments_text(line)]
        if len(content) > 1:
            return (*content[:1], _tool_hidden_line(len(content) - 1, context))
    return rendered


# LLM: thinking 展开/折叠只由 context mode 与 typed phase 决定，正文内容不能触发展开。
# 函数用途: 渲染活动 spinner 或已完成思考摘要/详细正文。
def _render_thinking(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    # 对齐 终端交互：思考统一浅灰（tui-thinking），stalled 不换红；
    # 活动态默认显示内容（实时可见），超长折叠提示展开。
    if block.phase not in {"completed", "failed", "interrupted"}:
        started_at = float(block.metadata.get("started_at") or 0.0)
        elapsed = max(0, int(context.now - started_at)) if started_at and context.now else 0
        word = _stable_spinner_choice(block.block_id, SPINNER_WORDS)
        glyph = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
        title = f"… {elapsed // 60}:{elapsed % 60:02d}"
        suffix = _spinner_status_suffix(block, context)
        activity_lines = wrap_fragments(
            (
                ("class:tui-spinner-highlight", glyph + " "),
                *_animated_spinner_word(word, context.spinner_index),
                ("class:tui-thinking", title),
                ("class:tui-thinking", suffix),
            ),
            width=context.width,
            continuation_prefix=(("class:tui-thinking", "  "),),
        )
        lines: list[FormattedLine] = list(activity_lines)
        if block.text:
            # 活动思考内容默认显示（灰色，终端交互 行为）；超 50 行折叠
            content_lines = _thinking_content_lines(block.text, context, closed=False)
            if len(content_lines) > _THINKING_LIVE_MAX_LINES:
                lines.extend(content_lines[:_THINKING_LIVE_MAX_LINES])
                lines.append(
                    (
                        (
                            "class:tui-thinking",
                            f"  … 思考内容共 {len(content_lines)} 行，Ctrl+O 查看全部",
                        ),
                    )
                )
            else:
                lines.extend(content_lines)
        return tuple(lines)
    detail = block.text or block.detail
    if not detail:
        return ()
    title = _thinking_title(block)
    # 终端交互 对齐：completed 思考内容默认展开（灰色常显），超长折叠提示。
    lines: list[FormattedLine] = list(
        wrap_fragments(
            (("class:tui-thinking", title),),
            width=context.width,
            first_prefix=(("class:tui-thinking", "∴ "),),
            continuation_prefix=(("class:tui-thinking", "  "),),
        )
    )
    content_lines = _thinking_content_lines(detail, context, closed=True)
    if len(content_lines) > _THINKING_LIVE_MAX_LINES and not context.detailed_transcript:
        lines.extend(content_lines[:_THINKING_LIVE_MAX_LINES])
        lines.append(
            (
                (
                    "class:tui-thinking",
                    f"  … 思考内容共 {len(content_lines)} 行，Ctrl+O 查看全部",
                ),
            )
        )
    else:
        lines.extend(content_lines)
    return tuple(lines)


# LLM: durable compact 进度只展示底层回调的结构化阶段/百分比；进度条不得根据墙钟虚构。
# 函数用途: 在 Compact 运行时显示闪动图标、真实阶段和百分比。
def _render_compact_progress(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    if block.phase in {"failed", "interrupted"}:
        label = (
            "Context compaction interrupted"
            if block.phase == "interrupted"
            else "Context compaction failed"
        )
        return wrap_fragments(
            (("class:tui-error", label),),
            width=context.width,
            first_prefix=(("class:tui-error", "! "),),
            continuation_prefix=(("class:tui-error", "  "),),
        )
    percent = min(100, max(0, int(block.metadata.get("percent") or 0)))
    stage = str(block.metadata.get("stage") or "preparing").replace("_", " ")
    bar_width = min(24, max(8, context.width - 48))
    filled = min(bar_width, max(0, int(round(percent * bar_width / 100))))
    meter = "━" * filled + "─" * (bar_width - filled)
    glyph = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
    return wrap_fragments(
        (
            ("class:tui-spinner-highlight", glyph + " "),
            ("class:tui-thinking", "Compacting context "),
            ("class:tui-thinking", f"[{meter}] {percent}% · {stage}"),
        ),
        width=context.width,
        continuation_prefix=(("class:tui-thinking", "  "),),
    )


# LLM: 思考正文统一浅灰 + 全角括号包裹；closed 时必须同时为左右两个双列括号预留宽度，
# 否则极窄终端会越过 viewport。活动态只为左括号预留两列。
# 函数用途: 生成不会超过终端显示宽度的括号包裹浅灰思考内容行。
def _thinking_content_lines(
    detail: str,
    context: TuiRenderContext,
    *,
    closed: bool,
) -> list[FormattedLine]:
    wrapper_width = 4 if closed else 2
    rendered = render_markdown(
        detail,
        MarkdownRenderContext(width=max(1, context.width - wrapper_width)),
    )
    last_visible = max((i for i, line in enumerate(rendered) if line), default=-1)
    out: list[FormattedLine] = []
    for idx, line in enumerate(rendered):
        if not line:
            out.append(())
            continue
        # 终端交互 给 Markdown 容器设置 dimColor；这里必须把灰色 role 追加到每个
        # Markdown fragment 的末尾，才能覆盖粗体、代码和链接各自的前景色。
        line = _append_terminal_role(line, "class:tui-thinking-detail")
        if idx == 0:
            out.append((("class:tui-thinking-detail", "（"), *line))
        else:
            out.append((("class:tui-thinking-detail", "  "), *line))
        if closed and idx == last_visible:
            out[-1] = (*out[-1], ("class:tui-thinking-detail", "）"))
    return out


def _thinking_title(block: TuiBlock) -> str:
    try:
        duration = max(0.0, float(block.metadata.get("duration_seconds") or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    if duration < 1.0:
        return "Thought"
    return f"Thought for {_format_duration(duration)}"


# LLM: 显示上限与 终端交互 的公开 UserPromptMessage 合同一致；只裁 UI 投影，完整 prompt 仍留在 canonical event/执行链。
# 函数用途: 对超长用户消息保留头尾各 2500 字符，并标出中间隐藏行数。
# LLM: 渲染投影截断(头尾保留 + 中间折叠提示); 只作用于渲染层, block.text 原文不变。
# 函数用途: 超长文本渲染前裁剪, 防 TUI 被输出洪水拖垮。
def _bounded_render_text(text: str, *, max_lines: int) -> str:
    normalized = str(text or "")
    lines = normalized.split("\n")
    if len(lines) <= max_lines:
        return normalized
    head_count = max(1, max_lines * 2 // 3)
    tail_count = max(5, max_lines // 10)
    head = lines[:head_count]
    tail = lines[-tail_count:]
    hidden = len(lines) - head_count - tail_count
    return "\n".join(head) + f"\n… 中间 {hidden} 行已折叠 (ctrl+o 展开更多) …\n" + "\n".join(tail)


# LLM: role 必须追加在 fragment 原样式之后，让容器级 muted/thinking 前景色覆盖 Markdown token 颜色，同时保留 bold/italic/underline 属性。
# 函数用途: 给一整行所有可见片段追加最终视觉角色，避免只染灰行前缀而正文仍是白色或彩色。
def _append_terminal_role(line: FormattedLine, role: str) -> FormattedLine:
    normalized_role = str(role or "").strip()
    if not normalized_role:
        return line
    return tuple(
        (f"{style} {normalized_role}".strip(), text)
        for style, text in line
    )


# LLM: 折叠提示识别只作用于 renderer 自己生成的固定投影，不得用于状态迁移或模型正文裁决。
# 函数用途: 判断一行是否为 renderer 的中段折叠提示，以便整行使用浅灰提示色。
def _is_fold_hint_line(line: FormattedLine) -> bool:
    text = fragments_text(line).strip()
    return text.startswith("… 中间") and "行已折叠" in text


def _bounded_user_text(text: str) -> str:
    normalized = str(text or "")
    if len(normalized) <= USER_MESSAGE_MAX_CHARS:
        return normalized
    head = normalized[:USER_MESSAGE_HEAD_CHARS]
    tail = normalized[-USER_MESSAGE_TAIL_CHARS:]
    hidden_lines = normalized[USER_MESSAGE_HEAD_CHARS:-USER_MESSAGE_TAIL_CHARS].count("\n")
    return f"{head}\n… +{hidden_lines} lines …\n{tail}"


# LLM: elapsed 只由 typed turn timestamps 与显式 render clock 计算，墙钟不写回 reducer，也不参与业务超时。
# 函数用途: 返回当前活动回合已经运行的秒数。
def _spinner_elapsed_seconds(context: TuiRenderContext) -> float:
    if context.now <= 0 or context.status_started_at <= 0:
        return 0.0
    return max(0.0, context.now - context.status_started_at)


# LLM: stall 仅表示 typed event 三秒未推进的视觉状态；它不能取消请求、重试模型或改变工具判断。
# 函数用途: 判断当前 spinner 是否应使用停滞错误色。
def _spinner_is_stalled(context: TuiRenderContext) -> bool:
    if context.has_active_tools or context.now <= 0 or context.status_last_event_at <= 0:
        return False
    return context.now - context.status_last_event_at > SPINNER_STALL_AFTER_SECONDS


# LLM: 状态后缀只消费 typed 时间/token 指标与 thinking block 角色；宽度裁剪仍由上层终端 renderer 统一处理。
# 函数用途: 生成 spinner 的 thinking、耗时和输出 token 状态括号。
def _spinner_status_suffix(block: TuiBlock, context: TuiRenderContext) -> str:
    parts: list[str] = []
    elapsed = _spinner_elapsed_seconds(context)
    if elapsed > SPINNER_METRICS_AFTER_SECONDS:
        parts.append(_format_duration(elapsed))
        if context.output_tokens > 0:
            parts.append(f"↓ {_format_compact_number(context.output_tokens)} tokens")
    if block.text or block.detail:
        parts.append("thinking")
    return f" ({' · '.join(parts)})" if parts else ""


# LLM: duration formatter 复刻参考的秒/分/时显示边界，只做视觉格式化，不承担 deadline 或租约计算。
# 函数用途: 将非负秒数格式化为紧凑运行时长。
def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {remaining_minutes}m {remaining_seconds}s"
    days, remaining_hours = divmod(hours, 24)
    return f"{days}d {remaining_hours}h {remaining_minutes}m"


# LLM: token 数字按参考的一位 compact 英文单位显示；输入只来自非负 typed counter。
# 函数用途: 把 token 数量格式化为 900、1.2k、1.0m 等短文本。
def _format_compact_number(value: int) -> str:
    number = max(0, int(value or 0))
    if number < 1_000:
        return str(number)
    if number < 1_000_000:
        return f"{number / 1_000:.1f}k"
    if number < 1_000_000_000:
        return f"{number / 1_000_000:.1f}m"
    return f"{number / 1_000_000_000:.1f}b"


# LLM: 工具标题/状态/输出只读 structured block/display；diff、write、command 走专用富渲染，未知类型保留通用回退且任何正文都不参与 phase 判断。
# 函数用途: 渲染 终端交互 风格的工具标题、命令输出、写入预览和带行号增删高亮。
# LLM: todo 面板 = task_progress 账本的持续投影; 状态图标: ☑ done / ● in_progress /
# □ pending / ⬜ blocked / ➖ skipped。只读渲染, 不修改账本。
# 函数用途: 渲染任务清单面板块(role=todo)。
def _render_todo(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    items = block.metadata.get("items")
    if not isinstance(items, list) or not items:
        return ()
    lines: list[FormattedLine] = [
        (("class:tui-todo-title", "📋 任务清单"),)
    ]
    icon_map = {
        "done": "☑ ",
        "in_progress": "● ",
        "pending": "□ ",
        "blocked": "⬜ ",
        "skipped": "➖ ",
    }
    style_map = {
        "done": "class:tui-todo-done",
        "in_progress": "class:tui-todo-active",
        "pending": "class:tui-todo-pending",
        "blocked": "class:tui-todo-blocked",
        "skipped": "class:tui-todo-skipped",
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "pending").strip().lower()
        title = str(item.get("title") or item.get("id") or "")
        if not title:
            continue
        style = style_map.get(status, "class:tui-todo-pending")
        icon = icon_map.get(status, "□ ")
        lines.append(((style, f"  {icon}{title}"),))
    return tuple(lines)


def _render_tool(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    display = block.metadata.get("display")
    public_display = display if isinstance(display, dict) else {}
    title = _tool_title(block, public_display, context.width)
    marker_style = "class:tui-error" if block.phase == "failed" else "class:tui-tool-marker"
    lines: list[FormattedLine] = [
        ((marker_style, "● "), ("class:tui-tool-title", title))
    ]
    display_kind = str(public_display.get("kind") or "")
    if display_kind == "diff":
        return (*lines, *_render_tool_diff(public_display, context))
    if display_kind == "write":
        return (*lines, *_render_tool_write(public_display, context))
    if display_kind == "patch":
        return (*lines, *_render_tool_patch(public_display, context))
    if display_kind == "command":
        return (*lines, *_render_tool_command(block, public_display, context))
    status_text = _tool_status_text(block)
    child_style = "class:tui-error" if block.phase == "failed" else "class:tui-muted"
    detail_lines = status_text.splitlines()
    # R2-1: show_all 也不能全量渲染(工具输出洪水时展开=崩溃); 超上限头尾保留+折叠提示。
    if context.show_all:
        detail_lines = _bounded_render_text(
            "\n".join(detail_lines),
            max_lines=_TOOL_RENDER_DETAIL_MAX_LINES,
        ).splitlines()
    visible_lines = detail_lines if context.show_all else detail_lines[:TOOL_PREVIEW_MAX_LINES]
    for index, detail_line in enumerate(visible_lines):
        marker = "  ⎿ " if index == 0 else "    "
        wrapped = wrap_fragments(
            ((child_style, detail_line),),
            width=context.width,
            first_prefix=((child_style, marker),),
            continuation_prefix=((child_style, "    "),),
        )
        lines.extend(wrapped)
    hidden_line_count = max(0, len(detail_lines) - len(visible_lines))
    if hidden_line_count:
        lines.extend(
            wrap_fragments(
                (("class:tui-muted", f"… +{hidden_line_count} lines (ctrl+o, then ctrl+e to show all)"),),
                width=context.width,
                first_prefix=(("class:tui-muted", "    "),),
                continuation_prefix=(("class:tui-muted", "    "),),
            )
        )
    return tuple(lines)


# LLM: 工具标题参数只来自 started invocation 或已脱敏 display.path；换行和宽度在 UI 层裁剪，不允许回读原始工具参数。
# 函数用途: 生成 `Bash(command)`、`Update(path)`、`Write(path)` 形式的单行标题。
def _tool_title(block: TuiBlock, display: dict[str, Any], width: int) -> str:
    base = _tool_display_name(block.title or str(block.metadata.get("tool") or "Tool"))
    argument = ""
    display_kind = str(display.get("kind") or "")
    if display_kind in {"diff", "write"}:
        argument = str(display.get("path") or "")
    elif display_kind == "patch":
        raw_files = display.get("files")
        files = [item for item in raw_files if isinstance(item, dict)] if isinstance(raw_files, list) else []
        hidden_files = max(0, int(display.get("hidden_files") or 0))
        if len(files) == 1 and hidden_files == 0:
            argument = str(files[0].get("path") or "")
        elif files or hidden_files:
            argument = f"{len(files) + hidden_files} files"
    elif display_kind == "command":
        argument = str(block.metadata.get("invocation") or "")
    argument = argument.splitlines()[0].strip() if argument else ""
    title = f"{base}({argument})" if argument else base
    return _fit_text(title, max(1, int(width or 1) - 2), "left").rstrip()


# LLM: diff summary 和每一行都来自结构化计数/行表；颜色不能把自然语言 error 误当删除，也不能改变真实工具 phase。
# 函数用途: 绘制增删统计、旧/新行号及红蓝背景的代码差异。
def _render_tool_diff(
    display: dict[str, Any],
    context: TuiRenderContext,
) -> list[FormattedLine]:
    added = max(0, int(display.get("lines_added") or 0))
    removed = max(0, int(display.get("lines_removed") or 0))
    lines: list[FormattedLine] = list(
        wrap_fragments(
            (("class:tui-muted", f"Added {added} lines, removed {removed} lines"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "  ⎿ "),),
            continuation_prefix=(("class:tui-muted", "    "),),
        )
    )
    raw_rows = display.get("lines")
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    visible, hidden = _visible_rich_items(rows, context, normal_limit=TOOL_RICH_PREVIEW_MAX_LINES)
    hidden += max(0, int(display.get("hidden_lines") or 0))
    number_width = max(
        2,
        max(
            (
                len(str(row.get("old_line") or ""))
                for row in rows
            ),
            default=0,
        ),
        max(
            (
                len(str(row.get("new_line") or ""))
                for row in rows
            ),
            default=0,
        ),
    )
    for row in visible:
        lines.append(_render_diff_row(row, context.width, number_width))
    if hidden:
        lines.append(_tool_hidden_line(hidden, context))
    return lines


# LLM: 多文件 patch 只组合已经公开脱敏的 diff 子项；展开级别限制文件数量，每个文件内部仍复用统一 diff 行裁剪。
# 函数用途: 显示一次补丁涉及的文件列表，并逐文件绘制红蓝增删差异。
def _render_tool_patch(
    display: dict[str, Any],
    context: TuiRenderContext,
) -> list[FormattedLine]:
    raw_files = display.get("files")
    files = [item for item in raw_files if isinstance(item, dict)] if isinstance(raw_files, list) else []
    if context.show_all:
        visible = files
    else:
        file_limit = 8 if context.detailed_transcript else 2
        visible = files[:file_limit]
    hidden = max(0, len(files) - len(visible)) + max(
        0,
        int(display.get("hidden_files") or 0),
    )
    lines: list[FormattedLine] = list(
        wrap_fragments(
            (
                (
                    "class:tui-muted",
                    f"Updated {len(files) + max(0, int(display.get('hidden_files') or 0))} files",
                ),
            ),
            width=context.width,
            first_prefix=(("class:tui-muted", "  ⎿ "),),
            continuation_prefix=(("class:tui-muted", "    "),),
        )
    )
    for file_display in visible:
        path = str(file_display.get("path") or "")
        lines.extend(
            wrap_fragments(
                (("class:tui-tool-title", path),),
                width=context.width,
                first_prefix=(("class:tui-tool-title", "    "),),
                continuation_prefix=(("class:tui-tool-title", "    "),),
            )
        )
        lines.extend(_render_tool_diff(file_display, context))
    if hidden:
        shortcut = "ctrl+e to show all" if context.detailed_transcript else "ctrl+o to expand"
        lines.extend(
            wrap_fragments(
                (("class:tui-muted", f"… +{hidden} files ({shortcut})"),),
                width=context.width,
                first_prefix=(("class:tui-muted", "    "),),
                continuation_prefix=(("class:tui-muted", "    "),),
            )
        )
    return lines


# LLM: 单行 diff 的 kind/line numbers 是上游白名单事实；超宽源码按显示列裁剪，背景补齐仅是视觉行为。
# 函数用途: 渲染一行 header/context/add/remove，并给增删行填满终端背景色。
def _render_diff_row(row: dict[str, Any], width: int, number_width: int) -> FormattedLine:
    kind = str(row.get("kind") or "context")
    text = str(row.get("text") or "").replace("\t", "    ")
    if kind == "header":
        content = "    " + text
        return (("class:tui-diff-header", _fit_text(content, width, "left")),)
    old_line = "" if row.get("old_line") is None else str(row.get("old_line"))
    new_line = "" if row.get("new_line") is None else str(row.get("new_line"))
    marker = "+" if kind == "add" else "-" if kind == "remove" else " "
    prefix = f" {old_line:>{number_width}} {new_line:>{number_width}} {marker} "
    style = {
        "add": "class:tui-diff-add",
        "remove": "class:tui-diff-remove",
    }.get(kind, "class:tui-diff-context")
    fitted = _fit_text(prefix + text, width, "left")
    return ((style, fitted),)


# LLM: write preview 只消费公开 lines/bytes/binary/mode；二进制绝不尝试语法高亮或文本解码。
# 函数用途: 显示写入行数、字节数和默认十行的可展开内容预览。
def _render_tool_write(
    display: dict[str, Any],
    context: TuiRenderContext,
) -> list[FormattedLine]:
    total_lines = max(0, int(display.get("total_lines") or 0))
    byte_count = max(0, int(display.get("bytes") or 0))
    if display.get("binary") is True:
        return list(
            wrap_fragments(
                (("class:tui-muted", f"Wrote {byte_count} bytes (binary)"),),
                width=context.width,
                first_prefix=(("class:tui-muted", "  ⎿ "),),
                continuation_prefix=(("class:tui-muted", "    "),),
            )
        )
    verb = "Added" if str(display.get("mode") or "") == "append" else "Wrote"
    lines: list[FormattedLine] = list(
        wrap_fragments(
            (("class:tui-muted", f"{verb} {total_lines} lines · {byte_count} bytes"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "  ⎿ "),),
            continuation_prefix=(("class:tui-muted", "    "),),
        )
    )
    raw = display.get("lines")
    content_lines = [str(item) for item in raw] if isinstance(raw, list) else []
    visible, hidden = _visible_rich_items(
        content_lines,
        context,
        normal_limit=WRITE_PREVIEW_MAX_LINES,
    )
    hidden += max(0, int(display.get("hidden_lines") or 0))
    number_width = max(2, len(str(total_lines or len(content_lines))))
    for index, content in enumerate(visible, start=1):
        row = f" {index:>{number_width}}  {content.replace(chr(9), '    ')}"
        lines.append((("class:tui-tool-output", _fit_text(row, context.width, "left")),))
    if hidden:
        lines.append(_tool_hidden_line(hidden, context))
    return lines


# LLM: command 结果按结构化 stdout/stderr 分流着色，return_code 只控制错误摘要；任何输出关键词都不能反推成功失败。
# 函数用途: 显示命令退出码、标准输出、红色错误输出和折叠行数。
def _render_tool_command(
    block: TuiBlock,
    display: dict[str, Any],
    context: TuiRenderContext,
) -> list[FormattedLine]:
    return_code = display.get("return_code")
    rows: list[tuple[str, str]] = []
    rows.extend(("stdout", line) for line in str(display.get("stdout") or "").splitlines())
    rows.extend(("stderr", line) for line in str(display.get("stderr") or "").splitlines())
    visible, hidden = _visible_command_items(rows, context)
    lines: list[FormattedLine] = []
    if return_code not in {None, 0}:
        lines.extend(
            wrap_fragments(
                (("class:tui-error", f"Error: Exit code {return_code}"),),
                width=context.width,
                first_prefix=(("class:tui-error", "  ⎿ "),),
                continuation_prefix=(("class:tui-error", "    "),),
            )
        )
    elif not rows:
        lines.append((("class:tui-muted", "  ⎿ Done"),))
    for index, (stream, content) in enumerate(visible):
        marker = "  ⎿ " if index == 0 and not lines else "    "
        style = "class:tui-error" if stream == "stderr" else "class:tui-tool-output"
        if block.phase == "failed" and stream == "stdout":
            style = "class:tui-tool-output-error"
        fitted = _fit_text(marker + content.replace("\t", "    "), context.width, "left")
        lines.append(((style, fitted),))
    if hidden:
        lines.append(_tool_hidden_line(hidden, context))
    return lines


# LLM: rich item 展开级别只由 context 的 Ctrl+O/Ctrl+E 状态决定，内容本身不能触发展开或改变裁剪上限。
# 函数用途: 在普通、详细和全部三种模式下选择通用富展示行。
def _visible_rich_items(
    items: list[Any],
    context: TuiRenderContext,
    *,
    normal_limit: int,
) -> tuple[list[Any], int]:
    if context.show_all:
        return items, 0
    limit = TOOL_RICH_EXPANDED_MAX_LINES if context.detailed_transcript else normal_limit
    return items[:limit], max(0, len(items) - limit)


# LLM: command 普通视图优先保留尾部诊断，详细视图保留有界头尾，show_all 才完整展开；选择规则不读取输出含义。
# 函数用途: 选择最有助于查看构建失败尾部的命令输出行，并计算隐藏数量。
def _visible_command_items(
    items: list[tuple[str, str]],
    context: TuiRenderContext,
) -> tuple[list[tuple[str, str]], int]:
    if context.show_all or len(items) <= TOOL_PREVIEW_MAX_LINES:
        return items, 0
    if not context.detailed_transcript:
        return items[-TOOL_PREVIEW_MAX_LINES:], len(items) - TOOL_PREVIEW_MAX_LINES
    limit = TOOL_RICH_EXPANDED_MAX_LINES
    if len(items) <= limit:
        return items, 0
    head = limit // 2
    tail = limit - head
    return [*items[:head], *items[-tail:]], len(items) - limit


# LLM: 隐藏行提示只报告显式计数和当前快捷键层级；它不修改 detailed/show_all 状态。
# 函数用途: 生成与 终端交互 一致的 Ctrl+O/Ctrl+E 展开提示行。
def _tool_hidden_line(hidden: int, context: TuiRenderContext) -> FormattedLine:
    shortcut = "ctrl+e to show all" if context.detailed_transcript else "ctrl+o to expand"
    text = f"    … +{max(0, int(hidden))} lines ({shortcut})"
    return (("class:tui-muted", _fit_text(text, context.width, "left")),)


# LLM: 系统与错误消息用 role/severity 样式，不从中文/英文错误关键词猜 severity。
# 函数用途: 渲染普通系统提示、警告或错误块。
def _render_system(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    style = "class:tui-error" if block.role == "error" else "class:tui-muted"
    marker = "! " if block.role == "error" else "◇ "
    if block.kind == "compact_boundary":
        generation = max(0, int(block.metadata.get("compact_generation") or 0))
        text = f"Context compacted · generation {generation}"
    elif block.kind == "context_window_compacted":
        before = _format_compact_number(int(block.metadata.get("before_tokens") or 0))
        after = _format_compact_number(int(block.metadata.get("after_tokens") or 0))
        dropped = max(0, int(block.metadata.get("dropped_pairs") or 0))
        generation = max(0, int(block.metadata.get("generation") or 0))
        text = (
            f"Tool history compacted · {before} → {after}"
            f" · removed {dropped} tool pairs · pass {generation}"
        )
    else:
        text = block.text or block.detail or block.title
    return wrap_fragments(
        ((style, text),),
        width=context.width,
        first_prefix=((style, marker),),
        continuation_prefix=((style, "  "),),
    )


# LLM: permission renderer 只显示 reducer overlay options/selection，decision 仍由 input controller typed intent 执行。
# 函数用途: 渲染固定底部审批面板。
def _render_permission(
    permission: TuiPermissionOverlay,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    lines: list[FormattedLine] = [
        (("class:tui-permission-accent", "─" * context.width),),
        *wrap_fragments(
            (("class:tui-permission-title", permission.title),),
            width=context.width,
            first_prefix=(("class:tui-permission-title", " "),),
            continuation_prefix=(("class:tui-permission-title", " "),),
        ),
    ]
    if permission.description:
        lines.extend(
            wrap_fragments(
                (("", permission.description),),
                width=max(1, context.width - 4),
                first_prefix=(("", "   "),),
                continuation_prefix=(("", "   "),),
            )
        )
    lines.extend(
        wrap_fragments(
            (("", "Do you want to proceed?"),),
            width=context.width,
            first_prefix=(("", " "),),
            continuation_prefix=(("", " "),),
        )
    )
    for index, option in enumerate(permission.options):
        label = str(option.get("label") or option.get("id") or f"Option {index + 1}")
        selected = index == permission.selected_index
        prefix = "❯ " if selected else "  "
        style = "class:tui-permission-selected" if selected else ""
        lines.extend(
            wrap_fragments(
                ((style, f"{index + 1}. {label}"),),
                width=context.width,
                first_prefix=((style, prefix),),
                continuation_prefix=((style, "  "),),
            )
        )
    selected_option = permission.options[permission.selected_index]
    feedback_enabled = bool(selected_option.get("feedback_type"))
    hint = " Esc to cancel"
    if feedback_enabled and not permission.feedback_mode:
        hint += " · Tab to amend"
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", hint.strip()),),
            width=context.width,
            first_prefix=(("class:tui-muted", " "),),
            continuation_prefix=(("class:tui-muted", " "),),
        )
    )
    return tuple(lines)


# LLM: Input status owns fixed pending/queue receipts, typed context metrics, and stash state.
# None may enter the input Document, scrollable transcript, history, or model prompt.
# 函数用途: 在输入框上方固定显示待插入/排队消息、实时上下文占用和草稿提示。
def _render_input_status(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    lines: list[FormattedLine] = []
    if snapshot.pending_steers:
        lines.extend(_render_pending_steers(snapshot.pending_steers, context))
    if snapshot.queued_inputs:
        lines.extend(_render_queue(snapshot.queued_inputs, context))
    if context.context_usage is not None:
        lines.extend(_render_context_usage(context.context_usage, context.width))
    if context.has_stash:
        lines.extend(
            wrap_fragments(
                (("class:tui-muted", "Stashed (auto-restores after submit)"),),
                width=context.width,
                first_prefix=(("class:tui-muted", "  › "),),
                continuation_prefix=(("class:tui-muted", "    "),),
            )
        )
    return tuple(lines)


# LLM: Width tiers alter display density only. Percent and warning style come from typed token
# counts and the configured compact trigger, never from model text or a second UI threshold.
# 函数用途: 将实时上下文快照压成一行，在宽屏显示构成、窄屏保留总量和占比。
def _render_context_usage(
    usage: TuiContextUsage,
    width: int,
) -> tuple[FormattedLine, ...]:
    available = max(1, int(width or 1))
    current = max(0, int(usage.current_tokens or 0))
    window = max(0, int(usage.context_window_tokens or 0))
    trigger = max(0, int(usage.compact_trigger_tokens or 0))
    percent = min(999, round(current * 100 / window)) if window > 0 else 0
    trigger_percent = min(100, round(trigger * 100 / window)) if window > 0 else 0
    style = _context_usage_style(current, trigger, window)
    estimate = "~" if usage.estimated else ""

    if available >= 110:
        text = (
            f"Context {estimate}{_format_compact_number(current)}/{_format_compact_number(window)}"
            f" · {percent}% · compact {trigger_percent}%"
            f" · prompt {_format_compact_number(usage.prompt_tokens)}"
            f" · messages {_format_compact_number(usage.messages_tokens)}"
            f" · tools {_format_compact_number(usage.tool_schema_tokens)}"
        )
    elif available >= 72:
        text = (
            f"Context {estimate}{_format_compact_number(current)}/{_format_compact_number(window)}"
            f" · {percent}% · compact at {_format_compact_number(trigger)}"
        )
    elif available >= 40:
        text = (
            f"Ctx {estimate}{_format_compact_number(current)}/{_format_compact_number(window)}"
            f" · {percent}%"
        )
    else:
        text = f"Ctx {percent}%"
    return wrap_fragments(
        ((style, text),),
        width=available,
        first_prefix=(("class:tui-context-label", "  ◉ "),),
        continuation_prefix=(("class:tui-context-label", "    "),),
    )


# LLM: Color bands are projections of the runtime-owned compact line: safe below 80% of the
# trigger, warning near it, and danger at/above it. They never trigger compaction themselves.
# 函数用途: 根据实际 compact 线选择上下文状态颜色，只影响显示。
def _context_usage_style(current: int, trigger: int, window: int) -> str:
    effective_trigger = trigger if trigger > 0 else window
    if effective_trigger <= 0:
        return "class:tui-context-safe"
    if current >= effective_trigger:
        return "class:tui-context-danger"
    if current * 5 >= effective_trigger * 4:
        return "class:tui-context-warning"
    return "class:tui-context-safe"


# LLM: Pending steer receipts stay fixed above the composer while scrolled history continues to
# move. The muted line is display state only; promotion still requires the exact typed event.
# 函数用途: 显示等待模型真正收到的当前任务补充消息及其未确认状态。
def _render_pending_steers(
    pending_steers: tuple[TuiPendingSteer, ...],
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    if context.width < 4:
        return ()
    lines: list[FormattedLine] = []
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", "Messages to be submitted after next tool call"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "• "),),
            continuation_prefix=(("class:tui-muted", "  "),),
        )
    )
    for item in pending_steers:
        lines.extend(_render_pending_input_message(item.text, context, italic=False))
    return tuple(lines)


# LLM: queue renderer uses reducer order and stays in the fixed composer-adjacent pane. It must
# not disappear when the user scrolls away from the transcript bottom.
# 函数用途: 把运行中排队的下一回合输入固定显示在输入框上方。
def _render_queue(
    queued_inputs: tuple[TuiQueuedInput, ...],
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    if context.width < 4:
        return ()
    lines: list[FormattedLine] = []
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", "Queued follow-up inputs"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "• "),),
            continuation_prefix=(("class:tui-muted", "  "),),
        )
    )
    for item in queued_inputs:
        lines.extend(_render_pending_input_message(item.text, context, italic=True))
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", "↑ edit queued messages"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "    "),),
            continuation_prefix=(("class:tui-muted", "    "),),
        )
    )
    return tuple(lines)


# LLM: Each pending-input message uses a three-line preview limit so one huge paste
# cannot consume the entire composer-adjacent pane. Truncation affects display only.
# 函数用途: 用箭头缩进渲染一条待处理消息，超过三行时显示省略号。
def _render_pending_input_message(
    text: str,
    context: TuiRenderContext,
    *,
    italic: bool,
) -> tuple[FormattedLine, ...]:
    style = "class:tui-muted italic" if italic else "class:tui-muted"
    wrapped = wrap_fragments(
        ((style, str(text or "")),),
        width=context.width,
        first_prefix=(("class:tui-muted", "  ↳ "),),
        continuation_prefix=(("class:tui-muted", "    "),),
    )
    visible = list(wrapped[:PENDING_INPUT_PREVIEW_LINE_LIMIT])
    if len(wrapped) > PENDING_INPUT_PREVIEW_LINE_LIMIT:
        visible.append((("class:tui-muted", "    …"),))
    return tuple(visible)


# LLM: footer 只读 structured mode/status/permission/help；详细模式和运行中提示优先级固定。
# 函数用途: 返回输入框下方的快捷键提示或按真实 my-agent 能力映射的帮助面板。
def _render_footer(snapshot: TuiViewSnapshot, context: TuiRenderContext) -> FormattedLine:
    if snapshot.permission is not None:
        return ()
    if any(block.role == "connection" for block in snapshot.active_blocks):
        text = "  Connecting…"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if context.is_pasting:
        text = "  Pasting text…"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if context.help_open:
        return _render_help_footer(context.width)
    if context.detailed_transcript:
        text = (
            "  Showing detailed transcript · ctrl+o to toggle"
            + (" · ctrl+e to show all" if not context.show_all else "")
        )
        if display_width_text(text) > context.width:
            text = "  Detailed · ctrl+o" + (" · ctrl+e all" if not context.show_all else "")
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if snapshot.status.phase in {"running", "interrupting"}:
        text = "  esc to interrupt"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if any(block.role == "background" for block in snapshot.active_blocks):
        text = "  Working in background · /stop to interrupt"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if context.notice:
        return ((
            "class:tui-muted",
            _fit_text("  " + context.notice, context.width, "left").rstrip(),
        ),)
    text = "  ? for shortcuts"
    return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)


# LLM: 帮助只列真实接通的输入/导航能力；宽屏三列、窄屏单列由 width 决定，不能为参考产品独有功能造空入口。
# 函数用途: 生成 `?` 切换的快捷键帮助文本，换行仍保留 fragment 样式供动态 footer 计算高度。
def _render_help_footer(width: int) -> FormattedLine:
    available = max(1, int(width or 1) - 4)
    rows: list[str] = []
    if available >= 72:
        column_width = max(20, available // len(HELP_SHORTCUT_GROUPS))
        row_count = max(len(group) for group in HELP_SHORTCUT_GROUPS)
        for row_index in range(row_count):
            columns = [
                _fit_text(
                    group[row_index] if row_index < len(group) else "",
                    column_width,
                    "left",
                )
                for group in HELP_SHORTCUT_GROUPS
            ]
            rows.append("  " + "".join(columns).rstrip())
    else:
        for group_index, group in enumerate(HELP_SHORTCUT_GROUPS):
            if group_index:
                rows.append("")
            rows.extend("  " + _fit_text(item, available, "left").rstrip() for item in group)
    return (("class:tui-muted", "\n".join(rows)),)


# LLM: tool phase 到用户文案的映射只使用 typed phase/detail；detail 已由 adapter 做脱敏和有界处理。
# 函数用途: 返回工具子行内容。
def _tool_status_text(block: TuiBlock) -> str:
    if block.phase == "waiting_permission":
        return "Waiting for permission…"
    if block.phase in {"started", "delta", "running"}:
        return block.detail or "Running…"
    if block.phase == "failed":
        return block.detail or "Failed"
    if block.phase == "interrupted":
        return block.detail or "Interrupted"
    return block.detail or "Done"


# LLM: display-name 映射只替换 my-agent 已知工具名称；开放世界未知 name 原样显示。
# 函数用途: 将 registry 工具名映射为参考界面惯用短名。
def _tool_display_name(name: str) -> str:
    normalized = str(name or "Tool")
    return {
        "run_command": "Bash",
        "read_file": "Read",
        "write_file": "Write",
        "edit_file": "Update",
        "apply_patch": "Update",
        "web_fetch": "WebFetch",
        "web_search": "WebSearch",
    }.get(normalized, normalized)


# LLM: block 间只保留一个空行，块内部的 Markdown 空行不被压缩。
# 函数用途: 将非空 block 追加到 transcript。
def _append_block(lines: list[FormattedLine], block_lines: tuple[FormattedLine, ...]) -> None:
    if not block_lines:
        return
    if lines and fragments_text(lines[-1]):
        lines.append(())
    lines.extend(block_lines)


# LLM: spinner 文案选择使用稳定 digest 而非 Python hash/random，保证同 block resize/replay 不跳词。
# 函数用途: 从候选集中为一个稳定 block key 选择显示文案。
def _stable_spinner_choice(key: str, choices: tuple[str, ...]) -> str:
    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    return choices[int.from_bytes(digest[:4], "big") % len(choices)]


# LLM: glimmer 仅改变一个可见字符的样式，不修改稳定活动文案或任务状态。
# 函数用途: 让工作动词上的高亮逐字移动，使用户能看出界面仍在工作。
def _animated_spinner_word(word: str, spinner_index: int) -> tuple[Fragment, ...]:
    normalized = str(word or "Working")
    highlighted = max(0, int(spinner_index or 0)) % max(1, len(normalized))
    fragments: list[Fragment] = []
    if normalized[:highlighted]:
        fragments.append(("class:tui-thinking", normalized[:highlighted]))
    fragments.append(("class:tui-spinner-highlight", normalized[highlighted : highlighted + 1]))
    if normalized[highlighted + 1 :]:
        fragments.append(("class:tui-thinking", normalized[highlighted + 1 :]))
    return tuple(fragments)


# LLM: background fill 按 terminal display width 补齐，不按 Python len 破坏中文列数。
# 函数用途: 将一行补到精确宽度并保留已有 style fragments。
def _fill_line(line: FormattedLine, width: int, style: str) -> FormattedLine:
    remaining = max(0, int(width) - display_width_fragments(line))
    return (*line, (style, " " * remaining)) if remaining else line


# LLM: fit 仅用于短欢迎元数据；超宽时尾部省略号，路径只展示不做文件访问。
# 函数用途: 截断并按左/中/右对齐补齐到精确列宽。
def _fit_text(text: str, width: int, align: str) -> str:
    width = max(0, int(width))
    fitted = _truncate_text(str(text or ""), width)
    remaining = max(0, width - display_width_text(fitted))
    if align == "center":
        left = remaining // 2
        return " " * left + fitted + " " * (remaining - left)
    if align == "right":
        return " " * remaining + fitted
    return fitted + " " * remaining


# LLM: truncate 使用 wcswidth 逐字符累积并为省略号预留一列，避免中日韩字符越过卡片边框。
# 函数用途: 将短展示文本限制到指定终端列宽。
def _truncate_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width_text(text) <= width:
        return text
    target = max(0, width - 1)
    result = ""
    for char in text:
        candidate = result + char
        if max(0, wcswidth(candidate)) > target:
            break
        result = candidate
    return result + "…"


__all__ = [
    "TuiBlockRenderCache",
    "TuiRenderCacheStats",
    "TuiRenderContext",
    "TuiRenderFrame",
    "render_tui_snapshot",
    "sanitize_tui_render_frame",
]
