"""Process-shared public transcript events for one delegated agent run."""

# LLM: This module is the only durable display-event bridge between a child runner
# subprocess and owner-facing clients. Rows are bounded public projections only;
# ConversationThread, SubAgentTask, guidance, cancellation, and lifecycle remain authoritative.
# 模块用途: 将子代理进程里的思考、工具、diff 和 Compact 展示事件写入有界 JSONL，供 TUI/Web 增量查看。

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path

from ..common.json_io import write_text_file_atomic
from ..common.opaque_id import validate_opaque_id
from ..gateway_parts.io import locked_file_transition
from ..io.jsonl import append_jsonl
from .background_transcript import (
    BACKGROUND_TRANSCRIPT_EVENT_KINDS,
    BACKGROUND_TRANSCRIPT_EVENT_PHASES,
    BACKGROUND_TRANSCRIPT_SCHEMA,
)
from .store import read_jsonl_tail_report

AGENT_TRANSCRIPT_MAX_EVENTS = 1024
AGENT_TRANSCRIPT_MAX_BYTES = 8 * 1024 * 1024


# LLM: The path is derived only from a validated opaque run id under the owner
# ConversationStore. A caller cannot select an absolute or parent-relative file.
# 函数用途: 返回一个子代理展示事件的唯一文件位置。
def agent_transcript_path(agent: object, run_id: str) -> Path:
    selected = validate_opaque_id(run_id, kind="run_id")
    store = getattr(agent, "conversation_store", None)
    root = getattr(store, "root", None)
    if root is None:
        raise RuntimeError("agent transcript requires ConversationStore")
    return Path(root) / "agent_transcript_events" / f"{selected}.jsonl"


# LLM: A child attempt gets a display identity separate from request/run
# authority. Starting a new attempt preserves the bounded prior process history.
# 函数用途: 为一个子代理执行尝试生成稳定的展示事件前缀。
def begin_agent_transcript_turn(
    agent: object,
    *,
    run_id: str,
    attempt_id: str,
) -> str:
    selected = validate_opaque_id(run_id, kind="run_id")
    attempt = validate_opaque_id(attempt_id, kind="attempt_id")
    _compact_agent_transcript_file(agent_transcript_path(agent, selected))
    return f"bg-agent:{selected}:{attempt}"


