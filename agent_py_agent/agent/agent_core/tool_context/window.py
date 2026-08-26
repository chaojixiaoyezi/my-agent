# LLM: This module owns bounded model-facing projections of archived tool activity. Live windows,
# cross-process handoffs, and terminal conversation folds must share redaction and size rules while
# canonical archives and ConversationStore remain the only durable fact sources.
# 模块用途: 把过长工具历史压成可续做、可核验的短上下文，供当前轮、恢复轮和跨回合历史共用。
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar

from ...common.log_redaction import redact_sensitive_value
from ...conversation.authority import conversation_transcript_is_authoritative
from ...tooling.output_projection import project_tool_output_body
from ..runtime.context_compactor import runtime_compact_policy

_DEFAULT_MAX_CHARS = 48_000
_DEFAULT_ACTIVE_TURN_HANDOFF_MAX_CHARS = 12_000
_DEFAULT_TERMINAL_TOOL_FOLD_MAX_CHARS = 6_000
_MAX_STORED_TERMINAL_TOOL_FOLD_CHARS = 48_000
_CHARS_PER_TOKEN_WINDOW = 3
_RECENT_REF_LIMIT = 8
TERMINAL_TOOL_FOLD_METADATA_KEY = "terminal_tool_fold"
TERMINAL_TOOL_FOLD_SCHEMA = "conversation_terminal_tool_fold.v1"


@dataclass(frozen=True)
class ToolContextWindowRequest:
    __test__: ClassVar[bool] = False

    tool_context: list
    archive_tool_calls: list
    max_chars: int = _DEFAULT_MAX_CHARS


@dataclass(frozen=True)
class ToolContextWindowResult:
    windowed: bool = False
    omitted_count: int = 0
    original_chars: int = 0
    preserved_count: int = 0


def window_tool_context_for_live_prompt(request: ToolContextWindowRequest) -> ToolContextWindowResult:
    entries = request.tool_context
    if not isinstance(entries, list) or not entries:
        return ToolContextWindowResult()
    max_chars = _positive_max_chars(request.max_chars)
    original_chars = _context_chars(entries)
    if original_chars <= max_chars:
        return ToolContextWindowResult(original_chars=original_chars)
    active_entries = _non_window_entries(entries)
    recent_entries = _recent_entries_within_budget(active_entries, max_chars)
    omitted_count = max(0, len(active_entries) - len(recent_entries))
    if omitted_count <= 0:
        return ToolContextWindowResult(original_chars=original_chars, preserved_count=len(recent_entries))
    entries[:] = [
        _window_summary(omitted_count, len(recent_entries), request.archive_tool_calls),
        *recent_entries,
    ]
    return ToolContextWindowResult(
        windowed=True,
        omitted_count=omitted_count,
        original_chars=original_chars,
        preserved_count=len(recent_entries),
    )


def window_tool_context_params(agent: object, params: object) -> None:
    max_chars = _tool_context_window_max_chars(agent)
    result = window_tool_context_for_live_prompt(
        ToolContextWindowRequest(
            tool_context=getattr(params, "tool_context", []),
            archive_tool_calls=getattr(params, "archive_tool_calls", []),
            max_chars=max_chars,
        )
    )
    if result.windowed and _persistent_compact_enabled(agent, params):
        _record_tool_context_window_overflow(params, result)


# LLM: This projection records one bounded native-window handoff in the existing tool_context;
# canonical tool archives and operation ledgers remain authoritative and no compact state is created.
# 函数用途: 原生工具历史裁剪后，保存近期工具摘要和归档引用，帮助下一轮从当前进度继续。
def record_native_ir_window(params: object, *, omitted_count: int, preserved_count: int) -> None:
    """把 native 整对窗口事实写回同一份 tool_context，不创建第二套 compact 状态。

    native 的旧调用/结果已从 provider IR 整对移除；这里仅留下有界、可恢复的归档引用和
    最近结果摘要，让模型知道已经做过什么、从哪里核验。完整工具记录仍在当前 owner 的
    archive，副作用事实也仍由 archive/operation ledger 掌权。
    """
    entries = getattr(params, "tool_context", None)
    if not isinstance(entries, list) or omitted_count <= 0:
        return
    entries[:] = [
        _window_summary(
            omitted_count,
            max(0, int(preserved_count)),
            getattr(params, "archive_tool_calls", None) or [],
        ),
        *_non_window_entries(entries),
    ]


