"""单步研判的隔离模式选择与受限分析执行 (WF-02)。

支持两种分析隔离模式：
1. stateless_skill: 纯无状态提示词/技能研判，无工具、无会话、无记忆写入；
2. lightweight_subagent: 挂载严格最小只读分析工具集、零环境记忆写入、有界轮次与超时的轻量子代理隔离。
"""
from __future__ import annotations

import json
from typing import Any

DEFAULT_ISOLATION_ALLOWED_TOOLS = (
    "read_file",
    "search_text",
    "list_files",
    "fetch_web",
    "query_data",
)

FORBIDDEN_ISOLATION_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "append_file",
    "delete_file",
    "bash",
    "sh",
    "exec",
    "run_command",
    "send_message",
    "create_session_task",
    "workflows",
    "workflows.run",
})


class WorkflowIsolationToolForbidden(PermissionError):
    """当分析隔离步骤请求了非只读或禁止的工具时抛出。"""
    error_code = "WORKFLOW_ISOLATION_TOOL_FORBIDDEN"

    def __init__(self, message: str = "WORKFLOW_ISOLATION_TOOL_FORBIDDEN", *, forbidden_tools: tuple[str, ...] = ()):
        super().__init__(message)
        self.error_code = "WORKFLOW_ISOLATION_TOOL_FORBIDDEN"
        self.forbidden_tools = forbidden_tools


# LLM: 选择只读结构化参数；无 isolation_executor 时保持向后兼容降级，有 isolation_executor 时激活受限 profile。
# 函数用途: 返回本次研判使用的隔离模式事实 (WF-02)。
def resolve_analysis_isolation(run_params: Any, *, step_config: dict[str, Any] | None = None, isolation_executor: Any = None) -> dict[str, Any]:
    config = step_config if isinstance(step_config, dict) else {}
    value = (run_params.get("analysis_isolation") if isinstance(run_params, dict)
             else getattr(run_params, "analysis_isolation", None))
    value = value if value is not None else config.get("analysis_isolation")
    requested = value if value in ("stateless_skill", "lightweight_subagent") else "stateless_skill"

    # 向后兼容：若未注入 isolation_executor，严格保持既有行为与 note
    if isolation_executor is None:
        return {
            "requested": requested,
            "mode": "stateless_skill",
            "note": "minimal_profile_entry_unavailable" if requested == "lightweight_subagent" else "",
        }

    # 注入了有效的 isolation_executor
    if requested == "stateless_skill":
        return {
            "requested": "stateless_skill",
            "mode": "stateless_skill",
            "note": "",
        }

    # requested == "lightweight_subagent"
    raw_tools = config.get("allowed_tools")
    if raw_tools is None and isinstance(run_params, dict):
        raw_tools = run_params.get("allowed_tools")
    if raw_tools is None:
        effective_tools = tuple(DEFAULT_ISOLATION_ALLOWED_TOOLS)
    elif isinstance(raw_tools, (list, tuple)):
        effective_tools = tuple(str(t).strip() for t in raw_tools if str(t).strip())
    else:
        effective_tools = ()

    # 严格最小 Profile 校验：拒绝一切破坏性、特权或写工具
    violating = [t for t in effective_tools if t in FORBIDDEN_ISOLATION_TOOLS]
    if violating:
        raise WorkflowIsolationToolForbidden(
            f"Requested isolation tools contain forbidden non-read-only tools: {violating}",
            forbidden_tools=tuple(violating),
        )

    max_turns = int(config.get("max_turns", 1))
    timeout_seconds = float(config.get("timeout_seconds", 30.0))

    profile = {
        "allowed_tools": effective_tools,
        "memory_write": False,
        "stateless_transcript": True,
        "max_turns": max_turns,
        "timeout_seconds": timeout_seconds,
    }
    return {
        "requested": "lightweight_subagent",
        "mode": "lightweight_subagent",
        "note": "",
        "profile": profile,
    }


# 函数用途: 执行受限轻量子代理分析隔离并校验结构化输出 (WF-02)。
def run_isolated_analysis(
    spec: dict[str, Any],
    context: Any,
    *,
    isolation_executor: Any,
    allowed_tools: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    if isolation_executor is None:
        raise ValueError("isolation_executor is required for run_isolated_analysis")

    config = spec.get("config") if isinstance(spec.get("config"), dict) else {}
    if allowed_tools is None:
        declared = config.get("allowed_tools")
        if declared is not None and isinstance(declared, (list, tuple)):
            allowed_tools = tuple(str(t).strip() for t in declared if str(t).strip())
        else:
            allowed_tools = tuple(DEFAULT_ISOLATION_ALLOWED_TOOLS)
    else:
        allowed_tools = tuple(str(t).strip() for t in allowed_tools if str(t).strip())

    violating = [t for t in allowed_tools if t in FORBIDDEN_ISOLATION_TOOLS]
    if violating:
        raise WorkflowIsolationToolForbidden(
            f"Requested isolation tools contain forbidden non-read-only tools: {violating}",
            forbidden_tools=tuple(violating),
        )

    profile = {
        "allowed_tools": allowed_tools,
        "memory_write": False,
        "stateless_transcript": True,
        "max_turns": int(config.get("max_turns", 1)),
        "timeout_seconds": float(config.get("timeout_seconds", 30.0)),
    }

    if hasattr(isolation_executor, "run_analysis") and callable(isolation_executor.run_analysis):
        result = isolation_executor.run_analysis(spec, context, profile=profile)
    elif hasattr(isolation_executor, "run") and callable(isolation_executor.run):
        result = isolation_executor.run(spec, context, profile=profile)
    elif callable(isolation_executor):
        result = isolation_executor(spec, context, profile=profile)
    else:
        raise ValueError("isolation_executor must be callable or provide run/run_analysis method")

    # 结构化输出校验
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception as exc:
            raise ValueError(f"Isolated analysis output must be a valid JSON object: {exc}") from exc

    if not isinstance(result, dict):
        raise ValueError(f"Isolated analysis output must be a dict, got {type(result).__name__}")

    return result
