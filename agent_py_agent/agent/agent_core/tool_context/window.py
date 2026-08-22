
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...conversation.authority import conversation_transcript_is_authoritative
from ...tooling.output_projection import project_tool_output_body
from ..runtime.context_compactor import runtime_compact_policy

_DEFAULT_MAX_CHARS = 48_000
_CHARS_PER_TOKEN_WINDOW = 3
_RECENT_REF_LIMIT = 8


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
            row += f" ref={ref}"
        if summary:
            row += f" summary={summary}"
        rows.append(row)
    return rows
