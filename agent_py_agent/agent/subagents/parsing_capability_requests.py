# LLM: Subagent parsing helper; recover parent-routable capability requests from compact structured fields only.
# 模块用途: 当模型写出“等待工具/能力”状态但漏填 capability_requests 时，生成保守的父级可路由申请。

from __future__ import annotations

"""Capability-request recovery for runner structured output."""

from .capability_status import is_pending_capability_status
from .parsing_values import _dict_list


# LLM: capability_requests_from_payload recovers routeable requests when models write only pending status.
# 函数用途: 优先使用显式 capability_requests；缺失时从 pending capability 状态和 pending_steps 生成父级可处理请求。
def capability_requests_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    explicit = _dict_list(payload.get("capability_requests", []))
    if explicit:
        return explicit
    status = str(payload.get("status", "") or "")
    if not is_pending_capability_status(status):
        return []
    text = _pending_capability_text(payload)
    request = _pending_capability_request(status, text)
    return [request] if request else []


# LLM: _pending_capability_text gathers only compact structured fields for request recovery.
# 函数用途: 拼接 status、summary、blocked_reason 和 pending_steps 文本，避免读取模型自由正文。
def _pending_capability_text(payload: dict[str, object]) -> str:
    parts = [
        str(payload.get("status", "") or ""),
        str(payload.get("summary", "") or ""),
        str(payload.get("blocked_reason", "") or ""),
    ]
    parts.extend(_pending_step_texts(payload.get("pending_steps", [])))
    return "\n".join(part for part in parts if part.strip())


# LLM: _pending_step_texts normalizes model pending step objects into searchable text.
# 函数用途: 从 pending_steps 中提取 action、step、status 等短字段，用于推断工具和命令名。
def _pending_step_texts(value: object) -> list[str]:
    return [
        " ".join(str(item.get(key, "") or "") for key in ("action", "step", "name", "status"))
        for item in _dict_list(value)
    ]


# LLM: _pending_capability_request builds a conservative parent-routable request, not an automatic grant.
# 函数用途: 从待能力状态构造最小请求；父级仍需按工具/路径/风险策略决定是否授权。
def _pending_capability_request(status: str, text: str) -> dict[str, object] | None:
    commands = _requested_commands_from_pending(text)
    tools = _requested_tools_from_pending(text, commands)
    if not commands and not tools:
        return None
    needed = "controlled_exec" if "controlled_exec" in tools else "shell"
    return {
        "problem": f"模型状态为 {status}，但未填写 capability_requests；系统从 pending_steps 兜底生成。",
        "needed_capability": needed,
        "capability_type": "shell" if commands or "controlled_exec" in tools else "generic",
        "expected_output": "父级路由可用能力后重新运行当前子任务。",
        "requested_tools": tools,
        "requested_commands": commands,
        "path_scope": [],
        "output_budget": {"stdout_bytes": 65536, "stderr_bytes": 32768},
        "risk_level": "medium" if "rm" in commands else "low",
        "evidence": [text[:1000]] if text.strip() else [],
    }


# LLM: _requested_tools_from_pending infers only known shell gateway requests from structured pending text.
# 函数用途: 判断是否需要 controlled_exec；其他未知能力交给通用 capability 路由。
def _requested_tools_from_pending(text: str, commands: list[str]) -> list[str]:
    lowered = text.lower()
    if "controlled_exec" in lowered or commands:
        return ["controlled_exec"]
    return []


# LLM: _requested_commands_from_pending extracts a small allowlist of shell commands mentioned by the task.
# 函数用途: 从 pending_steps/summary 中恢复命令名，避免把任意词当 shell 命令。
def _requested_commands_from_pending(text: str) -> list[str]:
    lowered = text.lower()
    commands = []
    for command in ("pwd", "python3", "python", "rm", "curl", "pytest", "npm", "node"):
        if command in lowered and command not in commands:
            commands.append(command)
    if "python3" in commands and "python" in commands:
        commands.remove("python")
    return commands
