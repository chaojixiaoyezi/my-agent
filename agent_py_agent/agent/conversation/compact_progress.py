"""Canonical public schema for conversation and active-turn Compact progress."""

# LLM: This module is the single validator for display-only Compact progress facts.
# Producers own real checkpoint/CAS state; clients may render this payload but cannot
# infer or advance a durable generation from animation phases.
# 模块用途: 统一校验会话压缩的来源、提交权、阶段、百分比、触发来源和安全错误码，供 Gateway 与各类界面复用。

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

# 触发来源是宿主给出的结构化事实（与 ModelResponse.runtime_source 同一词表），不从进度正文或 detail 文本推断：
# preflight = 发送前按可见上下文/完整请求判定越线；provider_error = 供应商已报上下文溢出；
# tool_context_overflow = 工具上下文窗口裁剪器登记了溢出。手动或旧事件没有这个字段。
COMPACT_TRIGGER_PREFLIGHT = "preflight"
COMPACT_TRIGGER_PROVIDER_ERROR = "provider_error"
COMPACT_TRIGGER_TOOL_CONTEXT_OVERFLOW = "tool_context_overflow"
COMPACT_TRIGGER_SOURCES = frozenset(
    {COMPACT_TRIGGER_PREFLIGHT, COMPACT_TRIGGER_PROVIDER_ERROR, COMPACT_TRIGGER_TOOL_CONTEXT_OVERFLOW}
)

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
# 失败时可选的候选容量计量（非负整数），只在生产者给出时复制；旧事件和其他阶段没有这些字段。
# 字段名与 compact_guard.CompactCapacityFacts 一一对应，由测试锁同步；fixed_tokens 是实测固定开销，
# retained_ir_items/retained_ir_tokens 是非工具归档保留 IR 的条数与估算，二者都只在失败路径出现。
COMPACT_CAPACITY_PROGRESS_FIELDS = (
    "candidate_tokens",
    "input_ceiling_tokens",
    "summary_tokens",
    "fixed_tokens",
    "retained_items",
    "retained_ir_items",
    "retained_ir_tokens",
    "candidates_tried",
)
# 只在生产者给出时才复制的可选计数：模型窗口，加上失败时的候选容量计量。
_OPTIONAL_COUNT_FIELDS = ("context_window_tokens", *COMPACT_CAPACITY_PROGRESS_FIELDS)
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
# 失败容量计量同样只在给出时复制，缺失不补零、不从 after_tokens 推断；trigger_source 只接受
# COMPACT_TRIGGER_SOURCES 里的值，其它值整项丢弃。
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
    error_code = _safe_error_code(value.get("error_code"))
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
    trigger_source = str(value.get("trigger_source") or "").strip()
    if trigger_source in COMPACT_TRIGGER_SOURCES:
        payload["trigger_source"] = trigger_source
    payload.update({key: _nonnegative_int(value[key]) for key in _OPTIONAL_COUNT_FIELDS if key in value})
    return payload


# LLM: 错误码只保留字母数字与下划线并限长，任何正文都不能借错误码进入公开事件。
# 函数用途: 把生产者给的错误码整理成安全的大写标识。
def _safe_error_code(value: object) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in str(value or "").upper()
    )[:96].strip("_")


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
    "COMPACT_CAPACITY_PROGRESS_FIELDS",
    "COMPACT_AUTHORITY_LEGACY",
    "COMPACT_AUTHORITY_TURN_LOCAL",
    "COMPACT_SOURCE_ACTIVE_TURN",
    "COMPACT_SOURCE_LEGACY",
    "COMPACT_SOURCE_TRANSCRIPT",
    "COMPACT_SOURCE_TURN_LOCAL",
    "COMPACT_TRIGGER_PREFLIGHT",
    "COMPACT_TRIGGER_PROVIDER_ERROR",
    "COMPACT_TRIGGER_SOURCES",
    "COMPACT_TRIGGER_TOOL_CONTEXT_OVERFLOW",
    "CONVERSATION_COMPACT_PROGRESS_PHASES",
    "CONVERSATION_COMPACT_PROGRESS_SCHEMA",
    "CONVERSATION_COMPACT_PROGRESS_STAGES",
    "normalize_conversation_compact_progress",
]
