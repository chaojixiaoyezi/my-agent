"""Bounded public transcript events for background main-agent turns."""

# LLM: This module is the only volatile background transcript transport. It may
# carry already-public display events, but it must never own conversation text,
# task lifecycle, completion, permissions, retries, or recovery authority.
# 模块用途: 在 Gateway 进程内暂存后台主代理的有界思考、工具和 diff 展示事件，供 TUI/Web 增量读取。

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .channels import project_user_reply, redact_host_absolute_paths

BACKGROUND_TRANSCRIPT_SCHEMA = "background_transcript_event.v1"
BACKGROUND_TRANSCRIPT_MAX_EVENTS = 1024
BACKGROUND_TRANSCRIPT_MAX_THREADS = 256
BACKGROUND_TRANSCRIPT_TEXT_LIMIT = 12_000

_TRANSCRIPT_STATE_ATTR = "_conversation_background_transcript_projection"
_TRANSCRIPT_LOCK_ATTR = "_conversation_background_transcript_projection_lock"
_TRANSCRIPT_SEQ_ATTR = "_conversation_background_transcript_next_seq"
_TRANSCRIPT_TURN_ATTR = "_conversation_background_transcript_next_turn"
_TRANSCRIPT_SETUP_LOCK = threading.Lock()
_ALLOWED_EVENT_KINDS = frozenset(
    {
        "assistant_completed",
        "thinking_started",
        "thinking_delta",
        "thinking_completed",
        "tool_started",
        "tool_progress",
        "tool_completed",
        "tool_failed",
        "system_message",
        "context_window_compacted",
        "conversation_compaction_started",
        "conversation_compaction_progress",
        "conversation_compaction_completed",
        "conversation_compaction_failed",
        "compact_boundary",
    }
)
_ALLOWED_EVENT_PHASES = frozenset(
    {"started", "delta", "updated", "completed", "failed", "interrupted"}
)
_TOOL_PAYLOAD_FIELDS = frozenset(
    {
        "tool",
        "round",
        "call_index",
        "phase",
        "status",
        "detail",
        "output",
        "display",
        "ok",
        "handler_executed",
        "duration_ms",
        "failure_stage",
        "error_code",
        "task_progress_items",
    }
)
_CONTEXT_COMPACTION_FIELDS = frozenset(
    {
        "generation",
        "before_tokens",
        "after_tokens",
        "trigger_tokens",
        "dropped_pairs",
        "preserved_pairs",
    }
)
_CONVERSATION_COMPACT_STAGES = frozenset(
    {
        "preparing",
        "summarizing",
        "measuring",
        "checkpointing",
        "committing",
        "completed",
        "failed",
    }
)


# LLM: One thread state owns only a monotonic display cursor and a bounded ring.
# Clearing retained rows for a new task must not reset either sequence counter.
# 类用途: 保存一个会话当前任务的易失展示事件和连续游标。
@dataclass
class _ThreadTranscriptState:
    task_id: str = ""
    next_seq: int = 0
    truncated_through_seq: int = 0
    events: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=BACKGROUND_TRANSCRIPT_MAX_EVENTS)
    )