# LLM: A cross-process continuation cannot replay fabricated ToolCall/ToolResult pairs. Preserve
# one bounded, redacted projection as CompactionSummary input while canonical owner archives stay
# authoritative for exact effects and full outputs.
# 函数用途: 把已归档的本轮工具轨迹整理成原生模型可持续看到的有界续接摘要。
def native_carried_tool_handoff(
    tool_context: object,
    archive_tool_calls: object,
    *,
    max_chars: int = _DEFAULT_ACTIVE_TURN_HANDOFF_MAX_CHARS,
) -> str:
    records = [item for item in list(archive_tool_calls or []) if isinstance(item, dict)]
    entries = _non_window_entries(tool_context if isinstance(tool_context, list) else [])
    if not records or not entries:
        return ""
    try:
        configured_limit = int(max_chars)
    except (TypeError, ValueError):
        configured_limit = 0
    limit = (
        configured_limit
        if configured_limit > 0
        else _DEFAULT_ACTIVE_TURN_HANDOFF_MAX_CHARS
    )
    index_budget = min(max(2_000, limit // 2), 6_000)
    index_lines = _bounded_carried_tool_index(records, index_budget)
    header = [
        "[active-turn-tool-handoff]",
        "- schema_version: active-turn-tool-handoff.v1",
        f"- archived_tool_call_count: {len(records)}",
        "- authority: canonical owner tool archive, operation ledger, artifact refs, and current files",
        "- replay_policy: continue from these completed effects; do not replay writes or dispatches merely because raw provider pairs are absent",
        "- ordered_tool_index:",
        *index_lines,
        "- bounded_tool_context:",
    ]
    prefix = "\n".join(header)
    detail_budget = max(1_000, limit - len(prefix) - 2)
    details = _bounded_carried_tool_details(entries, records, detail_budget)
    rendered = f"{prefix}\n{details}" if details else prefix
    if len(rendered) <= limit:
        return rendered
    suffix = "\n[active-turn-tool-handoff truncated]"
    return rendered[: max(0, limit - len(suffix))].rstrip() + suffix


# LLM: One completed run may contribute exactly one immutable terminal fold. It is built only from
# canonical archive rows, never by another model call, so subsequent prompts retain an append-only
# stable prefix and provider cache hits are not invalidated by re-summarizing old turns.
# 函数用途: 将一个已结束回合的工具调用整理成有界、脱敏、确定性的跨回合续接记录。
def build_conversation_terminal_tool_fold(
    agent: object,
    archive_tool_calls: object,
) -> dict[str, object]:
    config = getattr(agent, "config", None)
    if not bool(getattr(config, "conversation_terminal_tool_fold_enabled", True)):
        return {}
    records = [item for item in list(archive_tool_calls or []) if isinstance(item, dict)]
    if not records:
        return {}
    try:
        configured_limit = int(
            getattr(
                config,
                "conversation_terminal_tool_fold_max_chars",
                _DEFAULT_TERMINAL_TOOL_FOLD_MAX_CHARS,
            )
        )
    except (TypeError, ValueError):
        configured_limit = _DEFAULT_TERMINAL_TOOL_FOLD_MAX_CHARS
    limit = min(
        _MAX_STORED_TERMINAL_TOOL_FOLD_CHARS,
        max(1_000, configured_limit),
    )
    succeeded = sum(1 for record in records if record.get("ok") is True)
    recent_lines = _recent_archive_handoff(records)
    header = [
        "[conversation-terminal-tool-fold]",
        f"- schema_version: {TERMINAL_TOOL_FOLD_SCHEMA}",
        f"- tool_call_count: {len(records)}",
        f"- successful_tool_call_count: {succeeded}",
        f"- non_successful_tool_call_count: {len(records) - succeeded}",
        "- authority: canonical owner tool archive, operation ledger, artifact refs, and current files",
        "- replay_policy: this turn has ended; do not replay writes, sends, or dispatches merely because raw provider pairs were folded",
        "- ordered_tool_index:",
    ]
    recent_section = ["- recent_tool_details:", *recent_lines]
    fixed_chars = len("\n".join([*header, *recent_section])) + 2
    index_lines = _bounded_carried_tool_index(
        records,
        max(500, limit - fixed_chars),
    )
    text = "\n".join([*header, *index_lines, *recent_section])
    if len(text) > limit:
        suffix = "\n[conversation-terminal-tool-fold truncated]"
        text = text[: max(0, limit - len(suffix))].rstrip() + suffix
    return {
        "schema": TERMINAL_TOOL_FOLD_SCHEMA,
        "tool_call_count": len(records),
        "successful_tool_call_count": succeeded,
        "non_successful_tool_call_count": len(records) - succeeded,
        "text": text,
    }


# LLM: Transcript metadata is untrusted on read. Accept only the current schema and bounded scalar
# counters/text; callers must never inject arbitrary nested metadata into the provider prompt.
# 函数用途: 从 assistant metadata 安全读取已落盘的工具终态折叠，旧行或坏数据返回空对象。
def conversation_terminal_tool_fold(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema") != TERMINAL_TOOL_FOLD_SCHEMA:
        return {}
    text = str(value.get("text") or "").strip()[:_MAX_STORED_TERMINAL_TOOL_FOLD_CHARS]
    if not text:
        return {}
    return {
        "schema": TERMINAL_TOOL_FOLD_SCHEMA,
        "tool_call_count": _nonnegative_int(value.get("tool_call_count")),
        "successful_tool_call_count": _nonnegative_int(
            value.get("successful_tool_call_count")
        ),
        "non_successful_tool_call_count": _nonnegative_int(
            value.get("non_successful_tool_call_count")
        ),
        "text": text,
    }


# LLM: The provider-facing fold is appended after the original assistant body without changing
# user-visible transcript content. This deterministic projection is shared by main and child turns.
# 函数用途: 把一条 assistant 正文与其工具终态折叠拼成下一轮模型看到的稳定历史内容。
def conversation_message_with_terminal_tool_fold(
    content: object,
    metadata: object,
) -> str:
    body = str(content or "")
    source = metadata if isinstance(metadata, dict) else {}
    fold = conversation_terminal_tool_fold(source.get(TERMINAL_TOOL_FOLD_METADATA_KEY))
    fold_text = str(fold.get("text") or "").strip()
    if not fold_text:
        return body
    return f"{body.rstrip()}\n\n{fold_text}" if body.strip() else fold_text


# LLM: Text-protocol windowing alone uses this character approximation; native protocol callers
# must use the full token estimator instead of converting this value back into an IR budget.
# 函数用途: 根据统一 Compact 配置计算文字工具记录的近似字符窗口，不负责原生工具消息计量。
def tool_context_window_max_chars(agent: object) -> int:
    """文本 tool_context 窗口的字符预算（按 compact 触发 token 数换算）。

    native IR 不再借用字符近似；它由 ``_tool_loop_service`` 使用同一 RuntimeCompactPolicy
    和完整 provider-visible token 估算直接收敛。两条协议共享配置，不共享失真的计量单位。
    """
    policy = runtime_compact_policy(agent, save=True)
    trigger_tokens = int(policy.trigger_tokens or 0)
    if trigger_tokens <= 0:
        return _DEFAULT_MAX_CHARS
    return max(_DEFAULT_MAX_CHARS, trigger_tokens * _CHARS_PER_TOKEN_WINDOW)


# 历史内部名保留为别名，避免改动现有调用点。
_tool_context_window_max_chars = tool_context_window_max_chars


# LLM: An authoritative main/child thread already owns durable Compact. Live tool-window
# reduction stays enabled, but it must never start the removed parallel archive continuation.
# 函数用途: 判断当前轮是否仍需旧持久压缩；有独立会话线程时明确关闭重复链路。
def _persistent_compact_enabled(agent: object, params: object) -> bool:
    if conversation_transcript_is_authoritative(
        getattr(params, "task_attributes", None)
    ):
        return False
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    return bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))


def _record_tool_context_window_overflow(params: object, result: ToolContextWindowResult) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    state["tool_context_window_overflow"] = {
        "omitted_count": result.omitted_count,
        "original_chars": result.original_chars,
        "preserved_count": result.preserved_count,
    }


def _positive_max_chars(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CHARS
    return parsed if parsed > 0 else _DEFAULT_MAX_CHARS


# LLM: Persisted numeric projections must fail closed to zero and never raise during prompt load.
# 函数用途: 将工具折叠里的计数规范成非负整数。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _context_chars(entries: list) -> int:
    return sum(len(str(item or "")) for item in entries)


def _non_window_entries(entries: list) -> list[str]:
    return [
        str(item or "")
        for item in entries
        if not str(item or "").startswith("[tool-context-window]")
    ]


def _recent_entries_within_budget(entries: list[str], max_chars: int) -> list[str]:
    budget = max(1_000, max_chars - 1_200)
    selected: list[str] = []
    used = 0
    for entry in reversed(entries):
        entry_size = len(entry)
        if selected and used + entry_size > budget:
            break
        selected.append(entry)
        used += entry_size
        if used >= budget:
            break
    return list(reversed(selected))


# LLM: The index is chronology metadata only. Nested arguments are already width/depth bounded and
# redacted by the durable tool index; include them only when they carry structural relationships.
# 函数用途: 生成按执行顺序排列的紧凑工具索引，并为 Todo/covers 等嵌套参数保留有限细节。
def _bounded_carried_tool_index(records: list[dict], max_chars: int) -> list[str]:
    lines: list[str] = []
    used = 0
    omitted = 0
    for index, record in enumerate(records, 1):
        tool = str(record.get("tool") or "unknown").strip() or "unknown"
        status = "ok" if record.get("ok") is True else "error"
        ref = _archive_ref(record)
        params = record.get("parameters")
        params = params if isinstance(params, dict) else {}
        redacted_params = redact_sensitive_value(params)
        params = redacted_params if isinstance(redacted_params, dict) else {}
        nested = any(isinstance(value, (dict, list, tuple)) for value in params.values())
        detail = ""
        if nested:
            detail = " params=" + json.dumps(
                params,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )[:800]
        elif params:
            detail = " parameter_keys=" + ",".join(str(key) for key in params)[:240]
        line = f"  - {index}: tool={tool} status={status}"
        if ref:
            line += f" ref={ref[:240]}"
        line += detail
        if lines and used + len(line) + 1 > max_chars:
            omitted = len(records) - index + 1
            break
        lines.append(line)
        used += len(line) + 1
    if omitted:
        lines.append(f"  - omitted_later_tool_index_entries: {omitted}")
    return lines


# LLM: Prefer the existing semantic compact item, then retain the newest mechanical records. This
# is a provider-facing projection only; omitted facts remain addressable through archive refs.
# 函数用途: 在固定字符预算内保留语义摘要和最近工具明细，超出部分用统一窗口说明替代。
def _bounded_carried_tool_details(
    entries: list[str],
    archive_tool_calls: list[dict],
    max_chars: int,
) -> str:
    rendered = "\n\n".join(entries)
    if len(rendered) <= max_chars:
        return rendered
    priority = [
        entry
        for entry in entries
        if entry.startswith("[compact-semantic-summary]")
        or entry.startswith("[compact-tool-operation-facts")
    ]
    priority_chars = sum(len(entry) + 2 for entry in priority)
    recent = _recent_entries_within_budget(
        [entry for entry in entries if entry not in priority],
        max(1_000, max_chars - priority_chars - 1_200),
    )
    selected = [entry for entry in entries if entry in priority or entry in recent]
    omitted = max(0, len(entries) - len(selected))
    parts = [_window_summary(omitted, len(selected), archive_tool_calls), *selected]
    return "\n\n".join(parts)[:max_chars]


# LLM: This marker is a bounded model-facing projection only; omitted/preserved counts and refs
# come from structured runtime state and must not be inferred from natural-language tool output.
# 函数用途: 生成窗口化说明，告诉模型删了多少旧记录、保留多少近期记录以及去哪里核验。
def _window_summary(omitted_count: int, preserved_count: int, archive_tool_calls: list) -> str:
    lines = [
        "[tool-context-window]",
        f"- omitted_old_tool_context_entries: {omitted_count}",
        f"- preserved_recent_tool_context_entries: {preserved_count}",
        "- policy: older tool calls are archived; do not replay old writes or reads. "
        "Use current files/artifact refs only when more detail is needed.",
    ]
    refs = _recent_archive_refs(archive_tool_calls)
    if refs:
        lines.append(f"- recent_archive_refs: {refs}")
    lines.extend(_recent_archive_handoff(archive_tool_calls))
    return "\n".join(lines)


def _recent_archive_refs(records: list) -> list[str]:
    refs: list[str] = []
    for record in reversed(records or []):
        ref = _archive_ref(record)
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= _RECENT_REF_LIMIT:
            break
    return list(reversed(refs))


def _archive_ref(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    for key in ("scoped_call_id", "artifact_ref", "id", "output_path"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


# LLM: Only bounded, policy-projected archive summaries may enter the model handoff; raw outputs,
# private fields, and archive payloads must remain owner-scoped and out of provider context.
# 函数用途: 从最近工具归档提取最多六条脱敏摘要，供窗口化后的模型确认当前进度。
def _recent_archive_handoff(records: list) -> list[str]:
    rows: list[str] = []
    for record in list(records or [])[-6:]:
        if not isinstance(record, dict):
            continue
        tool = str(record.get("tool") or "unknown").strip() or "unknown"
        status = "ok" if record.get("ok") is True else "error"
        ref = _archive_ref(record)
        summary = str(record.get("model_summary") or record.get("output_preview") or "").strip()
        if summary:
            summary = project_tool_output_body(
                tool=tool,
                output=summary[:240],
                trust=str(record.get("tool_output_trust") or "runtime"),
                redaction=str(record.get("tool_output_redaction") or "default"),
            ).replace("\n", " ")
        row = f"- recent_tool: tool={tool} status={status}"
        if ref:
            row += f" ref={ref[:240]}"
        if summary:
            row += f" summary={summary}"
        rows.append(row)
    return rows
