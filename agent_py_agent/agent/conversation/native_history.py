"""Canonical provider-neutral message envelopes for ended conversation turns.

The visible transcript remains ordinary user/assistant prose.  A final assistant row may also
carry the exact native messages used by that turn, including an interrupted/failed turn, so a later turn can retain tool calls,
tool results and their append-only cache prefix.  Compact is the only operation allowed to replace
that raw tail with a summary.
"""

# LLM: This module owns the sole persisted provider-neutral native-message envelope used by both
# Gateway and child threads; changes must keep schema validation, turn grouping and Compact aligned.
# 模块用途: 保存并隔离回放正常或中断回合的原生消息；停止空正文仍保留工具往返，展示不生成替代回复。

from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import deepcopy
from typing import Any

from .display_checkpoint import is_display_checkpoint
from .message_replay import message_rows_iterator

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
# display行不参与选择；重放核心只保存envelope行位置，列表接口仍返回一次独占隔离投影。
# 函数用途: 为需要完整内存请求的调用者物化原生历史，摘要可直接复用同一迭代核心。
def provider_history_messages_from_rows(rows: Iterable[object]) -> tuple[dict[str, Any], ...]:
    return tuple(iter_provider_history_messages_from_rows(rows))


# LLM: 两遍索引/输出都关闭上游；无ID行按位置绑定，不能以重建对象的id跨遍匹配；单次Iterable仍先固定引用。
# 函数用途: 顺序生成独立原生消息，保持identified/匿名重复及display过滤语义，不驻留全部历史正文。
def iter_provider_history_messages_from_rows(rows: Iterable[object]):
    selected = rows if isinstance(rows, Sequence) else tuple(rows)
    envelopes, anonymous_envelopes = {}, {}
    with message_rows_iterator(selected) as iterator:
        for index, row in enumerate(iterator):
            if is_display_checkpoint(row) or not _has_native_envelope(row):
                continue
            identity = _row_turn_identity(row)
            if identity:
                envelopes[identity] = index
            else:
                anonymous_envelopes[_anonymous_row_key(row, index)] = index
    emitted: set[str] = set()
    with message_rows_iterator(selected) as iterator:
        for index, row in enumerate(iterator):
            if is_display_checkpoint(row):
                continue
            identity = _row_turn_identity(row)
            envelope = envelopes.get(identity) if identity else None
            if envelope is not None:
                if identity not in emitted:
                    yield from canonical_native_messages_from_metadata(getattr(selected[envelope], 'metadata', None))
                    emitted.add(identity)
                continue
            anonymous = anonymous_envelopes.get(_anonymous_row_key(row, index))
            if anonymous is not None:
                yield from canonical_native_messages_from_metadata(getattr(selected[anonymous], 'metadata', None))
                continue
            legacy = _legacy_row_message(row)
            if legacy is not None:
                yield legacy


# LLM: 显式message_id仍合并最后信封；缺ID没有持久身份，只能按本次序列位置独立回放，不能借对象地址跨遍匹配。
# 函数用途: 为匿名行生成不依赖正文或内存地址的临时索引键。
def _anonymous_row_key(row, index):
    message_id = str(getattr(row, 'message_id', '') or '')
    return ('message', message_id) if message_id else ('position', index)


# LLM: 仅检查与normalizer一致的schema/role/content形状，不复制正文；是否有效不得按文本或工具名猜测。
# 函数用途: 为第一遍索引判断一行是否有可回放信封，不持有其内容。
def _has_native_envelope(row):
    metadata = getattr(row, 'metadata', None)
    envelope = metadata.get(CANONICAL_NATIVE_MESSAGES_METADATA_KEY) if isinstance(metadata, dict) else None
    return (isinstance(envelope, dict) and str(envelope.get('schema') or '') == CANONICAL_NATIVE_MESSAGES_SCHEMA
            and next(_native_message_values(envelope.get('messages')), None) is not None)


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


# LLM: 顶层校验共用唯一迭代器，输出嵌套值在此隔离，不字符串化或缩短未来content类型。
# 函数用途: 将有效原生消息复制为调用者独占的列表。
def _normalized_native_messages(value: object) -> list[dict[str, Any]]:
    return [{'role': role, 'content': deepcopy(content)} for role, content in _native_message_values(value)]


# LLM: 本入口只借用值作形状验证，调用方不得保存或修改借用content；最终公开投影由normalizer隔离。
# 函数用途: 统一首次索引和正式回放的顶层role/content合法性规则。
def _native_message_values(value):
    if not isinstance(value, (list, tuple)):
        return
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
        yield role, content


__all__ = [
    "CANONICAL_NATIVE_MESSAGES_METADATA_KEY",
    "CANONICAL_NATIVE_MESSAGES_SCHEMA",
    "canonical_native_messages_envelope",
    "canonical_native_messages_from_metadata",
    "provider_history_messages_from_rows",
]
