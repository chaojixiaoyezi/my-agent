"""Canonical public schema for conversation and active-turn Compact progress."""

# LLM: This module is the single validator for display-only Compact progress facts.
# Producers own real checkpoint/CAS state; clients may render this payload but cannot
# infer or advance a durable generation from animation phases.
# 模块用途: 统一校验会话压缩的来源、提交权、阶段、百分比和安全错误码，供 Gateway 与各类界面复用。

from __future__ import annotations

from collections.abc import Mapping

CONVERSATION_COMPACT_PROGRESS_SCHEMA = "conversation_compaction_progress.v1"

COMPACT_SOURCE_TRANSCRIPT = "conversation_transcript"
COMPACT_SOURCE_ACTIVE_TURN = "active_turn_tool_archive"
COMPACT_SOURCE_TURN_LOCAL = "turn_local_tool_ir"
COMPACT_SOURCE_LEGACY = "legacy"

COMPACT_AUTHORITY_CONVERSATION = "conversation_thread"
COMPACT_AUTHORITY_TURN_LOCAL = "turn_local"
COMPACT_AUTHORITY_LEGACY = "legacy"

CONVERSATION_COMPACT_PROGRESS_PHASES = frozenset(
    {"started", "progress", "completed", "superseded", "failed"}
)
CONVERSATION_COMPACT_PROGRESS_STAGES = frozenset(
    {
        "preparing",
        "summarizing",
        "measuring",
        "checkpointing",
        "committing",
        "completed",
        "candidate_discarded",
        "failed",
    }
)
_COMPACT_SOURCE_AUTHORITY_PAIRS = frozenset(
    {
        (COMPACT_SOURCE_TRANSCRIPT, COMPACT_AUTHORITY_CONVERSATION),
        (COMPACT_SOURCE_ACTIVE_TURN, COMPACT_AUTHORITY_CONVERSATION),
        (COMPACT_SOURCE_TURN_LOCAL, COMPACT_AUTHORITY_TURN_LOCAL),
        (COMPACT_SOURCE_LEGACY, COMPACT_AUTHORITY_LEGACY),
    }
)


# LLM: Historical v1 events predate source fields. Missing both fields maps to one
# explicit legacy display pair; optional model window is copied, never inferred from a threshold.
# 函数用途: 把一条外部 Compact 进度整理成固定公开字段，拒绝未知来源与提交权限组合。
def normalize_conversation_compact_progress(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    if value.get("schema") != CONVERSATION_COMPACT_PROGRESS_SCHEMA:
        return {}
    phase = str(value.get("phase") or "")
    stage = str(value.get("stage") or "")
    if (
        phase not in CONVERSATION_COMPACT_PROGRESS_PHASES
        or stage not in CONVERSATION_COMPACT_PROGRESS_STAGES
    ):
        return {}
    generation = _nonnegative_int(value.get("generation"))
    if generation <= 0:
        return {}
    source_kind = str(value.get("source_kind") or "").strip()
    commit_authority = str(value.get("commit_authority") or "").strip()
    if not source_kind and not commit_authority:
        source_kind = COMPACT_SOURCE_LEGACY
        commit_authority = COMPACT_AUTHORITY_LEGACY
    if (source_kind, commit_authority) not in _COMPACT_SOURCE_AUTHORITY_PAIRS:
        return {}
    operation_id = str(value.get("operation_id") or "").strip()[:128]
    error_code = "".join(
        character if character.isalnum() else "_"
        for character in str(value.get("error_code") or "").upper()
    )[:96].strip("_")
    payload: dict[str, object] = {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA,
        "phase": phase,
        "stage": stage,
        "percent": min(100, _nonnegative_int(value.get("percent"))),
        "generation": generation,
        "operation_id": operation_id or f"legacy:{generation}",
        "source_kind": source_kind,
        "commit_authority": commit_authority,
        "before_tokens": _nonnegative_int(value.get("before_tokens")),
        "after_tokens": _nonnegative_int(value.get("after_tokens")),
        "trigger_tokens": _nonnegative_int(value.get("trigger_tokens")),
        "source_messages": _nonnegative_int(value.get("source_messages")),
    }
    if error_code:
        payload["error_code"] = error_code
    if "context_window_tokens" in value:
        payload["context_window_tokens"] = _nonnegative_int(value["context_window_tokens"])
    return payload


# LLM: Numeric display fields accept ordinary JSON scalars but never raise across
# the projection boundary; negative and malformed values collapse to zero.
# 函数用途: 把进度计数安全转成非负整数。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "COMPACT_AUTHORITY_CONVERSATION",
    "COMPACT_AUTHORITY_LEGACY",
    "COMPACT_AUTHORITY_TURN_LOCAL",
    "COMPACT_SOURCE_ACTIVE_TURN",
    "COMPACT_SOURCE_LEGACY",
    "COMPACT_SOURCE_TRANSCRIPT",
    "COMPACT_SOURCE_TURN_LOCAL",
    "CONVERSATION_COMPACT_PROGRESS_PHASES",
    "CONVERSATION_COMPACT_PROGRESS_SCHEMA",
    "CONVERSATION_COMPACT_PROGRESS_STAGES",
    "normalize_conversation_compact_progress",
]