# LLM: This writer converts one background model callback into the same public
# block vocabulary already rendered by the foreground TUI. It buffers candidate
# model text until a real tool boundary so the durable final reply is not shown
# twice, and it never changes the scalar main activity or business lifecycle.
# 类用途: 把一次后台主代理回调转换成灰色过程、实时思考、工具结果和 diff 展示事件。
class BackgroundTranscriptSink:
    # LLM: Construction allocates one display-only turn identity; canonical task
    # and thread ids are inputs, while the generated request id is not authority.
    # 函数用途: 为一次后台续轮创建事件块编号和内部缓冲区。
    def __init__(self, agent: object, *, thread_id: str, task_id: str) -> None:
        self.agent = agent
        self.thread_id = str(thread_id or "").strip()
        self.task_id = str(task_id or "").strip()
        self.request_id = begin_background_transcript_turn(
            agent,
            thread_id=self.thread_id,
            task_id=self.task_id,
        )
        self._assistant_index = 0
        self._thinking_index = 0
        self._thinking_block_id = ""
        self._thinking_text = ""
        self._thinking_active = False
        self._model_text = ""
        self._tool_blocks: set[str] = set()
        self._retry_index = 0

    # LLM: Candidate answer deltas stay private until a subsequent tool start
    # proves that segment was process commentary rather than the committed final.
    # 函数用途: 暂存模型过程文字，等待真实工具边界后再显示。
    def write_model(self, text: str) -> None:
        value = str(text or "")
        if not value or len(self._model_text) >= BACKGROUND_TRANSCRIPT_TEXT_LIMIT:
            return
        remaining = BACKGROUND_TRANSCRIPT_TEXT_LIMIT - len(self._model_text)
        self._model_text += value[:remaining]

    # LLM: Provider thinking deltas are explicit public thinking, not hidden
    # reasoning. Each accepted piece is host-path-redacted and bounded before it
    # enters the ring; a later full event replaces it at terminal freeze.
    # 函数用途: 实时追加后台主代理的显式思考增量。
    def write_thinking_delta(self, text: str) -> bool:
        remaining = BACKGROUND_TRANSCRIPT_TEXT_LIMIT - len(self._thinking_text)
        if remaining <= 0:
            return False
        content = redact_host_absolute_paths(str(text or ""))[:remaining]
        if not content:
            return False
        self._ensure_thinking_started()
        self._thinking_text += content
        self._event(
            "thinking_delta",
            "delta",
            self._thinking_block_id,
            {"text": content},
        )
        return True

    # LLM: A full explicit thinking result freezes the currently streamed block,
    # or creates a terminal-recoverable pair when no delta was observed.
    # 函数用途: 用完整思考正文和真实耗时收口当前思考块。
    def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> bool:
        content = public_background_transcript_text(text)
        if not content and not self._thinking_active:
            return False
        self._ensure_thinking_started()
        if content:
            self._thinking_text = content
        self._event(
            "thinking_completed",
            "completed",
            self._thinking_block_id,
            {
                "text": self._thinking_text,
                "duration_seconds": max(0.0, float(duration_seconds or 0.0)),
            },
        )
        self._thinking_active = False
        self._thinking_block_id = ""
        self._thinking_text = ""
        return True

    # LLM: Tool events already come from _structured_tool_progress, whose output
    # and display are bounded public projections. This layer whitelists fields and
    # maps typed phases without parsing status prose.
    # 函数用途: 将后台工具开始、进度、成功或失败映射为现有 TUI 工具卡片事件。
    def write_progress(self, progress: Mapping[str, object]) -> None:
        public = {
            key: copy.deepcopy(progress[key])
            for key in _TOOL_PAYLOAD_FIELDS
            if key in progress
        }
        block_id = self._tool_block_id(progress)
        phase = str(progress.get("phase") or "updated").strip().lower()
        if phase == "started":
            self._flush_model_commentary()
            if progress.get("detail"):
                public["invocation"] = copy.deepcopy(progress["detail"])
        if block_id not in self._tool_blocks:
            self._tool_blocks.add(block_id)
            self._event("tool_started", "started", block_id, public)
        terminal_kind, terminal_phase = _tool_terminal(phase, progress)
        if terminal_kind:
            self._event(terminal_kind, terminal_phase, block_id, public)
        elif phase != "started":
            self._event("tool_progress", "updated", block_id, public)

    # LLM: Retry display contains typed counters only. Raw exception, endpoint,
    # credential, and provider response never enter this public event.
    # 函数用途: 追加一条灰色模型重连提示。
    def write_provider_retry(
        self,
        *,
        attempt: int,
        total: int,
        delay_seconds: float,
    ) -> None:
        self._retry_index += 1
        self._event(
            "system_message",
            "completed",
            f"{self.request_id}:retry:{self._retry_index}",
            {
                "text": (
                    f"模型重连 {max(1, int(attempt))}/{max(1, int(total))}，"
                    f"等待 {max(0.0, float(delay_seconds)):.1f}s"
                ),
                "severity": "info",
            },
        )

    # LLM: Native IR compaction exposes numeric counters only and remains
    # separate from the durable conversation compact generation.
    # 函数用途: 在后台正文留下真实工具上下文裁剪记录。
    def write_context_compaction(self, value: Mapping[str, object]) -> bool:
        if value.get("schema") != "model_visible_context_compaction.v1":
            return False
        payload = {
            key: _nonnegative_int(value.get(key))
            for key in _CONTEXT_COMPACTION_FIELDS
        }
        generation = payload["generation"]
        if generation <= 0:
            return False
        self._event(
            "context_window_compacted",
            "completed",
            f"{self.request_id}:context-window:{generation}",
            payload,
        )
        return True

    # LLM: Durable Compact display consumes only its frozen numeric schema. The
    # generation and phase select one block; summary and archived messages stay private.
    # 函数用途: 原位发布后台会话 Compact 的开始、进度、完成或失败阶段。
    def write_conversation_compact_progress(self, value: Mapping[str, object]) -> bool:
        if value.get("schema") != "conversation_compaction_progress.v1":
            return False
        phase = str(value.get("phase") or "").strip().lower()
        stage = str(value.get("stage") or "").strip().lower()
        generation = _nonnegative_int(value.get("generation"))
        if (
            generation <= 0
            or phase not in {"started", "progress", "completed", "failed"}
            or stage not in _CONVERSATION_COMPACT_STAGES
        ):
            return False
        payload = {
            "generation": generation,
            "phase": phase,
            "stage": stage,
            "percent": min(100, _nonnegative_int(value.get("percent"))),
            "before_tokens": _nonnegative_int(value.get("before_tokens")),
            "after_tokens": _nonnegative_int(value.get("after_tokens")),
            "trigger_tokens": _nonnegative_int(value.get("trigger_tokens")),
            "source_messages": _nonnegative_int(value.get("source_messages")),
        }
        kind, event_phase = {
            "started": ("conversation_compaction_started", "started"),
            "progress": ("conversation_compaction_progress", "updated"),
            "completed": ("conversation_compaction_completed", "completed"),
            "failed": ("conversation_compaction_failed", "failed"),
        }[phase]
        self._event(
            kind,
            event_phase,
            f"{self.request_id}:conversation-compact:{generation}",
            payload,
        )
        return True

    # LLM: Finish freezes an incomplete explicit thinking block but deliberately
    # drops the remaining candidate model segment; the durable background notice
    # is the sole owner-facing final response.
    # 函数用途: 收口后台展示流，避免最终回复与持久通知重复显示。
    def finish(self) -> None:
        if self._thinking_active:
            self.write_thinking(self._thinking_text)
        self._model_text = ""

    # LLM: Failure follows the same display cleanup as finish. Runtime exception
    # classification and recovery remain in the background scheduler.
    # 函数用途: 后台轮失败时关闭临时思考并丢弃未确认的候选回复。
    def fail(self) -> None:
        self.finish()

    # LLM: Thinking identity increments once per provider model call and carries
    # started_at solely for elapsed-time rendering.
    # 函数用途: 在第一段显式思考到达时创建活动思考块。
    def _ensure_thinking_started(self) -> None:
        if self._thinking_active:
            return
        self._thinking_index += 1
        self._thinking_block_id = f"{self.request_id}:thinking:{self._thinking_index}"
        self._thinking_active = True
        self._event(
            "thinking_started",
            "started",
            self._thinking_block_id,
            {"started_at": time.time()},
        )

    # LLM: Only a real tool-start boundary promotes tentative model text to a
    # gray process block; this mirrors Gateway rich commentary and avoids final duplication.
    # 函数用途: 把工具调用前的模型说明冻结成可折叠灰色过程消息。
    def _flush_model_commentary(self) -> None:
        content = public_background_transcript_text(self._model_text)
        self._model_text = ""
        if not content:
            return
        self._assistant_index += 1
        self._event(
            "assistant_completed",
            "completed",
            f"{self.request_id}:assistant:{self._assistant_index}",
            {"text": content, "process": True},
        )

    # LLM: Tool identity comes only from typed round/call_index within this
    # display turn; details and output text cannot merge unrelated tool cards.
    # 函数用途: 生成后台工具卡片的稳定块编号。
    def _tool_block_id(self, progress: Mapping[str, object]) -> str:
        return (
            f"{self.request_id}:tool:{_nonnegative_int(progress.get('round'))}:"
            f"{_nonnegative_int(progress.get('call_index'))}"
        )

    # LLM: Every event append keeps the writer's exact canonical thread/task and
    # generated display request identity; callers cannot substitute another run.
    # 函数用途: 向本轮所属会话追加一条展示事件。
    def _event(
        self,
        kind: str,
        phase: str,
        block_id: str,
        payload: Mapping[str, object],
    ) -> None:
        append_background_transcript_event(
            self.agent,
            thread_id=self.thread_id,
            task_id=self.task_id,
            request_id=self.request_id,
            kind=kind,
            phase=phase,
            block_id=block_id,
            payload=payload,
        )


