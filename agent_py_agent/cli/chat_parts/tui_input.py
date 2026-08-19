# LLM: 本模块实现 TUI 输入侧的纯补全、队列回取和占位投影；真实执行顺序仍由 canonical chat Queue 与 typed runtime 共同裁决。
# 模块用途: 为 终端交互 风格输入框提供 slash/路径补全、排队提示，并把尚未执行的消息安全恢复到编辑器。

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Any

from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.utils import get_cwidth

from .chat_prompt_queue import pop_all_matching
from .slash_command_types import CHAT_SLASH_COMMANDS
from .tui_runtime import TuiRuntime

QUEUE_EDIT_PLACEHOLDER = "Press up to edit queued messages"
_IGNORED_PATH_NAMES = frozenset({".git", ".hg", ".svn", "__pycache__"})
_COMPLETION_MENU_MAX_ITEMS = 6


# LLM: TuiCompletion 只携带 UI 接受动作，不改变命令权限；submit 仍必须进入既有 command dispatcher。
# 类用途: 在普通补全文本之外，标记 Enter 应提交、只应用候选，还是提交当前原文。
class TuiCompletion(Completion):
    # LLM: kind/enter_action/append_space 是输入控制的结构化事实，renderer 与 keybinding 不得解析 display_meta 文案。
    # 函数用途: 创建带有输入行为元数据的 prompt_toolkit 候选。
    def __init__(
        self,
        text: str,
        *,
        start_position: int,
        display: str,
        display_meta: str,
        kind: str,
        enter_action: str = "apply",
        append_space: bool = False,
    ) -> None:
        super().__init__(
            text,
            start_position=start_position,
            display=display,
            display_meta=display_meta,
        )
        self.kind = str(kind)
        self.enter_action = str(enter_action)
        self.append_space = bool(append_space)


# LLM: slash menu 可见时不能叠加历史 ghost text；普通输入仍完全复用 FileHistory 的既有建议算法。
# 类用途: 为聊天输入提供历史灰字建议，并在斜杠命令补全期间隐藏它。
class TuiHistoryAutoSuggest(AutoSuggestFromHistory):
    # LLM: 此判断只识别输入语法位置，不执行命令，也不根据历史内容改变系统状态。
    # 函数用途: 返回普通历史建议；斜杠命令名前缀下返回空建议。
    def get_suggestion(self, buffer: Any, document: Document):
        if _slash_command_prefix(document.text_before_cursor) is not None:
            return None
        return super().get_suggestion(buffer, document)


