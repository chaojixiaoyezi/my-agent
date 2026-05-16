# LLM: Tool context windowing keeps long-running agents from replaying every old tool record.
# 模块用途: 在构建下一轮 prompt 前压缩 live tool_context，只保留最近上下文和可恢复归档引用。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

_DEFAULT_MAX_CHARS = 48_000
_RECENT_REF_LIMIT = 8


# LLM: ToolContextWindowRequest bundles mutable live context and immutable archive refs for prompt windowing.
# 类用途: 保存待压缩的 tool_context、工具归档记录和字符预算；函数会原地改写 tool_context 列表。
@dataclass(frozen=True)
class ToolContextWindowRequest:
    __test__: ClassVar[bool] = False

    tool_context: list
    archive_tool_calls: list
    max_chars: int = _DEFAULT_MAX_CHARS


# LLM: window_tool_context_for_live_prompt trims old tool transcript entries but keeps archive refs recoverable.
# 函数用途: 长任务工具上下文过大时，原地替换为窗口摘要加最近记录，避免 prompt 随工具轮数无限膨胀。
def window_tool_context_for_live_prompt(request: ToolContextWindowRequest) -> None:
    entries = request.tool_context
    if not isinstance(entries, list) or not entries:
        return
    max_chars = _positive_max_chars(request.max_chars)
    if _context_chars(entries) <= max_chars:
        return
    active_entries = _non_window_entries(entries)
    recent_entries = _recent_entries_within_budget(active_entries, max_chars)
    omitted_count = max(0, len(active_entries) - len(recent_entries))
    if omitted_count <= 0:
        return
    entries[:] = [
        _window_summary(omitted_count, recent_entries, request.archive_tool_calls),
        *recent_entries,
    ]


# LLM: window_tool_context_params is the thin ToolLoopService adapter for windowing mutable params.
# 函数用途: 从 ToolLoopExecuteParams 取 live tool_context 和归档记录，避免核心循环文件展开构造细节。
def window_tool_context_params(params: object) -> None:
    window_tool_context_for_live_prompt(
        ToolContextWindowRequest(
            tool_context=getattr(params, "tool_context", []),
            archive_tool_calls=getattr(params, "archive_tool_calls", []),
        )
    )


# LLM: _positive_max_chars keeps pathological caller values from deleting all live context.
# 函数用途: 归一化窗口预算；无效值回退到内部默认值，不暴露额外用户配置。
def _positive_max_chars(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CHARS
    return parsed if parsed > 0 else _DEFAULT_MAX_CHARS


# LLM: _context_chars measures list entries after string coercion, matching prompt builder behavior.
# 函数用途: 估算 tool_context 进入 prompt 的字符量，作为是否压缩的判断依据。
def _context_chars(entries: list) -> int:
    return sum(len(str(item or "")) for item in entries)


# LLM: _non_window_entries removes prior synthetic summaries before recomputing a fresh window.
# 函数用途: 避免多次压缩时摘要套摘要，保证窗口内容始终来自真实最近工具记录。
def _non_window_entries(entries: list) -> list[str]:
    return [
        str(item or "")
        for item in entries
        if not str(item or "").startswith("[tool-context-window]")
    ]


# LLM: _recent_entries_within_budget keeps tail entries because they are most useful for the next model turn.
# 函数用途: 从后往前收集最近工具上下文，预算不足时至少保留最后一条，避免模型完全失去上轮结果。
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


# LLM: _window_summary tells the model where old facts went without copying old bodies back into context.
# 函数用途: 生成给模型看的压缩说明，包含省略条数、保留条数和最近归档引用。
def _window_summary(omitted_count: int, recent_entries: list[str], archive_tool_calls: list) -> str:
    lines = [
        "[tool-context-window]",
        f"- omitted_old_tool_context_entries: {omitted_count}",
        f"- preserved_recent_tool_context_entries: {len(recent_entries)}",
        "- policy: older tool calls are archived; do not replay old writes or reads. "
        "Use current files/artifact refs only when more detail is needed.",
    ]
    refs = _recent_archive_refs(archive_tool_calls)
    if refs:
        lines.append(f"- recent_archive_refs: {refs}")
    return "\n".join(lines)


# LLM: _recent_archive_refs exposes short recovery handles instead of huge historical tool outputs.
# 函数用途: 从 archive_tool_calls 取最近少量 call id/artifact ref，帮助模型需要时精确 read_artifact。
def _recent_archive_refs(records: list) -> list[str]:
    refs: list[str] = []
    for record in reversed(records or []):
        ref = _archive_ref(record)
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= _RECENT_REF_LIMIT:
            break
    return list(reversed(refs))


# LLM: _archive_ref prefers scoped call ids, then artifact refs, so refs remain short and task-local.
# 函数用途: 从单条工具归档记录中提取可恢复引用；坏形态返回空字符串。
def _archive_ref(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    for key in ("scoped_call_id", "artifact_ref", "id", "output_path"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""
