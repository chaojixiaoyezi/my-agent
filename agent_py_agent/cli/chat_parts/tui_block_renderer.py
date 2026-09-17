# LLM: 本模块是 TuiViewSnapshot 到 prompt_toolkit formatted lines 的唯一 block renderer；不得读取原始模型文本猜工具、权限或生命周期。
# 模块用途: 生成消息、权限、队列和提示；快照失败明确提示，未知上下文不冒充零 token，旧 Working 不冒充实时进展。

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from wcwidth import wcswidth

from ...agent.conversation.compact_progress import (
    COMPACT_SOURCE_ACTIVE_TURN,
    COMPACT_SOURCE_TRANSCRIPT,
    COMPACT_SOURCE_TURN_LOCAL,
)
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
from .tui_model_metrics import render_model_metrics
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
# R256: 行数上限挡不住"无换行巨行"——单行 1M 字符仍会整段进 Markdown 排版（实测 494ms/帧、
# 生成 17773 显示行）。字符预算必须在排版前生效，与用户消息路径（USER_MESSAGE_MAX_CHARS）对齐；
# 只裁显示投影，canonical block.text 与发给模型的上下文都不受影响，完整原文仍走 Ctrl+E 分页。
_ASSISTANT_RENDER_MAX_CHARS = 10_000
_ASSISTANT_RENDER_DETAIL_MAX_CHARS = 40_000
# 活动思考内容的默认可见行数上限（终端交互 行为：灰色实时可见；
# 超长折叠为提示行，Ctrl+O 看全部）。
_THINKING_LIVE_MAX_LINES = 50
_TOOL_RENDER_MAX_LINES = 200
_TOOL_RENDER_DETAIL_MAX_LINES = 2_000
_TOOL_RENDER_DETAIL_MAX_CHARS = 40_000
USER_MESSAGE_TAIL_CHARS = 2_500
SPINNER_METRICS_AFTER_SECONDS = 30.0
SPINNER_STALL_AFTER_SECONDS = 3.0
PENDING_INPUT_PREVIEW_LINE_LIMIT = 3
# LLM: 少量回执保持完整预览；超过阈值后必须切成固定摘要，避免大量排队输入把
# prompt_toolkit HSplit 的最小高度撑过终端并退化成 `Window too small`。
# 常量用途: 控制输入框附近何时从逐条预览切换为有界队列摘要，不改变真实队列内容。
INPUT_RECEIPT_EXPANDED_ITEM_LIMIT = 4
TODO_COLLAPSED_MAX_ITEMS = 4
TERMINAL_RENDER_PHASES = frozenset({"completed", "failed", "interrupted"})
FOCUSED_AGENT_TERMINAL_STATUSES = frozenset(
    {
        "DONE",
        "FAILED",
        "BLOCKED",
        "CHANNEL_ERROR",
        "TIMEOUT",
        "CANCELLED",
        "ABANDONED",
        "TAKEN_OVER",
    }
)
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
        "ctrl + g back from child",
        "ctrl + o for detailed transcript",
        "ctrl + t to expand tasks",
        "ctrl + r to search prompts",
        "meta + enter for newline",
    ),
    (
        "ctrl + s to stash prompt",
        "F6 切换原生复制/滚轮",
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
# 类用途: 指定一次 TUI 渲染的宽度、模式、动画帧和后台刷新健康，不授予任务状态变更权。
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
    model_metrics: tuple[tuple[str, object], ...] = ()
    compact_count: int = 0
    output_tokens: int = 0
    has_active_tools: bool = False
    notice: str = ""
    background_sync_failed: bool = False
    has_stash: bool = False
    is_pasting: bool = False
    help_open: bool = False
    todos_expanded: bool = False
    mouse_capture_enabled: bool = True
    focused_agent_run_id: str = ""
    focused_agent_name: str = "main"
    focused_agent_status: str = ""
    selected_agent_run_id: str = ""
    expanded_goal_id: str = ""
    agent_view_depth: int = 0

    # LLM: width 最小一列，名称字段只做展示字符串规范，不获得路径或配置控制权。
    # 函数用途: 规范渲染上下文，保证动画索引非负和刷新健康使用布尔标记。
    def __post_init__(self) -> None:
        object.__setattr__(self, "width", max(1, int(self.width or 1)))
        object.__setattr__(self, "background_sync_failed", bool(self.background_sync_failed))
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
        object.__setattr__(self, "compact_count", max(0, int(self.compact_count or 0)))
        object.__setattr__(self, "output_tokens", max(0, int(self.output_tokens or 0)))
        object.__setattr__(self, "has_active_tools", bool(self.has_active_tools))
        object.__setattr__(self, "notice", str(self.notice or ""))
        object.__setattr__(self, "has_stash", bool(self.has_stash))
        object.__setattr__(self, "is_pasting", bool(self.is_pasting))
        object.__setattr__(self, "help_open", bool(self.help_open))
        object.__setattr__(self, "todos_expanded", bool(self.todos_expanded))
        object.__setattr__(
            self,
            "mouse_capture_enabled",
            bool(self.mouse_capture_enabled),
        )
        object.__setattr__(
            self,
            "focused_agent_run_id",
            str(self.focused_agent_run_id or "").strip(),
        )
        object.__setattr__(
            self,
            "focused_agent_name",
            str(self.focused_agent_name or "main").strip() or "main",
        )
        object.__setattr__(
            self,
            "focused_agent_status",
            str(self.focused_agent_status or "").strip().upper(),
        )
        object.__setattr__(
            self,
            "selected_agent_run_id",
            str(self.selected_agent_run_id or "").strip(),
        )
        object.__setattr__(
            self,
            "expanded_goal_id",
            str(self.expanded_goal_id or "").strip(),
        )
        object.__setattr__(
            self,
            "agent_view_depth",
            max(0, int(self.agent_view_depth or 0)),
        )


# LLM: frame provider 必须复用 renderer 的可见上下文规则；Todo 的时钟只在 snapshot.has_active_work 时进入 key。
# 函数用途: 生成完整画面的缓存键，刷新健康变化必须重绘，避免恢复连接后仍显示旧警告。
def tui_render_context_key(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
) -> tuple[Any, ...]:
    connection_animation = (
        context.spinner_index % len(SPINNER_GLYPHS)
        if any(block.role == "connection" for block in snapshot.active_blocks)
        else None
    )
    active_thinking = tuple(
        block
        for block in snapshot.active_blocks
        if block.role == "thinking"
        and block.phase not in TERMINAL_RENDER_PHASES
    )
    # 已经有正文的思考属于真实 transcript，按 created_seq 留在对应 assistant
    # 前面；只有尚无正文的等待 spinner 固定在底部。
    active_activity = tuple(
        block for block in active_thinking if not (block.text or block.detail)
    )
    visible_activity = _visible_activity_blocks(snapshot, active_activity)
    thinking_animation = tuple(
        (block.block_id, _thinking_animation_key(block, context))
        for block in visible_activity
    )
    background_animation = tuple(
        (block.block_id, _background_animation_key(block, context))
        for block in snapshot.active_blocks
        if block.role == "background"
        and block.phase not in TERMINAL_RENDER_PHASES
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
    tool_input_animation = tuple(
        (block.block_id, _tool_input_animation_key(block, context))
        for block in snapshot.active_blocks
        if block.role == "tool_input"
    )
    todo_animation = (
        context.spinner_index % len(SPINNER_GLYPHS)
        if snapshot.has_active_work and any(
            block.role == "todo"
            and _todo_has_in_progress_items(block.metadata.get("items"))
            for block in snapshot.active_blocks
        )
        else None
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
        context.model_metrics,
        context.compact_count,
        context.output_tokens,
        context.has_active_tools,
        context.notice,
        context.background_sync_failed,
        context.has_stash,
        context.is_pasting,
        context.help_open,
        context.todos_expanded,
        context.mouse_capture_enabled,
        context.focused_agent_run_id,
        context.focused_agent_name,
        context.focused_agent_status,
        context.selected_agent_run_id,
        context.expanded_goal_id,
        context.agent_view_depth,
        connection_animation,
        thinking_animation,
        background_animation,
        compact_animation,
        tool_input_animation,
        todo_animation,
    )


# LLM: TuiRenderFrame 分离各显示区，并以 block ID 给出正文行锚点；布局不能从正文猜位置或执行状态。
# 类用途: 返回不可变画面及分页后保持阅读位置所需的块起始行。
@dataclass(frozen=True)
class TuiRenderFrame:
    transcript_lines: tuple[FormattedLine, ...]
    overlay_lines: tuple[FormattedLine, ...]
    input_status_lines: tuple[FormattedLine, ...]
    todo_lines: tuple[FormattedLine, ...]
    agent_lines: tuple[FormattedLine, ...]
    footer: FormattedLine
    block_line_offsets: tuple[tuple[str, int], ...] = ()


# LLM: TuiRenderCacheStats 是只读诊断，不参与渲染决策或业务状态。
# 类用途: 暴露 block cache 命中、未命中和当前条目数，供压测验收。
@dataclass(frozen=True)
class TuiRenderCacheStats:
    hits: int
    misses: int
    entries: int


# LLM: 流式活动块（phase=delta/active thinking/compact/tool-input）每帧 seq 都变，渲染 miss 后若每次都全量
# 重渲染，万字级正文在 show_all 展开/滚动时每帧数十毫秒拖垮事件循环。短时间窗内复用
# 最近一次渲染，增量 0.15s 后自然追上，稳定块不受影响。
# 函数用途: 判断 block 是否处于逐帧更新的流式活动状态。
_LIVE_BLOCK_RENDER_THROTTLE_SECONDS = 0.15


def _is_live_stream_block(block: TuiBlock) -> bool:
    if block.phase == "delta":
        return True
    return block.role in {"thinking", "compact", "tool_input"} and block.phase not in TERMINAL_RENDER_PHASES


# LLM: 字符记账只需要量级正确：按 fragment 可见文本长度求和，不含样式与对象开销；
# 目的是给渲染缓存一个可验证的上界，不是精确内存统计，也不参与任何业务判定。
# 函数用途: 估算一个 block 渲染结果占用的字符量。
def _rendered_char_count(lines: tuple[FormattedLine, ...]) -> int:
    return sum(len(str(fragment[1])) for line in lines for fragment in line)


# LLM: TuiBlockRenderCache 只缓存 immutable block 输出；key 含 updated_seq/宽度/显示模式，绝不跨语义版本复用。
# 类用途: 避免流式活动块更新时重新渲染全部稳定历史，并把缓存限制在显式上限内。
class TuiBlockRenderCache:
    # LLM: max_entries 与 max_chars 都是 UI 内存边界，裁剪只影响重渲染性能，不改变 transcript 事实。
    # 流式活动块每更新一版就会新增一个 key（含 updated_seq），只限条数时缓存字符量实测可达最终文本数百倍；
    # 因此同时按字符预算淘汰，并在同一 block 出新版本时立刻丢掉旧版本。
    # 函数用途: 创建一个有界（条数 + 字符）的 LRU block cache。
    def __init__(self, *, max_entries: int = 20_000, max_chars: int = 6_000_000) -> None:
        self.max_entries = max(1, int(max_entries or 1))
        self.max_chars = max(1, int(max_chars or 1))
        self._entries: OrderedDict[tuple[Any, ...], tuple[FormattedLine, ...]] = OrderedDict()
        self._entry_chars: dict[tuple[Any, ...], int] = {}
        self._chars = 0
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
        if _is_live_stream_block(block):
            previous_key = self._live_last_key.get(block.block_id)
            if previous_key is not None and previous_key != key:
                # 同一活动块只保留最新渲染版本：旧版本除了占内存没有其它读取方。
                self._drop_entry(previous_key)
            self._live_last_key[block.block_id] = key
            self._live_last_at[block.block_id] = context.now
        self._entries[key] = rendered
        self._entries.move_to_end(key)
        self._entry_chars[key] = _rendered_char_count(rendered)
        self._chars += self._entry_chars[key]
        self._misses += 1
        while self._entries and (
            len(self._entries) > self.max_entries or self._chars > self.max_chars
        ):
            self._drop_entry(next(iter(self._entries)))
        return rendered

    # LLM: 淘汰与显式丢弃共用同一条记账路径，字符预算不能因漏减而失真。
    # 函数用途: 移除一个缓存条目并同步字符记账。
    def _drop_entry(self, key: tuple[Any, ...]) -> None:
        if self._entries.pop(key, None) is None:
            return
        self._chars -= self._entry_chars.pop(key, 0)
        if self._chars < 0:
            self._chars = 0

    # LLM: stats 只读取计数，调用方不能取得内部 OrderedDict 后篡改缓存。
    # 函数用途: 返回当前缓存诊断快照。
    def stats(self) -> TuiRenderCacheStats:
        return TuiRenderCacheStats(self._hits, self._misses, len(self._entries))

# LLM: render_tui_snapshot 只组合 snapshot 中 typed block/order；active/stable 通过 created_seq 合流但不改写 reducer。
# 终端交互's SpinnerWithVerb 位于消息区末尾，因此 main 活动从 background block 单独放到 transcript 末尾。
# 函数用途: 渲染完整画面并输出正文块的起始行，供翻页保持阅读锚点；不读取历史或执行任务。
def render_tui_snapshot(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
    *,
    cache: TuiBlockRenderCache | None = None,
) -> TuiRenderFrame:
    lines: list[FormattedLine] = []
    offsets: list[tuple[str, int]] = []
    active_thinking = tuple(
        block
        for block in snapshot.active_blocks
        if block.role == "thinking"
        and block.phase not in TERMINAL_RENDER_PHASES
    )
    active_activity = tuple(
        block for block in active_thinking if not (block.text or block.detail)
    )
    suppress_all_thinking = bool(snapshot.permission) or any(
        block.role in {"compact", "tool_input"} for block in snapshot.active_blocks
    )
    fixed_thinking = active_thinking if suppress_all_thinking else active_activity
    fixed_background_ids = {
        block.block_id
        for block in snapshot.active_blocks
        if block.role == "background"
        and block.phase not in TERMINAL_RENDER_PHASES
    }
    fixed_todo_ids = {
        block.block_id
        for block in snapshot.active_blocks
        if block.role == "todo"
    }
    active_activity_ids = {
        block.block_id for block in fixed_thinking
    } | fixed_background_ids | fixed_todo_ids
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
        if rendered:
            offsets.append((block.block_id, len(lines) - len(rendered)))
    for block in visible_activity:
        rendered = cache.render(block, context) if cache is not None else _render_block(block, context)
        _append_block(lines, rendered)
    background = next(
        (
            block
            for block in snapshot.active_blocks
            if block.role == "background"
            and block.phase not in TERMINAL_RENDER_PHASES
        ),
        None,
    )
    if background is not None:
        rendered = (
            cache.render(background, context)
            if cache is not None
            else _render_block(background, context)
        )
        _append_block(lines, rendered)
    overlay = _render_permission(snapshot.permission, context) if snapshot.permission else ()
    return sanitize_tui_render_frame(
        TuiRenderFrame(
            tuple(lines),
            tuple(overlay),
            _render_input_status(snapshot, context),
            _render_fixed_todo(snapshot, context),
            _render_fixed_agent_panel(snapshot, context),
            _render_footer(snapshot, context),
            block_line_offsets=tuple(offsets),
        )
    )


# LLM: This is the final TUI display-security chokepoint. It must preserve style and mouse
# handlers byte-for-byte while removing terminal controls from every visible text fragment.
# The operation is idempotent so transcript/search decorators can safely pass through it again.
# 函数用途: 在画面交给 prompt_toolkit 前统一净化 transcript、覆盖层、输入状态、Todo、代理面板和 footer，确保
# provider、工具、路径和搜索内容都不能向宿主终端注入控制序列。
def sanitize_tui_render_frame(frame: TuiRenderFrame) -> TuiRenderFrame:
    return TuiRenderFrame(
        transcript_lines=tuple(_sanitize_formatted_line(line) for line in frame.transcript_lines),
        overlay_lines=tuple(_sanitize_formatted_line(line) for line in frame.overlay_lines),
        input_status_lines=tuple(
            _sanitize_formatted_line(line) for line in frame.input_status_lines
        ),
        todo_lines=tuple(_sanitize_formatted_line(line) for line in frame.todo_lines),
        agent_lines=tuple(_sanitize_formatted_line(line) for line in frame.agent_lines),
        footer=_sanitize_formatted_line(frame.footer),
        block_line_offsets=frame.block_line_offsets,
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


# LLM: Empty waiting spinners follow 终端交互 and disappear behind a visible assistant stream;
# explicit thinking text stays in chronological transcript order and is never removed/reinserted.
# 函数用途: 选择本帧应显示在可滚动正文末尾的前台思考活动块。
def _visible_activity_blocks(
    snapshot: TuiViewSnapshot,
    activity_blocks: tuple[TuiBlock, ...],
) -> tuple[TuiBlock, ...]:
    if snapshot.permission is not None:
        return ()
    if any(block.role == "compact" for block in snapshot.active_blocks):
        return ()
    if any(block.role == "tool_input" for block in snapshot.active_blocks):
        return ()
    has_visible_assistant_stream = any(
        block.role == "assistant"
        and block.phase not in TERMINAL_RENDER_PHASES
        and bool(block.text or block.detail)
        for block in snapshot.active_blocks
    )
    if has_visible_assistant_stream:
        return ()
    return tuple(block for block in activity_blocks if block.role == "thinking")


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
    if block.role == "tool_input":
        return _render_tool_input_progress(block, context)
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


# LLM: The main row comes only from the canonical conversation activity
# projection and mirrors 终端交互's animated SpinnerWithVerb at transcript tail.
# Fixed prefix/suffix reserve their columns before the activity is truncated, so
# arbitrary thinking text can never wrap this removable display state.
# 函数用途: 在正文与 Context/Todo 之间显示主行；真实审批等待使用静态提示，失联保留上次状态，不推断任务已结束。
def _render_background_activity(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    main_activity = (
        dict(block.metadata.get("main_activity"))
        if isinstance(block.metadata.get("main_activity"), dict)
        else {}
    )
    active_task_count = max(0, _safe_render_int(block.metadata.get("active_task_count")))
    if not context.focused_agent_run_id and active_task_count <= 0:
        return ()
    # 面板的首次出现时间可能跨越多个普通回合；终端交互 也只用当前
    # query 的 loadingStartTime。主行缺少结构化任务起点时宁可显示 0:00。
    started_at = float(main_activity.get("started_at") or 0.0)
    rows = _subagent_activity_rows(block.metadata.get("subagents"))
    main_phase = str(main_activity.get("phase") or "").strip().lower()
    derived_label = _main_agent_activity_label(rows)
    main_label = str(main_activity.get("activity") or "").strip() or derived_label
    if main_phase in {"", "waiting", "finalizing"} and _active_subagent_count(rows):
        main_label = derived_label
    focused_status = context.focused_agent_status
    focused_terminal = bool(
        context.focused_agent_run_id
        and focused_status in FOCUSED_AGENT_TERMINAL_STATUSES
    )
    ended_at = _safe_render_float(main_activity.get("ended_at"))
    elapsed_end = ended_at if focused_terminal and ended_at > 0 else context.now
    elapsed = max(0, int(elapsed_end - started_at)) if started_at and elapsed_end else 0
    main_style = (
        "class:tui-error"
        if main_phase == "failed" or focused_status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR"}
        else "class:tui-subagent-done"
        if focused_status == "DONE"
        else "class:tui-subagent-running"
    )
    if context.background_sync_failed:
        prefix = (
            ("class:tui-context-warning", "! 状态未同步"),
            ("class:tui-muted", f" · {context.focused_agent_name} · 上次"),
        )
    elif focused_terminal:
        terminal_icon, terminal_label, terminal_style = _subagent_status_display(
            focused_status
        )
        prefix = (
            (terminal_style, f"{terminal_icon} "),
            ("class:tui-strong", context.focused_agent_name),
            (terminal_style, f" · {terminal_label}"),
        )
    elif main_phase == "waiting_permission":
        prefix = (
            ("class:tui-context-warning", "◌ 等待审批"),
            ("class:tui-muted", f" · {context.focused_agent_name if context.focused_agent_run_id else 'main'}"),
        )
    else:
        glyph = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
        prefix = (
            (main_style, f"{glyph} "),
            ("class:tui-strong", "Working"),
            (
                "class:tui-muted",
                f" · {context.focused_agent_name if context.focused_agent_run_id else 'main'}",
            ),
        )
    suffix: tuple[Fragment, ...] = (
        ("class:tui-muted", f" · {_format_activity_duration(elapsed)}"),
    )
    label = sanitize_terminal_text(" ".join(main_label.split()))
    label_width = max(
        0,
        context.width
        - display_width_fragments(prefix)
        - display_width_fragments(suffix)
        - (3 if label else 0),
    )
    fragments = list(prefix)
    if label and label_width > 0:
        fragments.append(
            ("class:tui-muted", f" · {_truncate_text(label, label_width)}")
        )
    fragments.extend(suffix)
    return (_truncate_formatted_line(tuple(fragments), context.width),)


# LLM: Exact Goal projections precede direct-child rows in 终端交互's
# coordinator region below the composer. The bounded viewport must always
# include the exact navigation id selected by the input layer so highlight and
# Enter target cannot diverge; neither row kind gains lifecycle authority here.
# 函数用途: 在输入框下方绘制 Goal 和直属子代理，并让方向键选中的项目始终留在八行可见窗口内。
def _render_subagent_panel(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    goals = _goal_activity_rows(block.metadata.get("goals"))
    rows = _subagent_activity_rows(block.metadata.get("subagents"))
    hidden = max(0, int(block.metadata.get("hidden_subagent_count") or 0))
    lines: list[FormattedLine] = []
    entries = [
        {**goal, "_panel_kind": "goal", "_selection_id": _goal_selection_id(goal)}
        for goal in goals
    ]
    entries.extend(
        {**row, "_panel_kind": "subagent", "_selection_id": str(row.get("run_id") or "")}
        for row in rows
    )
    visible_entries = _visible_agent_activity_entries(
        entries,
        selected_id=context.selected_agent_run_id,
        limit=8,
    )
    for entry in visible_entries:
        if entry.get("_panel_kind") == "goal":
            lines.extend(_render_goal_activity_row(entry, context))
        else:
            lines.extend(_render_subagent_activity_row(entry, context))
    hidden += max(0, len(entries) - len(visible_entries))
    if hidden:
        hidden_label = (
            f"还有 {hidden} 项未展开"
            if goals
            else f"还有 {hidden} 个子代理未展开"
        )
        lines.extend(
            wrap_fragments(
                (("class:tui-muted", hidden_label),),
                width=context.width,
                first_prefix=(("class:tui-muted", "    … "),),
                continuation_prefix=(("class:tui-muted", "      "),),
            )
        )
    return tuple(lines)


# LLM: This is a pure presentation window over the canonical Goal-then-child
# roster. Goal and run ids already occupy disjoint namespaces upstream.
# It never changes selection or run order; when selection falls past the first
# page, only the minimum leading rows are displaced to reveal that exact item.
# 函数用途: 裁出固定行数的 Goal/子代理窗口，避免游标已经移到隐藏项而屏幕仍停在前几项。
def _visible_agent_activity_entries(
    entries: list[dict[str, object]],
    *,
    selected_id: str,
    limit: int,
) -> list[dict[str, object]]:
    visible_limit = max(1, int(limit or 1))
    if len(entries) <= visible_limit:
        return entries
    selected = str(selected_id or "").strip()
    selected_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if str(entry.get("_selection_id") or "").strip() == selected
        ),
        -1,
    )
    start = (
        max(0, min(selected_index - visible_limit + 1, len(entries) - visible_limit))
        if selected_index >= visible_limit
        else 0
    )
    return entries[start : start + visible_limit]


# LLM: Goal metadata already passed two scalar whitelists; this final bounded
# shape check keeps malformed replay input out of the fixed panel.
# 函数用途: 从 Working block 中安全取出可选择的 Goal 展示行。
def _goal_activity_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    return [
        dict(item)
        for item in value[:16]
        if isinstance(item, dict) and str(item.get("goal_id") or "").strip()
    ]


# LLM: The display namespace must match TuiAgentNavigationState exactly; it is
# not accepted by any Gateway command or child endpoint.
# 函数用途: 为 Goal 行生成只供界面选择使用的稳定 ID。
def _goal_selection_id(row: dict[str, object]) -> str:
    return f"goal:{str(row.get('goal_id') or '').strip()}"


# LLM: Goal status, usage and objective are projections of exact ThreadGoal
# fields. Rendering never parses the objective or changes Goal lifecycle.
# 函数用途: 绘制固定 Goal 状态行；选中并回车后在原位展开完整目标与用量。
def _render_goal_activity_row(
    row: dict[str, object],
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    status = str(row.get("status") or "").strip().lower()
    icon, label, style = _goal_status_display(status, context)
    selection_id = _goal_selection_id(row)
    selected = selection_id == context.selected_agent_run_id
    expanded = selection_id == context.expanded_goal_id
    name = sanitize_terminal_text(str(row.get("name") or "").strip()) or "持续目标"
    objective = sanitize_terminal_text(" ".join(str(row.get("objective") or "").split()))
    elapsed = max(0, _safe_render_int(row.get("time_used_seconds")))
    tokens_used = max(0, _safe_render_int(row.get("tokens_used")))
    token_budget = max(0, _safe_render_int(row.get("token_budget")))
    suffix: list[Fragment] = [
        ("class:tui-muted", f" · {_format_activity_duration(elapsed)}"),
    ]
    if token_budget > 0:
        suffix.append(
            (
                "class:tui-muted",
                " · "
                f"{_format_compact_number(tokens_used)}/{_format_compact_number(token_budget)} tokens",
            )
        )
    row_prefix = "› " if selected else "  "
    fixed: tuple[Fragment, ...] = (
        ("class:tui-agent-selected" if selected else "class:tui-muted", row_prefix),
        (style, f"{icon} Goal "),
        ("class:tui-agent-selected" if selected else "class:tui-strong", name),
        (style, f" · {label}"),
        *suffix,
    )
    objective_width = max(0, context.width - display_width_fragments(fixed) - 3)
    fragments: list[Fragment] = [
        ("class:tui-agent-selected" if selected else "class:tui-muted", row_prefix),
        (style, f"{icon} Goal "),
        ("class:tui-agent-selected" if selected else "class:tui-strong", name),
        (style, f" · {label}"),
    ]
    if objective and objective_width > 0:
        fragments.append(("class:tui-muted", f" · {_truncate_text(objective, objective_width)}"))
    fragments.extend(suffix)
    lines: list[FormattedLine] = [
        _truncate_formatted_line(tuple(fragments), context.width)
    ]
    if expanded:
        lines.extend(_render_goal_activity_detail(row, context))
    return tuple(lines)


# LLM: Expanded Goal detail remains a read-only rendering of the selected exact
# row and never exposes internal task ids, paths, policies, or wake records.
# 函数用途: 在 Goal 行下展开完整目标、状态、用量和可用控制命令提示。
def _render_goal_activity_detail(
    row: dict[str, object],
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    objective = sanitize_terminal_text(str(row.get("objective") or "").strip())
    status = str(row.get("status") or "").strip().lower()
    _icon, label, _style = _goal_status_display(status, context)
    elapsed = max(0, _safe_render_int(row.get("time_used_seconds")))
    tokens_used = max(0, _safe_render_int(row.get("tokens_used")))
    token_budget = max(0, _safe_render_int(row.get("token_budget")))
    duration_seconds = max(0, _safe_render_int(row.get("duration_seconds")))
    usage = f"状态：{label} · 已运行 {_format_activity_duration(elapsed)}"
    if tokens_used or token_budget:
        usage += f" · token {_format_compact_number(tokens_used)}"
        if token_budget:
            usage += f"/{_format_compact_number(token_budget)}"
    if duration_seconds:
        usage += f" · 时限 {_format_activity_duration(duration_seconds)}"
    lines: list[FormattedLine] = []
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", objective or "（目标内容为空）"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "    目标："),),
            continuation_prefix=(("class:tui-muted", "          "),),
        )
    )
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", usage),),
            width=context.width,
            first_prefix=(("class:tui-muted", "    "),),
            continuation_prefix=(("class:tui-muted", "    "),),
        )
    )
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", "/goal pause · /goal resume · /goal clear"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "    控制："),),
            continuation_prefix=(("class:tui-muted", "          "),),
        )
    )
    return tuple(lines)


