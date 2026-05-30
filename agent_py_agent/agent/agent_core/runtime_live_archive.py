# LLM: Runtime live archive bridge keeps raw archive writes optional and non-blocking for the tool loop.
# 模块用途: 把工具循环中的模型可见文字和工具结果增量写入 raw archive，失败只记录在本轮工具记录里。

from __future__ import annotations

from typing import Any

from ..memory_archive.runtime.live_archiver import (
    ArchiveAssistantToolRoundParams,
    ArchiveLiveToolCallParams,
    archive_assistant_tool_round,
    archive_live_tool_call,
)
from ..memory_archive.runtime_fact_source import RuntimeFactSourceRequest, write_runtime_fact_source


# LLM: write_runtime_fact_start_if_enabled creates the live task card before any model/tool loop grows.
# 函数用途: run 开始就写 runtime_fact，后续工具循环持续更新；失败不阻断主流程。
def write_runtime_fact_start_if_enabled(agent: object, params: object) -> None:
    if not _live_archive_enabled(agent, params):
        return
    request_id = str(getattr(params, "request_id", "") or "")
    if not request_id:
        return
    try:
        write_runtime_fact_source(
            RuntimeFactSourceRequest(
                root=agent.root,
                request_id=request_id,
                user_prompt=_root_user_prompt(params),
                status="running",
                runtime_injections=tuple(str(item) for item in getattr(params, "runtime_injections", []) or []),
                run_id=str(getattr(params, "run_id", "") or ""),
                task_id=str(getattr(params, "task_id", "") or ""),
                source=str(getattr(params, "source", "") or "run"),
                phase="started",
            )
        )
    except Exception:
        return


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


# LLM: update_runtime_fact_progress_if_enabled folds the old live checkpoint role into runtime_fact.
# 函数用途: 工具循环运行中更新 runtime_facts/<request_id>/task.json；失败不影响模型继续工作。
def update_runtime_fact_progress_if_enabled(agent: object, params: object, *, tool_round: int) -> None:
    if not _live_archive_enabled(agent, params):
        return
    request_id = str(getattr(params, "request_id", "") or "")
    if not request_id:
        return
    try:
        write_runtime_fact_source(
            RuntimeFactSourceRequest(
                root=agent.root,
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
            )
        )
    except Exception:
        return


# LLM: _runtime_next_actions keeps the runtime fact whiteboard focused on the latest model-visible hint.
# 函数用途: 从最近工具上下文提取一条下一步提示，不把完整上下文复制进 runtime_fact。
def _runtime_next_actions(params: object) -> list[str]:
    context = [str(item) for item in list(getattr(params, "tool_context", []) or [])[-2:] if str(item).strip()]
    return context[-1:] if context else []


# LLM: _root_user_prompt keeps compact continuation prompts from replacing the original task goal.
# 函数用途: 优先使用 root_user_prompt；没有时退回本轮 user_prompt，保证 runtime_fact 目标不被续接提示覆盖。
def _root_user_prompt(params: object) -> str:
    return str(getattr(params, "root_user_prompt", "") or getattr(params, "user_prompt", "") or "")


# LLM: _latest_archive_refs exposes only recent archive paths for the runtime fact whiteboard.
# 函数用途: 从工具归档记录中取最近 raw_archive_path，不读取归档正文。
def _latest_archive_refs(params: object) -> list[str]:
    records = list(getattr(params, "archive_tool_calls", []) or [])
    return [
        str(record.get("raw_archive_path") or "")
        for record in records[-20:]
        if isinstance(record, dict) and str(record.get("raw_archive_path") or "")
    ]


# LLM: _artifact_refs keeps output refs discoverable without creating a second artifact registry.
# 函数用途: 从工具记录中提取产物和归档路径，写入 runtime_fact 的轻量进度白板。
def _artifact_refs(params: object) -> list[str]:
    records = list(getattr(params, "archive_tool_calls", []) or [])
    keys = ("artifact_ref", "artifact_path", "output_artifact_ref", "raw_archive_path")
    refs: list[str] = []
    for record in records[-20:]:
        if not isinstance(record, dict):
            continue
        refs.extend(str(record.get(key) or "") for key in keys if str(record.get(key) or ""))
    return refs


# LLM: _live_archive_enabled respects the same save/auto-save boundary for raw archive and runtime_fact.
# 函数用途: 判断当前 run 是否允许写运行中归档；save=False 时不偷偷落盘。
def _live_archive_enabled(agent: object, params: object) -> bool:
    config = getattr(agent, "config", None)
    save = getattr(params, "save", None)
    auto_save = bool(getattr(config, "auto_save_memory", True))
    return auto_save if save is None else bool(save)


# LLM: _session_id resolves the current archive session id without requiring callers to pass it around.
# 函数用途: 从 agent/session/config 中得到稳定会话标识，兜底为 myagent。
def _session_id(agent: object) -> str:
    config = getattr(agent, "config", None)
    return str(getattr(agent, "session_id", "") or getattr(config, "agent_name", "") or "myagent")


# LLM: _archive_level reads the configured raw archive detail level.
# 函数用途: 统一读取 memory_archive_level，非法或缺失时用 schema 默认值。
def _archive_level(agent: object) -> int:
    value = getattr(getattr(agent, "config", None), "memory_archive_level", 3)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 3
    return parsed if 0 <= parsed <= 3 else 3


# LLM: _summary_chars reads the configured preview summary length for live archive events.
# 函数用途: 统一读取 memory_archive_summary_chars，避免 live archive 写死第二份默认值。
def _summary_chars(agent: object) -> int:
    return int(getattr(getattr(agent, "config", None), "memory_archive_summary_chars", 96) or 96)


# LLM: _archive_preview_limits reads per-level preview budgets from AgentConfig.
# 函数用途: 组装 raw archive 预览长度表，让 live 写入和收尾归档使用同一配置。
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