# LLM: The lock lives on the shared Agent, so every background scheduler thread
# serializes cursor allocation without introducing a service or durable store.
# 函数用途: 取得 Gateway 内所有后台展示事件共用的线程锁。
def _background_transcript_lock(agent: object) -> threading.Lock:
    lock = getattr(agent, _TRANSCRIPT_LOCK_ATTR, None)
    if lock is not None:
        return lock
    with _TRANSCRIPT_SETUP_LOCK:
        lock = getattr(agent, _TRANSCRIPT_LOCK_ATTR, None)
        if lock is None:
            lock = threading.Lock()
            setattr(agent, _TRANSCRIPT_LOCK_ATTR, lock)
    return lock


# LLM: The mapping is process-local display state keyed by canonical thread id;
# callers receive copies and cannot mutate the ring directly.
# 函数用途: 取得或初始化后台展示事件表。
def _background_transcript_rows(
    agent: object,
) -> OrderedDict[str, _ThreadTranscriptState]:
    rows = getattr(agent, _TRANSCRIPT_STATE_ATTR, None)
    if not isinstance(rows, OrderedDict):
        rows = OrderedDict(
            (str(key), value)
            for key, value in (rows.items() if isinstance(rows, dict) else ())
            if isinstance(value, _ThreadTranscriptState)
        )
        setattr(agent, _TRANSCRIPT_STATE_ATTR, rows)
    return rows