# LLM: Goal status styles are a pure mapping of the closed ThreadGoal enum.
# Unknown values stay visibly unknown and never acquire active semantics.
# 函数用途: 把 Goal 结构化状态映射为图标、中文标签和颜色。
def _goal_status_display(
    status: str,
    context: TuiRenderContext,
) -> tuple[str, str, str]:
    normalized = str(status or "").strip().lower()
    if normalized == "active":
        return (
            SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)],
            "进行中",
            "class:tui-subagent-running",
        )
    if normalized == "paused":
        return "Ⅱ", "已暂停", "class:tui-subagent-pending"
    if normalized == "blocked":
        return "!", "受阻", "class:tui-subagent-blocked"
    if normalized == "usage_limited":
        return "!", "额度受限", "class:tui-subagent-blocked"
    if normalized == "budget_limited":
        return "!", "预算已到", "class:tui-subagent-blocked"
    if normalized == "complete":
        return "✓", "已完成", "class:tui-subagent-done"
    return "?", "状态未知", "class:tui-muted"


# LLM: Rows are already whitelisted by runtime/reducer; this final shape check
# prevents malformed replay metadata from reaching the row renderer.
# 函数用途: 从 Working block 中安全取出直属子代理展示行。
def _subagent_activity_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    return [dict(item) for item in value[:64] if isinstance(item, dict)]


