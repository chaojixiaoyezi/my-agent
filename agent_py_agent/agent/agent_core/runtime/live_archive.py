# LLM: 原实时归档遵守聊天保存开关；工具最小审计单独保留结构事实，不写正文或改变operation权威。
# 模块用途: 保存运行实时经历或最小工具审计，后台save=False不再丢失安全诊断。

from __future__ import annotations

import logging
from typing import Any

from ...memory_archive.runtime.live_archiver import (
    ArchiveAssistantToolRoundParams,
    ArchiveLiveToolCallParams,
    archive_assistant_tool_round,
    archive_live_tool_call,
)
from ...memory_archive.runtime_fact_source import (
    RuntimeFactSourceRequest,
    RuntimeFactTerminalRequest,
    update_runtime_fact_terminal,
    write_runtime_fact_source,
)
from ...prompting_parts.runtime_injection import runtime_injection_texts
from ...runtime_errors import runtime_error_report
from .owner_roots import runtime_archive_roots

_LOGGER = logging.getLogger(__name__)


def _runtime_fact_recent_tests_limit(agent: object) -> int:
    default_limit = 20
    raw = getattr(getattr(agent, "config", None), "memory_runtime_fact_recent_tests", default_limit)
    if isinstance(raw, bool):
        return default_limit
    try:
        parsed = int(default_limit if raw is None else raw)
    except (TypeError, ValueError):
        return default_limit
    return max(0, parsed)


def _tail_sequence(values: object, limit: int) -> list[Any]:
    if limit <= 0 or not values:
        return []
    if isinstance(values, (list, tuple)):
        return list(values[-limit:])
    return list(values)[-limit:]


def write_runtime_fact_start_if_enabled(agent: object, params: object) -> None:
    if not _live_archive_enabled(agent, params):
        return
    request_id = str(getattr(params, "request_id", "") or "")
    if not request_id:
        return
    try:
        for root in runtime_archive_roots(agent):
            write_runtime_fact_source(
                RuntimeFactSourceRequest(
                    root=root,
                    request_id=request_id,
                    user_prompt=_root_user_prompt(params),
                    status="running",
                    runtime_injections=runtime_injection_texts(getattr(params, "runtime_injections", None)),
                    run_id=str(getattr(params, "run_id", "") or ""),
                    task_id=str(getattr(params, "task_id", "") or ""),
                    source=str(getattr(params, "source", "") or "run"),
                    phase="started",
                    delivery_contract=_delivery_contract(params),
                )
            )
    except Exception as exc:
        _record_live_archive_error(params, exc, context="live_archive.runtime_fact.start")


# LLM: 只提取宿主运行属性中的线程编号；缺失保持空，不把session或工具参数变成来源权威。
# 函数用途: 给实时归档传递可供后续精确核对的线程身份。
def archive_thread_id(params: object) -> str:
    attrs = getattr(params, "task_attributes", None)
    return str(attrs.get("conversation_thread_id") or "") if isinstance(attrs, dict) else ""


# LLM: 实时助手归档只使用宿主thread/request身份；保持save开关及归档去重，不从模型参数推导线程。
# 函数用途: 保存带真实宿主会话身份的助手工具轮。
def archive_assistant_tool_round_if_enabled(
    agent: object,
    params: object,
    *,
    tool_round: int,
    response_text: str,
    tool_calls: list[dict[str, Any]],
) -> None:
    if not _live_archive_enabled(agent, params):
        return
    try:
        for root in runtime_archive_roots(agent):
            archive_assistant_tool_round(
                ArchiveAssistantToolRoundParams(
                    root=root,
                    session_id=_session_id(agent),
                    thread_id=archive_thread_id(params),
                    request_id=str(getattr(params, "request_id", "") or ""),
                    run_id=str(getattr(params, "run_id", "") or ""),
                    task_id=str(getattr(params, "task_id", "") or ""),
                    tool_round=tool_round,
                    response_text=response_text,
                    tool_calls=tool_calls,
                    archive_level=_archive_level(agent),
                    preview_limits=dict(ARCHIVE_PREVIEW_LIMITS),
                    summary_chars=ARCHIVE_SUMMARY_CHARS,
                )
            )
    except Exception as exc:
        _record_live_archive_error(params, exc, context="live_archive.assistant_tool_round")