# LLM: The process-wide display cache is capped independently from each thread
# ring. Least-recently-used eviction may lose only optional process text; final
# notices and lifecycle facts live elsewhere and are never touched.
# 函数用途: 取得一个会话的事件环，并在会话过多时淘汰最久未使用的纯展示缓存。
def _background_thread_state(
    rows: OrderedDict[str, _ThreadTranscriptState],
    thread_key: str,
) -> _ThreadTranscriptState:
    state = rows.get(thread_key)
    if state is not None:
        rows.move_to_end(thread_key)
        return state
    while len(rows) >= BACKGROUND_TRANSCRIPT_MAX_THREADS:
        rows.popitem(last=False)
    state = _ThreadTranscriptState()
    rows[thread_key] = state
    return state


# LLM: A new display turn receives a unique request id inside its thread. When
# task identity changes, stale retained bodies are removed but cursor monotonicity
# is preserved so an already-connected client can continue incrementally.
# 函数用途: 为一次后台主代理续轮建立展示身份，并在新任务开始时清除旧任务过程。
def begin_background_transcript_turn(
    agent: object,
    *,
    thread_id: str,
    task_id: str,
) -> str:
    thread_key = str(thread_id or "").strip()
    if not thread_key:
        return ""
    task_key = str(task_id or "").strip()
    lock = _background_transcript_lock(agent)
    with lock:
        rows = _background_transcript_rows(agent)
        state = _background_thread_state(rows, thread_key)
        if task_key and state.task_id and state.task_id != task_key:
            state.events.clear()
            state.truncated_through_seq = 0
        if task_key:
            state.task_id = task_key
        next_turn = max(0, _safe_int(getattr(agent, _TRANSCRIPT_TURN_ATTR, 0))) + 1
        setattr(agent, _TRANSCRIPT_TURN_ATTR, next_turn)
        return f"bg-main:{thread_key}:{next_turn}"