# LLM: 菜单直接读取 Buffer.complete_state 这一份候选事实；禁止另建 selection store 或使用默认带背景/滚动条的 CompletionsMenu。
# 类用途: 以内联、六行、首项选中且无背景的 终端交互 几何渲染当前补全候选。
class TuiCompletionMenuControl(UIControl):
    # LLM: buffer 由输入 TextArea 创建并贯穿 app 生命周期；control 不持有候选副本。
    # 函数用途: 绑定输入 buffer 和最大可见候选数。
    def __init__(self, buffer: Any, max_items: int = _COMPLETION_MENU_MAX_ITEMS) -> None:
        self.buffer = buffer
        self.max_items = max(1, int(max_items))

    # LLM: 高度只由当前 canonical completion count 决定，避免空菜单占行或超过参考的六行窗口。
    # 函数用途: 告诉布局当前补全菜单需要多少行。
    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix: Any,
    ) -> int:
        del width, wrap_lines, get_line_prefix
        state = getattr(self.buffer, "complete_state", None)
        count = len(state.completions) if state is not None else 0
        return min(count, self.max_items, max(0, int(max_available_height)))

    # LLM: selected index 默认零且只写 complete_state；不得用 go_to_completion 提前改写用户 Document。
    # 函数用途: 生成命令名固定列、描述截断和选中色的菜单行。
    def create_content(self, width: int, height: int) -> UIContent:
        del height
        state = getattr(self.buffer, "complete_state", None)
        completions = list(state.completions) if state is not None else []
        current = selected_completion(self.buffer)
        selected_index = state.complete_index if state is not None and current is not None else 0
        available_width = max(1, int(width))
        max_name = max((get_cwidth(item.display_text) for item in completions), default=0)
        name_width = min(max_name + 5, max(1, available_width * 2 // 5))

        def get_line(index: int):
            item = completions[index]
            name = _truncate_to_width(item.display_text, max(0, name_width - 2))
            padded_name = name + " " * max(0, name_width - get_cwidth(name))
            description_width = max(0, available_width - name_width - 4)
            description = _truncate_to_width(
                " ".join(item.display_meta_text.split()),
                description_width,
            )
            style = (
                "class:tui-completion-selected"
                if index == selected_index
                else "class:tui-completion"
            )
            return [(style, "  " + padded_name + description)]

        return UIContent(
            get_line=get_line,
            line_count=len(completions),
            cursor_position=Point(x=0, y=selected_index),
        )


# LLM: restore result 只包含编辑器投影和被移除 request ids；job 对象不会泄漏到 UI 控件成为第二执行入口。
# 类用途: 返回一次队列回取后应写入输入框的正文、光标和任务身份。
@dataclass(frozen=True)
class TuiQueuedRestore:
    text: str
    cursor_position: int
    request_ids: tuple[str, ...]


# LLM: 视觉行坐标必须复用 prompt_toolkit 的逐字符显示宽度语义；软折行边界属于下一视觉行，不能退化为逻辑换行或 history 判断。
# 函数用途: 计算一条逻辑输入行内每个光标位置所在的屏幕折行和显示列。
def _wrapped_line_cursor_positions(
    text: str,
    width: int,
) -> dict[int, tuple[int, int]]:
    available = max(1, int(width or 1))
    positions: dict[int, tuple[int, int]] = {0: (0, 0)}
    visual_row = 0
    display_column = 0
    for offset, character in enumerate(str(text or "")):
        character_width = max(0, int(get_cwidth(character)))
        if display_column + character_width > available:
            visual_row += 1
            display_column = 0
        positions[offset] = (visual_row, display_column)
        display_column += character_width
        if display_column >= available:
            visual_row += display_column // available
            display_column %= available
        positions[offset + 1] = (visual_row, display_column)
    return positions


# LLM: 此入口只在当前逻辑行的 prompt_toolkit 软折行间移动光标；到达视觉边界必须返回 False，让调用方继续走既有逻辑行/history/queue 主链。
# 函数用途: 让上下键先在屏幕自动折出的第 2、3 行间移动，并保持尽可能相同的显示列。
def move_input_cursor_by_wrapped_rows(
    buffer: Any,
    *,
    width: int,
    delta: int,
) -> bool:
    document = buffer.document
    line = str(document.current_line or "")
    positions = _wrapped_line_cursor_positions(line, width)
    current_offset = max(0, min(len(line), int(document.cursor_position_col)))
    current_row, current_column = positions.get(current_offset, (0, 0))
    last_row = max((row for row, _column in positions.values()), default=0)
    target_row = max(0, min(last_row, current_row + int(delta)))
    if target_row == current_row:
        return False

    candidates = sorted(
        (column, offset)
        for offset, (row, column) in positions.items()
        if row == target_row
    )
    if not candidates:
        return False
    target_column, target_offset = candidates[0]
    for candidate_column, candidate_offset in candidates:
        if candidate_column > current_column:
            break
        if candidate_column > target_column or (
            candidate_column == target_column and candidate_offset > target_offset
        ):
            target_column = candidate_column
            target_offset = candidate_offset
    line_start = int(document.cursor_position) - current_offset
    buffer.cursor_position = line_start + target_offset
    return True


# LLM: completer 的目录来自结构化 slash registry 与显式 workspace root；候选仅编辑 buffer，不执行命令或读取文件内容。
# 类用途: 补全 my-agent 斜杠命令、`@路径` 和 `/prompt-file` 路径参数。
class TuiInputCompleter(Completer):
    # LLM: workspace root 在 app 创建时固定；后续补全只能在该根或用户显式输入的绝对父目录做单层枚举。
    # 函数用途: 创建输入补全器并规范工作目录。
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)

    # LLM: completion 分支只看光标前的明确语法位置；普通中文与正文中的斜杠不会被当成系统命令。
    # 函数用途: 根据当前 token 生成命令或文件候选。
    def get_completions(self, document: Document, complete_event: Any):
        del complete_event
        before = document.text_before_cursor
        slash_prefix = _slash_command_prefix(before)
        if slash_prefix is not None:
            directory = {}
            for spec in CHAT_SLASH_COMMANDS:
                directory.setdefault(spec.name, spec)
            for name in sorted(directory):
                spec = directory[name]
                if not name.startswith(slash_prefix):
                    continue
                yield TuiCompletion(
                    "/" + name,
                    start_position=-len(before),
                    display="/" + name,
                    display_meta=spec.summary,
                    kind="command",
                    enter_action="submit" if spec.submit_on_enter else "apply",
                    append_space=True,
                )
            return
        query = _path_query(before)
        if query is None:
            return
        token, include_at = query
        for candidate, is_dir in _path_candidates(self.workspace, token):
            completed = ("@" if include_at else "") + candidate
            yield TuiCompletion(
                completed,
                start_position=-len(("@" if include_at else "") + token),
                display=completed,
                display_meta="directory" if is_dir else "file",
                kind="path_directory" if is_dir else "path_file",
                enter_action=(
                    "submit_original"
                    if before.startswith("/prompt-file ")
                    else "apply"
                ),
                append_space=not is_dir,
            )


# LLM: processor 只在空 buffer 且 reducer 存在 queued_inputs 时附加 dim ghost text；占位文字不进入 Document、history 或模型 prompt。
# 类用途: 在排队消息可回取时显示 `Press up to edit queued messages` 提示。
class TuiQueuedPlaceholderProcessor(Processor):
    # LLM: runtime 是唯一 typed snapshot 来源；processor 不缓存 queue 长度，避免 stale placeholder。
    # 函数用途: 绑定当前 TUI runtime。
    def __init__(self, runtime: TuiRuntime) -> None:
        self.runtime = runtime

    # LLM: Transformation 保留原 fragments/cursor 映射，只在唯一空行末尾增加不可编辑显示片段。
    # 函数用途: 为当前输入行生成可选的队列编辑提示。
    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        show = (
            ti.lineno == 0
            and ti.document.text == ""
            and bool(self.runtime.store.snapshot().queued_inputs)
        )
        suffix = QUEUE_EDIT_PLACEHOLDER if show else ""
        return Transformation(
            fragments=ti.fragments + [("class:tui-placeholder", suffix)]
        )


# LLM: 此协调入口先按 runtime 的结构化 queue identity 从真实 Queue 原子移除任务，再发布 restore event；不按 pending 文案或显示顺序猜 job。
# 函数用途: 取回所有尚未执行的可编辑排队消息，并与当前草稿合并成一个输入文档。
def restore_queued_prompts(
    *,
    jobs: Queue[Any],
    runtime: TuiRuntime,
    state_lock: threading.Lock,
    pending_jobs_ref: list[int],
    current_text: str,
    current_cursor: int,
) -> TuiQueuedRestore | None:
    removed = pop_all_matching(
        jobs,
        lambda job: runtime.has_queued_prompt(str(getattr(job, "request_id", "") or "")),
    )
    if not removed:
        return None
    request_ids = tuple(str(job.request_id) for job in removed)
    texts = tuple(str(getattr(job, "display_text", "") or job.user) for job in removed)
    runtime.restore_prompts(request_ids)
    with state_lock:
        pending_jobs_ref[0] = max(0, int(pending_jobs_ref[0]) - len(removed))
    queued_text = "\n".join(texts)
    combined = "\n".join(part for part in (*texts, str(current_text)) if part)
    cursor = len(queued_text) + (1 if queued_text and current_text else 0) + max(0, int(current_cursor))
    return TuiQueuedRestore(combined, min(len(combined), cursor), request_ids)


# LLM: 默认选中只改变 completion_state 索引；用户输入在 Tab/Enter 显式接受前必须保持原样。
# 函数用途: 返回当前候选，并在菜单刚出现时把首项标为选中。
def selected_completion(buffer: Any) -> Completion | None:
    state = getattr(buffer, "complete_state", None)
    if state is None or not state.completions:
        return None
    if state.complete_index is None:
        state.complete_index = 0
    index = max(0, min(int(state.complete_index), len(state.completions) - 1))
    state.complete_index = index
    return state.completions[index]


# LLM: 导航只循环修改同一 complete_state 的 index；禁止调用会把候选预写入 Document 的 complete_next/previous。
# 函数用途: 向前或向后循环移动补全菜单选中项，同时保留用户原输入。
def move_completion_selection(buffer: Any, delta: int) -> Completion | None:
    current = selected_completion(buffer)
    state = getattr(buffer, "complete_state", None)
    if current is None or state is None:
        return None
    state.complete_index = (int(state.complete_index) + int(delta)) % len(state.completions)
    return state.completions[state.complete_index]


# LLM: 接受候选只调用 Buffer 的标准替换一次；尾随空格由 typed completion 字段决定，不从显示说明猜测。
# 函数用途: 把当前选中候选写入输入文档，并按命令/文件语义补一个空格。
def apply_selected_completion(buffer: Any) -> Completion | None:
    completion = selected_completion(buffer)
    if completion is None:
        return None
    buffer.apply_completion(completion)
    if isinstance(completion, TuiCompletion) and completion.append_space:
        if not str(buffer.text).endswith(" "):
            buffer.insert_text(" ")
    return completion


# LLM: slash prefix 只匹配从 buffer 开头到光标的单个 `/name` token；参数阶段交给对应参数补全。
# 函数用途: 返回当前命令名前缀，非命令位置返回 None。
def _slash_command_prefix(before: str) -> str | None:
    if not before.startswith("/") or any(character.isspace() for character in before):
        return None
    return before[1:].casefold()


# LLM: 路径语法只开放 `@token` 和 `/prompt-file token`；空白后的普通词不触发磁盘枚举。
# 函数用途: 提取待补全路径和是否保留 @ 前缀。
def _path_query(before: str) -> tuple[str, bool] | None:
    token = before.rsplit(maxsplit=1)[-1] if before else ""
    if token.startswith("@"):
        return token[1:], True
    prefix = "/prompt-file "
    if before.startswith(prefix):
        return before[len(prefix) :], False
    return None


# LLM: 文件补全只枚举一个父目录并按名称排序；它不递归扫描 workspace，也不读取候选内容。
# 函数用途: 返回相对/绝对路径候选及目录标记。
def _path_candidates(workspace: Path, token: str) -> tuple[tuple[str, bool], ...]:
    expanded = os.path.expanduser(token)
    raw_path = Path(expanded or ".")
    absolute = raw_path.is_absolute()
    parent_token = str(raw_path.parent)
    name_prefix = "" if token.endswith(("/", os.sep)) else raw_path.name
    if token.endswith(("/", os.sep)):
        parent_token = expanded
    parent = Path(parent_token) if absolute else workspace / parent_token
    try:
        entries = sorted(parent.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
    except (OSError, ValueError):
        return ()
    candidates: list[tuple[str, bool]] = []
    for entry in entries[:500]:
        if entry.name in _IGNORED_PATH_NAMES or not entry.name.startswith(name_prefix):
            continue
        candidate_path = entry if absolute else entry.relative_to(workspace)
        candidate = str(candidate_path)
        if entry.is_dir():
            candidate += os.sep
        candidates.append((candidate, entry.is_dir()))
    return tuple(candidates)


# LLM: 终端宽度裁剪按显示列而非 Python 字符数；组合字符保留，超宽字符不得越过菜单边界。
# 函数用途: 把候选名或说明截到给定终端显示宽度。
def _truncate_to_width(text: str, max_width: int) -> str:
    remaining = max(0, int(max_width))
    output: list[str] = []
    for character in str(text):
        width = max(0, get_cwidth(character))
        if width > remaining:
            break
        output.append(character)
        remaining -= width
    return "".join(output)


__all__ = [
    "QUEUE_EDIT_PLACEHOLDER",
    "TuiCompletion",
    "TuiCompletionMenuControl",
    "TuiHistoryAutoSuggest",
    "TuiInputCompleter",
    "TuiQueuedPlaceholderProcessor",
    "TuiQueuedRestore",
    "apply_selected_completion",
    "move_input_cursor_by_wrapped_rows",
    "move_completion_selection",
    "restore_queued_prompts",
    "selected_completion",
]
