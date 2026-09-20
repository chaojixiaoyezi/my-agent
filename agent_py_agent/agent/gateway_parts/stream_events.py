# LLM: 公开事件按既有 schema 清洗；除长思考沿绑定 sink 归档外只做投影，不写会话/权限/执行状态。
# 模块用途: 集中构造流式统计、用户插话、退避和思考载荷；修改时同步脱敏、归档、插话和普通客户端测试。
from __future__ import annotations

from typing import Literal

from ..common.value_parsing import non_negative_int
from ..conversation.channels import (
    project_host_paths_for_channel,
    project_user_reply,
    redact_structured_identifiers,
)

CONTEXT_USAGE_SCHEMA = "model_visible_context_usage.v1"
_CONTEXT_USAGE_TOKEN_FIELDS = (
    "context_window_tokens",
    "compact_trigger_tokens",
    "current_tokens",
    "prompt_tokens",
    "messages_tokens",
    "runtime_guidance_tokens",
    "tool_schema_tokens",
)
_CONTEXT_COMPACTION_SCHEMA = "model_visible_context_compaction.v1"
_CONTEXT_COMPACTION_FIELDS = (
    "generation",
    "before_tokens",
    "after_tokens",
    "trigger_tokens",
    "dropped_pairs",
    "preserved_pairs",
)


# LLM: Gateway stream sanitization accepts only the frozen schema and known numeric fields;
# unknown keys and all content-bearing values are dropped before the public event is written.
# 函数用途: 清洗上下文用量快照，防止模型正文或工具定义意外进入 Gateway chunk。
def public_context_usage_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema") != CONTEXT_USAGE_SCHEMA:
        return {}
    protocol = str(value.get("protocol") or "")
    return {
        "schema": CONTEXT_USAGE_SCHEMA,
        "estimated": value.get("estimated") is True,
        **{key: non_negative_int(value.get(key) or 0) for key in _CONTEXT_USAGE_TOKEN_FIELDS},
        "protocol": protocol if protocol in {"native", "text"} else "unknown",
    }


# LLM: Gateway projection validates the frozen compaction schema and copies only nonnegative
# counters; no model-generated summary or tool record can cross this boundary.
# 函数用途: 清洗活动回合上下文裁剪事件，拒绝未知 schema 和正文载荷。
def public_context_compaction_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema") != _CONTEXT_COMPACTION_SCHEMA:
        return {}
    return {
        "schema": _CONTEXT_COMPACTION_SCHEMA,
        **{key: non_negative_int(value.get(key) or 0) for key in _CONTEXT_COMPACTION_FIELDS},
    }


# LLM: All public model text shares redaction. A positive max applies only to volatile
# display-only thinking; zero preserves committed assistant prose for transcript/Compact.
# 函数用途: 生成公开模型文本；max_chars=0 时保留完整已确认回复。
def public_model_text(text: object, *, max_chars: int, delivery_channel: str, identifiers: tuple[tuple[object, str], ...]) -> str:
    projection = project_user_reply(str(text or ""))
    content = redact_structured_identifiers(
        project_host_paths_for_channel(projection.content, delivery_channel),
        identifiers,
        channel=delivery_channel,
    ).strip()
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    head = max_chars * 2 // 3
    tail = max_chars - head
    return f"{content[:head]}\n…（内容过长，已省略）…\n{content[-tail:]}"


# LLM: Sanitize the full thinking before preview clipping and archive through the bound
# owner/thread sink. Events carry only preview/ref; display failure never changes model state.
# 函数用途: 保留完整公开思考原文，再发送有界预览和引用；保持思考先于正文，不丢失长思考中间部分。
def thinking_event(
    text: str, *, duration_seconds: float, delivery_channel: str,
    identifiers: tuple[tuple[object, str], ...], transcript_sink: object | None,
) -> dict[str, object]:
    full_content = public_model_text(text, max_chars=0, delivery_channel=delivery_channel, identifiers=identifiers)
    if not full_content:
        return {}
    archived = {}
    content = full_content
    if len(full_content) > 12_000:
        from ..conversation.background_transcript import _thinking_archive_payload

        archived = _thinking_archive_payload(transcript_sink, full_content)
        content = public_model_text(full_content, max_chars=12_000, delivery_channel=delivery_channel, identifiers=identifiers)
    return {
        "kind": "assistant_thinking",
        "text": content,
        "duration_seconds": max(0.0, round(float(duration_seconds or 0.0), 3)),
        **archived,
    }


# LLM: provider retry 只向显式 rich 客户端公开有界结构化进度；原始异常和 endpoint 不得进入公开 chunk。
# 函数用途: 在传输层或模型回合退避期间立即显示重连次数和等待秒数。
def provider_retry_event(
    *,
    scope: str,
    attempt: int,
    total: int,
    delay_seconds: float,
    error_type: str,
) -> dict[str, object]:
    retry_scope = "transport" if scope == "transport" else "model_turn"
    retry_attempt = max(1, int(attempt or 1))
    retry_total = max(retry_attempt, int(total or retry_attempt))
    wait_seconds = max(0.0, round(float(delay_seconds or 0.0), 1))
    layer = "连接" if retry_scope == "transport" else "模型回合"
    return {
        "kind": "runtime_progress",
        "text": (
            f"模型服务暂时不可用，{wait_seconds:g} 秒后自动重连"
            f"（{layer} {retry_attempt}/{retry_total}）"
        ),
        "verbose_level": "full",
        "retry": {
            "scope": retry_scope,
            "attempt": retry_attempt,
            "total": retry_total,
            "wait_seconds": wait_seconds,
            "error_type": str(error_type or ""),
        },
    }


# LLM: submitted/consumed 来自两个已提交状态边界，绝不互相推断；只携带命中的结构化编号及其正文。
# 函数用途: 构造可重放的插话事件，丢弃未被接收的正文，无有效编号时不发布。
def active_turn_input_event(
    kind: Literal["active_turn_input_submitted", "active_turn_input_consumed"],
    client_message_ids: tuple[str, ...], *, client_messages: tuple[tuple[str, str], ...],
    provider_call_id: str = "",
) -> dict[str, object]:
    message_ids = tuple(item for item in (str(value or "").strip() for value in client_message_ids) if item)
    if not message_ids:
        return {}
    accepted = set(message_ids)
    messages = [
        {"message_id": message_id, "text": text}
        for raw_id, raw_text in tuple(client_messages or ())
        if (message_id := str(raw_id or "").strip()) in accepted and (text := str(raw_text or ""))
    ]
    return {
        "kind": kind,
        "client_message_ids": list(message_ids),
        **({"provider_call_id": str(provider_call_id or "").strip()} if kind == "active_turn_input_submitted" else {}),
        **({"messages": messages} if messages else {}),
    }