# LLM: Append accepts only the frozen public event vocabulary and scalar
# identities. Payloads are deep-copied because tool display dictionaries remain
# mutable in the executing thread; no event can affect task or transcript state.
# 函数用途: 向指定会话追加一条有界后台展示事件并返回新游标。
def append_background_transcript_event(
    agent: object,
    *,
    thread_id: str,
    task_id: str,
    request_id: str,
    kind: str,
    phase: str,
    block_id: str,
    payload: Mapping[str, object] | None = None,
) -> int:
    thread_key = str(thread_id or "").strip()
    request_key = str(request_id or "").strip()
    block_key = str(block_id or "").strip()
    event_kind = str(kind or "").strip()
    event_phase = str(phase or "").strip()
    if (
        not thread_key
        or not request_key
        or not block_key
        or event_kind not in _ALLOWED_EVENT_KINDS
        or event_phase not in _ALLOWED_EVENT_PHASES
    ):
        return 0
    task_key = str(task_id or "").strip()
    lock = _background_transcript_lock(agent)
    with lock:
        rows = _background_transcript_rows(agent)
        state = _background_thread_state(rows, thread_key)
        if task_key and state.task_id and state.task_id != task_key:
            state.events.clear()
            state.truncated_through_seq = 0
        if task_key:
            state.task_id = task_key
        next_seq = max(0, _safe_int(getattr(agent, _TRANSCRIPT_SEQ_ATTR, 0))) + 1
        setattr(agent, _TRANSCRIPT_SEQ_ATTR, next_seq)
        state.next_seq = next_seq
        event = {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": next_seq,
            "thread_id": thread_key,
            "task_id": task_key,
            "request_id": request_key,
            "kind": event_kind,
            "phase": event_phase,
            "block_id": block_key,
            "payload": copy.deepcopy(dict(payload or {})),
        }
        if len(state.events) >= BACKGROUND_TRANSCRIPT_MAX_EVENTS and state.events:
            state.truncated_through_seq = max(
                state.truncated_through_seq,
                int(state.events[0]["seq"]),
            )
        state.events.append(event)
        return next_seq


# LLM: Readback is cursor-only and returns retained public copies. A truncated
# flag is diagnostic for slow clients; terminal events carry full content so the
# TUI reducer can still recover completed blocks after a missed start.
# 函数用途: 读取指定游标之后的后台展示事件、最新游标和是否发生环形裁剪。
def read_background_transcript_events(
    agent: object,
    *,
    thread_id: str,
    after: int,
) -> dict[str, object]:
    thread_key = str(thread_id or "").strip()
    cursor = max(0, _safe_int(after))
    if not thread_key:
        return {"events": [], "cursor": cursor, "truncated": False}
    lock = _background_transcript_lock(agent)
    with lock:
        rows = _background_transcript_rows(agent)
        state = rows.get(thread_key)
        if state is None:
            return {"events": [], "cursor": cursor, "truncated": False}
        rows.move_to_end(thread_key)
        retained = [copy.deepcopy(row) for row in state.events if int(row["seq"]) > cursor]
        return {
            "events": retained,
            "cursor": max(cursor, state.next_seq),
            "truncated": bool(cursor < state.truncated_through_seq),
        }


# LLM: Model-authored display text passes through the same user-reply projection
# and host-path redaction as Gateway rich commentary before entering the ring.
# 函数用途: 将完整思考或过程说明整理成有界、可公开展示的正文。
def public_background_transcript_text(value: object, *, limit: int = 12_000) -> str:
    projected = project_user_reply(str(value or "")).content
    content = redact_host_absolute_paths(projected).strip()
    max_chars = max(1, int(limit or BACKGROUND_TRANSCRIPT_TEXT_LIMIT))
    if len(content) <= max_chars:
        return content
    keep_head = max_chars * 2 // 3
    keep_tail = max_chars - keep_head
    return f"{content[:keep_head]}\n…（内容过长，已省略）…\n{content[-keep_tail:]}"


# LLM: Terminal mapping reads only typed phase/ok. Status and output prose are
# display content and cannot decide whether a tool succeeded.
# 函数用途: 将后台工具终态映射为现有 TUI 的成功、失败或中断事件。
def _tool_terminal(
    phase: str,
    progress: Mapping[str, object],
) -> tuple[str, str]:
    if phase in {"interrupted", "cancelled"}:
        return "tool_failed", "interrupted"
    if phase not in {"finished", "completed", "succeeded", "failed"}:
        return "", ""
    ok = bool(progress.get("ok")) if "ok" in progress else phase != "failed"
    return ("tool_completed", "completed") if ok else ("tool_failed", "failed")


# LLM: Display counters clamp malformed values to zero; they never feed back
# into runtime accounting, Compact generations, or tool lifecycle.
# 函数用途: 安全读取后台展示使用的非负整数。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Cursor parsing affects only a display page and therefore clamps malformed
# values to zero instead of raising into the Gateway request path.
# 函数用途: 安全读取后台展示事件游标。
def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "BACKGROUND_TRANSCRIPT_SCHEMA",
    "BackgroundTranscriptSink",
    "append_background_transcript_event",
    "begin_background_transcript_turn",
    "public_background_transcript_text",
    "read_background_transcript_events",
]
