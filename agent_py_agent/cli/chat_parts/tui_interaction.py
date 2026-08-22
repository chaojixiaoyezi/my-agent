# LLM: 本模块保存一次 TUI 生命周期内的纯输入交互状态；它不执行命令、不写 history，也不把提示文案当控制信号。
# 模块用途: 为 终端交互 风格的草稿 stash、Ctrl-R 历史搜索、快捷键帮助和括号粘贴提示提供线程安全状态机。

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .tui_paste import (
    TuiPastedTextRef,
    collapse_tui_paste,
    expand_tui_paste_refs,
)


# LLM: TuiDraft 是编辑器内容、光标与文本粘贴 refs 的不可变快照；隐藏正文只能由 refs 提供，不能从占位符或 history 文案猜测。
# 类用途: 保存一份可恢复的输入草稿。
@dataclass(frozen=True)
class TuiDraft:
    text: str
    cursor_position: int
    pasted_text_refs: tuple[TuiPastedTextRef, ...] = ()

    # LLM: 光标必须被限制在正文边界内，坏输入不能让 prompt_toolkit Document 构造失败。
    # 函数用途: 规范草稿文本和光标位置。
    def __post_init__(self) -> None:
        text = str(self.text or "")
        cursor = max(0, min(len(text), int(self.cursor_position or 0)))
        references = tuple(self.pasted_text_refs or ())
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "cursor_position", cursor)
        object.__setattr__(self, "pasted_text_refs", references)


# LLM: TuiInteractionSnapshot 只暴露 renderer/keybinding 所需字段；历史正文集合和原始草稿不泄漏到画面缓存键。
# 类用途: 一次性读取 stash、搜索、快捷键帮助和粘贴的可见状态。
@dataclass(frozen=True)
class TuiInteractionSnapshot:
    has_stash: bool = False
    history_search_active: bool = False
    history_query: str = ""
    history_failed_match: bool = False
    is_pasting: bool = False
    help_open: bool = False
    todos_expanded: bool = False


# LLM: _HistorySearchSession 是 Ctrl-R 的内部游标；entries 已按新到旧去重，query 匹配只影响编辑器投影，不执行历史 prompt。
# 类用途: 保存一次增量历史搜索的原始草稿、候选和当前位置。
@dataclass
class _HistorySearchSession:
    original: TuiDraft
    entries: tuple[str, ...]
    query: str = ""
    matching_entries: tuple[str, ...] = ()
    match_index: int = -1
    current: TuiDraft | None = None
    failed: bool = False