# LLM: 完整档案仍受save控制，关闭时只调用同owner最小审计投影；修改需同步后台与脱敏回归。
# 函数用途: 归档真实工具及宿主thread身份；save=False仍保留原最小审计，不复制参数/输出正文。
def archive_tool_call_if_enabled(
    agent: object,
    params: object,
    archive_record: dict[str, object],
    *,
    tool_round: int,
    tool_index: int,
) -> None:
    if not _live_archive_enabled(agent, params):
        from .minimal_tool_audit import archive_minimal_tool_audit
        archive_minimal_tool_audit(agent, params, archive_record)
        return
    try:
        result = None
        for root in runtime_archive_roots(agent):
            result = archive_live_tool_call(
                ArchiveLiveToolCallParams(
                    root=root,
                    session_id=_session_id(agent),
                    thread_id=archive_thread_id(params),
                    request_id=str(getattr(params, "request_id", "") or ""),
                    run_id=str(getattr(params, "run_id", "") or ""),
                    task_id=str(getattr(params, "task_id", "") or ""),
                    tool_round=tool_round,
                    tool_index=tool_index,
                    tool_record=dict(archive_record),
                    archive_level=_archive_level(agent),
                    preview_limits=dict(ARCHIVE_PREVIEW_LIMITS),
                )
            )
        if result is not None:
            archive_record["raw_archive_event_id"] = result.event_ids[0] if result.event_ids else ""
            archive_record["raw_archive_path"] = str(result.write_paths[0]) if result.write_paths else ""
    except Exception as exc:
        archive_record["raw_archive_error"] = str(exc)
        _record_live_archive_error(params, exc, context="live_archive.tool_call")


def update_runtime_fact_progress_if_enabled(agent: object, params: object, *, tool_round: int) -> None:
    if not _live_archive_enabled(agent, params):
        return
    request_id = str(getattr(params, "request_id", "") or "")
    if not request_id:
        return
    try:
        recent_tests_limit = _runtime_fact_recent_tests_limit(agent)
        raw_calls = getattr(params, "archive_tool_calls", None) or ()
        if not isinstance(raw_calls, (list, tuple)):
            raw_calls = list(raw_calls)
        total_calls = len(raw_calls)
        raw_executed = getattr(params, "executed_tools", None) or ()
        if not isinstance(raw_executed, (list, tuple)):
            raw_executed = list(raw_executed)
        total_executed = len(raw_executed)

        state = getattr(params, "live_archive_state", None)
        state_dict = state if isinstance(state, dict) else None
        if state_dict is not None:
            if state_dict.get("runtime_fact_request_id") != request_id:
                state_dict["runtime_fact_request_id"] = request_id
                state_dict["runtime_fact_call_cursor"] = 0
                state_dict["runtime_fact_executed_cursor"] = 0
                state_dict["runtime_fact_run_intent_by_root"] = {}
            prev_cursor = int(state_dict.get("runtime_fact_call_cursor", 0) or 0)
            if prev_cursor < 0 or prev_cursor > total_calls:
                prev_cursor = 0
            executed_cursor = int(state_dict.get("runtime_fact_executed_cursor", 0) or 0)
            if executed_cursor < 0 or executed_cursor > total_executed:
                executed_cursor = 0
            intent_cache = state_dict.setdefault("runtime_fact_run_intent_by_root", {})
            if not isinstance(intent_cache, dict):
                intent_cache = {}
                state_dict["runtime_fact_run_intent_by_root"] = intent_cache
            incremental = True
        else:
            prev_cursor = 0
            executed_cursor = max(0, total_executed - 20)
            intent_cache = {}
            incremental = False

        if recent_tests_limit == 0:
            delta_calls: list[Any] = []
        else:
            scan_start = max(prev_cursor, total_calls - recent_tests_limit)
            delta_calls = list(raw_calls[scan_start:total_calls])
        delta_executed = [str(item) for item in raw_executed[executed_cursor:total_executed]]

        user_prompt = _root_user_prompt(params)
        next_actions = _runtime_next_actions(params)
        runtime_injections = runtime_injection_texts(getattr(params, "runtime_injections", None))
        run_id = str(getattr(params, "run_id", "") or "")
        task_id = str(getattr(params, "task_id", "") or "")
        latest_archive_refs = _latest_archive_refs(params)
        artifact_refs = _artifact_refs(params)
        delivery_contract = _delivery_contract(params)

        roots = runtime_archive_roots(agent)
        for root in roots:
            root_key = str(root)
            cached_entry = intent_cache.get(root_key) if isinstance(intent_cache, dict) else None
            cached_intent = (
                cached_entry.get("run_intent")
                if isinstance(cached_entry, dict)
                and cached_entry.get("user_prompt") == user_prompt
                and cached_entry.get("delivery_present") == (delivery_contract is not None)
                and cached_entry.get("injections_count") == len(runtime_injections)
                and isinstance(cached_entry.get("run_intent"), dict)
                else None
            )
            computed_intent = None
            if isinstance(intent_cache, dict) and cached_intent is None:
                from ...memory_archive import runtime_fact_source
                from ...memory_archive.runtime_workspace_outputs import (
                    run_intent_has_desired_outputs,
                    runtime_desired_outputs,
                )

                desired_outputs = runtime_desired_outputs(delivery_contract, runtime_injections)
                computed_intent = runtime_fact_source.build_run_intent(
                    user_prompt=user_prompt,
                    delivery_contract=delivery_contract,
                    workspace_root=root,
                )
                if desired_outputs and not run_intent_has_desired_outputs(computed_intent):
                    computed_intent = runtime_fact_source.run_intent_payload(
                        reference_roots=[],
                        desired_outputs=[item["target_path"] for item in desired_outputs if item.get("target_path")],
                    )
                cached_intent = computed_intent
            write_runtime_fact_source(
                RuntimeFactSourceRequest(
                    root=root,
                    request_id=request_id,
                    user_prompt=user_prompt,
                    status="running",
                    next_actions=next_actions,
                    archive_tool_calls=delta_calls,
                    runtime_injections=runtime_injections,
                    run_id=run_id,
                    task_id=task_id,
                    source="live_tool_loop",
                    phase="tool_loop",
                    tool_rounds=tool_round,
                    executed_tools=delta_executed,
                    latest_archive_refs=latest_archive_refs,
                    artifact_refs=artifact_refs,
                    delivery_contract=delivery_contract,
                    incremental=incremental,
                    recent_tests_limit=recent_tests_limit,
                    cached_run_intent=cached_intent,
                )
            )
            if isinstance(intent_cache, dict) and computed_intent is not None:
                intent_cache[root_key] = {
                    "user_prompt": user_prompt,
                    "delivery_present": delivery_contract is not None,
                    "injections_count": len(runtime_injections),
                    "run_intent": computed_intent,
                }
        # 所有目标根均写入成功后才推进已提交游标，避免多根部分失败时漏掉未写入根的增量事件。
        if state_dict is not None:
            state_dict["runtime_fact_call_cursor"] = total_calls
            state_dict["runtime_fact_executed_cursor"] = total_executed
    except Exception as exc:
        _record_live_archive_error(params, exc, context="live_archive.runtime_fact.progress")


