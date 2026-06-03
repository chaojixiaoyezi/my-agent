
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
    write_runtime_fact_source,
)
from ...runtime_errors import runtime_error_report
from .owner_roots import runtime_archive_roots

_LOGGER = logging.getLogger(__name__)


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
                    runtime_injections=tuple(str(item) for item in getattr(params, "runtime_injections", []) or []),
                    run_id=str(getattr(params, "run_id", "") or ""),
                    task_id=str(getattr(params, "task_id", "") or ""),
                    source=str(getattr(params, "source", "") or "run"),
                    phase="started",
                    delivery_contract=_delivery_contract(params),
                )
            )
    except Exception as exc:
        _record_live_archive_error(params, exc, context="live_archive.runtime_fact.start")


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
                    request_id=str(getattr(params, "request_id", "") or ""),
                    run_id=str(getattr(params, "run_id", "") or ""),
                    task_id=str(getattr(params, "task_id", "") or ""),
                    tool_round=tool_round,
                    response_text=response_text,
                    tool_calls=tool_calls,
                    archive_level=_archive_level(agent),
                    preview_limits=_archive_preview_limits(agent),
                    summary_chars=_summary_chars(agent),
                )
            )
    except Exception as exc:
        _record_live_archive_error(params, exc, context="live_archive.assistant_tool_round")


def archive_tool_call_if_enabled(
    agent: object,
    params: object,
    archive_record: dict[str, object],
    *,
    tool_round: int,
    tool_index: int,
) -> None:
    if not _live_archive_enabled(agent, params):
        return
    try:
        result = None
        for root in runtime_archive_roots(agent):
            result = archive_live_tool_call(
                ArchiveLiveToolCallParams(
                    root=root,
                    session_id=_session_id(agent),
                    request_id=str(getattr(params, "request_id", "") or ""),
                    run_id=str(getattr(params, "run_id", "") or ""),
                    task_id=str(getattr(params, "task_id", "") or ""),
                    tool_round=tool_round,
                    tool_index=tool_index,
                    tool_record=dict(archive_record),
                    archive_level=_archive_level(agent),
                    preview_limits=_archive_preview_limits(agent),
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
        for root in runtime_archive_roots(agent):
            write_runtime_fact_source(
                RuntimeFactSourceRequest(
                    root=root,
                    request_id=request_id,
                    user_prompt=_root_user_prompt(params),
                    status="running",
                    next_actions=_runtime_next_actions(params),
                    archive_tool_calls=list(getattr(params, "archive_tool_calls", []) or []),
                    runtime_injections=tuple(str(item) for item in getattr(params, "runtime_injections", []) or []),
                    run_id=str(getattr(params, "run_id", "") or ""),
                    task_id=str(getattr(params, "task_id", "") or ""),
                    source="live_tool_loop",
                    phase="tool_loop",
                    tool_rounds=tool_round,
                    executed_tools=list(getattr(params, "executed_tools", []) or []),
                    latest_archive_refs=_latest_archive_refs(params),
                    artifact_refs=_artifact_refs(params),
                    delivery_contract=_delivery_contract(params),
                )
            )
    except Exception as exc:
        _record_live_archive_error(params, exc, context="live_archive.runtime_fact.progress")


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
    records = list(getattr(params, "archive_tool_calls", []) or [])
    return [
        str(record.get("raw_archive_path") or "")
        for record in records[-20:]
        if isinstance(record, dict) and str(record.get("raw_archive_path") or "")
    ]


def _artifact_refs(params: object) -> list[str]:
    records = list(getattr(params, "archive_tool_calls", []) or [])
    keys = ("artifact_ref", "artifact_path", "output_artifact_ref", "raw_archive_path")
    refs: list[str] = []
    for record in records[-20:]:
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


def _summary_chars(agent: object) -> int:
    return int(getattr(getattr(agent, "config", None), "memory_archive_summary_chars", 96) or 96)


def _archive_preview_limits(agent: object) -> dict[int, int]:
    config = getattr(agent, "config", None)
    return {
        0: int(getattr(config, "memory_archive_preview_level_0_chars", 2048) or 0),
        1: int(getattr(config, "memory_archive_preview_level_1_chars", 1024) or 0),
        2: int(getattr(config, "memory_archive_preview_level_2_chars", 512) or 0),
        3: int(getattr(config, "memory_archive_preview_level_3_chars", 160) or 0),
    }


__all__ = [
    "archive_assistant_tool_round_if_enabled",
    "archive_tool_call_if_enabled",
    "write_runtime_fact_start_if_enabled",
    "update_runtime_fact_progress_if_enabled",
]
