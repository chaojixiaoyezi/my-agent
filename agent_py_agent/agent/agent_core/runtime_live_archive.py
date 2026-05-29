# LLM: Runtime live archive bridge keeps raw archive writes optional and non-blocking for the tool loop.
# 模块用途: 把工具循环中的模型可见文字和工具结果增量写入 raw archive，失败只记录在本轮工具记录里。

from __future__ import annotations

import time
from typing import Any

from ..memory_archive.runtime.live_archiver import (
    ArchiveAssistantToolRoundParams,
    ArchiveLiveToolCallParams,
    ArchiveRunCheckpointParams,
    archive_assistant_tool_round,
    archive_live_tool_call,
    archive_run_checkpoint,
)


# LLM: archive_assistant_tool_round_if_enabled records visible assistant tool-round prose without blocking execution.
# 函数用途: 工具执行前把模型这一轮可见说明写进 raw archive；关闭保存或配置关闭时不写。
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
        archive_assistant_tool_round(
            ArchiveAssistantToolRoundParams(
                root=agent.root,
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
    except Exception:
        return


# LLM: archive_tool_call_if_enabled records each completed tool result and annotates the archive record with refs.
# 函数用途: 工具返回后立即写 raw archive，并把 raw_archive_event_id/path 附到 archive_tool_calls 记录；失败不阻断任务。
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
        result = archive_live_tool_call(
            ArchiveLiveToolCallParams(
                root=agent.root,
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
        archive_record["raw_archive_event_id"] = result.event_ids[0] if result.event_ids else ""
        archive_record["raw_archive_path"] = str(result.write_paths[0]) if result.write_paths else ""
    except Exception as exc:
        archive_record["raw_archive_error"] = str(exc)


# LLM: archive_checkpoint_if_due writes bounded continuation notes by round/time budget.
# 函数用途: 周期性把最近运行摘要落到 raw archive；只用于恢复提示，失败不影响工具循环。
def archive_checkpoint_if_due(agent: object, params: object, *, tool_round: int) -> None:
    if not _live_archive_enabled(agent, params):
        return
    if not _checkpoint_due(agent, params, tool_round=tool_round):
        return
    try:
        archive_run_checkpoint(
            ArchiveRunCheckpointParams(
                root=agent.root,
                session_id=_session_id(agent),
                request_id=str(getattr(params, "request_id", "") or ""),
                run_id=str(getattr(params, "run_id", "") or ""),
                task_id=str(getattr(params, "task_id", "") or ""),
                tool_round=tool_round,
                user_prompt=str(getattr(params, "user_prompt", "") or ""),
                executed_tools=list(getattr(params, "executed_tools", []) or []),
                recent_context=list(getattr(params, "tool_context", []) or [])[-3:],
                archive_level=_archive_level(agent),
                preview_limits=_archive_preview_limits(agent),
                summary_chars=_summary_chars(agent),
            )
        )
        state = _live_archive_state(params)
        state["last_checkpoint_round"] = int(tool_round)
        state["last_checkpoint_time"] = time.time()
    except Exception:
        return


def _checkpoint_due(agent: object, params: object, *, tool_round: int) -> bool:
    state = _live_archive_state(params)
    config = getattr(agent, "config", None)
    round_interval = int(getattr(config, "memory_live_archive_checkpoint_rounds", 0) or 0)
    second_interval = int(getattr(config, "memory_live_archive_checkpoint_seconds", 0) or 0)
    last_round = int(state.get("last_checkpoint_round", 0) or 0)
    if round_interval > 0 and tool_round > 0 and tool_round % round_interval == 0 and last_round != tool_round:
        return True
    last_time = float(state.get("last_checkpoint_time", 0) or 0)
    return second_interval > 0 and last_time > 0 and time.time() - last_time >= second_interval


def _live_archive_state(params: object) -> dict[str, object]:
    state = getattr(params, "live_archive_state", None)
    return state if isinstance(state, dict) else {}


def _live_archive_enabled(agent: object, params: object) -> bool:
    config = getattr(agent, "config", None)
    if not bool(getattr(config, "memory_live_archive_enabled", True)):
        return False
    save = getattr(params, "save", None)
    auto_save = bool(getattr(config, "auto_save_memory", True))
    return auto_save if save is None else bool(save)


def _session_id(agent: object) -> str:
    config = getattr(agent, "config", None)
    return str(getattr(agent, "session_id", "") or getattr(config, "agent_name", "") or "myagent")


def _archive_level(agent: object) -> int:
    return int(getattr(getattr(agent, "config", None), "memory_archive_level", 3) or 3)


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
    "archive_checkpoint_if_due",
    "archive_tool_call_if_enabled",
]