def update_runtime_fact_terminal_if_enabled(
    agent: object,
    params: object,
    exc: BaseException,
) -> None:
    """Persist the same terminal outcome that the run lifecycle will expose."""
    if not _live_archive_enabled(agent, params):
        return
    request_id = str(getattr(params, "request_id", "") or "")
    if not request_id:
        return
    status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
    error = runtime_error_report(exc, context="agent.run")
    try:
        for root in runtime_archive_roots(agent):
            update_runtime_fact_terminal(
                RuntimeFactTerminalRequest(
                    root=root,
                    request_id=request_id,
                    status=status,
                    error=error,
                )
            )
    except Exception as write_exc:
        _record_live_archive_error(
            params,
            write_exc,
            context="live_archive.runtime_fact.terminal",
        )


def _record_live_archive_error(params: object, exc: Exception, *, context: str) -> None:
    report = runtime_error_report(exc, context=context)
    state = getattr(params, "live_archive_state", None)
    if isinstance(state, dict):
        errors = state.setdefault("live_archive_errors", [])
        if isinstance(errors, list):
            errors.append(report)
    _LOGGER.warning("live archive write failed: %s", report)


def _runtime_next_actions(params: object) -> list[str]:
    context = [str(item) for item in list(getattr(params, "tool_context", []) or [])[-2:] if str(item).strip()]
    return context[-1:] if context else []


def _root_user_prompt(params: object) -> str:
    return str(getattr(params, "root_user_prompt", "") or getattr(params, "user_prompt", "") or "")


def _latest_archive_refs(params: object) -> list[str]:
    records = _tail_sequence(getattr(params, "archive_tool_calls", None), 20)
    return [
        str(record.get("raw_archive_path") or "")
        for record in records
        if isinstance(record, dict) and str(record.get("raw_archive_path") or "")
    ]


def _artifact_refs(params: object) -> list[str]:
    records = _tail_sequence(getattr(params, "archive_tool_calls", None), 20)
    keys = ("artifact_ref", "artifact_path", "output_artifact_ref", "raw_archive_path")
    refs: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        refs.extend(str(record.get(key) or "") for key in keys if str(record.get(key) or ""))
    return refs


def _delivery_contract(params: object) -> dict[str, object] | None:
    value = getattr(params, "delivery_contract", None)
    return value if isinstance(value, dict) else None


def _live_archive_enabled(agent: object, params: object) -> bool:
    config = getattr(agent, "config", None)
    save = getattr(params, "save", None)
    auto_save = bool(getattr(config, "auto_save_memory", True))
    return auto_save if save is None else bool(save)


def _session_id(agent: object) -> str:
    config = getattr(agent, "config", None)
    return str(getattr(agent, "session_id", "") or getattr(config, "agent_name", "") or "myagent")


def _archive_level(agent: object) -> int:
    value = getattr(getattr(agent, "config", None), "memory_archive_level", 3)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 3
    return parsed if 0 <= parsed <= 3 else 3


# LLM: 归档摘要长度与四个预览档位只是内部显示预算，2026-09-28 参数减量 C 组后不再作为用户参数；活动归档与回合
#   收尾共用这一份定义，改值要一起发版，不要再退回成两个模块各写一份默认值。
# 函数用途: 给出归档摘要字符数与按归档等级的预览字符预算。
ARCHIVE_SUMMARY_CHARS = 96
ARCHIVE_PREVIEW_LIMITS: dict[int, int] = {0: 2048, 1: 1024, 2: 512, 3: 160}


__all__ = [
    "archive_assistant_tool_round_if_enabled",
    "archive_tool_call_if_enabled",
    "write_runtime_fact_start_if_enabled",
    "update_runtime_fact_progress_if_enabled",
    "update_runtime_fact_terminal_if_enabled",
]
