"""Canonical provider-neutral message envelopes for ended conversation turns.

The visible transcript remains ordinary user/assistant prose.  A final assistant row may also
carry the exact native messages used by that turn, including an interrupted/failed turn, so a later turn can retain tool calls,
tool results and their append-only cache prefix.  Compact is the only operation allowed to replace
that raw tail with a summary.
"""

# LLM: This module owns the sole persisted provider-neutral native-message envelope used by both
# Gateway and child threads; changes must keep schema validation, turn grouping and Compact aligned.
# 模块用途: 保存正常或中断回合的原生消息；停止的空正文只携带历史，不生成替代回复或丢失工具往返。

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from .display_checkpoint import is_display_checkpoint

CANONICAL_NATIVE_MESSAGES_METADATA_KEY = "canonical_native_messages"
CANONICAL_NATIVE_MESSAGES_SCHEMA = "conversation_native_messages.v1"


# LLM: This envelope is internal owner-scoped conversation metadata, never a public channel
# payload. It accepts open-world content blocks but only user/assistant roles so persisted data
# cannot manufacture a system instruction on replay.
# 函数用途: 把一次已结束原生回合的消息整理成可写入 transcript metadata 的稳定结构。
def canonical_native_messages_envelope(messages: object) -> dict[str, object]:
    normalized = _normalized_native_messages(messages)
    if not normalized:
        return {}
    return {
        "schema": CANONICAL_NATIVE_MESSAGES_SCHEMA,
        "messages": normalized,
    }


# LLM: Metadata is an untrusted persistence boundary. Read only the current schema and return a
# detached copy; malformed or legacy values fall back to visible transcript projection.
# 函数用途: 从一条消息 metadata 安全读取已保存的原生消息，没有合法数据时返回空。
def canonical_native_messages_from_metadata(metadata: object) -> tuple[dict[str, Any], ...]:
    row = metadata if isinstance(metadata, dict) else {}
    envelope = row.get(CANONICAL_NATIVE_MESSAGES_METADATA_KEY)
    if not isinstance(envelope, dict):
        return ()
    if str(envelope.get("schema") or "") != CANONICAL_NATIVE_MESSAGES_SCHEMA:
        return ()
    return tuple(_normalized_native_messages(envelope.get("messages")))


# LLM: A final-row canonical envelope replaces every visible row bearing the same structured
# request identity for provider replay only. Visible prose is still preserved independently for
# TUI/search/text backends, and turns without an envelope keep the legacy role/text projection.
# display rows are excluded before envelope selection, including malformed or forged display metadata.
# 函数用途: 排除纯展示行后恢复原生消息；display中即使有伪造native metadata也不能污染下一轮缓存或输入。
def provider_history_messages_from_rows(rows: Iterable[object]) -> tuple[dict[str, Any], ...]:
    selected = [row for row in rows if not is_display_checkpoint(row)]
    envelopes: dict[str, tuple[dict[str, Any], ...]] = {}
    anonymous_envelopes: dict[str, tuple[dict[str, Any], ...]] = {}
    for row in selected:
        messages = canonical_native_messages_from_metadata(getattr(row, "metadata", None))
        if not messages:
            continue
        identity = _row_turn_identity(row)
        if identity:
            envelopes[identity] = messages
        else:
            anonymous_envelopes[str(getattr(row, "message_id", "") or id(row))] = messages

    emitted: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in selected:
        identity = _row_turn_identity(row)
        envelope = envelopes.get(identity) if identity else None
        if envelope:
            if identity not in emitted:
                result.extend(deepcopy(list(envelope)))
                emitted.add(identity)
            continue
        anonymous_key = str(getattr(row, "message_id", "") or id(row))
        anonymous = anonymous_envelopes.get(anonymous_key)
        if anonymous:
            result.extend(deepcopy(list(anonymous)))
            continue
        legacy = _legacy_row_message(row)
        if legacy is not None:
            result.append(legacy)
    return tuple(result)


# LLM: Request identity comes only from host-written metadata keys shared by Gateway and child
# threads; natural-language content and timestamps must never be used to guess turn grouping.
# 函数用途: 取得一条 transcript 记录所属的结构化回合编号。
def _row_turn_identity(row: object) -> str:
    metadata = getattr(row, "metadata", None)
    values = metadata if isinstance(metadata, dict) else {}
    return str(
        values.get("conversation_request_id")
        or values.get("gateway_request_id")
        or values.get("agent_attempt_id")
        or ""
    ).strip()


# LLM: Legacy transcript rows remain a provider-neutral text fallback. Only explicit user and
# assistant roles are replayable; internal/control rows cannot enter model history through here.
# 函数用途: 把没有原生 envelope 的旧消息转换成一条兼容原生后端的文本消息。
def _legacy_row_message(row: object) -> dict[str, Any] | None:
    role = str(getattr(row, "role", "") or "").strip().lower()
    content = str(getattr(row, "content", "") or "")
    if role not in {"user", "assistant"} or not content:
        return None
    return {"role": role, "content": [{"type": "text", "text": content}]}


# LLM: Keep the canonical envelope open to future provider-neutral block types while enforcing a
# JSON-shaped role/content boundary. Exact nested values are copied, never stringified or parsed.
# 函数用途: 校验并复制原生消息列表，过滤不能安全回放的顶层消息。
def _normalized_native_messages(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        content = raw.get("content")
        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, (str, list)):
            continue
        if isinstance(content, list) and not all(isinstance(block, dict) for block in content):
            continue
        normalized.append({"role": role, "content": deepcopy(content)})
    return normalized


__all__ = [
    "CANONICAL_NATIVE_MESSAGES_METADATA_KEY",
    "CANONICAL_NATIVE_MESSAGES_SCHEMA",
    "canonical_native_messages_envelope",
    "canonical_native_messages_from_metadata",
    "provider_history_messages_from_rows",
]
