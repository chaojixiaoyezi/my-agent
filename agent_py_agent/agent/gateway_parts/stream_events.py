# LLM: 公开事件按既有 schema 清洗；除长思考沿绑定 sink 归档外只做投影，不写会话/权限/执行状态。
# 模块用途: 集中构造统计、插话、排队、退避和思考载荷；修改时同步脱敏、归档及普通客户端测试。
from __future__ import annotations

import threading
from typing import Any, Literal

from ..backends.assistant_messages import message_projection
from ..common.value_parsing import non_negative_int
from ..conversation.channels import (
    project_host_paths_for_channel,
    project_user_reply,
    redact_structured_identifiers,
)
from ..conversation.context_usage import public_context_usage

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


# LLM: 公开排队只接受阶段和数字白名单；正文、端点及 owner 永不投影，不参与准入或重试裁决。
# 函数用途: 复用 TUI 已支持的运行进度，给 IM 状态保留同一结构化排队事实。
def provider_queue_event(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("phase") not in {"waiting", "admitted", "cancelled"}:
        return {}
    phase = value["phase"]
    public = {"phase": phase, "admission_wait_reason": "provider_concurrency",
              "wait_seconds": max(0.0, round(float(value.get("wait_seconds") or 0), 3))}
    text = {"waiting": "模型接口排队中", "admitted": "模型接口排队结束，正在请求", "cancelled": "模型接口排队已结束"}[phase]
    return {"kind": "runtime_progress", "text": text, "verbose_level": "full", "provider_queue": public}


# LLM: Gateway沿唯一公开白名单保留数字、短标签及有明确证据的估算来源；不推断旧来源，不放行正文或工具参数。
# 函数用途: 让Gateway事件与持久快照使用同一上下文用量投影。
def public_context_usage_payload(value: object) -> dict[str, object]:
    return public_context_usage(value)


# 函数用途: 只接受字符串、去首尾空白、截到 80 字符；其它类型按空串，不让长正文借这两个字段进事件流。
def _short_label(value: object) -> str:
    return value.strip()[:80] if isinstance(value, str) else ""


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


# LLM: 输入只来自已验响应提交；整段安全投影不等于分段时合并显示并标记降级，不制造provider phase或执行许可。
# 函数用途: 将一次完整模型正文转换为可校正候选的原显示事件，不输出隐藏推理。
def committed_model_events(payload: dict, *, delivery_channel: str = "", identifiers: tuple = ()) -> list[dict]:
    source = payload.get("source")
    if (payload.get("schema") != "assistant_messages.v1" or not isinstance(source, dict)
            or not source.get("call_id") or payload.get("boundary") not in {"tools", "terminal"}):
        return []
    rows = payload.get("messages")
    if not isinstance(rows, list):
        return []
    rows = [row for row in rows if isinstance(row, dict)]
    rows, full, separable = message_projection(rows, lambda text: public_model_text(
        text, max_chars=0, delivery_channel=delivery_channel, identifiers=identifiers))
    events = []
    final_known = any(row.get("phase") == "final_answer" for row in rows)
    if not separable:
        # 安全上下文跨消息时只发送原整段的安全投影；阶段资格仍由原结构决定，不给合并段伪造消息身份。
        final_eligible = final_known or any(row.get("phase") != "commentary" for row in rows)
        if full:
            events.append({"kind": "assistant_final" if payload["boundary"] == "terminal" and final_eligible else "assistant_commentary",
                           "text": full, "model_source": source, "projection": "full_response_security_context"})
    else:
        for row in rows:
            process = payload["boundary"] == "tools" or row.get("phase") == "commentary" or (final_known and row.get("phase") != "final_answer")
            if process and row["text"]:
                events.append({"kind": "assistant_commentary", "text": row["text"], "model_source": source,
                               "message_source": {key: row.get(key) for key in ("protocol", "message_id", "output_index", "phase", "source_type")}})
        if payload["boundary"] == "terminal":
            final = public_model_text(payload.get("final_text"), max_chars=0, delivery_channel=delivery_channel, identifiers=identifiers)
            if final:
                events.append({"kind": "assistant_final", "text": final, "model_source": source})
    for index, event in enumerate(events):
        event["replace_candidate"] = index == 0
    return events


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
#   params 是可选收口参数包：{"final": True, "error_code": ...} 表示「已到总时长上限、本轮不再重试」，
#   retry 载荷额外带 final/error_code 两个字段，文案复用 request_errors 已登记的用户文案；
#   普通重试不传 params，载荷一个字段都不加，保持旧客户端字节兼容。判断只看结构化字段，不解析文案。
# 函数用途: 在传输层或模型回合退避期间立即显示重连次数和等待秒数；到上限时显示收口提示。
def provider_retry_event(
    *,
    scope: str,
    attempt: int,
    total: int,
    delay_seconds: float,
    error_type: str,
    params: dict | None = None,
) -> dict[str, object]:
    retry_scope = "transport" if scope == "transport" else "model_turn"
    retry_attempt = max(1, int(attempt or 1))
    retry_total = max(retry_attempt, int(total or retry_attempt))
    wait_seconds = max(0.0, round(float(delay_seconds or 0.0), 1))
    layer = "连接" if retry_scope == "transport" else "模型回合"
    retry: dict[str, object] = {
        "scope": retry_scope,
        "attempt": retry_attempt,
        "total": retry_total,
        "wait_seconds": wait_seconds,
        "error_type": str(error_type or ""),
    }
    if isinstance(params, dict) and params.get("final") is True:
        from .request_errors import gateway_client_error_message

        error_code = str(params.get("error_code") or "")
        retry["final"] = True
        retry["error_code"] = error_code
        text = gateway_client_error_message(error_code)
    else:
        text = (
            f"模型服务暂时不可用，{wait_seconds:g} 秒后自动重连"
            f"（{layer} {retry_attempt}/{retry_total}）"
        )
    return {
        "kind": "runtime_progress",
        "text": text,
        "verbose_level": "full",
        "retry": retry,
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


# LLM: 仅在非终局刷新时保留已闭合命中之后的敏感标识真前缀尾部，防止重叠规则拆碎已完整命中的敏感ID。
# 函数用途: 拆分待发思考与未闭合的标识前缀，防止跨chunk边界泄漏敏感ID。
def drain_thinking_delta_text(writer: Any, raw_joined: str, *, final: bool = True) -> str:
    emit_raw = _hold_incomplete_redaction_suffix(writer, raw_joined) if not final and raw_joined else raw_joined
    if not emit_raw:
        return ""
    return redact_structured_identifiers(
        project_host_paths_for_channel(emit_raw, writer.delivery_channel),
        writer._identifier_redactions,
        channel=writer.delivery_channel,
    )


# LLM: 扫描完整命中边界并将未闭合的敏感标识真前缀尾部锁内压回缓冲头部，不截断已闭合的短敏感ID。
# 函数用途: 为非终局思考刷新分离可安全发送的前缀与需要继续等待后续chunk的尾部。
def _hold_incomplete_redaction_suffix(writer: Any, raw_joined: str) -> str:
    needles = sorted(
        {src for src, _ in (writer._identifier_redactions or ()) if isinstance(src, str) and len(src) > 1},
        key=len,
        reverse=True,
    )
    if not needles:
        return raw_joined
    complete_end, pos = 0, 0
    while pos < len(raw_joined):
        matched = next((src for src in needles if raw_joined.startswith(src, pos)), "")
        if not matched:
            pos += 1
            continue
        end = pos + len(matched)
        if end == len(raw_joined) and any(len(other) > len(matched) and other.startswith(matched) for other in needles):
            break
        complete_end = end
        pos = end
    max_len = max(len(src) for src in needles)
    start_min = max(complete_end, len(raw_joined) - max_len + 1)
    for idx in range(start_min, len(raw_joined)):
        suffix = raw_joined[idx:]
        if any(len(suffix) < len(src) and src.startswith(suffix) for src in needles):
            with writer._commit_lock:
                writer._thinking_buffer.insert(0, suffix)
                writer._thinking_buffer_chars += len(suffix)
            return raw_joined[:idx]
    return raw_joined


# LLM: 锁内原子摘取待发思考批次并设置发布线程标识；若已有在途发布则记录待排空意图，防止并发丢尾或越序。
# 函数用途: 为单次思考刷新取出独立批次列表并标记当前发布线程。
def begin_thinking_flush(writer: Any, now: float, *, final: bool = True) -> list[str]:
    cancel_thinking_flush_timer(writer)
    ident = threading.get_ident()
    with writer._commit_lock:
        if writer._thinking_publishing_tid is not None:
            if writer._thinking_publishing_tid != ident:
                if final:
                    writer._thinking_pending_final = True
                else:
                    writer._thinking_pending_nonfinal = True
            return []
        if not writer._thinking_buffer:
            return []
        writer._last_thinking_flush_at = now
        batch = list(writer._thinking_buffer)
        writer._thinking_buffer = []
        writer._thinking_buffer_chars = 0
        writer._thinking_pending_final = False
        writer._thinking_pending_nonfinal = False
        writer._thinking_publishing_tid = ident
        return batch


# LLM: 锁内按"先在途待排空思考、后延迟普通事件"顺序取下一批工作，无剩余工作时原子清除发布线程标识并唤醒等待者。
# 函数用途: 为complete_thinking_flush提供单步锁内工作提取，保证新到思考先于后到普通事件。
def _next_completion_work(
    writer: Any,
    ident: int,
) -> tuple[list[str], bool, list[tuple[dict[str, object], Any]]]:
    with writer._commit_lock:
        if writer._thinking_publishing_tid != ident:
            return [], False, []
        closed = bool(getattr(writer, "_liveness_closed", False))
        if writer._thinking_buffer and (
            writer._thinking_pending_final or writer._thinking_pending_nonfinal or closed
        ):
            batch = list(writer._thinking_buffer)
            writer._thinking_buffer = []
            writer._thinking_buffer_chars = 0
            batch_final = bool(writer._thinking_pending_final or closed)
            writer._thinking_pending_final = False
            writer._thinking_pending_nonfinal = False
            return batch, batch_final, []
        if writer._deferred_after_thinking:
            deferred = list(writer._deferred_after_thinking)
            writer._deferred_after_thinking.clear()
            return [], False, deferred
        writer._thinking_publishing_tid = None
        writer._thinking_pending_final = False
        writer._thinking_pending_nonfinal = False
        cond = getattr(writer, "_thinking_cond", None)
        if cond is not None:
            cond.notify_all()
        return [], False, []


# LLM: 逐条发布延迟普通事件；若单条抛异常则将未发剩余事件压回队列头部，避免吞掉未执行事件。
# 函数用途: 异常安全地冲刷一批在思考发布期间排队等待的普通事件。
def _publish_deferred_batch(
    writer: Any,
    publish_fn: Any,
    deferred_batch: list[tuple[dict[str, object], Any]],
) -> None:
    for idx, (deferred_event, deferred_current) in enumerate(deferred_batch):
        try:
            publish_fn(writer, deferred_event, still_current=deferred_current)
        except BaseException:
            remaining = deferred_batch[idx + 1 :]
            if remaining:
                with writer._commit_lock:
                    writer._deferred_after_thinking[:0] = remaining
            raise


# LLM: 在保持发布线程标识的前提下先排空期间请求的思考尾块、再排空延迟事件，finally仅在仍归属当前线程时清理tid，绝不误清接班线程。
# 函数用途: 完成思考批次发布后按因果顺序冲刷在途思考与延迟的非thinking事件。
def complete_thinking_flush(writer: Any, publish_fn: Any) -> None:
    ident = threading.get_ident()
    has_more = False
    try:
        while True:
            next_batch, batch_final, deferred_batch = _next_completion_work(writer, ident)
            if next_batch:
                if content := drain_thinking_delta_text(writer, "".join(next_batch), final=batch_final):
                    writer._thinking_emitted = True
                    publish_fn(writer, {"kind": "thinking_delta", "text": content})
                continue
            if not deferred_batch:
                break
            _publish_deferred_batch(writer, publish_fn, deferred_batch)
        with writer._commit_lock:
            has_more = bool(writer._thinking_buffer) and not getattr(writer, "_liveness_closed", False)
    finally:
        with writer._commit_lock:
            if writer._thinking_publishing_tid == ident:
                writer._thinking_publishing_tid = None
                cond = getattr(writer, "_thinking_cond", None)
                if cond is not None:
                    cond.notify_all()
    if has_more:
        schedule_thinking_flush_timer(writer)


# LLM: 当无新思考缓冲但延迟事件队列因前序异常残留时，认领发布线程标识并排空剩余事件，保持严格FIFO顺序。
# 函数用途: 为close/flush/_write_event提供不依赖新思考到来的延迟事件恢复排空入口。
def drain_orphaned_deferred_events(writer: Any, publish_fn: Any) -> None:
    ident = threading.get_ident()
    claimed = False
    with writer._commit_lock:
        if (
            writer._thinking_publishing_tid is None
            and not writer._thinking_buffer
            and writer._deferred_after_thinking
        ):
            writer._thinking_publishing_tid = ident
            claimed = True
    if claimed:
        complete_thinking_flush(writer, publish_fn)


# LLM: 跨线程终局收口（close/prepare_display_history）在有界时间内等待在途思考发布完成，随后排空任何残留思考或延迟事件。
# 函数用途: 防止其他线程正在落盘思考或延迟事件时提前关闭sink或冻结最终显示快照。
def wait_thinking_flush_idle(writer: Any, publish_fn: Any, *, timeout: float = 1.0) -> None:
    ident = threading.get_ident()
    cond = getattr(writer, "_thinking_cond", None)
    if cond is not None:
        with cond:
            if writer._thinking_publishing_tid is not None and writer._thinking_publishing_tid != ident:
                cond.wait_for(lambda: writer._thinking_publishing_tid is None, timeout=timeout)
    writer.flush_thinking_delta(final=True)
    drain_orphaned_deferred_events(writer, publish_fn)


# LLM: 后台守护定时器只在单调时钟真正达到间隔且流未关闭时触发合批刷新，不绕过测试时钟。
# 函数用途: 在思考流暂停且无新delta到达时，按刷新间隔自动排空缓冲尾部。
def schedule_thinking_flush_timer(writer: Any, due_check: Any = None) -> None:
    if getattr(writer, "_liveness_closed", False) or not writer._thinking_buffer:
        return
    existing = getattr(writer, "_thinking_flush_timer", None)
    if existing is not None:
        return
    delay = max(0.01, min(float(getattr(writer, "thinking_flush_interval_seconds", 0.25) or 0.25), 0.25))

    def _on_timer() -> None:
        writer._thinking_flush_timer = None
        if getattr(writer, "_liveness_closed", False) or not writer._thinking_buffer:
            return
        if callable(due_check) and not due_check():
            return
        writer.flush_thinking_delta(final=False)

    timer = threading.Timer(delay, _on_timer)
    timer.daemon = True
    writer._thinking_flush_timer = timer
    timer.start()


# LLM: 在任何显式刷新或流关闭时取消未触发的思考定时器，避免结束后残留后台线程。
# 函数用途: 停止并清理当前writer挂起的思考尾块刷新定时器。
def cancel_thinking_flush_timer(writer: Any) -> None:
    timer = getattr(writer, "_thinking_flush_timer", None)
    writer._thinking_flush_timer = None
    if timer is not None:
        timer.cancel()