# LLM: This label is a view-only consequence of exact child statuses. It does
# not decide whether the main turn may close or whether a child is acceptable.
# 函数用途: 根据直属子代理状态说明主代理当前是在等待下级还是整理结果。
def _main_agent_activity_label(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "运行中"
    running = sum(
        1
        for row in rows
        if str(row.get("status") or "").upper()
        in {"PLANNING", "PENDING", "RUNNING", "BLOCKED", "PAUSED"}
    )
    if running:
        return f"等待 {running} 个子代理"
    return "整理结果中"


# LLM: Active child count reads canonical status only and exists solely to
# avoid showing a completed model-turn label while descendants still run.
# 函数用途: 统计代理面板里尚未结束的直属子代理数。
def _active_subagent_count(rows: list[dict[str, object]]) -> int:
    return sum(
        1
        for row in rows
        if str(row.get("status") or "").upper()
        in {"PLANNING", "PENDING", "RUNNING", "BLOCKED", "PAUSED"}
    )


# LLM: A child row mirrors 终端交互's width budgeting: canonical status and
# numeric suffixes are reserved first, then one bounded task description is
# truncated into the remaining columns. Attempts include normal child wakes and
# process resumes, so they do not prove failures and must not become retry counts.
# 函数用途: 显示子代理状态、职责、耗时和上下文；没有本代快照显示破折号，正常续作不标为失败重试。
def _render_subagent_activity_row(
    row: dict[str, object],
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    status = str(row.get("status") or "").strip().upper()
    lifecycle_phase = str(row.get("lifecycle_phase") or "").strip().lower()
    icon, label, style = _subagent_status_display(
        status,
        lifecycle_phase=lifecycle_phase,
    )
    if lifecycle_phase in {
        "starting",
        "waiting_first_event",
        "waiting_descendants",
        "running",
    }:
        icon = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
    raw_name = sanitize_terminal_text(
        str(row.get("name") or row.get("role") or "subagent").strip() or "subagent"
    )
    description = sanitize_terminal_text(
        " ".join(str(row.get("description") or "").split())
    )
    elapsed = _subagent_elapsed_seconds(row, context.now)
    context_tokens = max(0, _safe_render_int(row.get("context_tokens")))
    context_text = "—" if row.get("context_known") is False else _format_compact_number(context_tokens)
    compact_count = max(0, _safe_render_int(row.get("compact_count")))
    suffix: list[tuple[str, str]] = [
        ("class:tui-muted", f" · {_format_activity_duration(elapsed)}"),
        ("class:tui-muted", f" · ctx {context_text}"),
        ("class:tui-muted", f" · compact {compact_count}"),
    ]
    selected = str(row.get("run_id") or "").strip() == context.selected_agent_run_id
    row_prefix = "› " if selected else "  "
    fixed_without_name: tuple[Fragment, ...] = (
        ("class:tui-agent-selected" if selected else "class:tui-muted", row_prefix),
        (style, f"{icon} "),
        (style, f" · {label}"),
        *suffix,
    )
    description_separator_width = 3 if description else 0
    available_for_name_and_description = max(
        1,
        context.width - display_width_fragments(fixed_without_name) - description_separator_width,
    )
    preferred_name_width = min(28, max(1, display_width_text(raw_name)))
    minimum_description_width = min(12, display_width_text(description)) if description else 0
    name_width = max(
        1,
        min(preferred_name_width, available_for_name_and_description - minimum_description_width),
    )
    name = _truncate_text(raw_name, name_width)
    available_for_description = max(
        0,
        context.width
        - display_width_fragments(fixed_without_name)
        - display_width_text(name)
        - description_separator_width,
    )
    fragments: list[Fragment] = [
        ("class:tui-agent-selected" if selected else "class:tui-muted", row_prefix),
        (style, f"{icon} "),
        ("class:tui-agent-selected" if selected else "class:tui-strong", name),
        (style, f" · {label}"),
    ]
    if description and available_for_description > 0:
        fragments.append(
            ("class:tui-muted", f" · {_truncate_text(description, available_for_description)}")
        )
    fragments.extend(suffix)
    return (_truncate_formatted_line(tuple(fragments), context.width),)


# LLM: Status-to-style mapping is a pure display projection of the canonical
# enum. Unknown values stay neutral and visibly unknown.
# 函数用途: 把子代理结构化状态映射为图标、中文标签和颜色。
def _subagent_status_display(
    status: str,
    *,
    lifecycle_phase: str = "",
) -> tuple[str, str, str]:
    phase = str(lifecycle_phase or "").strip().lower()
    if status == "PLANNING" or phase == "queued":
        return "○", "排队中", "class:tui-subagent-pending"
    if phase == "waiting_descendants":
        return "●", "等待下级", "class:tui-subagent-running"
    if status == "PENDING" or phase == "starting":
        return "○", "启动中", "class:tui-subagent-pending"
    if status == "RUNNING" and phase == "waiting_first_event":
        return "●", "等待模型", "class:tui-subagent-running"
    if status == "RUNNING":
        return "●", "运行中", "class:tui-subagent-running"
    if status == "DONE":
        return "✓", "已完成", "class:tui-subagent-done"
    if status in {"BLOCKED", "PAUSED"}:
        return "!", "等待处理", "class:tui-subagent-blocked"
    if status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return "×", "失败", "class:tui-error"
    if status in {"CANCELLED", "ABANDONED", "TAKEN_OVER"}:
        return "–", "已停止", "class:tui-muted"
    return "?", status or "状态未知", "class:tui-muted"


# LLM: Elapsed time uses child timestamps only; the UI clock never writes back
# or decides whether a heartbeat is stale.
# 函数用途: 计算子代理本次展示的运行时长秒数。
def _subagent_elapsed_seconds(row: dict[str, object], now: float) -> int:
    created_at = _safe_render_float(row.get("created_at"))
    ended_at = _safe_render_float(row.get("ended_at"))
    end = ended_at if ended_at > 0 else max(0.0, float(now or 0.0))
    if created_at <= 0 or end <= 0:
        return 0
    return max(0, int(end - created_at))


# LLM: Duration formatting affects text only and remains stable at minute/hour
# boundaries for golden tests.
# 函数用途: 将秒数格式化为 m:ss 或 h:mm:ss。
def _format_activity_duration(seconds: int) -> str:
    value = max(0, int(seconds or 0))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# LLM: Invalid display numerics become zero and never affect canonical state.
# 函数用途: 安全读取 renderer 使用的整数。
def _safe_render_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# LLM: Invalid display timestamps become zero and never affect canonical state.
# 函数用途: 安全读取 renderer 使用的时间戳。
def _safe_render_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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


# LLM: cache key 不包含无关状态；Todo 的显示活动投影可能不改变原 block seq，必须独立入 key，空闲时排除 spinner。
# 函数用途: 生成可哈希的块渲染版本键，保证执行停止后不会复用旧动画，但保留模型清单事实。
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
    if block.role == "tool_input" and block.phase not in {
        "completed",
        "failed",
        "interrupted",
    }:
        key += _tool_input_animation_key(block, context)
    if block.role == "todo":
        active_execution = bool(block.metadata.get("active_execution"))
        key += (context.todos_expanded, active_execution)
        if active_execution and _todo_has_in_progress_items(block.metadata.get("items")):
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


# LLM: 工具参数活动行只按动画帧、已接收计数和自身起点秒数失效缓存；
# 不借用全局 spinner 的 token/stall 语义。
# 函数用途: 生成大工具参数临时行的最小动画缓存键。
def _tool_input_animation_key(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[Any, ...]:
    try:
        started_at = max(0.0, float(block.metadata.get("started_at") or 0.0))
        received_chars = max(0, int(block.metadata.get("received_chars") or 0))
    except (TypeError, ValueError):
        started_at = 0.0
        received_chars = 0
    elapsed = (
        int(max(0.0, context.now - started_at))
        if context.now > 0 and started_at > 0
        else 0
    )
    return (
        context.spinner_index % len(SPINNER_GLYPHS),
        received_chars,
        elapsed,
    )


# LLM: Background animation uses its own typed start timestamp and count; it
# must not borrow the completed foreground turn's token/stall metrics.
# 函数用途: 生成后台 Working 的缓存键；刷新失败时保持静态提示，不用动画冒充实时进展。
def _background_animation_key(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[Any, ...]:
    started_at = float(block.metadata.get("started_at") or 0.0)
    elapsed = max(0, int(context.now - started_at)) if started_at and context.now else 0
    return (
        0 if context.background_sync_failed else (
            context.spinner_index % math.lcm(len(SPINNER_GLYPHS), len("Working"))
        ),
        elapsed,
        int(block.metadata.get("active_task_count") or 0),
        context.background_sync_failed,
    )


# LLM: 欢迎卡保持 终端交互 宽/窄结构；实时确认的模型选择优先于启动块旧标签，不从响应正文猜模型。
# 函数用途: 渲染中文欢迎卡和提示，切模型后立即更新模型名，长名继续按列宽裁短。
def _render_welcome(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    version = str(block.metadata.get("version") or context.version)
    model = str(context.model_name or block.metadata.get("model") or "模型不可用")
    workspace = str(block.metadata.get("workspace") or context.workspace or ".")
    card_width = min(WELCOME_CARD_MAX_WIDTH, context.width)
    if card_width >= 80:
        lines = _wide_welcome_card(context.agent_name, version, model, workspace, card_width)
    else:
        lines = _narrow_welcome_card(context.agent_name, version, model, workspace, card_width)
    tip_text = "/help 查看命令 · /status 查看当前会话"
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
        (("class:tui-strong", "欢迎回来！"),),
        *TERMINAL_BUNNY_AVATAR,
        (("class:tui-muted", f"{model} · API" if model else "API"),),
        (("class:tui-muted", workspace),),
    )
    right_rows = (
        ("最近活动", "class:tui-welcome-heading"),
        ("暂无最近活动", "class:tui-muted"),
        (" " + "─" * max(0, right_width - 2) + " ", "class:tui-accent"),
        ("新内容", "class:tui-welcome-heading"),
        ("输入 /help 查看 my-agent 命令", "class:tui-muted"),
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
        (("class:tui-strong", "欢迎回来！"),),
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


# LLM: assistant marker 与 Markdown 只做视觉组合；typed process 仍是完整会话消息，
# 不能因为后面还有工具调用就折叠或染成思考色；只有思考块和快捷键提示使用灰色。
# 函数用途: 渲染完整助手 Markdown；包括插话答复和工作进展，均保持正常正文色。
def _render_assistant(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    content_width = max(1, context.width - 2)
    text = _bounded_render_text(
        block.text,
        max_lines=_ASSISTANT_RENDER_DETAIL_MAX_LINES if context.detailed_transcript else _ASSISTANT_RENDER_MAX_LINES,
        max_chars=(
            _ASSISTANT_RENDER_DETAIL_MAX_CHARS
            if context.detailed_transcript
            else _ASSISTANT_RENDER_MAX_CHARS
        ),
    )
    markdown_lines = render_markdown(text, MarkdownRenderContext(width=content_width))
    lines: list[FormattedLine] = []
    first_content = True
    for line in markdown_lines:
        # 每行只拼一次可见文本：既有实现把 fragments_text 走两遍（空行判断 + 折叠提示识别），
        # 长历史冷帧里这是每行一次的额外 join；识别规则本身不变。
        line_text = fragments_text(line)
        if not line_text:
            lines.append(())
            continue
        marker = "● " if first_content else "  "
        fold_hint = _is_fold_hint_text(line_text)
        if fold_hint:
            style = "class:tui-muted"
        else:
            style = "class:tui-assistant-marker" if first_content else ""
        if fold_hint:
            # 折叠提示不是模型答复，仍作为辅助文案浅灰显示。
            line = _append_terminal_role(line, "class:tui-muted")
        lines.append(((style, marker), *line))
        first_content = False
    return tuple(lines or [(("class:tui-assistant-marker", "●"),)])


# LLM: thinking 展开/折叠只由 context mode 与 typed phase 决定；实时计数只读已收到文本，
# 不推测模型总长度或完成率。预览预算、完整原文分页和终态自动折叠必须保持一致。
# 函数用途: 渲染活动思考及实时折叠数量，结束后恢复摘要；不为计数排版完整长文本。
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
            # 先限制Markdown输入，再按视口折叠；不能先排版十万字再丢掉大部分。
            preview, truncated = _thinking_preview_text(block.text, live=True)
            content_lines = _thinking_content_lines(preview, context, closed=False)
            if truncated or len(content_lines) > _THINKING_LIVE_MAX_LINES:
                lines.extend(content_lines[:_THINKING_LIVE_MAX_LINES])
                lines.extend(
                    _thinking_live_fold_hint(
                        block.text, preview,
                        hidden_preview_lines=max(0, len(content_lines) - _THINKING_LIVE_MAX_LINES),
                        context=context,
                    )
                )
            else:
                lines.extend(content_lines)
        return tuple(lines)
    detail = block.text or block.detail
    if not detail:
        return ()
    title = _thinking_title(block)
    # 对齐 终端交互 的 hidePastThinking：流式思考结束后只保留灰色摘要行；
    # Ctrl+O为详细预览，Ctrl+E为完整原文分页；折叠不删除typed block。
    lines: list[FormattedLine] = list(
        wrap_fragments(
            (("class:tui-thinking", f"{title}（Ctrl+O 展开）"),),
            width=context.width,
            first_prefix=(("class:tui-thinking", "∴ "),),
            continuation_prefix=(("class:tui-thinking", "  "),),
        )
    )
    if not context.detailed_transcript:
        return tuple(lines)
    preview, truncated = _thinking_preview_text(detail, live=False)
    content_lines = _thinking_content_lines(preview, context, closed=True)
    lines.extend(content_lines)
    if truncated:
        lines.append((("class:tui-muted", "… 思考预览已折叠，Ctrl+E 分页查看完整原文"),))
    return tuple(lines)


# LLM: 仅裁显示副本，限制Markdown每次排版字数/行数；完整typed block不变，不能用作模型上下文。
# 函数用途: 避免慢模型长思考每个delta或动画帧都重新排版全部历史文本。
def _thinking_preview_text(text: str, *, live: bool) -> tuple[str, bool]:
    char_limit, line_limit = (4_000, 50) if live else (12_000, 200)
    prefix = text[:char_limit]
    rows = prefix.split("\n", line_limit)
    return "\n".join(rows[:line_limit]), len(text) > char_limit or len(rows) > line_limit


# LLM: 计数基于原文换行和有界预览的排版行，两个口径分开显示；跨字符上限的半行
# 计入折叠原文。只用 count 的索引扫描，不复制尾部、不全量 Markdown 排版，不改上下文。
# 函数用途: 随流式增量刷新隐藏行数和接收字符数，单段无换行时也能看见新内容到达。
def _thinking_live_fold_hint(
    text: str, preview: str, *, hidden_preview_lines: int, context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    tail_start = len(preview)
    if text[tail_start:tail_start + 1] == "\n":
        tail_start += 1
    hidden_source_lines = (
        text.count("\n", tail_start) + int(not text.endswith("\n"))
        if tail_start < len(text) else 0
    )
    parts = []
    if hidden_source_lines:
        parts.append(f"已折叠 {hidden_source_lines:,} 行原文")
    if hidden_preview_lines:
        label = "另折叠" if parts else "已折叠"
        parts.append(f"{label} {hidden_preview_lines:,} 行预览")
    if not parts:
        parts.append("预览已折叠")
    parts.append(f"已接收 {len(text):,} 字符")
    return wrap_fragments(
        (("class:tui-thinking", " · ".join(parts) + "（Ctrl+O 后 Ctrl+E 查看完整原文）"),),
        width=context.width,
        first_prefix=(("class:tui-thinking", "  … "),),
        continuation_prefix=(("class:tui-thinking", "    "),),
    )


# LLM: Durable Compact renders provider-backed milestones only. Manual control operations cannot
# stream server stages, so they use an explicitly indeterminate moving bar rather than a fake
# percentage; renderer time never becomes compact authority.
# 函数用途: 在 Compact 运行时显示动画；有真实阶段时显示百分比，手动压缩只显示不定进度。
def _render_compact_progress(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    if block.phase in {"failed", "interrupted"}:
        label = (
            "上下文压缩已中断"
            if block.phase == "interrupted"
            else "上下文压缩失败，原上下文已保留"
        )
        error_code = str(block.metadata.get("error_code") or "").strip()
        if block.phase == "failed" and error_code:
            label = f"{label} · {error_code}"
        return wrap_fragments(
            (("class:tui-error", label),),
            width=context.width,
            first_prefix=(("class:tui-error", "! "),),
            continuation_prefix=(("class:tui-error", "  "),),
        )
    percent = min(100, max(0, int(block.metadata.get("percent") or 0)))
    stage_key = str(block.metadata.get("stage") or "preparing")
    stage = {
        "preparing": "准备中",
        "summarizing": "正在生成摘要",
        "measuring": "正在重新计量",
        "checkpointing": "正在写恢复点",
        "committing": "正在提交",
        "completed": "已完成",
        "candidate_discarded": "继续当前任务",
        "failed": "失败",
    }.get(stage_key, "处理中")
    bar_width = min(24, max(8, context.width - 48))
    indeterminate = block.metadata.get("indeterminate") is True
    if indeterminate:
        pulse_width = min(5, max(3, bar_width // 4))
        travel = max(1, bar_width - pulse_width)
        cycle = travel * 2
        offset = context.spinner_index % cycle
        start = offset if offset <= travel else cycle - offset
        meter = (
            "─" * start
            + "━" * pulse_width
            + "─" * (bar_width - start - pulse_width)
        )
        progress_text = "处理中"
    else:
        filled = min(bar_width, max(0, int(round(percent * bar_width / 100))))
        meter = "━" * filled + "─" * (bar_width - filled)
        progress_text = f"{percent}% · {stage}"
    source_kind = str(block.metadata.get("source_kind") or "")
    source_label = {
        COMPACT_SOURCE_TRANSCRIPT: "正在压缩会话上下文 ",
        COMPACT_SOURCE_ACTIVE_TURN: "正在整理工具上下文 ",
        COMPACT_SOURCE_TURN_LOCAL: "正在整理当前工具历史 ",
    }.get(source_kind, "正在压缩上下文 ")
    glyph = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
    return wrap_fragments(
        (
            ("class:tui-spinner-highlight", glyph + " "),
            ("class:tui-thinking", source_label),
            ("class:tui-thinking", f"[{meter}] {progress_text}"),
        ),
        width=context.width,
        continuation_prefix=(("class:tui-thinking", "  "),),
    )


# LLM: 该行只能读取已脱敏的工具名/累计字符数与 display timestamp；它不
# 展示参数片段，也不把 ready/字符量解释为工具执行或任务完成。
# 函数用途: 在大 write/apply_patch 参数仍生成时显示一条 终端交互 风格动画进度。
def _render_tool_input_progress(
    block: TuiBlock,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    glyph = SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)]
    tool = _tool_display_name(block.title or str(block.metadata.get("tool") or "Tool"))
    try:
        received_chars = max(0, int(block.metadata.get("received_chars") or 0))
        started_at = max(0.0, float(block.metadata.get("started_at") or 0.0))
    except (TypeError, ValueError):
        received_chars = 0
        started_at = 0.0
    details = [f"{_format_compact_number(received_chars)} chars"]
    if context.now > 0 and started_at > 0:
        details.append(_format_duration(max(0.0, context.now - started_at)))
    return wrap_fragments(
        (
            ("class:tui-spinner-highlight", glyph + " "),
            ("class:tui-thinking", f"正在准备 {tool} 参数"),
            ("class:tui-muted", f" · {' · '.join(details)}"),
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
# 字符预算先于 Markdown 排版生效（max_chars>0），行预算再作用一次；两者都只影响显示。
# 函数用途: 超长文本渲染前裁剪, 防 TUI 被输出洪水(含无换行巨行)拖垮。
def _bounded_render_text(text: str, *, max_lines: int, max_chars: int = 0) -> str:
    normalized = str(text or "")
    if max_chars > 0 and len(normalized) > max_chars:
        normalized = _clip_render_chars(normalized, max_chars)
    lines = normalized.split("\n")
    if len(lines) <= max_lines:
        return normalized
    head_count = max(1, max_lines * 2 // 3)
    tail_count = max(5, max_lines // 10)
    head = lines[:head_count]
    tail = lines[-tail_count:]
    hidden = len(lines) - head_count - tail_count
    return "\n".join(head) + f"\n… 中间 {hidden} 行已折叠 (ctrl+o 展开更多) …\n" + "\n".join(tail)


# LLM: 字符级折叠与行级折叠同语义：保留头尾、标出隐藏量、给同一个 Ctrl+O 提示；
# 只在渲染投影上工作，不写回 block.text，也不参与任何发给模型的上下文。
# 函数用途: 把超长正文裁到字符预算内，优先保住开头与结尾。
def _clip_render_chars(text: str, max_chars: int) -> str:
    head_chars = max(1, max_chars * 3 // 5)
    tail_chars = max(1, max_chars - head_chars)
    hidden = len(text) - head_chars - tail_chars
    return (
        f"{text[:head_chars]}\n… 中间 {hidden} 字符已折叠 (ctrl+o 展开更多) …\n{text[-tail_chars:]}"
    )


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


# LLM: 折叠提示识别只作用于 renderer 自己生成的固定投影，不得用于状态迁移或模型正文裁决；
# 入口接收已经拼好的可见文本，调用方不得再为同一行调用 fragments_text 第二次。
# 函数用途: 判断一行可见文本是否为 renderer 的中段折叠提示，以便整行使用浅灰提示色。
def _is_fold_hint_text(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("… 中间") and "行已折叠" in stripped


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


# LLM: Todo is a read-only projection. The header counts typed completed/running
# states and separately reports active child rows not represented by a visible Todo.
# The display-only active_execution projection gates animation, never ledger completion.
# 函数用途: 渲染四行任务窗口；有真实执行才闪动，空闲时把未勾完项显示为待继续，Ctrl-T 不改任务状态。
def _render_todo(block: TuiBlock, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    items = block.metadata.get("items")
    if not isinstance(items, list) or not items:
        return ()
    public_items = [item for item in items if isinstance(item, dict)]
    active_execution = bool(block.metadata.get("active_execution"))
    visible_items, hidden_count = _todo_window_items(
        public_items,
        expanded=context.todos_expanded,
    )
    completed_count = sum(
        1
        for item in public_items
        if str(item.get("status") or "").strip().lower() == "done"
    )
    in_progress_count = sum(
        1
        for item in public_items
        if str(item.get("status") or "").strip().lower() == "in_progress"
    )
    title = f"📋 任务清单 · 完成 {completed_count}/{len(public_items)}"
    if in_progress_count:
        label = "进行中" if active_execution else "待继续"
        title += f" · {label} {in_progress_count}"
    unrepresented_active_child_count = max(
        0,
        _safe_render_int(block.metadata.get("unrepresented_active_child_count")),
    )
    if unrepresented_active_child_count:
        title += f" · 另有 {unrepresented_active_child_count} 个子代理运行中"
    if hidden_count:
        title += "（Ctrl+T 展开）"
    elif context.todos_expanded and len(public_items) > TODO_COLLAPSED_MAX_ITEMS:
        title += "（Ctrl+T 收起）"
    lines: list[FormattedLine] = [
        (("class:tui-todo-title", _truncate_text(title, context.width)),)
    ]
    icon_map = {
        "done": "☑ ",
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
    for item in visible_items:
        status = str(item.get("status") or "pending").strip().lower()
        item_title = sanitize_terminal_text(
            " ".join(str(item.get("title") or item.get("id") or "").split())
        )
        if not item_title:
            continue
        style = style_map.get(status, "class:tui-todo-pending")
        icon = (
            SPINNER_GLYPHS[context.spinner_index % len(SPINNER_GLYPHS)] + " "
            if status == "in_progress" and active_execution
            else icon_map.get(status, "□ ")
        )
        prefix = f"  {icon}"
        fitted_title = _truncate_text(
            item_title,
            max(0, context.width - display_width_text(prefix)),
        )
        lines.append(((style, prefix), (style, fitted_title)))
    return tuple(lines)


# LLM: Selection uses typed statuses and original ledger positions only. It preserves one
# recent completion, then running rows, then upcoming work; prose and child names are irrelevant.
# 函数用途: 从完整 Todo 中挑出默认四条状态窗口，展开时原顺序返回全部。
def _todo_window_items(
    items: list[dict[str, object]],
    *,
    expanded: bool,
) -> tuple[list[dict[str, object]], int]:
    if expanded or len(items) <= TODO_COLLAPSED_MAX_ITEMS:
        return list(items), 0
    statuses = [str(item.get("status") or "pending").strip().lower() for item in items]
    running = [index for index, status in enumerate(statuses) if status == "in_progress"]
    open_items = [
        index for index, status in enumerate(statuses) if status in {"blocked", "pending"}
    ]
    anchor_start = min(running) if running else (open_items[0] if open_items else len(items))
    anchor_end = max(running) if running else anchor_start - 1
    priority: list[int] = []

    # LLM: This local deduplicator preserves priority order without mutating the source rows.
    # 函数用途: 将一个合法下标按优先顺序加入候选，重复项自动跳过。
    def add(index: int) -> None:
        if 0 <= index < len(items) and index not in priority:
            priority.append(index)

    previous_done = next(
        (
            index
            for index in range(anchor_start - 1, -1, -1)
            if statuses[index] in {"done", "skipped"}
        ),
        None,
    )
    if previous_done is not None:
        add(previous_done)
    for index in running:
        add(index)
    for index in range(max(0, anchor_end + 1), len(items)):
        if statuses[index] in {"blocked", "pending"}:
            add(index)
    for index in open_items:
        add(index)
    for index in range(anchor_start - 1, -1, -1):
        if statuses[index] in {"done", "skipped"}:
            add(index)
    for index in range(len(items)):
        add(index)
    selected = sorted(priority[:TODO_COLLAPSED_MAX_ITEMS])
    return [items[index] for index in selected], len(items) - len(selected)


# LLM: Animation eligibility comes only from typed in_progress status so render caching and
# periodic refresh never parse task titles or keep idle completed lists ticking.
# 函数用途: 判断一份 Todo 快照里是否存在需要动画的运行项。
def _todo_has_in_progress_items(value: object) -> bool:
    if not isinstance(value, list | tuple):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() == "in_progress"
        for item in value
    )


# LLM: 工具标题/状态/输出只读 structured block/display；diff、write、command 走专用富渲染，未知类型保留通用回退且任何正文都不参与 phase 判断。
# 函数用途: 渲染 终端交互 风格的工具标题、命令输出、写入预览和带行号增删高亮。
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
            max_chars=_TOOL_RENDER_DETAIL_MAX_CHARS,
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


# LLM: 工具标题参数只来自公共 metadata 的 invocation 或已脱敏 display.path；实时/恢复共用，不回读原始工具参数。
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
        text = block.text or f"Context compacted · generation {generation}"
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
# LLM: 审批只投影精确调用与选项；F4 入口允许用户主动切模式，不把可见文案变成自动授权。
# 函数用途: 渲染底部审批面板，并提示如何调整后续主/子代理的审批方式。
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
            (("", "是否继续执行？"),),
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
    hint = " Esc 取消 · F4 权限模式"
    if feedback_enabled and not permission.feedback_mode:
        hint += " · Tab 补充说明"
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", hint.strip()),),
            width=context.width,
            first_prefix=(("class:tui-muted", " "),),
            continuation_prefix=(("class:tui-muted", " "),),
        )
    )
    return tuple(lines)


# LLM: 活动判断来自前台 phase 或后台 typed count；不因前台 worker 空闲就把后台插话显示为已结束。
# 函数用途: 为输入回执和快捷键说明提供一致的活动状态，不修改任何任务状态。
def _has_running_conversation(snapshot: TuiViewSnapshot) -> bool:
    return snapshot.status.phase in {"running", "interrupting"} or any(
        block.role == "background" and _safe_render_int(block.metadata.get("active_task_count")) > 0
        for block in snapshot.active_blocks
    )


# LLM: Input status owns only pending/queue receipts, typed context metrics and
# stash state. Todo and agent panels have separate fixed layout regions.
# 函数用途: 在输入框上方固定显示待插入消息、上下文和草稿状态。
def _render_input_status(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    lines: list[FormattedLine] = []
    receipt_count = len(snapshot.pending_steers) + len(snapshot.queued_inputs)
    if receipt_count > INPUT_RECEIPT_EXPANDED_ITEM_LIMIT:
        lines.extend(
            _render_compact_input_receipts(
                snapshot.pending_steers,
                snapshot.queued_inputs,
                context,
                turn_active=_has_running_conversation(snapshot),
            )
        )
    else:
        if snapshot.pending_steers:
            lines.extend(
                _render_pending_steers(
                    snapshot.pending_steers,
                    context,
                    turn_active=_has_running_conversation(snapshot),
                )
            )
        if snapshot.queued_inputs:
            lines.extend(_render_queue(snapshot.queued_inputs, context))
    if context.context_usage is not None:
        lines.extend(
            _render_context_usage(
                context.context_usage,
                context.width,
                compact_count=context.compact_count,
            )
        )
    elif context.compact_count > 0:
        lines.extend(
            wrap_fragments(
                (
                    (
                        "class:tui-muted",
                        f"Context 将在下次模型调用时刷新 · compact {context.compact_count}",
                    ),
                ),
                width=context.width,
                first_prefix=(("class:tui-context-label", "  ◉ "),),
                continuation_prefix=(("class:tui-context-label", "    "),),
            )
        )
    if context.has_stash:
        lines.extend(
            wrap_fragments(
                (("class:tui-muted", "Stashed (auto-restores after submit)"),),
                width=context.width,
                first_prefix=(("class:tui-muted", "  › "),),
                continuation_prefix=(("class:tui-muted", "    "),),
            )
        )
    lines.extend(render_model_metrics(dict(context.model_metrics), context.width))
    return tuple(lines)


# LLM: Todo is selected from the one reducer-owned task_progress block and may
# overlay exact direct-child statuses for display. Auto-seeded items whose id is
# an exact visible child run id are omitted because the coordinator panel owns
# those rows; active children without an explicit visible Todo link remain a
# separate header count. Snapshot activity gates the local animation projection only;
# the canonical ledger remains unchanged even if the model finishes with incomplete items.
# 函数用途: 生成输入框上方清单、投影真实执行活动及关联子代理状态；空闲保留未完成项但停止闪动，不代替模型打勾。
def _render_fixed_todo(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    todo = next(
        (block for block in reversed(snapshot.active_blocks) if block.role == "todo"),
        None,
    )
    if todo is None:
        return ()
    todo = replace(todo, metadata={**todo.metadata, "active_execution": snapshot.has_active_work})
    background = next(
        (
            block
            for block in snapshot.active_blocks
            if block.role == "background"
            and block.phase not in TERMINAL_RENDER_PHASES
        ),
        None,
    )
    if background is None:
        return _render_todo(todo, context)
    child_rows = _subagent_activity_rows(background.metadata.get("subagents"))
    statuses = _todo_child_statuses(child_rows)
    child_run_ids = {
        str(row.get("run_id") or "").strip()
        for row in child_rows
        if str(row.get("run_id") or "").strip()
    }
    items = todo.metadata.get("items")
    if not isinstance(items, list):
        return _render_todo(todo, context)
    visible_items = [
        item
        for item in items
        if not (
            isinstance(item, dict)
            and str(item.get("id") or "").strip() in child_run_ids
        )
    ]
    visible_item_ids = {
        str(item.get("id") or "").strip()
        for item in visible_items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    unrepresented_active_child_count = _unrepresented_active_child_count(
        child_rows,
        visible_item_ids,
    )
    projected_metadata = {
        **todo.metadata,
        "items": visible_items,
        "unrepresented_active_child_count": unrepresented_active_child_count,
    }
    if not statuses:
        return _render_todo(
            replace(todo, metadata=projected_metadata),
            context,
        )
    projected = [
        _todo_item_with_child_status(item, statuses)
        if isinstance(item, dict)
        else item
        for item in visible_items
    ]
    return _render_todo(
        replace(todo, metadata={**projected_metadata, "items": projected}),
        context,
    )


# LLM: This mapping uses exact child run ids and explicit covers ids with
# canonical child status only; titles, goals, summaries and output never affect it.
# 函数用途: 把派工清单中的子代理项投影为完成、阻塞、跳过或进行中图标。
def _todo_item_with_child_status(
    item: dict[str, object],
    statuses: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    run_id = str(item.get("id") or "")
    linked_statuses = statuses.get(run_id, ())
    if not linked_statuses:
        return dict(item)
    if any(
        status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "BLOCKED", "PAUSED"}
        for status in linked_statuses
    ):
        display_status = "blocked"
    elif any(
        status in {"PLANNING", "PENDING", "RUNNING"}
        for status in linked_statuses
    ):
        display_status = "in_progress"
    elif all(status == "DONE" for status in linked_statuses):
        display_status = "done"
    elif all(
        status in {"DONE", "CANCELLED", "ABANDONED", "TAKEN_OVER"}
        for status in linked_statuses
    ):
        display_status = "skipped"
    else:
        return dict(item)
    return {**item, "status": display_status}


# LLM: One child may explicitly cover multiple Todo ids and multiple children
# may share one id. The join remains exact and status aggregation stays view-only.
# 函数用途: 建立 Todo 项 ID 到直属子代理状态的结构化关联表。
def _todo_child_statuses(
    rows: list[dict[str, object]],
) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    for row in rows:
        status = str(row.get("status") or "").upper()
        run_id = str(row.get("run_id") or "").strip()
        raw_progress_ids = row.get("progress_item_ids")
        progress_ids = (
            [str(value).strip() for value in raw_progress_ids if str(value or "").strip()]
            if isinstance(raw_progress_ids, list | tuple)
            else []
        )
        for item_id in dict.fromkeys(([run_id] if run_id else []) + progress_ids):
            collected.setdefault(item_id, []).append(status)
    return {key: tuple(values) for key, values in collected.items()}


# LLM: This count uses only canonical active child statuses and explicit
# progress_item_ids. It describes display coverage and must never infer a Todo link
# from child names, goals, descriptions or run output.
# 函数用途: 统计仍在运行但没有映射到当前可见 Todo 项的直属子代理，避免清单全勾时误显得已经停工。
def _unrepresented_active_child_count(
    rows: list[dict[str, object]],
    visible_item_ids: set[str],
) -> int:
    count = 0
    for row in rows:
        if str(row.get("status") or "").strip().upper() not in {
            "PLANNING",
            "PENDING",
            "RUNNING",
        }:
            continue
        raw_progress_ids = row.get("progress_item_ids")
        progress_ids = (
            {
                str(value).strip()
                for value in raw_progress_ids
                if str(value or "").strip()
            }
            if isinstance(raw_progress_ids, list | tuple)
            else set()
        )
        if progress_ids & visible_item_ids:
            continue
        count += 1
    return count


# LLM: The coordinator panel consumes exact Goal and child rows from the
# removable background activity block and stays outside transcript/history like
# 终端交互. An active or paused Goal keeps the panel visible without a task.
# 函数用途: 生成输入框下方的 Goal/直属子代理固定面板；两者都没有时不占高度。
def _render_fixed_agent_panel(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    background = next(
        (
            block
            for block in snapshot.active_blocks
            if block.role == "background"
            and block.phase not in TERMINAL_RENDER_PHASES
        ),
        None,
    )
    return _render_subagent_panel(background, context) if background is not None else ()


# LLM: The persistent strip shows total usage, the canonical committed compact count, and the
# exact trigger derived from the same provider-visible token budget.
# Protocol-specific prompt/messages/tools breakdown stays in /context because text mode folds
# messages and tools into prompt and would otherwise render misleading zeroes.
# 函数用途: 常驻行显示上下文总量、占比、已压缩次数和自动压缩点；详细构成留给 /context。
def _render_context_usage(
    usage: TuiContextUsage,
    width: int,
    *,
    compact_count: int = 0,
) -> tuple[FormattedLine, ...]:
    available = max(1, int(width or 1))
    current = max(0, int(usage.current_tokens or 0))
    window = max(0, int(usage.context_window_tokens or 0))
    trigger = max(0, int(usage.compact_trigger_tokens or 0))
    percent = min(999, round(current * 100 / window)) if window > 0 else 0
    trigger_percent = (
        min(100, max(0, round(trigger * 100 / window)))
        if trigger > 0 and window > 0
        else 0
    )
    style = _context_usage_style(current, trigger, window)
    estimate = "~" if usage.estimated else ""
    compact = max(0, int(compact_count or 0))

    if available >= 72:
        text = (
            f"Context {estimate}{_format_compact_number(current)}/{_format_compact_number(window)}"
            f" · {percent}% · compact {compact} · 压缩点 {trigger_percent}%"
        )
    elif available >= 40:
        text = (
            f"Ctx {estimate}{_format_compact_number(current)}/{_format_compact_number(window)}"
            f" · {percent}% · c{compact} · 点{trigger_percent}%"
        )
    else:
        text = f"Ctx {percent}% · c{compact} · 点{trigger_percent}%"
    return wrap_fragments(
        ((style, text),),
        width=available,
        first_prefix=(("class:tui-context-label", "  ◉ "),),
        continuation_prefix=(("class:tui-context-label", "    "),),
    )


# LLM: `/context` in rich TUI must render the exact same TuiContextUsage object as the fixed
# strip. It may explain numeric components but must not recompute history or compact policy.
# 函数用途: 把底部状态栏正在使用的上下文快照展开成普通用户看得懂的详细中文报告。
def render_tui_context_usage_report(
    usage: TuiContextUsage | None,
    *,
    compact_count: int = 0,
) -> str:
    compact = max(0, int(compact_count or 0))
    if usage is None:
        return (
            "上下文：尚无最近一次模型调用前的真实快照。\n"
            "发送一条普通消息后，/context 会与底部 Context 使用同一份数据刷新。\n"
            f"已完成 compact：{compact} 次"
        )
    current = max(0, int(usage.current_tokens or 0))
    window = max(0, int(usage.context_window_tokens or 0))
    trigger = max(0, int(usage.compact_trigger_tokens or 0))
    percent = current * 100.0 / window if window > 0 else 0.0
    filled = min(20, max(0, int(round(min(100.0, percent) / 5.0))))
    meter = "█" * filled + "░" * (20 - filled)
    accuracy = "估算" if usage.estimated else "供应商计量"
    lines = [
        "上下文用量（与底部状态栏同一份最近快照）",
        (
            f"总量（{accuracy}）：{meter} {current:,} / {window:,} tokens"
            f"（{percent:.1f}%）"
            if window > 0
            else f"总量（{accuracy}）：{current:,} tokens；模型窗口未知"
        ),
    ]
    if usage.protocol == "native":
        lines.extend(
            (
                f"固定与本轮提示：{max(0, int(usage.prompt_tokens or 0)):,} tokens",
                f"对话与工具消息：{max(0, int(usage.messages_tokens or 0)):,} tokens",
                f"待注入运行说明：{max(0, int(usage.runtime_guidance_tokens or 0)):,} tokens",
                f"工具定义：{max(0, int(usage.tool_schema_tokens or 0)):,} tokens",
            )
        )
    else:
        lines.append(
            f"合并后的提示输入：{max(0, int(usage.prompt_tokens or current)):,} tokens"
        )
        lines.append("当前接口未把消息、运行说明和工具定义可靠拆开。")
    if trigger > 0:
        trigger_percent = trigger * 100.0 / window if window > 0 else 0.0
        remaining = max(0, trigger - current)
        state = "已到压缩点" if remaining == 0 else f"还差约 {remaining:,} tokens"
        lines.append(
            f"自动 compact 压缩点：{trigger:,} tokens"
            + (f"（{trigger_percent:.1f}%）" if window > 0 else "")
            + f"；{state}"
        )
    else:
        lines.append("自动 compact 压缩点：当前模型窗口不足，暂无法计算")
    lines.append(f"已完成 compact：{compact} 次")
    return "\n".join(lines)


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
# move. A terminal child changes only the truthful label: absence of a consumed event cannot be
# presented as future delivery or promoted to user history.
# 函数用途: 显示尚未获模型消费确认的补充消息；子代理结束后明确标为不再自动重发。
def _render_pending_steers(
    pending_steers: tuple[TuiPendingSteer, ...],
    context: TuiRenderContext,
    *,
    turn_active: bool = True,
) -> tuple[FormattedLine, ...]:
    if context.width < 4:
        return ()
    child_terminal = bool(
        context.focused_agent_run_id
        and context.focused_agent_status in FOCUSED_AGENT_TERMINAL_STATUSES
    )
    queued = tuple(item for item in pending_steers if item.state != "submitted")
    submitted = tuple(item for item in pending_steers if item.state == "submitted")
    lines: list[FormattedLine] = []
    if queued:
        if child_terminal:
            label = "子代理已结束；以下插话未获模型消费确认，不会自动重发"
        elif turn_active:
            label = "将在下一次工具调用后送入当前回合"
        else:
            # 主回合已经终态:再承诺"下一次工具调用"就是假话(那一轮不会再有工具调用)。
            label = "当前回合已结束；以下插话未获模型消费确认"
        lines.extend(_pending_steer_label(label, context))
        for item in queued:
            lines.extend(_render_pending_input_message(item.text, context, italic=False))
    if submitted:
        # 正文已经按原提交位置进入上方历史,这里只说明"已提交/未确认"的真实状态,不重复正文,
        # 也绝不再说"将在下一次工具调用后送入"(那会让用户以为还没送进去)。
        if child_terminal:
            label = "子代理已结束；以下插话已送入但未获模型确认，不会自动重发"
        elif turn_active:
            label = "已送入当前回合，等待模型回应"
        else:
            label = "已送入当前回合但未获模型确认；不会自动重发"
        lines.extend(_pending_steer_label(label, context))
    return tuple(lines)


# LLM: 等待区标题只是文案投影,不参与任何状态判定;身份与状态完全来自 typed 事件。
# 函数用途: 按统一缩进渲染一条等待区说明行。
def _pending_steer_label(label: str, context: TuiRenderContext) -> tuple[FormattedLine, ...]:
    return wrap_fragments(
        (("class:tui-muted", label),),
        width=context.width,
        first_prefix=(("class:tui-muted", "• "),),
        continuation_prefix=(("class:tui-muted", "  "),),
    )


# LLM: A receipt flood stays fully authoritative in TuiViewSnapshot, while this projection keeps
# only one bounded preview per group. It mirrors 终端交互's queue-placeholder geometry and must
# never delete, reorder, consume, or acknowledge an underlying input.
# 函数用途: 当补充消息和后续队列很多时压成最多五行摘要，保证小终端仍能显示正文与输入框。
def _render_compact_input_receipts(
    pending_steers: tuple[TuiPendingSteer, ...],
    queued_inputs: tuple[TuiQueuedInput, ...],
    context: TuiRenderContext,
    *,
    turn_active: bool = True,
) -> tuple[FormattedLine, ...]:
    if context.width < 4:
        return ()
    child_terminal = bool(
        context.focused_agent_run_id
        and context.focused_agent_status in FOCUSED_AGENT_TERMINAL_STATUSES
    )
    lines: list[FormattedLine] = []

    def append_single_line(text: str, *, italic: bool = False) -> None:
        style = "class:tui-muted italic" if italic else "class:tui-muted"
        leading_spaces = len(str(text or "")) - len(str(text or "").lstrip(" "))
        fitted = _fit_text(
            " " * leading_spaces + " ".join(str(text or "").split()),
            context.width,
            "left",
        ).rstrip()
        lines.append(((style, fitted),) if fitted else ())

    queued_steers = tuple(item for item in pending_steers if item.state != "submitted")
    submitted_steers = tuple(item for item in pending_steers if item.state == "submitted")
    if queued_steers:
        # 与完整等待视图同源的真实终态:主回合/子代理已结束时不得再承诺"等待接收"或"下一次工具调用"。
        if child_terminal:
            pending_label = "• 子代理已结束；插话未获消费确认"
        elif turn_active:
            pending_label = "• 等待当前回合接收"
        else:
            pending_label = "• 当前回合已结束；插话未获消费确认"
        append_single_line(f"{pending_label}（{len(queued_steers)} 条）")
        append_single_line(f"  ↳ {queued_steers[0].text}")
    if submitted_steers:
        # 已提交项的正文已经在历史里,压缩视图只报数量与真实状态,不重复正文。
        if child_terminal:
            submitted_label = "• 子代理已结束；插话已送入但未获确认"
        elif turn_active:
            submitted_label = "• 已送入当前回合（等待模型回应）"
        else:
            submitted_label = "• 已送入当前回合但未获模型确认"
        append_single_line(f"{submitted_label}（{len(submitted_steers)} 条）")
    if queued_inputs:
        append_single_line(f"• 已排队的后续消息（{len(queued_inputs)} 条）")
        append_single_line(f"  ↳ {queued_inputs[0].text}", italic=True)
        append_single_line("    ↑ 取回并编辑排队消息")
    return tuple(lines)


# LLM: queue renderer uses reducer order and stays in the fixed composer-adjacent pane. Chinese
# display copy must not disappear when the user scrolls away from the transcript bottom.
# 函数用途: 把运行中排队的下一回合输入用中文固定显示在输入框上方。
def _render_queue(
    queued_inputs: tuple[TuiQueuedInput, ...],
    context: TuiRenderContext,
) -> tuple[FormattedLine, ...]:
    if context.width < 4:
        return ()
    lines: list[FormattedLine] = []
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", "已排队的后续消息"),),
            width=context.width,
            first_prefix=(("class:tui-muted", "• "),),
            continuation_prefix=(("class:tui-muted", "  "),),
        )
    )
    for item in queued_inputs:
        lines.extend(_render_pending_input_message(item.text, context, italic=True))
    lines.extend(
        wrap_fragments(
            (("class:tui-muted", "↑ 取回并编辑排队消息"),),
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


# LLM: footer 只读状态；主代理中断与目标暂停必须区分，子代理仍走停止语义，不改变控制权限。
# 函数用途: 展示当前页面可用按键，避免把 Esc 中断一轮误解为已暂停整个 Goal。
def _render_footer(snapshot: TuiViewSnapshot, context: TuiRenderContext) -> FormattedLine:
    if snapshot.permission is not None:
        return ()
    if any(block.role == "connection" for block in snapshot.active_blocks):
        text = "  Connecting…"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if context.is_pasting:
        text = "  Pasting text…"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if context.background_sync_failed:
        text = "  状态刷新失败，显示上次状态；正在重试"
        return (("class:tui-context-warning", _fit_text(text, context.width, "left").rstrip()),)
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
    # 网络回执、复制和停止反馈必须短暂覆盖常驻快捷键，否则用户只会看见
    # “Ctrl+G/Esc”而误以为刚才的输入或操作没有发生。
    if context.notice:
        return ((
            "class:tui-muted",
            _fit_text("  " + context.notice, context.width, "left").rstrip(),
        ),)
    history_hint = _history_mouse_hint(context)
    if context.focused_agent_run_id:
        if context.focused_agent_status in {
            "DONE",
            "FAILED",
            "BLOCKED",
            "CHANNEL_ERROR",
            "TIMEOUT",
            "CANCELLED",
            "ABANDONED",
            "TAKEN_OVER",
        }:
            text = f"  Ctrl+G 返回 · 已结束，只读 · {history_hint}"
        else:
            text = f"  Ctrl+G 返回 · Esc 停止 · {history_hint}"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if context.selected_agent_run_id:
        if context.selected_agent_run_id.startswith("goal:"):
            action = "Enter 查看/编辑 Goal"
            text = f"  ↑↓ 选择 · {action} · {history_hint}"
            if context.expanded_goal_id:
                text = f"  Ctrl+G 收起 Goal · ↑↓ 选择 · {history_hint}"
        else:
            text = f"  ↑↓ 选择 · Enter 查看 · {history_hint}"
        if _has_running_conversation(snapshot):
            text += " · Esc 中断本轮"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    if _has_running_conversation(snapshot):
        text = f"  Esc 中断本轮 · /stop 暂停任务 · {history_hint}"
        return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)
    text = f"  ? 快捷键 · F4 权限 · {history_hint}"
    return (("class:tui-muted", _fit_text(text, context.width, "left").rstrip()),)


# LLM: footer 必须从 typed mouse mode 说明当前真实交互；不得固定显示 F6 滚轮而在默认已开启时误导用户。
# 函数用途: 生成当前鼠标模式下的历史浏览与 F6 备用模式提示。
def _history_mouse_hint(context: TuiRenderContext) -> str:
    if context.mouse_capture_enabled:
        return "滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式"
    return "PgUp/Ctrl+Home 历史 · F6 恢复滚轮"


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
        return "等待授权…"
    if block.phase in {"started", "delta", "running"}:
        return block.detail or "Running…"
    if block.phase == "failed":
        return block.detail or "Failed"
    if block.phase == "interrupted":
        return block.detail or "已中断"
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


# LLM: Single-row panels must not wrap even when a name, localized status, or
# numeric suffix exceeds the terminal. This helper preserves fragment styles and
# handlers while clipping by display columns and adding one visible ellipsis.
# 函数用途: 把已分样式的子代理行裁到终端宽度内，避免中文导致换行或对齐错位。
def _truncate_formatted_line(line: FormattedLine, width: int) -> FormattedLine:
    limit = max(0, int(width or 0))
    if limit <= 0:
        return ()
    if display_width_fragments(line) <= limit:
        return line
    target = max(0, limit - 1)
    used = 0
    rendered: list[Fragment] = []
    ellipsis_style = "class:tui-muted"
    for fragment in line:
        style, text, *tail = fragment
        kept = ""
        for char in str(text or ""):
            char_width = max(0, wcswidth(char))
            if used + char_width > target:
                break
            kept += char
            used += char_width
        if kept:
            rendered.append((style, kept, *tail))
            ellipsis_style = style
        if kept != str(text or ""):
            break
    rendered.append((ellipsis_style, "…"))
    return tuple(rendered)


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
    "render_tui_context_usage_report",
    "render_tui_snapshot",
    "sanitize_tui_render_frame",
]