# LLM: Append accepts the same frozen public event vocabulary as the main
# background ring, allocates one monotonically increasing file cursor under a
# cross-process transition lock, and never mutates agent lifecycle state.
# 函数用途: 并发安全地追加一条子代理公开展示事件，并返回该 run 的新游标。
def append_agent_transcript_event(
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
    run_id = validate_opaque_id(task_id, kind="run_id")
    request_key = str(request_id or "").strip()
    block_key = str(block_id or "").strip()
    event_kind = str(kind or "").strip()
    event_phase = str(phase or "").strip()
    if (
        not request_key.startswith(f"bg-agent:{run_id}:")
        or not block_key.startswith(f"{request_key}:")
        or event_kind not in BACKGROUND_TRANSCRIPT_EVENT_KINDS
        or event_phase not in BACKGROUND_TRANSCRIPT_EVENT_PHASES
    ):
        return 0
    path = agent_transcript_path(agent, run_id)
    transition = path.with_name(f".{path.name}.transition")
    with locked_file_transition(transition):
        prior = read_jsonl_tail_report(
            path,
            context="conversation.agent_transcript.cursor",
            limit=1,
        )
        last_seq = _safe_int(prior.rows[-1].get("seq")) if prior.rows else 0
        seq = last_seq + 1
        append_jsonl(
            path,
            {
                "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
                "seq": seq,
                "thread_id": str(thread_id or "").strip(),
                "task_id": run_id,
                "request_id": request_key,
                "kind": event_kind,
                "phase": event_phase,
                "block_id": block_key,
                "payload": copy.deepcopy(dict(payload or {})),
            },
            sort_keys=True,
        )
        if path.stat().st_size > AGENT_TRANSCRIPT_MAX_BYTES:
            _compact_agent_transcript_file_locked(path)
    return seq


# LLM: Readback returns only exact-run rows after the caller cursor. Tail bounds
# protect latency even if a runner crashed before its normal final compaction.
# 函数用途: 增量读取一个子代理最近的公开过程事件，并报告游标和截断情况。
def read_agent_transcript_events(
    agent: object,
    *,
    run_id: str,
    after: int,
) -> dict[str, object]:
    selected = validate_opaque_id(run_id, kind="run_id")
    cursor = max(0, _safe_int(after))
    path = agent_transcript_path(agent, selected)
    report = read_jsonl_tail_report(
        path,
        context="conversation.agent_transcript.read",
        limit=AGENT_TRANSCRIPT_MAX_EVENTS,
    )
    valid = [
        dict(row)
        for row in report.rows
        if _valid_agent_transcript_row(row, selected)
    ]
    retained = [row for row in valid if _safe_int(row.get("seq")) > cursor]
    latest = max((int(row["seq"]) for row in valid), default=cursor)
    earliest = min((int(row["seq"]) for row in valid), default=0)
    return {
        "events": retained,
        "cursor": max(cursor, latest),
        "truncated": bool(cursor and earliest and cursor < earliest),
        "load_errors": list(report.load_errors),
    }


# LLM: Startup presentation may distinguish PendingInit-like work only when the exact active
# attempt has emitted a public typed event. The bounded tail lookup is display-only, owner-rooted,
# and cannot promote status, heartbeat, completion, or retry authority.
# 函数用途: 判断某个子代理的当前执行尝试是否已经产生首条公开过程事件，避免旧尝试让新尝试冒充运行中。
def agent_transcript_attempt_has_events(
    agent: object,
    *,
    run_id: str,
    attempt_id: str,
) -> bool:
    selected = validate_opaque_id(run_id, kind="run_id")
    attempt = validate_opaque_id(attempt_id, kind="attempt_id")
    path = agent_transcript_path(agent, selected)
    report = read_jsonl_tail_report(
        path,
        context="conversation.agent_transcript.attempt_started",
        limit=8,
    )
    request_id = f"bg-agent:{selected}:{attempt}"
    return any(
        _valid_agent_transcript_row(row, selected)
        and str(row.get("request_id") or "") == request_id
        for row in report.rows
    )


# LLM: Explicit finalization trims the optional display stream only. It cannot
# delete the canonical child transcript, result, task, or Compact checkpoint.
# 函数用途: 子代理一轮结束后把展示文件裁成最近 1024 条，限制长期磁盘占用。
def compact_agent_transcript_events(agent: object, *, run_id: str) -> None:
    _compact_agent_transcript_file(agent_transcript_path(agent, run_id))


# LLM: Transition locking is shared by every writer and compactor for this run;
# it prevents a trim from dropping a concurrently appended event.
# 函数用途: 在跨进程互斥区内执行展示流裁剪。
def _compact_agent_transcript_file(path: Path) -> None:
    transition = path.with_name(f".{path.name}.transition")
    with locked_file_transition(transition):
        _compact_agent_transcript_file_locked(path)


# LLM: The caller already owns the transition lock. Atomic replacement keeps
# readers from observing a partial JSONL file.
# 函数用途: 保留展示文件尾部的合法对象并原子覆盖旧文件。
def _compact_agent_transcript_file_locked(path: Path) -> None:
    if not path.exists():
        return
    report = read_jsonl_tail_report(
        path,
        context="conversation.agent_transcript.compact",
        limit=AGENT_TRANSCRIPT_MAX_EVENTS,
    )
    content = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in report.rows
        if isinstance(row, dict)
    )
    write_text_file_atomic(path, content)


# LLM: Validation is an exact schema/identity check; display prose never
# authorizes a row or changes the selected run.
# 函数用途: 过滤损坏、跨 run 或未知协议的展示事件。
def _valid_agent_transcript_row(row: object, run_id: str) -> bool:
    if not isinstance(row, dict):
        return False
    request_id = str(row.get("request_id") or "")
    return bool(
        row.get("schema") == BACKGROUND_TRANSCRIPT_SCHEMA
        and str(row.get("task_id") or "") == run_id
        and request_id.startswith(f"bg-agent:{run_id}:")
        and str(row.get("block_id") or "").startswith(f"{request_id}:")
        and str(row.get("kind") or "") in BACKGROUND_TRANSCRIPT_EVENT_KINDS
        and str(row.get("phase") or "") in BACKGROUND_TRANSCRIPT_EVENT_PHASES
        and isinstance(row.get("payload"), dict)
        and _safe_int(row.get("seq")) > 0
    )


# LLM: Cursor coercion affects display pagination only and never writes a
# lifecycle field, so malformed values safely become zero.
# 函数用途: 将展示游标规范成整数。
def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "AGENT_TRANSCRIPT_MAX_EVENTS",
    "agent_transcript_attempt_has_events",
    "agent_transcript_path",
    "append_agent_transcript_event",
    "begin_agent_transcript_turn",
    "compact_agent_transcript_events",
    "read_agent_transcript_events",
]