# LLM: TuiInteractionState 是输入交互唯一状态源；所有 mutation 都在同一锁内完成并在锁外触发轻量 redraw。
# 类用途: 管理单槽 stash、可取消历史搜索、快捷键帮助和短暂粘贴状态。
class TuiInteractionState:
    # LLM: invalidate callback 只能请求重绘，不能重入本状态机或执行业务动作。
    # 函数用途: 创建空交互状态，并可绑定界面刷新函数。
    def __init__(self, invalidate: Callable[[], None] | None = None) -> None:
        self._stash: TuiDraft | None = None
        self._history: _HistorySearchSession | None = None
        self._is_pasting = False
        self._help_open = False
        self._todos_expanded = False
        self._current_pasted_text_refs: tuple[TuiPastedTextRef, ...] = ()
        self._next_paste_id = 1
        self._invalidate = invalidate
        self._lock = threading.RLock()

    # LLM: callback 可在 Application 创建后替换；旧 callback 不保留，避免一个状态更新触发多套 UI。
    # 函数用途: 绑定当前应用的重绘入口。
    def set_invalidate_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._invalidate = callback

    # LLM: snapshot 只复制可见布尔值和查询文本，不返回内部可变 session。
    # 函数用途: 返回当前交互状态的不可变投影。
    def snapshot(self) -> TuiInteractionSnapshot:
        with self._lock:
            history = self._history
            return TuiInteractionSnapshot(
                has_stash=self._stash is not None,
                history_search_active=history is not None,
                history_query=history.query if history is not None else "",
                history_failed_match=history.failed if history is not None else False,
                is_pasting=self._is_pasting,
                help_open=self._help_open,
                todos_expanded=self._todos_expanded,
            )

    # LLM: 帮助面板只是一项显式 UI 状态；切换不能向输入 Buffer 写入 `?`、提交命令或改会话历史。
    # 函数用途: 打开或关闭输入框下方的快捷键帮助，并返回新状态。
    def toggle_help(self) -> bool:
        with self._lock:
            self._help_open = not self._help_open
            opened = self._help_open
        self._notify()
        return opened

    # LLM: 普通输入、Enter、Esc 和删除键共用这一幂等关闭入口，避免各按键维护自己的 help flag。
    # 函数用途: 在用户继续编辑时关闭快捷键帮助；本来已关闭时不触发多余重绘。
    def close_help(self) -> bool:
        with self._lock:
            if not self._help_open:
                return False
            self._help_open = False
        self._notify()
        return True

    # LLM: Todo expansion is a process-local display preference only; toggling it must not
    # mutate task_progress, reorder canonical items, or enter the conversation transcript.
    # 函数用途: 用 Ctrl-T 在四条任务窗口和完整任务清单之间切换，并请求界面重绘。
    def toggle_todos(self) -> bool:
        with self._lock:
            self._todos_expanded = not self._todos_expanded
            expanded = self._todos_expanded
        self._notify()
        return expanded

    # LLM: stash 是严格单槽：非空草稿覆盖当前槽，空编辑器只弹出已有槽；空白输入不会制造不可见 stash。
    # 函数用途: 按 Ctrl-S 语义保存当前草稿或恢复已保存草稿。
    def toggle_stash(self, draft: TuiDraft) -> TuiDraft | None:
        changed = False
        restored: TuiDraft | None = None
        with self._lock:
            if draft.text.strip():
                self._stash = draft
                changed = True
            elif self._stash is not None:
                restored = self._stash
                self._stash = None
                changed = True
        if changed:
            self._notify()
        return restored

    # LLM: submit 后恢复会原子消费 stash；调用方负责把返回草稿写入当前输入 Buffer，不能再次执行它。
    # 函数用途: 取出提交后应自动恢复的草稿。
    def consume_stash_after_submit(self) -> TuiDraft | None:
        with self._lock:
            restored = self._stash
            self._stash = None
        if restored is not None:
            self._notify()
        return restored

    # LLM: history entries 在搜索开始时冻结并按显示正文去重；后续磁盘变化留到下一次 Ctrl-R 会话读取。
    # 函数用途: 从当前草稿和新到旧历史列表启动一次搜索。
    def start_history_search(
        self,
        original: TuiDraft,
        entries: Iterable[str],
    ) -> None:
        normalized = _unique_history_entries(entries)
        with self._lock:
            self._history = _HistorySearchSession(original=original, entries=normalized)
        self._notify()

    # LLM: query 更新会从最新历史重新扫描；零匹配保留上一个可见 match，避免查询失败时编辑区突然跳回。
    # 函数用途: 更新搜索词并返回需要投影到主输入框的新草稿；无需变化时返回 None。
    def update_history_query(self, query: str) -> TuiDraft | None:
        with self._lock:
            session = self._required_history()
            session.query = str(query or "")
            if not session.query:
                session.matching_entries = ()
                session.match_index = -1
                session.current = None
                session.failed = False
                draft = session.original
            else:
                session.matching_entries = tuple(
                    entry for entry in session.entries if session.query in entry
                )
                if not session.matching_entries:
                    session.match_index = -1
                    session.failed = True
                    draft = None
                else:
                    session.match_index = 0
                    session.failed = False
                    session.current = _matching_draft(
                        session.matching_entries[0],
                        session.query,
                    )
                    draft = session.current
        self._notify()
        return draft

    # LLM: next 只在当前 query 的冻结匹配中向旧记录推进且不循环；到尾部保留最后 match 并标记 failed。
    # 函数用途: 再次按 Ctrl-R 时选择下一条历史匹配。
    def next_history_match(self) -> TuiDraft | None:
        with self._lock:
            session = self._required_history()
            next_index = session.match_index + 1
            if not session.query or next_index >= len(session.matching_entries):
                session.failed = bool(session.query)
                draft = None
            else:
                session.match_index = next_index
                session.failed = False
                session.current = _matching_draft(
                    session.matching_entries[next_index],
                    session.query,
                )
                draft = session.current
        self._notify()
        return draft

    # LLM: accept 保留当前 match；若没有 match 则保留原草稿，随后销毁搜索 session。
    # 函数用途: 用 Esc/Tab 接受搜索结果但不提交。
    def accept_history_search(self) -> TuiDraft:
        with self._lock:
            session = self._required_history()
            draft = session.current or session.original
            self._history = None
        self._notify()
        return draft

    # LLM: cancel 无条件恢复搜索开始前的正文和光标，不能保留临时匹配或 query。
    # 函数用途: 用 Ctrl-C 取消历史搜索。
    def cancel_history_search(self) -> TuiDraft:
        with self._lock:
            session = self._required_history()
            draft = session.original
            self._history = None
        self._notify()
        return draft

    # LLM: execute 只在空 query 或已有匹配时返回可提交草稿；无匹配时关闭搜索但不伪造提交正文。
    # 函数用途: 用 Enter 接受当前搜索项并决定是否立即执行。
    def execute_history_search(self) -> TuiDraft | None:
        with self._lock:
            session = self._required_history()
            if not session.query:
                draft: TuiDraft | None = session.original
            else:
                draft = session.current
            self._history = None
        self._notify()
        return draft

    # LLM: paste 状态是短暂 UI 反馈，不进入 journal/history，也不授权或提交当前输入。
    # 函数用途: 开启或结束 `Pasting text…` 底栏提示。
    def set_pasting(self, value: bool) -> None:
        normalized = bool(value)
        with self._lock:
            if self._is_pasting == normalized:
                return
            self._is_pasting = normalized
        self._notify()

    # LLM: capture_draft 将当前 paste refs 与编辑器文本一起冻结；调用方不得只保存可见占位符，否则 stash/取消搜索会丢失真实正文。
    # 函数用途: 从当前 Buffer 文本和光标建立完整草稿快照。
    def capture_draft(self, text: str, cursor_position: int) -> TuiDraft:
        with self._lock:
            references = self._current_pasted_text_refs
        return TuiDraft(str(text or ""), int(cursor_position or 0), references)

    # LLM: install_draft 原子替换当前 paste refs；空草稿必须清除旧隐藏正文，防止下一次提交夹带已删除内容。
    # 函数用途: 将一份 stash/history 草稿设为当前输入的结构化状态。
    def install_draft(self, draft: TuiDraft) -> None:
        references = tuple(draft.pasted_text_refs or ())
        with self._lock:
            self._current_pasted_text_refs = references
            if references:
                self._next_paste_id = max(
                    self._next_paste_id,
                    max(reference.paste_id for reference in references) + 1,
                )

    # LLM: register_text_paste 使用单调 id 将长 paste 折叠，并把新引用追加到当前草稿；短 paste 不改变引用集合。
    # 函数用途: 返回应一次性插入 Buffer 的可见文本。
    def register_text_paste(self, text: str) -> str:
        with self._lock:
            paste_id = self._next_paste_id
            visible, reference = collapse_tui_paste(text, paste_id=paste_id)
            if reference is not None:
                self._next_paste_id += 1
                self._current_pasted_text_refs = (
                    *self._current_pasted_text_refs,
                    reference,
                )
        return visible

    # LLM: expand_draft 只使用草稿自带 refs 展开实际存在的占位符；结果是提交正文，不能回写显示 Buffer 或 transcript。
    # 函数用途: 还原一份草稿中的完整文本粘贴内容。
    def expand_draft(self, draft: TuiDraft) -> str:
        return expand_tui_paste_refs(draft.text, draft.pasted_text_refs)

    # LLM: 缺少搜索 session 是 keybinding 接线错误，必须显式失败而不是隐式创建空会话。
    # 函数用途: 返回当前历史搜索内部状态。
    def _required_history(self) -> _HistorySearchSession:
        if self._history is None:
            raise RuntimeError("TUI history search is not active")
        return self._history

    # LLM: redraw 在锁外执行并允许为空；回调异常不得吞掉状态 mutation 的结果。
    # 函数用途: 通知界面交互状态已变化。
    def _notify(self) -> None:
        with self._lock:
            callback = self._invalidate
        if callback is not None:
            callback()


# LLM: 去重保持来源次序，空历史项不参与 Ctrl-R；比较使用完整字符串而非首行展示。
# 函数用途: 规范新到旧的搜索候选列表。
def _unique_history_entries(entries: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in entries:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


# LLM: 搜索光标定位使用最后一个精确 substring，与 终端交互 的 lastIndexOf 行为一致；匹配不存在时安全落到末尾。
# 函数用途: 为一条匹配历史构造显示草稿。
def _matching_draft(text: str, query: str) -> TuiDraft:
    position = str(text).rfind(str(query))
    return TuiDraft(str(text), len(str(text)) if position < 0 else position)


__all__ = [
    "TuiDraft",
    "TuiInteractionSnapshot",
    "TuiInteractionState",
]
